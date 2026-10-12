"""Operator-authorized synthetic participant loop; never accepts proposals."""
import json
from .contracts import InquiryRequest
from .errors import BridgeError
from .questions import identity
from .runner import run_investigation
from .simulation import apply_replies,history,requested_before
from .util import digest

def derived_requests(dossier):
    grouped={}
    for inquiry in dossier["inquiries"]:
        for proposition in inquiry["propositions"]:
            grouped.setdefault(proposition,[]).append(inquiry["claim"])
    if len(grouped)>8: raise BridgeError("inquiry_scope","More than eight distinct missing propositions; narrow scope explicitly")
    return [{"claims":sorted(set(claims)),"proposition":proposition,"owner":"merchant_or_AR_analyst",
             "draft":"Please provide "+proposition+" for claims "+", ".join(sorted(set(claims)))+"."}
            for proposition,claims in sorted(grouped.items())]

def pending_requests(store,rid,actor):
    run=store.run(rid,actor)
    explicit=run["checkpoint"]["inquiries"]
    if explicit:
        if run["revision"]!=store.case(run["case_id"],actor)["revision"]:store.investigation_history_access(run["investigation"],actor)
        return explicit
    if run["mode"]=="live": return []
    proposal=run["checkpoint"].get("proposal")
    return derived_requests(store.proposal(proposal["proposal"],actor)["payload"]) if proposal else []

def simulate(store,investigation,actor,reply_map,variant="C",mode="offline",provider=None,trace_ready=None,recover_interrupted=False,after_reply=None,case_guard=None):
    inv=store.one("SELECT * FROM investigations WHERE id=?",(investigation,))
    if not inv: raise BridgeError("not_found","Investigation not found")
    store.case(inv["case_id"],actor)
    if not store.bundle(inv["case_id"],actor).synthetic: raise BridgeError("scope","Frozen replies are restricted to synthetic cases")
    if store.one("SELECT id FROM runs WHERE investigation=? AND (variant!=? OR mode!=?) LIMIT 1",(investigation,variant,mode)):
        raise BridgeError("conflict","An investigation cannot switch variant or mode across evidence revisions")
    initial_session=store.one("SELECT map_hash FROM simulation_sessions WHERE investigation=?",(investigation,))
    if initial_session and initial_session["map_hash"]!=digest(reply_map.model_dump()):
        raise BridgeError("conflict","Frozen reply map changed")
    while True:
        if case_guard:case_guard()
        case=store.case(inv["case_id"],actor)
        last=store.one("SELECT id,status,variant,mode FROM runs WHERE investigation=? AND revision=? ORDER BY rowid DESC LIMIT 1",(investigation,case["revision"]))
        if last and (last["variant"],last["mode"])!=(variant,mode):
            raise BridgeError("conflict","An investigation cannot switch variant or mode during simulation")
        if last and last["status"]=="running":
            if not recover_interrupted: raise BridgeError("recovery","Interrupted run requires explicit recovery; no inference is retried automatically")
            run=store.recover(last["id"],actor)
            result=run_investigation(store,run["id"],actor,provider)
        elif last and last["status"] in ("review","awaiting_evidence","invalid_proposal"):
            run=store.run(last["id"],actor)
            p=run["checkpoint"].get("proposal")
            result=dict(p or {},run_id=run["id"],status=last["status"],mode=mode,variant=variant)
        elif last and last["status"].startswith(("failed","exhausted","cancelled","blocked")):
            raise BridgeError("stopped","Prior run stopped; inspect it and explicitly start a new bounded run if appropriate")
        else:
            if trace_ready: trace_ready(case["id"])
            if case_guard:case_guard()
            run=store.start_run(investigation,actor,variant,mode)
            result=run_investigation(store,run["id"],actor,provider)
        if trace_ready: trace_ready(case["id"])
        if result.get("proposal") and not result["valid"]:
            return dict(result,outcome="invalid_proposal",reply_history=history(store,investigation,actor),accepted=False)
        requests=pending_requests(store,result["run_id"],actor)
        previous=requested_before(store,investigation,actor)
        session=store.one("SELECT rounds FROM simulation_sessions WHERE investigation=?",(investigation,))
        rounds=session["rounds"] if session else 0
        # New evidence can make a previously unavailable response admissible. At an unchanged
        # revision repeated questions add nothing and should end in an honest handoff.
        past=history(store,investigation,actor)
        unchanged=bool(past and past[-1]["output_revision"]==past[-1]["input_revision"])
        if unchanged: requests=[q for q in requests if identity(q) not in {identity(p) for p in previous}]
        if not requests or rounds>=reply_map.max_rounds:
            return dict(result,outcome=("review_with_open_items" if requests else "review") if result.get("proposal") else "unresolved_handoff_no_proposal",reply_history=past,accepted=False)
        receipt=apply_replies(store,investigation,actor,reply_map,requests,
            "reply-"+digest({"investigation":investigation,"map":digest(reply_map.model_dump()),"round":rounds+1})[:40],
            case["revision"],result["run_id"])
        if after_reply: after_reply(receipt)
        if receipt["input_revision"]==receipt["output_revision"]:
            return dict(result,outcome="reply_unavailable_handoff",reply_history=history(store,investigation,actor),accepted=False)
