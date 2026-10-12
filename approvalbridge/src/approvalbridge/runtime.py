"""Operator-owned configuration. Browser requests never supply routes or secrets."""
import json
import os
from pathlib import Path
from pydantic import Field
from .contracts import Strict
from .errors import BridgeError
from .provider import Grant, ChatProvider
from .mail import MailConfig, MicrosoftGraphMailProvider
from .telemetry import ensure_ready
from .util import digest

class RuntimeConfig(Strict):
    allow_live: bool = False
    grant: Grant | None = None
    allowed_routes: list[dict[str,str]] = Field(default_factory=list,max_length=8)
    trace_region: str | None = None
    trace_project: str | None = None
    trace_key_env: str = "APPROVALBRIDGE_NEATLOGS_WRITE_KEY"
    mail: list[MailConfig] = Field(default_factory=list,max_length=2)

class Runtime:
    def __init__(self,config=None,provider_factory=ChatProvider,mail_factory=MicrosoftGraphMailProvider,tracer=None):
        self.config=config or RuntimeConfig();self.provider_factory=provider_factory;self.mail_factory=mail_factory;self._tracer=tracer
    @classmethod
    def load(cls,path):
        if not path:return cls()
        p=Path(path)
        if p.is_symlink() or not p.is_file() or p.stat().st_size>32768:raise BridgeError("configuration","Runtime configuration must be a bounded regular operator file")
        if p.stat().st_mode & 0o077:raise BridgeError("configuration","Runtime configuration must be private to its operator (chmod 600)")
        from .ingestion import load_json
        return cls(RuntimeConfig.model_validate(load_json(p,32768)))
    def provider(self):
        cfg=self.config
        if not cfg.allow_live or not cfg.grant:raise BridgeError("route","Live disabled: configure a verified organizer inference grant on the server")
        g=cfg.grant
        if {"endpoint":g.endpoint,"model":g.model} not in cfg.allowed_routes:raise BridgeError("route","Funded endpoint/model is not in the operator's exact route allowlist")
        provider=self.provider_factory(g);provider.validate()
        if not os.environ.get(g.api_key_env):raise BridgeError("credential","Server-side inference credential is missing")
        if self._tracer is None:
            if not cfg.trace_key_env.startswith("APPROVALBRIDGE_"): raise BridgeError("trace_config","Use an APPROVALBRIDGE_ neatlogs key variable")
            from .telemetry import check_config
            check_config(cfg.trace_region,cfg.trace_project,os.environ.get(cfg.trace_key_env,""))
        return provider
    def trace(self,store,case,actor):
        if self._tracer:return self._tracer(store,case,actor)
        cfg=self.config
        return ensure_ready(store,case,actor,cfg.trace_region,cfg.trace_project,os.environ.get(cfg.trace_key_env,""))
    def mail_provider(self,connection):
        cfg=next((x for x in self.config.mail if x.id==connection),None)
        if not cfg or not cfg.enabled:raise BridgeError("mail_config","Mailbox is not configured and enabled by its operator")
        return self.mail_factory(cfg)
    def readiness(self):
        try:provider=self.provider();live=True;reason="Configured; each case still requires trace acknowledgement and remaining grant budget";execution=provider.transport_kind
        except BridgeError as error:live=False;reason=error.message;execution="disabled"
        g=self.config.grant
        return {"mode":"offline_default","model_calls_enabled":live,"execution":execution,"inference":reason,
            "model":g.model if g else None,"config_hash":digest(self.config.model_dump()),
            "mail":[{"id":x.id,"enabled":x.enabled and bool(os.environ.get(x.token_env)),"folders":x.folders,"shared_mailbox":False} for x in self.config.mail],
            "neatlogs":"Requires actual acknowledged delivery; local outbox alone is not evidence",
            "entire":"Actual development-context capture is unverified","cfo_ai":"Separate hypothetical business planning in cfo.ai; no application connection or runtime integration"}
