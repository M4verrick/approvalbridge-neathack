"""neatlogs HTTP injection outbox. Local recording is not remote delivery."""
import json
import os
import re
from .errors import BridgeError
from .transport import post_json
from .util import canonical,now

ENDPOINTS={"us":"https://ingest.neatlogs.com/v1/trace","eu":"https://eu.ingest.neatlogs.com/v1/trace"}
MAX_TRACE_BYTES=8000  # neatlogs ingest rejects bodies over ~8 KiB with HTTP 403, despite the documented 50 MB

def fit(payload,limit=MAX_TRACE_BYTES):
    """Shrink span inputs first, then outputs, until the trace fits. The local outbox keeps full content."""
    for field in ("input","output"):
        cap=2000
        while len(canonical(payload).encode())>limit and cap>=32:
            for span in payload.get("children",[]):
                if field in span:
                    text=span[field] if isinstance(span[field],str) else canonical(span[field])
                    if len(text)>cap: span[field]=text[:cap]+"...[truncated %d chars]"%(len(text)-cap)
            cap//=2
    if len(canonical(payload).encode())>limit:
        for span in payload.get("children",[]): span.pop("input",None); span.pop("output",None)
    return payload

def check_config(region,project,key):
    # A project API key is bound to its project; a user-level nlw_ write key must name the project.
    if region not in ENDPOINTS or not key or (key.startswith("nlw_") and not (project or "").strip()):
        raise BridgeError("trace_config","neatlogs region and key are required; an nlw_ write key also needs --project")

def flush(store,case_id,actor,region,project,write_key,transport=post_json,retry_uncertain=False):
    store.case(case_id,actor)
    check_config(region,project,write_key)
    rows=store.db.execute("SELECT * FROM outbox WHERE case_id=? AND status!='delivered' ORDER BY rowid",(case_id,)).fetchall()
    results=[]
    for raw in rows:
        row=dict(raw); store.case(case_id,actor)
        if row["attempts"]>=3: continue
        if row["status"]=="dispatching":
            results.append({"event":row["id"],"status":"delivery_in_progress_or_interrupted","recovery":"stop original worker, then explicitly mark this event uncertain"}); continue
        if row["status"]=="uncertain" and not retry_uncertain:
            results.append({"event":row["id"],"status":"uncertain_requires_explicit_retry"}); continue
        with store.tx():
            changed=store.db.execute("UPDATE outbox SET status='dispatching',attempts=attempts+1 WHERE id=? AND status=? AND attempts=?",
                (row["id"],row["status"],row["attempts"])).rowcount
            if not changed: continue
        payload=json.loads(row["payload"])
        if project: payload["project"]=project
        fit(payload)
        try:
            try: response=transport(ENDPOINTS[region],{"Authorization":"Bearer "+write_key},payload)
            except BridgeError as rejected:
                # A 4xx is never delivered, so one resend cannot duplicate. The ingest firewall blocks some
                # synthetic content (e.g. the injection fixture); resend structure, errors and usage without span content.
                if rejected.code!="provider_rejected": raise
                for span in payload.get("children",[]): span.pop("input",None); span.pop("output",None)
                payload.setdefault("metadata",{})["span_content"]="withheld: ingest rejected the full trace"
                response=transport(ENDPOINTS[region],{"Authorization":"Bearer "+write_key},payload)
            store.case(case_id,actor)
            if response.get("success") is not True or not re.fullmatch(r"[0-9a-fA-F]{32}",str(response.get("trace_id",""))):
                raise BridgeError("trace_ack","neatlogs acknowledgement invalid")
            with store.tx():
                store.db.execute("INSERT OR REPLACE INTO trace_receipts VALUES(?,?,?,?,?)",(row["id"],project or "bound-to-project-key",region,now(),"live_http" if transport is post_json else "injected_test_transport"))
                store.db.execute("UPDATE outbox SET status='delivered',trace_id=? WHERE id=?",(response["trace_id"],row["id"]))
            results.append({"event":row["id"],"status":"delivered","trace_id":response["trace_id"]})
        except Exception:
            store.db.execute("UPDATE outbox SET status='uncertain' WHERE id=?",(row["id"],))
            results.append({"event":row["id"],"status":"uncertain","remote_deduplication":"not_guaranteed"})
    return results

def ensure_ready(store,case_id,actor,region,project,write_key,transport=post_json,retry_uncertain=False):
    store.case(case_id,actor)
    if not store.one("SELECT id FROM outbox WHERE case_id=?",(case_id,)):
        with store.tx():
            store._queue(case_id,{"name":"ApprovalBridge trace preflight","kind":"WORKFLOW","status":"OK","metadata":{"synthetic":store.bundle(case_id,actor).synthetic,"purpose":"delivery readiness; no agent execution claimed"}})
    results=flush(store,case_id,actor,region,project,write_key,transport,retry_uncertain)
    if store.one("SELECT id FROM outbox WHERE case_id=? AND status!='delivered' LIMIT 1",(case_id,)):
        raise BridgeError("trace_gate","Trace delivery incomplete; inspect and explicitly recover or retry uncertain events")
    return results
