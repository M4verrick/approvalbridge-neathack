"""Small allowlisted operational events. Evidence is stored separately.

Application append-only behavior is not protection from the database owner.
"""
import json
import re
from .util import canonical, digest, now

CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
HASH = re.compile(r"^[a-f0-9]{64}$")
COUNTERS = {"tools", "attempts", "inquiries", "active_ms", "reserved_micro"}

def clean(details):
    result = {}
    for key, value in (details or {}).items():
        if key in {"request_hash", "result_hash", "config_hash", "proposal_hash", "content_hash", "operation_hash"} and isinstance(value, str) and HASH.fullmatch(value):
            result[key] = value
        elif key in {"before", "after"} and isinstance(value, dict):
            result[key] = {k: v for k, v in value.items() if k in COUNTERS and type(v) is int and v >= 0}
        elif key in {"count", "seq", "attempt", "bytes", "elapsed_ms"} and type(value) is int and value >= 0:
            result[key] = value
        elif key == "source_refs" and isinstance(value, list):
            result[key] = [digest(str(v)) for v in value[:32]]
    return result

def record(store, event, actor="operator", case=None, revision=None, run=None, tool=None, outcome="recorded", reason=None, details=None):
    def code(value):
        return value if isinstance(value, str) and CODE.fullmatch(value) else None
    store.db.execute("INSERT INTO audit_events(recorded_at,event,actor_ref,case_id,revision,run_id,tool,outcome,reason,details) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now(), code(event) or "operation", digest(actor), digest(case) if code(case) else None, revision if type(revision) is int else None,
         code(run), tool if tool in ("search", "read", "lookup", "inquire", "propose") else None,
         code(outcome) or "unknown", code(reason), canonical(clean(details))))

def events(store, case, actor, limit=200):
    store.case(case, actor)
    return [dict(r, details=json.loads(r["details"])) for r in store.db.execute(
        "SELECT * FROM audit_events WHERE case_id=? ORDER BY id DESC LIMIT ?", (digest(case), min(200, limit)))]
