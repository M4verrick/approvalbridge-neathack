"""Frozen synthetic participants. Responses are data, never authority or acceptance."""
import json
from pathlib import Path
from typing import Annotated,Literal
from pydantic import Field,model_validator
from .contracts import Strict,Identifier,Evidence,Settlement,InquiryRequest,Bundle
from .errors import BridgeError
from .ingestion import visible,load_json
from .questions import intents,identity,fact_keys
from .util import canonical,digest,now

class ReplyRule(Strict):
    id: Identifier
    claims: list[Identifier] = Field(min_length=1,max_length=32)
    intent: Literal["approval","condition","cap","settlement","hold","authority","conflict","basis"]
    aliases: list[str] = Field(default_factory=list,max_length=16)
    requires_sources: list[Identifier] = Field(default_factory=list,max_length=32)
    outcome: Literal["reply","unavailable"] = "reply"
    evidence: list[Evidence] = Field(default_factory=list,max_length=16)
    settlements: list[Settlement] = Field(default_factory=list,max_length=16)
    @model_validator(mode="after")
    def payload(self):
        if self.outcome=="unavailable" and (self.evidence or self.settlements):
            raise ValueError("Unavailable response cannot carry hidden evidence")
        if self.outcome=="reply" and not (self.evidence or self.settlements):
            raise ValueError("Reply requires frozen evidence or ledger records")
        return self

class ReplyMap(Strict):
    schema_version: Literal[1] = 1
    id: Identifier
    version: Identifier = "v1"
    synthetic: Literal[True] = True
    max_rounds: Annotated[int,Field(ge=0,le=2)] = 2
    rules: list[ReplyRule] = Field(max_length=32)
    @model_validator(mode="after")
    def unique(self):
        if len({r.id for r in self.rules})!=len(self.rules): raise ValueError("Duplicate reply rule")
        return self

def load_reply_map(path):
    p=Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size>1024*1024:
        raise BridgeError("simulation","Reply map must be a bounded regular local JSON file")
    try: return ReplyMap.model_validate(load_json(p,1024*1024))
    except (ValueError,UnicodeError): raise BridgeError("simulation","Invalid frozen reply map") from None

def history(store,investigation,actor):
    inv=store.one("SELECT case_id FROM investigations WHERE id=?",(investigation,))
    if not inv: raise BridgeError("not_found","Investigation not found")
    store.case(inv["case_id"],actor)
    rows=[json.loads(r["payload"]) for r in store.db.execute("SELECT payload FROM simulation_rounds WHERE investigation=? ORDER BY rowid",(investigation,))]
    if rows:store.investigation_history_access(investigation,actor)
    return rows

def requested_before(store,investigation,actor):
    return [q for h in history(store,investigation,actor) for q in h["requests"]]

def apply_replies(store,investigation,actor,reply_map,requests,key,expected_revision,run_id=None):
    if not key or len(key)>200: raise BridgeError("invalid","Bounded nonempty reply key required")
    queries=[InquiryRequest.model_validate(x).model_dump() for x in requests]
    if not queries or len(queries)>8: raise BridgeError("simulation","A reply round requires one to eight bounded requested facts")
    if len({identity(q) for q in queries})!=len(queries): raise BridgeError("no_progress","Duplicate requested fact in reply batch")
    map_hash=digest(reply_map.model_dump())
    normalized=sorted({(c,intent) for q in queries for claims,intent in fact_keys(q) for c in claims})
    request_hash=digest({"investigation":investigation,"map":map_hash,"questions":normalized})
    with store.tx():
        inv=store.one("SELECT * FROM investigations WHERE id=?",(investigation,))
        if not inv: raise BridgeError("not_found","Investigation not found")
        case=store.case(inv["case_id"],actor)
        receipt=store.one("SELECT * FROM simulation_rounds WHERE id=?",(key,))
        if receipt:
            store.investigation_history_access(investigation,actor)
            if receipt["investigation"]!=investigation or receipt["request_hash"]!=request_hash:
                raise BridgeError("conflict","Reply key already used with a different request")
            return json.loads(receipt["payload"])
        if case["revision"]!=expected_revision: raise BridgeError("stale","Reply request snapshot is stale")
        if store.one("SELECT id FROM runs WHERE case_id=? AND status='running'",(case["id"],)):
            raise BridgeError("busy","Finish or cancel the active run before importing a simulated reply")
        bundle=store.bundle(case["id"],actor)
        if not bundle.synthetic:raise BridgeError("scope","Frozen replies are restricted to synthetic cases")
        scope={c.id for c in bundle.claims}
        if any(not set(q["claims"])<=scope for q in queries) or any(not set(r.claims)<=scope for r in reply_map.rules):
            raise BridgeError("scope","Reply request or map crosses case scope")
        session=store.one("SELECT * FROM simulation_sessions WHERE investigation=?",(investigation,))
        if session and session["map_hash"]!=map_hash: raise BridgeError("conflict","Frozen reply map changed within an investigation")
        round_number=(session["rounds"] if session else 0)+1
        if round_number>reply_map.max_rounds: raise BridgeError("budget","Simulated reply-round cap exhausted")
        reserved=False
        if run_id:
            run=store.run(run_id,actor)
            if run["investigation"]!=investigation or run["revision"]!=expected_revision:
                raise BridgeError("scope","Reply run does not match investigation snapshot")
            reserved=bool(run["checkpoint"].get("inquiry_round_reserved"))
            if any(h.get("run_id")==run_id for h in history(store,investigation,actor)):
                raise BridgeError("conflict","This run already consumed its reply reservation; recover the original receipt")
            if reserved and {identity(q) for q in queries}!={identity(q) for q in run["checkpoint"]["inquiries"]}:
                raise BridgeError("conflict","Reply batch must match the run's reserved questions")
        limits=json.loads(inv["limits"])
        if not reserved:
            if inv["inquiries"]>=limits["inquiries"]: raise BridgeError("budget","Investigation inquiry-round budget exhausted")
            store.db.execute("UPDATE investigations SET inquiries=inquiries+1 WHERE id=?",(investigation,))
        old_history=history(store,investigation,actor)
        delivered={i for h in old_history for i in h["delivered_rules"]}
        permitted={d["id"] for d in visible(bundle)["evidence"]}
        packet=bundle.model_dump(); docs={d["id"]:d for d in packet["evidence"]}; events={s["id"]:s for s in packet["settlements"]}
        batch_scope={}
        for query in queries:
            for intent in intents(query["proposition"]):
                batch_scope.setdefault(intent,set()).update(query["claims"])
        outcomes=[]; new_rules=[]; new_sources=[]
        for q in queries:
            resolved=set(intents(q["proposition"])); matched=False
            for rule in reply_map.rules:
                alias_match=any(identity(dict(q,proposition=a))==identity(q) for a in rule.aliases)
                if rule.intent not in resolved and not alias_match: continue
                scope_for_intent=batch_scope.get(rule.intent,set(q["claims"]) if alias_match else set())
                if not set(rule.claims)<=scope_for_intent or not set(rule.claims)&set(q["claims"]):continue
                matched=True
                if rule.id in delivered or rule.id in new_rules:
                    outcomes.append({"claims":q["claims"],"intent":rule.intent,"status":"already_delivered"});continue
                if rule.outcome=="unavailable" or not set(rule.requires_sources)<=permitted:
                    outcomes.append({"claims":q["claims"],"intent":rule.intent,"status":"unavailable"});continue
                for source in rule.evidence:
                    data=source.model_dump()
                    if source.id in docs and docs[source.id]!=data: raise BridgeError("conflict","Frozen reply rewrites an immutable source")
                    if source.id not in docs:
                        docs[source.id]=data;new_sources.append(source.id)
                for event in rule.settlements:
                    data=event.model_dump()
                    if event.id in events and events[event.id]!=data: raise BridgeError("conflict","Frozen reply rewrites a ledger event")
                    events[event.id]=data
                new_rules.append(rule.id)
                outcomes.append({"claims":q["claims"],"intent":rule.intent,"status":"delivered","rule":rule.id,"sources":[d.id for d in rule.evidence]})
            if not matched: outcomes.append({"claims":q["claims"],"status":"unavailable","intent":"unmatched"})
        packet["evidence"]=list(docs.values());packet["settlements"]=list(events.values())
        amended=Bundle.model_validate(packet)
        output_revision=expected_revision
        if new_sources or amended.settlements!=bundle.settlements:
            imported=store.import_bundle(amended,actor,"sim-"+digest({"round":key,"request":request_hash})[:40])
            output_revision=imported["input_revision"]
        payload={"schema_version":1,"run_id":run_id,"investigation":investigation,"map_hash":map_hash,"round":round_number,"mode":"synthetic_frozen_reply","requests":queries,"outcomes":outcomes,"delivered_rules":new_rules,"source_ids":new_sources,"input_revision":expected_revision,"output_revision":output_revision,"nothing_sent":True}
        store.db.execute("INSERT OR IGNORE INTO simulation_sessions VALUES(?,?,?,0)",(investigation,case["id"],map_hash))
        store.db.execute("UPDATE simulation_sessions SET rounds=? WHERE investigation=?",(round_number,investigation))
        store.db.execute("INSERT INTO simulation_rounds VALUES(?,?,?,?,?,?,?,?)",(key,investigation,case["id"],request_hash,expected_revision,output_revision,canonical(payload),now()))
        store._queue(case["id"],{"name":"synthetic participant reply","kind":"WORKFLOW","status":"OK","metadata":{"investigation":investigation,"map_hash":map_hash,"round":round_number,"request_hash":request_hash,"reply_hash":digest(payload),"input_revision":expected_revision,"output_revision":output_revision,"synthetic":True}})
        return payload
