"""Durable matched experiments. Oracles never enter model-visible packets."""
import json
import hashlib
import random
import time
from pathlib import Path
from typing import Annotated,Literal
from pydantic import Field,model_validator
from .contracts import Strict,Identifier,Bundle,InquiryRequest
from .errors import BridgeError
from .ingestion import load_json
from .provider import SYSTEM,TOOLS
from .questions import fact_keys
from .simulation import ReplyMap,history
from .workflow import simulate
from .util import canonical,digest,text_hash,now

class Scenario(Strict):
    id: Identifier
    packet: Bundle
    replies: ReplyMap
    expected_rows: dict
    necessary_requests: list[InquiryRequest] = Field(default_factory=list,max_length=32)
    label_status: Literal["author_inspected_regression"] = "author_inspected_regression"
    @model_validator(mode="after")
    def valid_scope(self):
        claims={c.id for c in self.packet.claims}
        if set(self.expected_rows)!=claims: raise ValueError("Oracle must account for every claim")
        allowed={"face_minor","approval_minor","conditional_minor","settlement_minor","remaining_recorded_minor","holds","state"}
        if any(not fields or not set(fields)<=allowed for fields in self.expected_rows.values()):
            raise ValueError("Oracle contains unsupported row fields")
        if any(not set(q.claims)<=claims for q in self.necessary_requests): raise ValueError("Oracle inquiry scope")
        if any(not set(q.claims)<=claims for q in self.replies.rules): raise ValueError("Reply scope")
        return self

class Protocol(Strict):
    schema_version: Literal[1] = 1
    id: Identifier
    file_hashes: dict[str,str] = Field(default_factory=dict)
    seed: int = 17
    repetitions: Annotated[int,Field(ge=1,le=3)] = 1
    variants: list[Literal["A","B","C"]] = Field(default_factory=lambda:["A","B","C"])
    max_model_attempts: Annotated[int,Field(ge=0,le=1000)] = 96
    max_reserved_micro_usd: Annotated[int,Field(ge=0)] = 0
    tools_per_investigation: Annotated[int,Field(ge=1,le=20)] = 20
    attempts_per_investigation: Annotated[int,Field(ge=1,le=8)] = 8
    inquiry_rounds: Annotated[int,Field(ge=0,le=2)] = 2
    cost_tradeoff: Literal["report_only_no_threshold"] = "report_only_no_threshold"
    scenarios: list[Scenario] = Field(min_length=1,max_length=12)
    @model_validator(mode="after")
    def unique(self):
        if not self.variants or len(set(self.variants))!=len(self.variants):raise ValueError("Unique variants required")
        if len({s.id for s in self.scenarios})!=len(self.scenarios):raise ValueError("Unique scenarios required")
        return self

def load_protocol(path):
    try:
        root=Path(path).absolute().parent
        data=load_json(path,2*1024*1024)
        for item in data.get("scenarios",[]):
            if "packet_file" not in item: continue
            def read_part(field):
                name=item.pop(field)
                candidate=root/name
                if Path(name).is_absolute() or ".." in Path(name).parts or any((root/Path(*Path(name).parts[:i])).is_symlink() for i in range(1,len(Path(name).parts)+1)):
                    raise BridgeError("path","Protocol inputs must be regular files inside its directory")
                if data.get("file_hashes",{}).get(name)!=hashlib.sha256(candidate.read_bytes()).hexdigest():
                    raise BridgeError("fixture","Frozen protocol input hash changed or is missing")
                return load_json(candidate,1200*1024)
            item["packet"]=read_part("packet_file")
            item["replies"]=read_part("replies_file")
            oracle=read_part("oracle_file")
            if not set(oracle)<={"expected_rows","necessary_requests"}:raise BridgeError("protocol","Unexpected oracle fields")
            item.update(oracle)
        return Protocol.model_validate(data)
    except ValueError:raise BridgeError("protocol","Invalid bounded comparison protocol") from None

def frozen_plan(protocol,mode,provider):
    if mode not in ("offline","instrumented","live"):raise BridgeError("route","Unknown comparison mode")
    if (mode=="live")!=(provider is not None):raise BridgeError("route","Live comparison requires its explicit organizer-funded provider")
    if provider:
        provider.validate()
        if protocol.max_reserved_micro_usd<=0:raise BridgeError("budget","Predeclare a positive comparison reserve cap for live mode")
    return {"protocol":protocol.model_dump(),"mode":mode,
        "execution":provider.transport_kind if provider else "none",
        "model":provider.grant.model if provider else None,
        "model_settings":provider.settings() if provider else None,
        "grant_hash":digest(provider.grant.model_dump()) if provider else None,
        "prompt_hash":text_hash(SYSTEM),"tool_schema_hash":digest({k:v.model_json_schema() for k,v in TOOLS.items()}),
        "scenario_hashes":{s.id:{"input":digest(s.packet.model_dump()),"reply":digest(s.replies.model_dump()),
            "oracle":digest({"rows":s.expected_rows,"questions":[q.model_dump() for q in s.necessary_requests]})} for s in protocol.scenarios}}

def report_access(store,cid,actor):
    saved=store.one("SELECT * FROM comparisons WHERE id=?",(cid,))
    if not saved:raise BridgeError("not_found","Comparison not found")
    if saved["actor"]!=actor:raise BridgeError("access","Comparison access denied")
    for job in store.db.execute("SELECT case_id,investigation FROM comparison_jobs WHERE comparison_id=?",(cid,)):
        store.case(job["case_id"],actor)
        store.investigation_history_access(job["investigation"],actor)
    return saved

def get_report(store,cid,actor):
    saved=report_access(store,cid,actor)
    if not saved["report"]:raise BridgeError("incomplete","Comparison has no report yet")
    report=json.loads(saved["report"])
    receipt=report.pop("receipt_hash")
    if digest(report)!=receipt:raise BridgeError("integrity","Comparison receipt hash mismatch")
    report["receipt_hash"]=receipt
    return report

def initialize(store,cid,actor,protocol,plan):
    # Validate the ID before using it in any human-readable output filename.
    if not cid or len(cid)>64 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in cid):
        raise BridgeError("invalid","Comparison id must be a bounded simple identifier")
    with store.tx():
        saved=store.one("SELECT * FROM comparisons WHERE id=?",(cid,))
        if saved:
            report_access(store,cid,actor)
            if saved["plan_hash"]!=digest(plan):raise BridgeError("conflict","Comparison id belongs to a different frozen plan, prompt or provider")
            return saved
        store.db.execute("INSERT INTO comparisons(id,actor,plan_hash,plan,status,max_model_attempts,max_reserved_micro) VALUES(?,?,?,?,'ready',?,?)",
            (cid,actor,digest(plan),canonical(plan),protocol.max_model_attempts,protocol.max_reserved_micro_usd))
        jobs=[(s,v,r) for s in protocol.scenarios for v in protocol.variants for r in range(1,protocol.repetitions+1)]
        random.Random(protocol.seed).shuffle(jobs)
        for pos,(scenario,variant,rep) in enumerate(jobs):
            jid="job-"+digest([cid,scenario.id,variant,rep])[:24]; case_id="cmp-"+digest([cid,jid])[:24]
            packet=scenario.packet.model_copy(update={"id":case_id})
            store.import_bundle(packet,actor,"import-"+jid)
            inv=store.new_investigation(case_id,actor,protocol.tools_per_investigation,protocol.attempts_per_investigation,protocol.inquiry_rounds)
            store.db.execute("INSERT INTO comparison_jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (jid,cid,case_id,inv,scenario.id,variant,rep,pos,"pending",None))
        return store.one("SELECT * FROM comparisons WHERE id=?",(cid,))

def verify_lineage(store,job,actor,scenario):
    expected=scenario.packet.model_copy(update={"id":job["case_id"]}).model_dump()
    revision=1
    def check():
        if store.bundle(job["case_id"],actor,revision).model_dump()!=expected:
            raise BridgeError("comparison_input","A comparison snapshot differs from its frozen input or permitted replies")
    check()
    rules={r.id:r for r in scenario.replies.rules}
    for receipt in history(store,job["investigation"],actor):
        if receipt["map_hash"]!=digest(scenario.replies.model_dump()) or receipt["input_revision"]!=revision:
            raise BridgeError("comparison_input","Comparison reply lineage differs from the frozen plan")
        docs={d["id"]:d for d in expected["evidence"]};events={s["id"]:s for s in expected["settlements"]}
        for rule_id in receipt["delivered_rules"]:
            if rule_id not in rules:raise BridgeError("comparison_input","Unknown frozen reply rule")
            for d in rules[rule_id].evidence:docs[d.id]=d.model_dump()
            for event in rules[rule_id].settlements:events[event.id]=event.model_dump()
        expected["evidence"]=list(docs.values());expected["settlements"]=list(events.values())
        if receipt["output_revision"] not in (revision,revision+1):raise BridgeError("comparison_input","Comparison revisions are not contiguous")
        revision=receipt["output_revision"];check()
    if store.case(job["case_id"],actor)["revision"]!=revision:
        raise BridgeError("comparison_input","External import changed a frozen comparison case; create a new comparison")

def facts(queries):
    # Score facts per claim: batching equivalent claims does not improve or hurt recall.
    return {(claim,intent) for q in queries for scope,intent in fact_keys(q) for claim in scope}

def score(store,job,actor,scenario,result,error,elapsed):
    failures=[];proposal=None
    if result and result.get("proposal"):
        proposal=store.proposal(result["proposal"],actor)
    if not proposal:failures.append("no_final_proposal")
    else:
        d=proposal["payload"];rows={x["claim"]:x for x in d["rows"]}
        if not d["valid"]:failures.append("invalid_proposal")
        if set(rows)!=set(scenario.expected_rows):failures.append("claim_inventory")
        for claim,fields in scenario.expected_rows.items():
            for field,value in fields.items():
                if rows.get(claim,{}).get(field)!=value:failures.append(claim+"."+field)
    if error:failures.append(error)
    replies=history(store,job["investigation"],actor)
    queries=[q for h in replies for q in h["requests"]]
    # Include unanswered terminal questions, not only questions the simulator could execute.
    for r in store.db.execute("SELECT checkpoint FROM runs WHERE investigation=?",(job["investigation"],)):
        queries.extend(json.loads(r["checkpoint"])["inquiries"])
    asked=facts(queries);wanted=facts([q.model_dump() for q in scenario.necessary_requests])
    missing=sorted(wanted-asked);extra=sorted(asked-wanted)
    if missing:failures.append("missing_necessary_requests")
    inv=store.one("SELECT * FROM investigations WHERE id=?",(job["investigation"],))
    attempts=[]
    for r in store.db.execute("SELECT st.run_id,st.seq,st.status,st.payload FROM steps st JOIN runs r ON r.id=st.run_id WHERE r.investigation=? AND st.kind='attempt' ORDER BY r.rowid,st.seq",(job["investigation"],)):
        attempts.append({**{k:r[k] for k in ("run_id","seq","status")},"receipt":json.loads(r["payload"]) if r["payload"] else None})
    invalid=store.one("SELECT COUNT(*) n FROM proposals p JOIN runs r ON r.id=p.run_id WHERE r.investigation=? AND p.valid=0",(job["investigation"],))["n"]
    return {"job":job["id"],"case":job["case_id"],"investigation":job["investigation"],"scenario":job["scenario"],
        "variant":job["variant"],"repetition":job["repetition"],"status":"complete" if not error else "failed",
        "quality_passed":not failures,"failures":failures,"invalid_proposals":invalid,
        "missing_requests":missing,"unnecessary_requests":extra,"necessary_request_recall":len(asked&wanted)/len(wanted) if wanted else 1.0,
        "tools":inv["tools"],"model_attempts":inv["attempts"],"inquiry_rounds":inv["inquiries"],
        "reserved_micro_usd":inv["reserved_micro"],"actual_billed_micro_usd":None,
        "attempt_receipts":attempts,"active_ms":inv["active_ms"],"orchestration_elapsed_ms":round(elapsed*1000,3),
        "proposal":None if not proposal else {"id":proposal["id"],"hash":proposal["hash"],"accepted":False},
        "reply_receipts":replies,"outcome":(result or {}).get("outcome",error)}

def summarize(store,cid,actor,plan,pause=None):
    saved=report_access(store,cid,actor);jobs=[]
    for row in store.db.execute("SELECT * FROM comparison_jobs WHERE comparison_id=? ORDER BY position",(cid,)):
        if row["result"]:jobs.append(json.loads(row["result"]))
        else:
            scenario=Scenario.model_validate(next(x for x in plan["protocol"]["scenarios"] if x["id"]==row["scenario"]))
            partial=score(store,dict(row),actor,scenario,None,"unfinished",0)
            begun=partial["model_attempts"] or partial["tools"] or partial["inquiry_rounds"]
            partial["status"]="unfinished" if begun else "pending"
            partial["failures"]=["unfinished" if begun else "not_executed"]
            jobs.append(partial)
    counts={v:{"planned":sum(j["variant"]==v for j in jobs),"quality_passed":sum(j["variant"]==v and j["quality_passed"] for j in jobs),
        "model_attempts":sum(j["model_attempts"] for j in jobs if j["variant"]==v),
        "reserved_micro_usd":sum(j["reserved_micro_usd"] for j in jobs if j["variant"]==v)} for v in plan["protocol"]["variants"]}
    report={"schema_version":1,"comparison":cid,"plan_hash":saved["plan_hash"],"protocol_hash":digest(plan["protocol"]),
        "scenario_hashes":plan["scenario_hashes"],"prompt_hash":plan["prompt_hash"],"tool_schema_hash":plan["tool_schema_hash"],
        "mode":plan["mode"],"execution":plan["execution"],"model":plan["model"],"model_settings":plan["model_settings"],"status":"paused" if pause else "complete",
        "pause_reason":pause,"created_at":now(),"planned_jobs":len(jobs),"jobs":jobs,"variants":counts,
        "passed":not pause and all(j["quality_passed"] for j in jobs),"model_attempts":saved["model_attempts"],
        "reserved_micro_usd":saved["reserved_micro"],"actual_billed_micro_usd":None,
        "retention_decision":"No superiority conclusion: inspect correctness, necessary and unnecessary requests, reported usage, and latency; no cost tradeoff threshold was declared.",
        "limitations":["Author-inspected synthetic regression cases, not a blind holdout or AR expert evaluation.",
            "Offline B/C use one deterministic parser; injected transports test contracts, not model intelligence.",
            "All planned jobs and failed attempts remain in the denominator; reservations are ceilings, not actual spend.",
            "Repeated runs are observations, not population estimates. No proposal was automatically accepted."]}
    report["receipt_hash"]=digest(report)
    with store.tx():
        store.db.execute("UPDATE comparisons SET status=?,report=? WHERE id=?",(report["status"],canonical(report),cid))
    return report

def compare(store,cid,actor,protocol,mode="offline",provider=None,trace_ready=None,recover_interrupted=False,after_job=None):
    plan=frozen_plan(protocol,mode,provider)
    if mode=="live" and trace_ready is None:raise BridgeError("trace_gate","Live comparisons require acknowledged trace delivery before and after each evidence batch")
    saved=initialize(store,cid,actor,protocol,plan)
    if saved["status"]=="complete":return get_report(store,cid,actor)
    with store.tx():
        current=report_access(store,cid,actor)
        if current["status"]=="running" and not recover_interrupted:
            raise BridgeError("recovery","Comparison interrupted or already running; confirm original worker stopped and recover explicitly")
        store.db.execute("UPDATE comparisons SET status='running' WHERE id=?",(cid,))
    scenarios={s.id:s for s in protocol.scenarios}
    for raw in store.db.execute("SELECT * FROM comparison_jobs WHERE comparison_id=? ORDER BY position",(cid,)).fetchall():
        job=dict(raw)
        if job["status"] in ("complete","failed"):continue
        result=None;error=None;start=time.monotonic()
        job_mode="instrumented" if mode=="live" and job["variant"]=="A" else mode
        try:
            if trace_ready:trace_ready(job["case_id"])
            result=simulate(store,job["investigation"],actor,scenarios[job["scenario"]].replies,job["variant"],job_mode,
                provider if job_mode=="live" else None,trace_ready,recover_interrupted,case_guard=lambda:verify_lineage(store,job,actor,scenarios[job["scenario"]]))
        except BridgeError as e:
            if e.code in ("trace_gate","trace_ack","trace_config","recovery","busy","lease","cancelled"):
                return summarize(store,cid,actor,plan,e.code)
            error=e.code
        scored=score(store,job,actor,scenarios[job["scenario"]],result,error,time.monotonic()-start)
        with store.tx():
            store.db.execute("UPDATE comparison_jobs SET status=?,result=? WHERE id=?",(scored["status"],canonical(scored),job["id"]))
        if after_job:after_job(scored)
        if trace_ready:
            try:trace_ready(job["case_id"])
            except BridgeError as e:return summarize(store,cid,actor,plan,e.code)
    # A previous pause after a completed job must flush that job before success.
    if trace_ready:
        for job in store.db.execute("SELECT case_id FROM comparison_jobs WHERE comparison_id=?",(cid,)):
            try:trace_ready(job["case_id"])
            except BridgeError as e:return summarize(store,cid,actor,plan,e.code)
    return summarize(store,cid,actor,plan)
