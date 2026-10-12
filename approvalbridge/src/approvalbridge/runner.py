"""Five scoped operations with durable budgets, checkpoints and exact proposals."""
import json
from pydantic import ValidationError
from .contracts import Decision
from .domain import reconcile
from .errors import BridgeError
from .ingestion import visible
from .provider import TOOLS,SYSTEM
from .questions import identity
from .rules import extract
from .util import canonical,digest,text_hash,now

def add_inquiries(bundle,items,checkpoint):
    scope={c.id for c in bundle.claims}
    additions=[]
    for request in items:
        item=request.model_dump()
        if not set(item["claims"])<=scope: raise BridgeError("invalid","Inquiry references unknown claims")
        if any(identity(previous)==identity(item) for previous in checkpoint["inquiries"]+additions):
            raise BridgeError("no_progress","Duplicate inquiry")
        additions.append(item)
    checkpoint["inquiries"].extend(additions)
    return checkpoint["inquiries"]

def proposal_validation_message(result,observations):
    """Bound feedback to validator reasons and array positions, never source text."""
    positions={o.id:i for i,o in enumerate(observations)}
    messages=[]
    for error in result["validation_errors"][:5]:
        position=positions.get(error.get("observation"))
        location=f"observations[{position}]" if position is not None else "dossier"
        messages.append(location+": "+error["reason"])
    return "Proposal validation rejected: "+"; ".join(messages)[:600]+". Correct the observations using supported evidence; leave unresolved facts unknown."

def tool_result(bundle,decision,checkpoint,require_valid=True):
    args=TOOLS[decision.tool].model_validate(decision.arguments)
    packet=visible(bundle);docs={d["id"]:d for d in packet["evidence"]}
    if decision.tool=="search":
        words=args.query.lower().split()
        matches=[{"id":d["id"],"kind":d["kind"],"sender":d["sender"],"snippet":d["text"][:160]} for d in docs.values()
                 if all(w in d["text"].lower() or w in d["id"].lower() for w in words)]
        return {"matches":matches[:args.limit],"total":len(matches)}
    if decision.tool=="read":
        if any(i not in docs for i in args.ids): raise BridgeError("access","Requested evidence is unavailable")
        checkpoint["read_ids"]=sorted(set(checkpoint["read_ids"])|set(args.ids))
        return {"evidence":[docs[i] for i in args.ids]}
    if decision.tool=="lookup":
        out={"policy":packet["policy"]}
        if args.subject in ("authority","all"): out["authorities"]=packet["authorities"]
        if args.subject in ("settlement","all"): out["settlements"]=packet["settlements"]
        return out
    if decision.tool=="inquire":
        return {"requests":add_inquiries(bundle,args.items(),checkpoint),"status":"draft_not_sent"}
    if decision.tool=="propose":
        if any(o.source_id not in checkpoint["read_ids"] for o in args.observations):
            raise BridgeError("invalid","Proposal cites unread evidence")
        result=reconcile(bundle,args.observations)
        if require_valid and not result["valid"]:
            raise BridgeError("invalid",proposal_validation_message(result,args.observations))
        # Validate the entire inquiry batch before any proposal state changes.
        add_inquiries(bundle,args.inquiries,checkpoint)
        checkpoint["observations"]=[o.model_dump() for o in args.observations]
        return dict(result,drafted_inquiries=checkpoint["inquiries"])

def simulated_decision(bundle,variant,cp):
    packet=visible(bundle)
    if variant in ("A","B"):
        cp["read_ids"]=[d["id"] for d in packet["evidence"]]
        return Decision(tool="propose",arguments={"observations":[o.model_dump() for o in extract(bundle)]},rationale="Deterministic offline fixture parser; no inference")
    history=cp["messages"]
    if not history: return Decision(tool="search",arguments={"query":""})
    if not any(x["decision"]["tool"]=="lookup" for x in history):
        return Decision(tool="lookup",arguments={"subject":"all"})
    remaining=[d["id"] for d in packet["evidence"] if d["id"] not in cp["read_ids"]]
    if remaining: return Decision(tool="read",arguments={"ids":remaining[:4]})
    docs=[d for d in bundle.evidence if d.id in cp["read_ids"]]
    return Decision(tool="propose",arguments={"observations":[o.model_dump() for o in extract(bundle,docs)]})

def run_investigation(store,rid,actor,provider=None,after_checkpoint=None):
    from .simulation import history as reply_history
    r=store.run(rid,actor);fence=r["fence"];cp=r["checkpoint"]
    try:
        if r["mode"]=="live" and provider is None: raise BridgeError("route","No verified organizer inference route configured")
        if r["mode"]!="live" and provider is not None: raise BridgeError("route","Offline mode cannot receive a network provider")
        if provider:
            provider.validate()
            if not store.one("SELECT id FROM outbox WHERE case_id=? AND status='delivered' LIMIT 1",(r["case_id"],)):
                store.finish(rid,actor,fence,"blocked:trace_gate")
                raise BridgeError("trace_gate","Deliver a real neatlogs trace for this case before live inference")
            if provider.transport_kind=="live_http" and not store.one("SELECT o.id FROM outbox o JOIN trace_receipts t ON t.event_id=o.id WHERE o.case_id=? AND o.status='delivered' AND t.transport='live_http' LIMIT 1",(r["case_id"],)):
                raise BridgeError("trace_gate","A real model request requires a trace acknowledged through the real transport")
        bundle=store.bundle(r["case_id"],actor,r["revision"])
        replies=reply_history(store,r["investigation"],actor)
        permitted=visible(bundle); allowed={d["id"] for d in permitted["evidence"]}
        cached_authorities=None
        if replies:
            known={sid for h in replies for sid in h["source_ids"]}
            for previous in store.db.execute("SELECT checkpoint FROM runs WHERE investigation=? AND id!=? ORDER BY rowid",(r["investigation"],rid)):
                previous_cp=json.loads(previous["checkpoint"]);known.update(previous_cp["read_ids"])
                for entry in previous_cp["messages"]:
                    if entry["decision"]["tool"]=="lookup" and entry["result"].get("authorities")==permitted["authorities"]:
                        cached_authorities=permitted["authorities"]
            cp["read_ids"]=sorted((set(cp["read_ids"])|known)&allowed)
        metadata={"schema_version":2,"input_hash":digest(bundle.model_dump()),"prompt_hash":text_hash(SYSTEM),
                  "tool_schema_hash":digest({k:v.model_json_schema() for k,v in TOOLS.items()}),
                  "mode":r["mode"],"variant":r["variant"],"input_revision":r["revision"],"access_epoch":r["epoch"],
                  "model":provider.grant.model if provider else None,"model_settings":provider.settings() if provider else None,"grant_id":provider.grant.id if provider else None,
                  "execution":"none" if not provider else provider.transport_kind}
        with store.tx():
            store.check(rid,actor,fence)
            old=store.one("SELECT payload FROM run_metadata WHERE run_id=?",(rid,))
            if old and json.loads(old["payload"])!=metadata: raise BridgeError("stale","Run prompt, schema or provider changed; cancel and start a fresh bounded run")
            store.db.execute("INSERT OR IGNORE INTO run_metadata VALUES(?,?)",(rid,canonical(metadata)))
        while True:
            store.check(rid,actor,fence)
            if "proposal" in cp:
                if not cp["proposal"]["valid"]:
                    if r["mode"]=="live":
                        raise BridgeError("invalid","Recorded proposal is invalid; no reviewable proposal is available")
                    store.finish(rid,actor,fence,"invalid_proposal")
                    return dict(cp["proposal"],run_id=rid,mode=r["mode"],variant=r["variant"],status="invalid_proposal",execution=metadata["execution"])
                store.finish(rid,actor,fence,"review")
                return dict(cp["proposal"],run_id=rid,mode=r["mode"],variant=r["variant"],status="review",execution=metadata["execution"])
            if cp["messages"] and cp["messages"][-1]["decision"]["tool"]=="inquire" and "error" not in cp["messages"][-1]["result"] and "pending_decision" not in cp:
                store.finish(rid,actor,fence,"awaiting_evidence")
                return {"run_id":rid,"status":"awaiting_evidence","requests":cp["inquiries"],"mode":r["mode"],"variant":r["variant"],"execution":metadata["execution"]}
            if r["mode"]=="live" and "pending_decision" not in cp:
                packet=visible(bundle)
                initial=packet if r["variant"]=="B" else {k:v for k,v in packet.items() if k not in ("evidence","authorities","settlements")}
                if r["variant"]=="B": cp["read_ids"]=[d["id"] for d in packet["evidence"]]
                else:
                    initial["evidence"]=[d for d in packet["evidence"] if d["id"] in cp["read_ids"]]
                    initial["settlements"]=[s for s in packet["settlements"] if s["source_id"] in cp["read_ids"]]
                    if cached_authorities is not None: initial["authorities"]=cached_authorities
                inv=store.one("SELECT tools,attempts,inquiries,limits FROM investigations WHERE id=?",(r["investigation"],));lim=json.loads(inv["limits"])
                remaining={"model_calls":lim["attempts"]-inv["attempts"],"tool_calls":lim["tools"]-inv["tools"],"inquiry_rounds":lim["inquiries"]-inv["inquiries"]}
                if remaining["tool_calls"]<=0:
                    raise BridgeError("budget","Investigation tool budget exhausted; no further decision can be executed")
                payload=provider.request({"packet":initial,"budget_remaining":remaining,"history":cp["messages"],**({"previous_output_error":cp["model_error"]} if cp.get("model_error") else {}),"reply_history":reply_history(store,r["investigation"],actor),"synthetic":bundle.synthetic,"variant":r["variant"]})
                seq=store.reserve_step(rid,actor,fence,"attempt",provider.reservation())
                try: decision,usage=provider.decide(payload)
                except BridgeError as error:
                    with store.tx():
                        store.attempt_outcome(rid,actor,fence,seq,"uncertain" if error.code=="transport_uncertain" else "failed",
                            {"error":error.code,"reservation":"retained","request_hash":digest(payload),"request":payload["messages"][1]["content"],**provider.last_receipt})
                        if error.code=="model_output":
                            cp["model_error"]="The previous response did not match the decision JSON contract. Return one valid tool decision."
                            store.check(rid,actor,fence)
                            store.db.execute("UPDATE runs SET checkpoint=? WHERE id=?",(canonical(cp),rid))
                    if error.code!="model_output": raise
                    # Persist bounded feedback before retrying; recovery never resets spent attempts.
                    if after_checkpoint: after_checkpoint(rid)
                    continue
                cp.pop("model_error",None)
                store.check(rid,actor,fence)
                cp["pending_decision"]=decision.model_dump()
                store.checkpoint(rid,actor,fence,seq,cp,dict(usage,request_hash=digest(payload),request=payload["messages"][1]["content"]))
            elif "pending_decision" not in cp:
                cp["pending_decision"]=simulated_decision(bundle,r["variant"],cp).model_dump()
            decision=Decision.model_validate(cp["pending_decision"])
            argument_error=None
            try: args=TOOLS[decision.tool].model_validate(decision.arguments)
            except ValidationError as invalid:
                if r["mode"]!="live": raise
                args=None; argument_error=BridgeError("invalid","Arguments rejected: "+"; ".join(f'{".".join(map(str,e["loc"]))}: {e["msg"]}' for e in invalid.errors())[:300])
            signature=digest(decision.model_dump(exclude={"rationale"}))
            if signature in [h["signature"] for h in cp["messages"]]: raise BridgeError("no_progress","Repeated action made no progress")
            store.audit("tool.request",actor,case=r["case_id"],revision=r["revision"],run=rid,tool=decision.tool,outcome="rejected" if argument_error else "validated",details={"request_hash":digest(args.model_dump() if args else decision.arguments),"source_refs":getattr(args,"ids",[])})
            seq=store.reserve_step(rid,actor,fence,"tool"); started=now()
            rejected=False
            if args is not None and r["mode"]=="live" and (decision.tool=="inquire" or (decision.tool=="propose" and args.inquiries)):
                inv=store.one("SELECT inquiries,limits FROM investigations WHERE id=?",(r["investigation"],))
                if inv["inquiries"]>=json.loads(inv["limits"])["inquiries"]:
                    argument_error=BridgeError("invalid","No inquiry rounds remain; propose with the available evidence and no inquiries, leaving open items unresolved")
            try:
                if argument_error: raise argument_error
                result=tool_result(bundle,decision,cp,require_valid=r["mode"]=="live")
            except BridgeError as error:
                # A rejected live tool call goes back to the model as its result; budgets and the no-progress guard still bound retries.
                if r["mode"]!="live" or error.code not in ("invalid","access"): raise
                result={"error":error.code,"message":error.message};rejected=True
            # Only an accepted call that drafts questions spends an inquiry round.
            inquiry_seq=None
            if not rejected and (decision.tool=="inquire" or (decision.tool=="propose" and bool(args.inquiries))):
                inquiry_seq=store.reserve_step(rid,actor,fence,"inquiry");cp["inquiry_round_reserved"]=True
            cp["messages"].append({"decision":decision.model_dump(),"result":result,"signature":signature})
            cp.pop("pending_decision",None)
            with store.tx():
                if decision.tool=="propose" and not rejected:
                    result=store.propose(rid,actor,fence,result);cp["proposal"]=result
                receipt={"tool":decision.tool,"arguments":decision.arguments,"result":result,**({"error":result["error"],"failure":result["message"]} if rejected else {}),"request_hash":digest(decision.arguments),"result_hash":digest(result),"started":started,"ended":now(),"request_id":rid+":"+str(seq)}
                store.checkpoint(rid,actor,fence,seq,cp,receipt)
                store.audit("tool.outcome",actor,case=r["case_id"],revision=r["revision"],run=rid,tool=decision.tool,outcome="complete",details={"seq":seq,"request_hash":receipt["request_hash"],"result_hash":receipt["result_hash"]})
                if inquiry_seq is not None: store.checkpoint(rid,actor,fence,inquiry_seq,cp,{"draft_only":True,"requested_facts":len(cp["inquiries"])})
            if after_checkpoint: after_checkpoint(rid)
            if decision.tool=="inquire" and not rejected:
                store.finish(rid,actor,fence,"awaiting_evidence")
                return {"run_id":rid,"status":"awaiting_evidence","requests":cp["inquiries"],"mode":r["mode"],"variant":r["variant"],"execution":metadata["execution"]}
    except (BridgeError,ValidationError) as caught:
        error=BridgeError("invalid","Run data does not fit the validated contract; review the source and recorded state") if isinstance(caught,ValidationError) else caught
        store.audit("run.failure",actor,case=r["case_id"],revision=r["revision"],run=rid,tool=cp.get("pending_decision",{}).get("tool"),outcome="denied" if error.code in ("access","invalid","route","stale","budget","comparison_budget") else "failed",reason=error.code)
        if cp.get("pending_decision"):
            store.audit("tool.outcome",actor,case=r["case_id"],revision=r["revision"],run=rid,tool=cp["pending_decision"].get("tool"),outcome="failed",reason=error.code)
        try: store.finish(rid,actor,fence,"exhausted" if error.code in ("budget","comparison_budget") else "failed:"+error.code)
        except BridgeError: pass
        if error is not caught: raise error from None
        raise
