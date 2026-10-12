import hashlib
import json
from datetime import datetime, timezone

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def now():
    return datetime.now(timezone.utc).isoformat()

def instant(value):
    dt=datetime.fromisoformat(value.replace("Z","+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return dt.astimezone(timezone.utc)
