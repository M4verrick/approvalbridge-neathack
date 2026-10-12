import json
from pathlib import Path
from .contracts import Bundle
from .errors import BridgeError

def load_json(path,max_bytes=1200*1024):
    p=Path(path)
    if p.is_symlink() or not p.is_file():
        raise BridgeError("invalid","Bundle must be a regular local JSON file, not a symlink")
    if p.stat().st_size>max_bytes:
        raise BridgeError("invalid","Bundle exceeds size limit")
    try:
        text=p.read_text(encoding="utf-8")
        def unique(pairs):
            out={}
            for k,v in pairs:
                if k in out: raise ValueError("duplicate JSON key")
                out[k]=v
            return out
        value=json.loads(text,object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        return value
    except (ValueError,UnicodeError) as exc:
        raise BridgeError("invalid",str(exc)) from exc

def load_bundle(path):
    return Bundle.model_validate(load_json(path))

def visible(bundle):
    docs=[d for d in bundle.evidence if d.available and not d.restricted and d.id not in bundle.withdrawn]
    allowed={d.id for d in docs}
    data=bundle.model_dump()
    data["evidence"]=[d.model_dump() for d in docs]
    data["settlements"]=[s.model_dump() for s in bundle.settlements if s.source_id in allowed]
    data["withdrawn"]=[]
    return data
