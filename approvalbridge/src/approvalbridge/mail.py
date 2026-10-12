"""Operator-selected, bounded read-only mail import. No mailbox mutations."""
import json
import os
import time
import urllib.request
import urllib.error
from typing import Annotated, Literal, Protocol
from urllib.parse import quote, urlencode, urlsplit, parse_qsl
from pydantic import Field, model_validator
from .contracts import Strict, Identifier, Evidence, MailProvenance, Bundle
from .errors import BridgeError
from .transport import NoRedirect
from .util import canonical, digest, instant, now

GRAPH = "https://graph.microsoft.com/v1.0"

class MailConfig(Strict):
    id: Identifier
    tenant: Identifier
    account_id: Identifier
    token_env: Annotated[str, Field(pattern=r"^APPROVALBRIDGE_[A-Z0-9_]+$")]
    folders: list[Literal["inbox", "sentitems"]] = Field(default_factory=lambda: ["inbox"], min_length=1, max_length=2)
    enabled: bool = False

class PreviewRequest(Strict):
    case: Identifier
    expected_revision: Annotated[int, Field(ge=1)]
    connection: Identifier
    folder: Literal["inbox", "sentitems"] = "inbox"
    start: str
    end: str
    key: Annotated[str, Field(min_length=1, max_length=200)]
    @model_validator(mode="after")
    def interval(self):
        a, b = instant(self.start), instant(self.end)
        if not 0 < (b-a).total_seconds() <= 30*86400:
            raise ValueError("Select a positive range of at most 30 days")
        return self

class MailImportRequest(Strict):
    case: Identifier
    expected_revision: Annotated[int, Field(ge=1)]
    preview: Identifier
    selected: list[Annotated[str, Field(min_length=1,max_length=512)]] = Field(min_length=1,max_length=16)
    key: Annotated[str, Field(min_length=1,max_length=200)]

class DisconnectRequest(Strict):
    connection: Identifier
    key: Annotated[str, Field(min_length=1,max_length=200)]

class MailProvider(Protocol):
    config: MailConfig
    synthetic_fixture: bool
    def verify_account(self): ...
    def preview(self, folder, start, end): ...
    def read(self, message, folder): ...

def get_json(url, headers, timeout):
    request=urllib.request.Request(url,headers=headers,method="GET")
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with opener.open(request,timeout=timeout) as response:
            raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024: raise BridgeError("mail_size","Mail response exceeds 2 MiB")
            data=json.loads(raw)
            if not isinstance(data,dict): raise ValueError()
            return data
    except BridgeError: raise
    except urllib.error.HTTPError as error:
        if error.code in (401,403): raise BridgeError("mail_access","Mailbox access expired or denied; reconnect through operator setup") from None
        if error.code==429: raise BridgeError("mail_throttled","Mailbox throttled; wait before explicitly retrying this read") from None
        if error.code==404: raise BridgeError("mail_missing","Selected message is unavailable; preview again") from None
        raise BridgeError("mail_transport","Mail service request failed; inspect saved import state") from None
    except Exception:
        raise BridgeError("mail_transport","Mail request failed or timed out; no mail was changed") from None

class MicrosoftGraphMailProvider:
    synthetic_fixture=False
    def __init__(self,config,transport=get_json):
        self.config=config;self.transport=transport;self.calls=0;self.started=time.monotonic()
    def _get(self,path):
        cfg=self.config
        if not cfg.enabled: raise BridgeError("mail_config","Mailbox reads are not enabled by the operator")
        url=path if path.startswith("https://") else GRAPH+path
        parts=urlsplit(url)
        if parts.scheme!="https" or parts.netloc!="graph.microsoft.com" or parts.fragment or not parts.path.startswith("/v1.0/me/") and parts.path!="/v1.0/me":
            raise BridgeError("mail_scope","Only this configured user's Microsoft Graph mailbox is permitted")
        elapsed=time.monotonic()-self.started
        if self.calls>=20 or elapsed>=60: raise BridgeError("mail_budget","Mail request/time budget exhausted; narrow the selection")
        key=os.environ.get(cfg.token_env)
        if not key: raise BridgeError("mail_config","Server-side mailbox credential is missing")
        self.calls+=1
        return self.transport(url,{"Authorization":"Bearer "+key,"Prefer":'IdType="ImmutableId", outlook.body-content-type="text"'},max(0.1,min(10,60-elapsed)))
    def verify_account(self):
        response=self._get("/me?"+urlencode({"$select":"id"}))
        if response.get("id")!=self.config.account_id: raise BridgeError("mail_account","Connected account differs from the configured mailbox")
        return digest([self.config.tenant,self.config.account_id])
    def preview(self,folder,start,end):
        if folder not in self.config.folders: raise BridgeError("mail_scope","Folder is outside this connection's allowed scope")
        field="sentDateTime" if folder=="sentitems" else "receivedDateTime"
        select="id,subject,sender,receivedDateTime,sentDateTime,parentFolderId,isDraft"
        path="/me/mailFolders/"+folder+"/messages"
        expected_filter=f"{field} ge {start} and {field} lt {end}"
        url=GRAPH+path+"?"+urlencode({"$select":select,"$top":20,"$filter":expected_filter,"$orderby":field+" desc"})
        messages=[];more=False
        for page in range(2):
            result=self._get(url)
            if not isinstance(result.get("value"),list) or len(result["value"])>20: raise BridgeError("mail_output","Invalid mail preview response")
            for item in result["value"]:
                if not isinstance(item,dict) or not isinstance(item.get("id"),str) or not 1<=len(item["id"])<=512: raise BridgeError("mail_output","Invalid mail message identity")
                timestamp=item.get(field)
                if item.get("isDraft") or not timestamp or not instant(start)<=instant(timestamp)<instant(end):continue
                sender=item.get("sender",{}).get("emailAddress",{})
                messages.append({"id":item["id"],"subject":str(item.get("subject",""))[:300],"sender":str(sender.get("address","unknown"))[:320],
                    "received_at":item.get("receivedDateTime"),"sent_at":item.get("sentDateTime"),"parent":item.get("parentFolderId")})
            next_url=result.get("@odata.nextLink")
            if not next_url:break
            parts=urlsplit(next_url)
            if parts.scheme!="https" or parts.netloc!="graph.microsoft.com" or parts.path!="/v1.0"+path or parts.fragment:
                raise BridgeError("mail_scope","Mail pagination attempted to leave the selected folder")
            query=parse_qsl(parts.query)
            values=dict(query)
            if len(next_url)>8192 or len(query)!=len(values) or set(values)-{"$select","$top","$filter","$orderby","$skip","$skiptoken"} or values.get("$filter")!=expected_filter or values.get("$select")!=select or values.get("$top")!="20":
                raise BridgeError("mail_scope","Mail pagination changed the approved query scope")
            url=next_url;more=True
        return {"messages":messages,"incomplete":bool(next_url)}
    def read(self,message,folder):
        if folder not in self.config.folders: raise BridgeError("mail_scope","Folder is unavailable")
        select="id,subject,sender,body,receivedDateTime,sentDateTime,parentFolderId,isDraft,webLink"
        raw=self._get("/me/mailFolders/"+folder+"/messages/"+quote(message,safe="")+"?"+urlencode({"$select":select}))
        body=raw.get("body",{})
        if body.get("contentType","").lower()!="text" or not isinstance(body.get("content"),str):raise BridgeError("mail_output","Plain-text mail body required")
        return {"id":raw.get("id"),"subject":raw.get("subject",""),"text":body["content"],"sender":raw.get("sender",{}),
            "received_at":raw.get("receivedDateTime"),"sent_at":raw.get("sentDateTime"),"parent":raw.get("parentFolderId"),
            "is_draft":raw.get("isDraft",False),"source_link":raw.get("webLink")}

def register(store,provider,actor):
    cfg=provider.config;account=provider.verify_account()
    with store.tx():
        previous=store.one("SELECT * FROM mail_connections WHERE id=?",(cfg.id,))
        if previous and (previous["revoked"] or previous["account_ref"]!=account or previous["actor_ref"]!=digest(actor)):
            raise BridgeError("mail_access","Connection was disconnected or changed; configure a new connection ID")
        store.db.execute("INSERT OR IGNORE INTO mail_connections(id,account_ref,actor_ref) VALUES(?,?,?)",(cfg.id,account,digest(actor)))
    return account

def preview(store,provider,actor,request):
    case=store.case(request.case,actor)
    operation={"action":"mail.preview","request":request.model_dump()}
    saved=store._replay(request.key,request.case,operation)
    if saved:return saved
    if case["revision"]!=request.expected_revision: raise BridgeError("stale","Case changed before mail preview")
    account=register(store,provider,actor)
    result=provider.preview(request.folder,request.start,request.end)
    from .store import ident
    pid=ident("mailpreview")
    with store.tx():
        case=store.case(request.case,actor)
        if case["revision"]!=request.expected_revision: raise BridgeError("stale","Case changed while mail preview was fetched")
        connection=store.one("SELECT * FROM mail_connections WHERE id=?",(request.connection,))
        if connection["revoked"]:raise BridgeError("mail_access","Mail connection disconnected")
        payload={"scope":request.model_dump(),"account_ref":account,"messages":result["messages"],"incomplete":result["incomplete"]}
        store.db.execute("INSERT INTO mail_previews VALUES(?,?,?,?,?,?,?,?)",(pid,digest(actor),request.case,case["revision"],request.connection,connection["epoch"],canonical(payload),time.time()+600))
        store.audit("mail.preview",actor,case=request.case,revision=case["revision"],outcome="complete",details={"count":len(result["messages"]),"request_hash":digest(request.model_dump())})
        output={"preview":pid,"messages":result["messages"],"incomplete":result["incomplete"],"expires_in_seconds":600}
        store._receipt(request.key,request.case,operation,output)
    return output

def import_selected(store,provider,actor,request):
    operation={"action":"mail.import","request":request.model_dump()}
    case=store.case(request.case,actor)
    previous=store._replay(request.key,request.case,operation)
    if previous:return previous
    row=store.one("SELECT * FROM mail_previews WHERE id=?",(request.preview,))
    if not row or row["actor_ref"]!=digest(actor) or row["case_id"]!=request.case:raise BridgeError("mail_scope","Preview does not belong to this case/operator")
    if row["expires_at"]<=time.time():raise BridgeError("mail_stale","Mail preview expired; preview again")
    if request.expected_revision!=case["revision"] or row["revision"]!=case["revision"]:raise BridgeError("stale","Mail preview case revision changed")
    cfg=provider.config
    if row["connection"]!=cfg.id:raise BridgeError("mail_scope","Wrong mailbox connection")
    payload=json.loads(row["payload"]);selected=set(request.selected);permitted={x["id"]:x for x in payload["messages"]}
    if len(selected)!=len(request.selected) or not selected<=set(permitted):raise BridgeError("mail_scope","Select unique messages from this preview only")
    account=register(store,provider,actor)
    initial=store.bundle(request.case,actor);packet=initial.model_dump();added=[];dedup=0
    existing={d.id:d for d in initial.evidence}
    for message in request.selected:
        store.case(request.case,actor)
        current_connection=store.one("SELECT * FROM mail_connections WHERE id=?",(cfg.id,))
        if current_connection["revoked"] or current_connection["epoch"]!=row["epoch"]:raise BridgeError("mail_access","Connection changed during mail retrieval")
        raw=provider.read(message,payload["scope"]["folder"])
        meta=permitted[message]
        if raw.get("id")!=message or raw.get("parent")!=meta["parent"] or raw.get("is_draft"):
            raise BridgeError("mail_stale","Message moved or changed scope; preview again")
        field="sent_at" if payload["scope"]["folder"]=="sentitems" else "received_at"
        if not instant(payload["scope"]["start"])<=instant(raw[field])<instant(payload["scope"]["end"]):raise BridgeError("mail_scope","Message date is outside the selected range")
        if not isinstance(raw.get("text"),str):raise BridgeError("mail_output","Normalized plain-text mail required")
        text="Subject: "+str(raw.get("subject",""))+"\n\n"+raw["text"]
        if len(text.encode())>32768:raise BridgeError("mail_size","Selected email exceeds 32 KiB; no partial message was imported")
        ref=digest([account,message]);h=digest({"text":text,"sender":raw.get("sender"),"sent":raw.get("sent_at"),"received":raw.get("received_at")})
        eid="mail-"+digest([ref,h])[:56]
        if eid in existing:dedup+=1;continue
        prior=next((d.id for d in reversed(list(existing.values())) if d.mail and d.mail.message_ref==ref),None)
        url=raw.get("source_link");parts=urlsplit(url or "")
        if url and (parts.scheme!="https" or parts.hostname not in ("outlook.office.com","outlook.office365.com","outlook.live.com") or parts.username or parts.password or parts.port not in (None,443)):
            url=None
        evidence=Evidence(id=eid,text=text,sender="sender-"+digest(raw.get("sender",{}))[:24],identity="unknown",kind="email",
            effective_at=raw.get("sent_at") or raw["received_at"],origin="Imported mailbox snapshot; sender authority unresolved",
            mail=MailProvenance(provider="microsoft_graph",connection=cfg.id,account_ref=account,message_ref=ref,content_hash=h,imported_at=now(),
                sent_at=raw.get("sent_at"),received_at=raw["received_at"],source_link=url,previous_source=prior,synthetic_fixture=provider.synthetic_fixture))
        existing[eid]=evidence;added.append(eid)
    if len(existing)>32:raise BridgeError("mail_size","Case would exceed 32 evidence sources; use a smaller selection")
    packet["evidence"]=[d.model_dump() for d in existing.values()]
    packet["synthetic"]=initial.synthetic and provider.synthetic_fixture
    # Advance the evidence cutoff, not a claim of business authority.
    packet["as_of"]=max([initial.as_of,now()],key=instant)
    amended=Bundle.model_validate(packet)
    with store.tx():
        current=store.case(request.case,actor);connection=store.one("SELECT * FROM mail_connections WHERE id=?",(cfg.id,))
        if current["revision"]!=request.expected_revision:raise BridgeError("stale","Case changed during mail retrieval")
        if connection["revoked"] or connection["epoch"]!=row["epoch"]:raise BridgeError("mail_access","Mailbox disconnected during retrieval")
        imported=store.import_bundle(amended,actor,"mail-bundle-"+digest(operation)) if added else {"case":request.case,"input_revision":current["revision"]}
        result={**imported,"added":added,"deduplicated":dedup,"nothing_sent":True,"synthetic_fixture":provider.synthetic_fixture}
        store._receipt(request.key,request.case,operation,result)
        store.audit("mail.import",actor,case=request.case,revision=result["input_revision"],outcome="complete",details={"count":len(added),"source_refs":added,"request_hash":digest(operation)})
        return result

def disconnect(store,connection,actor):
    with store.tx():
        row=store.one("SELECT * FROM mail_connections WHERE id=?",(connection,))
        if not row:return {"connection":connection,"status":"disconnected","cases":0}
        if row["actor_ref"]!=digest(actor):raise BridgeError("access","Connection access denied")
        store.db.execute("UPDATE mail_connections SET revoked=1,epoch=epoch+1 WHERE id=? AND revoked=0",(connection,))
        count=0
        for c in store.db.execute("SELECT id FROM cases WHERE owner=? AND revoked=0",(actor,)).fetchall():
            bundle=store.bundle(c["id"],actor);sources=[d.id for d in bundle.evidence if d.mail and d.mail.connection==connection and d.id not in bundle.withdrawn]
            if sources:
                packet=bundle.model_dump();packet["withdrawn"]=sorted(set(packet["withdrawn"])|set(sources))
                result=store.import_bundle(Bundle.model_validate(packet),actor,"mail-disconnect-"+digest([connection,c["id"],row["epoch"]]))
                store.audit("mail.disconnect",actor,case=c["id"],revision=result["input_revision"],outcome="disconnected",details={"source_refs":sources});count+=1
        return {"connection":connection,"status":"disconnected","cases":count}
