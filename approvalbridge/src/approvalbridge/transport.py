"""Explicit, bounded HTTPS transport. No retries, redirects, or environment proxies."""
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from .errors import BridgeError
from .util import canonical

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise BridgeError("redirect","Redirects are forbidden")

def rejection_reason(error):
    # Provider bodies may echo secrets: keep only the error message, truncated, with key-like strings removed.
    try: body=json.loads(error.read(4096)); text=str((body.get("error") or {}).get("message") or body.get("error") or "")
    except Exception: text=""
    return re.sub(r"\b(?:sk-|nlw_|gho_)?[A-Za-z0-9_\-]{32,}\b","[redacted]",text)[:300] or "request rejected"

def post_json(url,headers,payload,timeout=30):
    parts=urlsplit(url)
    if parts.scheme!="https" or not parts.hostname or parts.username or parts.password or parts.fragment or parts.query or parts.port not in (None,443):
        raise BridgeError("endpoint","A fixed HTTPS endpoint without credentials, query, or custom port is required")
    request=urllib.request.Request(url,canonical(payload).encode(),headers={"Content-Type":"application/json",**headers},method="POST")
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with opener.open(request,timeout=timeout) as response:
            raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024: raise BridgeError("response","Response too large")
            return json.loads(raw)
    except BridgeError: raise
    except urllib.error.HTTPError as e:
        if e.code>=500: raise BridgeError("transport_uncertain","Remote server error; delivery and billing may be uncertain") from None
        raise BridgeError("provider_rejected","HTTP %d: %s"%(e.code,rejection_reason(e))) from None
    except Exception:
        # Provider bodies/headers may contain secrets. Preserve uncertainty, not raw exception text.
        raise BridgeError("transport_uncertain","Remote request failed or timed out; delivery and billing may be uncertain") from None
