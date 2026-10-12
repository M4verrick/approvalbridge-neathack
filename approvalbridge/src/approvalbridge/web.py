"""Local review surface over the guarded engine.

The trusted OS owner supplies server-only runtime configuration. Mail reads and
funded inference are disabled by default. No browser credential controls.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Literal
import fcntl
import hmac
import json
import os
import re
import secrets
import threading
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, model_validator
from .comparison import Protocol, compare, get_report, load_protocol, summarize
from .contracts import Bundle, Identifier, Strict
from .errors import BridgeError
from .fixtures import demo
from .ingestion import visible
from .render import markdown
from .runner import run_investigation
from .simulation import ReplyMap, apply_replies, history
from .store import Store, ident
from .util import canonical, digest, now, text_hash
from .workflow import pending_requests, simulate
from .runtime import Runtime
from .mail import PreviewRequest, MailImportRequest, DisconnectRequest, preview as mail_preview, import_selected, disconnect
from .audit import events as audit_events

MAX_BODY = 1200 * 1024
Key = Annotated[str, Field(min_length=1, max_length=200)]
Revision = Annotated[int, Field(ge=1)]
Reason = Annotated[str, Field(min_length=1, max_length=1000)]
ACTIVE = ("queued", "running")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin", "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


class ImportRequest(Strict):
    bundle: Bundle
    key: Key
    expected_revision: Revision | None


class DemoRequest(Strict):
    id: Identifier
    stage: Annotated[int, Field(ge=1, le=3)] = 1
    key: Key
    expected_revision: Revision | None


class InvestigationRequest(Strict):
    case: Identifier
    key: Key
    expected_revision: Revision


class JobRequest(Strict):
    action: Literal["run", "simulate", "compare", "recover"]
    key: Key
    investigation: Identifier | None = None
    variant: Literal["A", "B", "C"] = "C"
    expected_revision: Revision | None = None
    comparison_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")] | None = None
    reply_map: ReplyMap | None = None
    protocol: Protocol | None = None
    run: Identifier | None = None
    job_id: Identifier | None = None
    confirm_worker_stopped: bool = False
    mode: Literal["offline", "live"] = "offline"

    @model_validator(mode="after")
    def action_scope(self):
        if self.action in ("run", "simulate") and (not self.investigation or self.expected_revision is None):
            raise ValueError("Investigation and expected revision required")
        if self.action == "compare" and not self.comparison_id:
            raise ValueError("Comparison id required")
        if self.action == "recover" and (not self.confirm_worker_stopped or not (self.run or self.job_id)):
            raise ValueError("Recovery requires a run or interrupted job and stopped-worker confirmation")
        if self.action != "compare" and self.protocol is not None:
            raise ValueError("Protocol is only valid for a comparison")
        if self.action not in ("simulate", "recover") and self.reply_map is not None:
            raise ValueError("Reply map is only valid for simulation")
        return self


class ReplyRequest(Strict):
    run: Identifier
    key: Key
    reply_map: ReplyMap | None = None


class AcceptRequest(Strict):
    proposal: Identifier
    hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    key: Key


class RejectRequest(Strict):
    proposal: Identifier
    reason: Reason
    key: Key | None = None


class CancelRequest(Strict):
    run: Identifier | None = None
    job_id: Identifier | None = None
    reason: Reason
    key: Key | None = None

    @model_validator(mode="after")
    def target(self):
        if bool(self.run) == bool(self.job_id):
            raise ValueError("Choose one run or job")
        return self


class WithdrawRequest(Strict):
    case: Identifier
    source: Identifier
    expected_revision: Revision
    key: Key


class ExportRequest(Strict):
    case: Identifier
    revision: Revision | None = None
    format: Literal["json", "markdown"] = "markdown"


REQUESTS = {"/api/import": ImportRequest, "/api/demo": DemoRequest,
            "/api/investigations": InvestigationRequest, "/api/jobs": JobRequest,
            "/api/replies": ReplyRequest, "/api/accept": AcceptRequest,
            "/api/reject": RejectRequest, "/api/cancel": CancelRequest,
            "/api/withdraw": WithdrawRequest, "/api/export": ExportRequest}


def parse_json(raw):
    """Reject ambiguous JSON before typed validation; errors never echo source text."""
    if len(raw) > MAX_BODY:
        raise BridgeError("too_large", "JSON request exceeds 1,228,800 bytes")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def nonfinite(_):
        raise ValueError("nonfinite JSON value")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=nonfinite)
        if not isinstance(result, dict):
            raise ValueError("JSON object required")
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise BridgeError("invalid", "Expected a bounded UTF-8 JSON object without duplicate keys or nonfinite numbers") from None


def default_reply_map():
    second = demo(2).model_dump()["evidence"]
    third = demo(3).model_dump()["evidence"]
    def source(records, name):
        return next(d for d in records if d["id"] == name)
    return ReplyMap.model_validate({"id": "two-rounds", "rules": [
        {"id": "approval-a", "claims": ["A"], "intent": "approval", "evidence": [source(second, "approval-a")]},
        {"id": "approval-b", "claims": ["B"], "intent": "approval", "evidence": [source(second, "approval-b")]},
        {"id": "condition-b", "claims": ["B"], "intent": "condition", "requires_sources": ["approval-b"], "evidence": [source(third, "condition-b")]},
        {"id": "unavailable", "claims": ["C"], "intent": "approval", "outcome": "unavailable"}]})


def default_protocol():
    return load_protocol(Path(__file__).resolve().parents[2] / "fixtures/conversations-v1/protocol.json")


class WebService:
    """Durable jobs use fresh SQLite connections in each request/worker thread."""
    def __init__(self, db, actor="analyst", start_workers=True, runtime=None):
        self.runtime = runtime or Runtime()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", actor):
            raise BridgeError("invalid", "Use a bounded local actor identifier")
        supplied = Path(db).absolute()
        if supplied.is_symlink():
            raise BridgeError("path", "Database cannot be a symlink")
        self.path = supplied.resolve()
        self.actor = actor
        self.csrf = secrets.token_urlsafe(32)
        self.start_workers = start_workers
        self._threads = set()
        self._thread_lock = threading.Lock()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".web.lock")
        try:
            self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.fchmod(self._lock_fd, 0o600)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if hasattr(self, "_lock_fd"):
                os.close(self._lock_fd)
            raise BridgeError("busy", "Another web server owns this database, or its lock is unavailable") from None
        try:
            with self.store() as store:
                store.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS web_operations(key TEXT PRIMARY KEY, actor TEXT NOT NULL, request_hash TEXT NOT NULL, case_id TEXT, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_jobs(id TEXT PRIMARY KEY, actor TEXT NOT NULL, case_id TEXT, investigation TEXT, comparison_id TEXT, action TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, run_id TEXT, result TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, parent_job TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS web_active_case ON web_jobs(case_id) WHERE case_id IS NOT NULL AND status IN ('queued','running');
                CREATE UNIQUE INDEX IF NOT EXISTS web_active_comparison ON web_jobs(comparison_id) WHERE comparison_id IS NOT NULL AND status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS web_downloads(id TEXT PRIMARY KEY, actor TEXT NOT NULL, case_id TEXT NOT NULL, dossier_revision INTEGER NOT NULL, format TEXT NOT NULL, hash TEXT NOT NULL);
                COMMIT;
                """)
                with store.tx():
                    store.db.execute("UPDATE web_jobs SET status='interrupted',error=?,updated_at=? WHERE status IN ('queued','running')",
                                     (canonical({"code": "interrupted", "message": "Server stopped before a completion receipt; confirm the previous worker stopped before recovery"}), now()))
        except BaseException:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            raise

    @contextmanager
    def store(self):
        store = Store(self.path)
        try:
            yield store
        finally:
            store.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        with self._thread_lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in threads):
            # Keep the process lock while a worker still owns the database.
            self._closed = False
            raise BridgeError("busy", "A local worker is still stopping")
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)

    def _revision(self, store, case_id, expected):
        case = store.one("SELECT id FROM cases WHERE id=?", (case_id,))
        if case:
            actual = store.case(case_id, self.actor)["revision"]
            if expected != actual:
                raise BridgeError("stale", "Case changed; refresh and review the current revision")
        elif expected is not None:
            raise BridgeError("stale", "New cases require expected_revision:null")

    def _idle(self, store, case_id):
        if store.one("SELECT id FROM web_jobs WHERE case_id=? AND status IN ('queued','running')", (case_id,)):
            raise BridgeError("busy", "A local job is active for this case")
        if store.one("SELECT id FROM runs WHERE case_id=? AND status='running'", (case_id,)):
            raise BridgeError("recovery", "An active or interrupted run must be cancelled or explicitly recovered")

    def _writable(self, store, case_id):
        store.review_writable(case_id, self.actor)
        self._idle(store, case_id)

    def _operation(self, store, path, request, case_id, operation):
        key = getattr(request, "key", None)
        hashed = digest({"path": path, "request": request.model_dump()})
        with store.tx():
            if case_id and store.one("SELECT id FROM cases WHERE id=?", (case_id,)):
                store.case(case_id, self.actor)
            if key:
                prior = store.one("SELECT * FROM web_operations WHERE key=?", (key,))
                if prior:
                    if prior["actor"] != self.actor:
                        raise BridgeError("access", "Operation receipt access denied")
                    if prior["request_hash"] != hashed:
                        raise BridgeError("conflict", "Operation key already used with a different request")
                    result = json.loads(prior["result"])
                    if result.get("job_id"):
                        return {"job_id": result["job_id"], "status": self._job(store, result["job_id"])["status"]}
                    return result
            result = operation()
            if key:
                store.db.execute("INSERT INTO web_operations VALUES(?,?,?,?,?)", (key, self.actor, hashed, case_id, canonical(result)))
            return result

    def _job(self, store, job_id):
        row = store.one("SELECT * FROM web_jobs WHERE id=?", (job_id,))
        if not row:
            raise BridgeError("not_found", "Job not found")
        if row["actor"] != self.actor:
            raise BridgeError("access", "Job access denied")
        if row["case_id"]:
            store.case(row["case_id"], self.actor)
        result = json.loads(row["result"]) if row["result"] else None
        access_error = None
        try:
            if row["investigation"]:
                store.investigation_history_access(row["investigation"], self.actor)
            if row["comparison_id"] and store.one("SELECT id FROM comparisons WHERE id=?", (row["comparison_id"],)):
                from .comparison import report_access
                report_access(store, row["comparison_id"], self.actor)
            if result and result.get("proposal"):
                store.proposal(result["proposal"], self.actor)
        except BridgeError as error:
            if error.code != "access":
                raise
            result = None
            access_error = error.message
        return {k: row[k] for k in ("id", "action", "status", "case_id", "investigation", "comparison_id", "run_id", "created_at", "updated_at", "parent_job")} | {
            "job_id": row["id"], "result": result, "error": json.loads(row["error"]) if row["error"] else None, "access_warning": access_error}

    def state(self, store):
        cases = []
        for row in store.db.execute("SELECT id FROM cases WHERE owner=? AND revoked=0 ORDER BY rowid DESC", (self.actor,)).fetchall():
            case = store.case(row["id"], self.actor)
            bundle = store.bundle(case["id"], self.actor)
            packet = visible(bundle)
            latest = store.one("SELECT status,revision FROM proposals WHERE case_id=? ORDER BY rowid DESC LIMIT 1", (case["id"],))
            cases.append({"id": case["id"], "title": bundle.title, "revision": case["revision"], "currency": bundle.currency, "synthetic": bundle.synthetic,
                          "counts": {"claims": len(bundle.claims), "evidence": len(packet["evidence"]), "unavailable_evidence": len(bundle.evidence) - len(packet["evidence"])},
                          "status": (latest["status"] if latest["revision"] == case["revision"] else "needs_review") if latest else "ready", "comparison_case": bool(store.one("SELECT id FROM comparison_jobs WHERE case_id=?", (case["id"],)))})
        comparisons = [dict(r) for r in store.db.execute("SELECT id,status,plan_hash,model_attempts,reserved_micro FROM comparisons WHERE actor=? ORDER BY rowid DESC", (self.actor,))]
        jobs = []
        for row in store.db.execute("SELECT id FROM web_jobs WHERE actor=? ORDER BY rowid DESC LIMIT 100", (self.actor,)).fetchall():
            try:
                job = self._job(store, row["id"])
                # State polling stays small; complete result is in the job endpoint.
                job.pop("result")
                jobs.append(job)
            except BridgeError as error:
                if error.code != "access":
                    raise
        readiness=self.runtime.readiness()
        if self.runtime.config.grant:
            grant=self.runtime.config.grant
            usage=store.one("SELECT reserved_micro FROM grants WHERE id=?",(grant.id,))
            reserved=usage["reserved_micro"] if usage else 0
            readiness["allowance"]={"remaining_micro":max(0,grant.limit_micro_usd-reserved),"reserved_micro":reserved,"call_micro":grant.worst_case_call_micro_usd}
        known={c["id"] for c in readiness["mail"]}
        for saved in store.db.execute("SELECT id,revoked FROM mail_connections WHERE actor_ref=?",(digest(self.actor),)):
            if saved["id"] not in known:readiness["mail"].append({"id":saved["id"],"enabled":False,"folders":[],"shared_mailbox":False})
        for connection in readiness["mail"]:
            saved=store.one("SELECT revoked FROM mail_connections WHERE id=?",(connection["id"],))
            connection["revoked"]=bool(saved and saved["revoked"])
        return {"cases": cases, "comparisons": comparisons, "jobs": jobs, "csrf": self.csrf,
                "actor": self.actor, "readiness": readiness}

    def case_view(self, store, case_id):
        case = store.case(case_id, self.actor)
        bundle = store.bundle(case_id, self.actor)
        packet = visible(bundle)
        investigations = []
        replies = []
        warnings = []
        for row in store.db.execute("SELECT * FROM investigations WHERE case_id=? ORDER BY rowid DESC", (case_id,)).fetchall():
            inv = dict(row)
            inv["limits"] = json.loads(inv["limits"])
            try:
                inv["reply_history"] = history(store, inv["id"], self.actor)
                replies.extend(dict(x, investigation=inv["id"]) for x in inv["reply_history"])
            except BridgeError as error:
                if error.code != "access":
                    raise
                inv["reply_history"] = []
                inv["access_warning"] = error.message
                warnings.append(error.message)
            investigations.append(inv)
        runs = []
        for raw in store.db.execute("SELECT id,investigation,revision,epoch,variant,mode,status,started FROM runs WHERE case_id=? ORDER BY rowid DESC", (case_id,)).fetchall():
            run = dict(raw)
            meta = store.one("SELECT payload FROM run_metadata WHERE run_id=?", (run["id"],))
            run["metadata"] = json.loads(meta["payload"]) if meta else {}
            run["steps"] = []
            try:
                store.investigation_history_access(run["investigation"], self.actor)
                run["requests"] = pending_requests(store, run["id"], self.actor)
                for step in store.db.execute("SELECT seq,kind,status,payload FROM steps WHERE run_id=? ORDER BY seq", (run["id"],)):
                    data = json.loads(step["payload"]) if step["payload"] else {}
                    run["steps"].append({"seq": step["seq"], "kind": step["kind"], "status": step["status"], "tool": data.get("tool"),
                                         "request_hash": data.get("request_hash"), "result_hash": data.get("result_hash")})
            except BridgeError as error:
                if error.code != "access":
                    raise
                run["requests"] = []
                run["access_warning"] = error.message
            runs.append(run)
        proposals = [dict(x) for x in store.db.execute("SELECT id,run_id,revision,epoch,hash,valid,status FROM proposals WHERE case_id=? ORDER BY rowid DESC", (case_id,))]
        latest = None
        if proposals:
            try:
                latest = store.proposal(proposals[0]["id"], self.actor)
            except BridgeError as error:
                if error.code != "access":
                    raise
                warnings.append(error.message)
        snapshots = [dict(x) for x in store.db.execute("SELECT s.revision,s.hash,m.recorded_at FROM snapshots s LEFT JOIN snapshot_metadata m ON m.case_id=s.case_id AND m.revision=s.revision WHERE s.case_id=? ORDER BY s.revision DESC", (case_id,))]
        dossiers = [dict(x) for x in store.db.execute("SELECT revision,proposal_id,hash,status FROM dossiers WHERE case_id=? ORDER BY revision DESC", (case_id,))]
        for dossier in dossiers:
            saved=store.one("SELECT payload FROM dossiers WHERE case_id=? AND revision=?",(case_id,dossier["revision"]))
            data=json.loads(saved["payload"])
            dossier["accepted_by"]=data.get("accepted_by");dossier["accepted_at"]=data.get("accepted_at")
        jobs = [self._job(store, x["id"]) for x in store.db.execute("SELECT id FROM web_jobs WHERE case_id=? ORDER BY rowid DESC", (case_id,)).fetchall()]
        metadata = store.one("SELECT provenance FROM snapshot_metadata WHERE case_id=? AND revision=?", (case_id, case["revision"]))
        provenance = json.loads(metadata["provenance"]) if metadata else {}
        provenance = {doc["id"]: provenance.get(doc["id"], {}) for doc in packet["evidence"]}
        return {"case": case | {"title": bundle.title, "comparison_case": bool(store.one("SELECT id FROM comparison_jobs WHERE case_id=?", (case_id,)))},
                "bundle": packet, "source_provenance": provenance, "investigations": investigations, "runs": runs,
                "proposals": proposals, "snapshots": snapshots, "latest_proposal": latest, "dossiers": dossiers,
                "reply_history": replies, "jobs": jobs, "active_job": next((j for j in jobs if j["status"] in ACTIVE), None),
                "audit": audit_events(store,case_id,self.actor), "warnings": sorted(set(warnings)), "unavailable_evidence_count": len(bundle.evidence) - len(packet["evidence"])}

    def get(self, path):
        with self.store() as store, store.tx():
            if path == "/api/state":
                return self.state(store)
            match = re.fullmatch(r"/api/(cases|proposals|jobs|comparisons)/([A-Za-z0-9][A-Za-z0-9_.-]{0,79})", path)
            if not match:
                raise BridgeError("not_found", "API route not found")
            resource, target = match.groups()
            if resource == "cases":
                return self.case_view(store, target)
            if resource == "proposals":
                return store.proposal(target, self.actor)
            if resource == "jobs":
                return self._job(store, target)
            return get_report(store, target, self.actor)

    def operational_event(self,event,outcome="recorded",reason=None,case=None,details=None):
        with self.store() as store:
            store.audit(event,self.actor,case=case,outcome=outcome,reason=reason,details=details)

    def post(self,path,payload):
        case=payload.get("case") if isinstance(payload,dict) else None
        if not case and isinstance(payload,dict):
            with self.store() as store:
                for field,table in (("proposal","proposals"),("run","runs"),("investigation","investigations"),("job_id","web_jobs")):
                    value=payload.get(field)
                    if isinstance(value,str):
                        row=store.one("SELECT case_id FROM "+table+" WHERE id=?",(value,))
                        if row and row["case_id"]:
                            owner=store.one("SELECT owner FROM cases WHERE id=?",(row["case_id"],))
                            if owner and owner["owner"]==self.actor:case=row["case_id"];break
        request_hash=digest(payload)
        try:
            if path in ("/api/mail/preview","/api/mail/import","/api/mail/disconnect"):
                result=self.mail_post(path,payload)
            else:result=self._post(path,payload)
            self.operational_event("request.complete",outcome="complete",case=case,details={"request_hash":request_hash,"result_hash":digest(result)})
            return result
        except (BridgeError,ValidationError) as error:
            code=error.code if isinstance(error,BridgeError) else "invalid"
            self.operational_event("request.denied",outcome="denied",reason=code,case=case,details={"request_hash":request_hash})
            if isinstance(error,ValidationError):raise BridgeError("invalid","Invalid mail request fields; no case was changed") from None
            raise
        except Exception:
            self.operational_event("request.failed",outcome="failed",reason="internal",case=case,details={"request_hash":request_hash})
            raise

    def mail_post(self,path,payload):
        model={"/api/mail/preview":PreviewRequest,"/api/mail/import":MailImportRequest,"/api/mail/disconnect":DisconnectRequest}[path]
        request=model.model_validate(payload)
        with self.store() as store:
            if path=="/api/mail/disconnect":
                return self._operation(store,path,request,None,lambda: disconnect(store,request.connection,self.actor))
            store.case(request.case,self.actor)
            operation={"action":"mail.import" if path=="/api/mail/import" else "mail.preview","request":request.model_dump()}
            replay=store._replay(request.key,request.case,operation)
            if replay:return replay
            self._revision(store,request.case,request.expected_revision);self._writable(store,request.case)
            if path=="/api/mail/preview":connection=request.connection
            else:
                saved=store.one("SELECT connection FROM mail_previews WHERE id=?",(request.preview,))
                if not saved:raise BridgeError("mail_scope","Mail preview unavailable")
                connection=saved["connection"]
            provider=self.runtime.mail_provider(connection)
            try:
                return mail_preview(store,provider,self.actor,request) if path=="/api/mail/preview" else import_selected(store,provider,self.actor,request)
            except BridgeError as error:
                store.audit("mail.failure",self.actor,case=request.case,revision=request.expected_revision,outcome="denied",reason=error.code)
                raise

    def _post(self, path, payload):
        model = REQUESTS.get(path)
        if model is None:
            raise BridgeError("not_found", "API route not found")
        def synthetic_flags(value):
            if isinstance(value, dict):
                if "synthetic" in value and type(value["synthetic"]) is not bool:
                    raise BridgeError("invalid", "Data provenance declarations must be explicit booleans")
                for item in value.values():
                    synthetic_flags(item)
            elif isinstance(value, list):
                for item in value:
                    synthetic_flags(item)
        synthetic_flags(payload)
        try:
            request = model.model_validate(payload)
        except ValidationError as error:
            locations = [".".join(map(str, e["loc"])) or "request" for e in error.errors(include_input=False, include_context=False, include_url=False)[:8]]
            raise BridgeError("invalid", "Invalid request fields: " + ", ".join(locations)) from None
        with self.store() as store:
            if path in ("/api/import", "/api/demo"):
                bundle = request.bundle if path == "/api/import" else demo(request.stage, request.id)
                def import_case():
                    self._revision(store, bundle.id, request.expected_revision)
                    self._writable(store, bundle.id)
                    return store.import_bundle(bundle, self.actor, request.key)
                return self._operation(store, path, request, bundle.id, import_case)
            if path == "/api/investigations":
                def investigation():
                    self._revision(store, request.case, request.expected_revision)
                    self._writable(store, request.case)
                    return {"investigation": store.new_investigation(request.case, self.actor), "case": request.case}
                return self._operation(store, path, request, request.case, investigation)
            if path == "/api/jobs":
                result = self._enqueue(store, request)
            elif path == "/api/replies":
                run = store.run(request.run, self.actor)
                store.investigation_history_access(run["investigation"], self.actor)
                def reply():
                    self._writable(store, run["case_id"])
                    requests = pending_requests(store, request.run, self.actor)
                    return apply_replies(store, run["investigation"], self.actor, request.reply_map or default_reply_map(), requests,
                                         request.key, run["revision"], request.run)
                return self._operation(store, path, request, run["case_id"], reply)
            elif path == "/api/accept":
                proposal = store.proposal(request.proposal, self.actor)
                self._writable(store, proposal["case_id"])
                return self._operation(store, path, request, proposal["case_id"], lambda: store.accept(request.proposal, request.hash, self.actor, request.key))
            elif path == "/api/reject":
                proposal = store.proposal(request.proposal, self.actor)
                self._writable(store, proposal["case_id"])
                def reject():
                    current = store.proposal(request.proposal, self.actor)
                    if current["status"] != "rejected: " + request.reason:
                        store.reject(request.proposal, self.actor, request.reason)
                    return {"proposal": request.proposal, "status": "rejected", "reason": request.reason}
                return self._operation(store, path, request, proposal["case_id"], reject)
            elif path == "/api/cancel":
                return self._cancel(store, request)
            elif path == "/api/withdraw":
                def withdraw():
                    self._revision(store, request.case, request.expected_revision)
                    self._writable(store, request.case)
                    bundle = store.bundle(request.case, self.actor).model_dump()
                    if request.source not in {d["id"] for d in visible(Bundle.model_validate(bundle))["evidence"]}:
                        raise BridgeError("access", "Source is not currently available")
                    bundle["withdrawn"] = sorted(set(bundle["withdrawn"]) | {request.source})
                    return store.import_bundle(Bundle.model_validate(bundle), self.actor, request.key)
                return self._operation(store, path, request, request.case, withdraw)
            else:
                return self._export(store, request)
        if self.start_workers:
            self._launch(result["job_id"])
        return result

    def _enqueue(self, store, request):
        prior = store.one("SELECT case_id FROM web_operations WHERE key=?", (request.key,))
        if prior:
            return self._operation(store, "/api/jobs", request, prior["case_id"], lambda: None)
        case_id = None
        investigation = request.investigation
        parent = None
        effective = request.model_dump()
        run_id = None
        if request.action == "recover":
            if request.job_id:
                old = store.one("SELECT * FROM web_jobs WHERE id=?", (request.job_id,))
                self._job(store, request.job_id)
                if old["status"] not in ("interrupted", "paused"):
                    raise BridgeError("conflict", "Only an interrupted or paused web job can be explicitly recovered")
                effective = json.loads(old["payload"])
                effective["recover_stopped"] = True
                parent = old["id"]
                case_id, investigation, run_id = old["case_id"], old["investigation"], old["run_id"]
            else:
                run = store.run(request.run, self.actor)
                if run["mode"] not in ("offline","live"):
                    raise BridgeError("route", "Unsupported web recovery mode")
                case_id, investigation, run_id = run["case_id"], run["investigation"], run["id"]
                effective = {"action": "run", "investigation": investigation, "variant": run["variant"], "mode": run["mode"], "recover_stopped": True}
        elif request.action in ("run", "simulate"):
            inv = store.one("SELECT * FROM investigations WHERE id=?", (investigation,))
            if not inv:
                raise BridgeError("not_found", "Investigation not found")
            case_id = inv["case_id"]
        if effective["action"] == "simulate":
            effective["reply_map"] = (ReplyMap.model_validate(effective["reply_map"]) if effective.get("reply_map") else default_reply_map()).model_dump()
        if effective["action"] == "compare":
            effective["protocol"] = (Protocol.model_validate(effective["protocol"]) if effective.get("protocol") else default_protocol()).model_dump()
        if effective.get("mode")=="live":
            if effective["action"]!="run" or effective["variant"]=="A":raise BridgeError("route","Web live mode supports model investigations B/C only; frozen comparisons remain offline")
            self.runtime.provider()
            current_hash=self.runtime.readiness()["config_hash"]
            if effective.get("runtime_hash") and effective["runtime_hash"]!=current_hash:raise BridgeError("stale","Live configuration changed; cancel and start fresh work")
            effective["runtime_hash"]=current_hash
        comparison_id = effective.get("comparison_id")
        def enqueue():
            if self._closed:
                raise BridgeError("busy", "Server is stopping")
            if case_id:
                store.case(case_id, self.actor)
                if not effective.get("recover_stopped"):
                    self._revision(store, case_id, request.expected_revision)
                    self._writable(store, case_id)
                    if store.one("SELECT id FROM runs WHERE investigation=? AND (variant!=? OR mode!=?) LIMIT 1", (investigation, effective["variant"],effective.get("mode","offline"))):
                        raise BridgeError("conflict", "An investigation cannot change variant or mode; create a fresh investigation")
                elif store.one("SELECT id FROM web_jobs WHERE case_id=? AND status IN ('queued','running')", (case_id,)):
                    raise BridgeError("busy", "Another local job is active for this case")
                if store.one("SELECT id FROM comparison_jobs WHERE case_id=?", (case_id,)):
                    raise BridgeError("comparison_input", "Recover the complete frozen comparison instead of an individual case")
                store.investigation_history_access(investigation, self.actor)
            if comparison_id and store.one("SELECT id FROM web_jobs WHERE comparison_id=? AND status IN ('queued','running')", (comparison_id,)):
                raise BridgeError("busy", "A local job is active for this comparison")
            if store.one("SELECT COUNT(*) n FROM web_jobs WHERE status IN ('queued','running')")["n"] >= 4:
                raise BridgeError("busy", "Four local jobs are already active")
            job_id = ident("webjob")
            timestamp = now()
            store.db.execute("INSERT INTO web_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (job_id, self.actor, case_id, investigation, comparison_id, effective["action"], canonical(effective),
                              "queued", run_id, None, None, timestamp, timestamp, parent))
            return {"job_id": job_id, "status": "queued"}
        return self._operation(store, "/api/jobs", request, case_id, enqueue)

    def _launch(self, job_id):
        with self._thread_lock:
            if self._closed:
                return
            thread = threading.Thread(target=self._thread_run, args=(job_id,), name="approvalbridge-" + job_id, daemon=True)
            self._threads.add(thread)
            thread.start()

    def _thread_run(self, job_id):
        try:
            self.execute_job(job_id)
        finally:
            with self._thread_lock:
                self._threads.discard(threading.current_thread())

    def execute_job(self, job_id):
        """Claim a queued durable job once. Exposed for network-denied service tests."""
        with self.store() as store:
            with store.tx():
                job = store.one("SELECT * FROM web_jobs WHERE id=?", (job_id,))
                if not job or job["actor"] != self.actor or job["status"] != "queued":
                    return
                store.db.execute("UPDATE web_jobs SET status='running',updated_at=? WHERE id=?", (now(), job_id))
            payload = json.loads(job["payload"])
            try:
                provider=None
                if payload.get("mode")=="live":
                    if payload.get("runtime_hash")!=self.runtime.readiness()["config_hash"]:raise BridgeError("stale","Live configuration changed; start fresh work")
                    provider=self.runtime.provider()
                    self.runtime.trace(store,job["case_id"],self.actor)
                def guard():
                    current = store.one("SELECT status FROM web_jobs WHERE id=?", (job_id,))
                    if current["status"] != "running":
                        raise BridgeError("cancelled", "Local job was cancelled")
                guard()
                if job["case_id"]:
                    store.case(job["case_id"], self.actor)
                    if not payload.get("recover_stopped"):
                        self._revision(store, job["case_id"], payload["expected_revision"])
                    store.investigation_history_access(job["investigation"], self.actor)
                if job["action"] == "compare":
                    result = compare(store, job["comparison_id"], self.actor, Protocol.model_validate(payload["protocol"]),
                                     recover_interrupted=payload.get("recover_stopped", False), after_job=lambda _: guard(),
                                     # Offline cancellation checkpoints only; no trace delivery or acknowledgement.
                                     trace_ready=lambda _case: guard())
                elif job["action"] == "simulate":
                    result = simulate(store, job["investigation"], self.actor, ReplyMap.model_validate(payload["reply_map"]),
                                      payload["variant"], recover_interrupted=payload.get("recover_stopped", False), case_guard=guard)
                else:
                    if payload.get("recover_stopped"):
                        prior = job["run_id"] or (store.one("SELECT id FROM runs WHERE investigation=? AND status='running' ORDER BY rowid DESC LIMIT 1", (job["investigation"],)) or {}).get("id")
                        if prior:
                            run = store.run(prior, self.actor)
                            if run["status"] != "running":
                                raise BridgeError("stale", "Interrupted run already finished; review its existing result")
                            run = store.recover(prior, self.actor)
                        else:
                            run = store.start_run(job["investigation"], self.actor, payload["variant"], payload.get("mode","offline"))
                    else:
                        run = store.start_run(job["investigation"], self.actor, payload["variant"], payload.get("mode","offline"))
                    with store.tx():
                        guard()
                        store.db.execute("UPDATE web_jobs SET run_id=? WHERE id=?", (run["id"], job_id))
                    result = run_investigation(store, run["id"], self.actor, provider=provider, after_checkpoint=lambda _: guard())
                    if provider:self.runtime.trace(store,job["case_id"],self.actor)
                run_id = result.get("run_id")
                with store.tx():
                    guard()
                    status = "paused" if result.get("status") == "paused" else "complete"
                    store.db.execute("UPDATE web_jobs SET status=?,result=?,error=NULL,run_id=COALESCE(?,run_id),updated_at=? WHERE id=?",
                                     (status, canonical(result), run_id, now(), job_id))
                    if job["parent_job"] and status == "complete":
                        store.db.execute("UPDATE web_jobs SET status='recovered',result=?,error=NULL,run_id=COALESCE(?,run_id),updated_at=? WHERE id=? AND status IN ('interrupted','paused')",
                                         (canonical({"recovered_by": job_id}), run_id, now(), job["parent_job"]))
            except Exception as error:
                code = error.code if isinstance(error, BridgeError) else "internal"
                store.audit("job.failure",self.actor,case=job["case_id"],run=job["run_id"],outcome="failed",reason=code)
                message = error.message if isinstance(error, BridgeError) else "Local job failed; inspect its recorded state before any explicit retry"
                with store.tx():
                    latest = store.one("SELECT id,status FROM runs WHERE investigation=? ORDER BY rowid DESC LIMIT 1", (job["investigation"],)) if job["investigation"] else None
                    current = store.one("SELECT status FROM web_jobs WHERE id=?", (job_id,))
                    if current["status"] == "cancelled" and job["comparison_id"]:
                        self._pause_comparison(store, job["comparison_id"])
                    if current["status"] == "cancelled" and latest and latest["status"] == "running":
                        store.cancel(latest["id"], self.actor, "Local job was cancelled")
                    status = "cancelled" if current["status"] == "cancelled" else ("interrupted" if latest and latest["status"] == "running" else "failed")
                    store.db.execute("UPDATE web_jobs SET status=?,error=CASE WHEN status='cancelled' AND error IS NOT NULL THEN error ELSE ? END,run_id=COALESCE(?,run_id),updated_at=? WHERE id=?",
                                     (status, canonical({"code": code, "message": message}), latest["id"] if latest else None, now(), job_id))

    def _pause_comparison(self, store, comparison_id):
        saved = store.one("SELECT plan FROM comparisons WHERE id=?", (comparison_id,))
        if saved:
            try:
                summarize(store, comparison_id, self.actor, json.loads(saved["plan"]), pause="cancelled")
            except BridgeError as error:
                if error.code != "access":
                    raise

    def _cancel(self, store, request):
        if request.job_id:
            job = self._job(store, request.job_id)
            case_id = job["case_id"]
        else:
            run = store.run(request.run, self.actor)
            case_id = run["case_id"]
        def cancel():
            if request.job_id:
                row = store.one("SELECT * FROM web_jobs WHERE id=?", (request.job_id,))
                if row["status"] not in (*ACTIVE, "interrupted", "cancelled"):
                    raise BridgeError("conflict", "Only an active or interrupted job can be cancelled")
                if row["comparison_id"]:
                    running = store.db.execute("SELECT r.id FROM runs r JOIN comparison_jobs j ON j.investigation=r.investigation WHERE j.comparison_id=? AND r.status='running'", (row["comparison_id"],)).fetchall()
                else:
                    running = store.db.execute("SELECT id FROM runs WHERE investigation=? AND status='running'", (row["investigation"],)).fetchall()
                for r in running:
                    store.cancel(r["id"], self.actor, request.reason)
                store.db.execute("UPDATE web_jobs SET status='cancelled',error=?,updated_at=? WHERE id=?",
                                 (canonical({"code": "cancelled", "message": request.reason}), now(), request.job_id))
                if row["comparison_id"]:
                    self._pause_comparison(store, row["comparison_id"])
                return {"job_id": request.job_id, "status": "cancelled", "reservations": "retained"}
            run = store.run(request.run, self.actor)
            if run["status"] == "cancelled: " + request.reason:
                return {"run": request.run, "status": "cancelled", "reservations": "retained"}
            result = store.cancel(request.run, self.actor, request.reason)
            store.db.execute("UPDATE web_jobs SET status='cancelled',error=?,updated_at=? WHERE investigation=? AND status IN ('queued','running')",
                             (canonical({"code": "cancelled", "message": request.reason}), now(), run["investigation"]))
            return result
        return self._operation(store, "/api/cancel", request, case_id, cancel)

    def _export(self, store, request):
        with store.tx():
            self._writable(store, request.case)
            dossier = store.dossier(request.case, self.actor, request.revision)
            store.proposal(dossier["proposal_id"], self.actor)
            content = canonical(dossier["payload"]) + "\n" if request.format == "json" else markdown(dossier["payload"])
            content_hash = text_hash(content)
            download_id = digest([self.actor, request.case, dossier["revision"], request.format, content_hash])
            store.db.execute("INSERT OR IGNORE INTO web_downloads VALUES(?,?,?,?,?,?)", (download_id, self.actor, request.case, dossier["revision"], request.format, content_hash))
            store.audit("export.created",self.actor,case=request.case,revision=dossier["payload"]["input_revision"],outcome="complete",details={"content_hash":content_hash})
            return {"download_url": "/api/download/" + download_id, "hash": content_hash, "dossier_hash": dossier["hash"],
                    "case": request.case, "dossier_revision": dossier["revision"], "format": request.format, "raw_sources_included": False}

    def download(self,path):
        case=None
        with self.store() as store:
            if re.fullmatch(r"/api/download/[a-f0-9]{64}",path):
                saved=store.one("SELECT case_id,actor FROM web_downloads WHERE id=?",(path.rsplit("/",1)[1],))
                if saved and saved["actor"]==self.actor:case=saved["case_id"]
        self.operational_event("download.attempt",case=case,details={"request_hash":digest(path)})
        try:return self._download(path)
        except BridgeError as error:
            self.operational_event("download.denied",case=case,outcome="denied",reason=error.code,details={"request_hash":digest(path)})
            raise

    def _download(self, path):
        match = re.fullmatch(r"/api/download/([a-f0-9]{64})", path)
        if not match:
            raise BridgeError("not_found", "Download not found")
        with self.store() as store, store.tx():
            saved = store.one("SELECT * FROM web_downloads WHERE id=?", (match[1],))
            if not saved:
                raise BridgeError("not_found", "Download not found")
            if saved["actor"] != self.actor:
                raise BridgeError("access", "Download access denied")
            store.review_writable(saved["case_id"], self.actor)
            dossier = store.dossier(saved["case_id"], self.actor, saved["dossier_revision"])
            store.proposal(dossier["proposal_id"], self.actor)
            content = canonical(dossier["payload"]) + "\n" if saved["format"] == "json" else markdown(dossier["payload"])
            if text_hash(content) != saved["hash"]:
                raise BridgeError("integrity", "Download changed; generate a new export")
            suffix = "json" if saved["format"] == "json" else "md"
            store.audit("download.prepared",self.actor,case=saved["case_id"],revision=dossier["payload"]["input_revision"],outcome="prepared",details={"content_hash":saved["hash"]})
            return content.encode(), "application/json; charset=utf-8" if suffix == "json" else "text/markdown; charset=utf-8", f"{saved['case_id']}-v{saved['dossier_revision']}.{suffix}"


class LocalServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, port, service):
        if type(port) is not int or not 0 <= port <= 65535:
            raise BridgeError("invalid", "Port must be an integer from 0 to 65535")
        self.service = service
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_address[1]}"
        self.allowed_host = f"127.0.0.1:{self.server_address[1]}"

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server_version = "ApprovalBridge"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        # Request paths, imported evidence and identifiers stay out of terminal logs.
        pass

    def _headers(self, name):
        return self.headers.get_all(name, [])

    def _guard(self, mutation=False):
        if self.client_address[0] != "127.0.0.1" or self._headers("Host") != [self.server.allowed_host]:
            raise BridgeError("origin", "Use the exact 127.0.0.1 launch URL")
        origins = self._headers("Origin")
        if origins and origins != [self.server.origin]:
            raise BridgeError("origin", "Cross-origin requests are denied")
        sites = self._headers("Sec-Fetch-Site")
        if sites and (len(sites) != 1 or sites[0] not in ("same-origin", "none")):
            raise BridgeError("origin", "Cross-site requests are denied")
        if mutation:
            if origins != [self.server.origin]:
                raise BridgeError("origin", "Same-origin JSON mutations require an Origin header")
            tokens = self._headers("X-CSRF-Token")
            if len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode("utf-8"), self.server.service.csrf.encode("ascii")):
                raise BridgeError("csrf", "Missing or expired local session token; refresh the page")
            types = self._headers("Content-Type")
            if len(types) != 1 or types[0].lower() not in ("application/json", "application/json; charset=utf-8"):
                raise BridgeError("media_type", "Use application/json")
        if self._headers("Transfer-Encoding") or self._headers("Expect") or self._headers("Content-Encoding"):
            raise BridgeError("invalid", "Encoded, chunked and expectation requests are unsupported")

    def _path(self):
        parts = urlsplit(self.path)
        if parts.scheme or parts.netloc or parts.query or parts.fragment or "%" in parts.path or "\\" in parts.path or any(part in (".", "..") for part in parts.path.split("/")):
            raise BridgeError("not_found", "Route not found")
        return parts.path

    def _send(self, status, body, content_type="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, error):
        self.server.service.operational_event("http.denied",outcome="denied",reason=error.code,details={"request_hash":digest(self.path)})
        statuses = {"not_found": 404, "access": 403, "origin": 403, "csrf": 403, "too_large": 413, "media_type": 415,
                    "busy": 409, "stale": 409, "conflict": 409, "recovery": 409, "lease": 409, "incomplete": 409, "comparison_input": 409,
                    "internal": 500}
        self._send(statuses.get(error.code, 400), canonical({"error": {"code": error.code, "message": error.message}}).encode())

    def _handle(self, mutation=False):
        try:
            self._guard(mutation)
            path = self._path()
            if mutation:
                lengths = self._headers("Content-Length")
                if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0]):
                    raise BridgeError("invalid", "One bounded Content-Length is required")
                length = int(lengths[0])
                if length > MAX_BODY:
                    raise BridgeError("too_large", "JSON request exceeds 1,228,800 bytes")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise BridgeError("invalid", "Incomplete JSON request")
                result = self.server.service.post(path, parse_json(raw))
                self._send(202 if path == "/api/jobs" else 200, canonical(result).encode())
            elif path in STATIC:
                name, kind = STATIC[path]
                target = Path(__file__).parent / "static" / name
                if target.parent.is_symlink() or target.is_symlink() or not target.is_file():
                    raise BridgeError("not_found", "Static asset unavailable")
                self._send(200, target.read_bytes(), kind)
            elif path.startswith("/api/download/"):
                content, kind, filename = self.server.service.download(path)
                self._send(200, content, kind, {"Content-Disposition": f'attachment; filename="{filename}"'})
                self.server.service.operational_event("download.served",outcome="served",details={"content_hash":text_hash(content.decode()),"bytes":len(content)})
            else:
                self._send(200, canonical(self.server.service.get(path)).encode())
        except BridgeError as error:
            self._error(error)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except (ValueError, RecursionError):
            self._error(BridgeError("invalid", "Invalid request"))
        except Exception:
            self._error(BridgeError("internal", "Local request failed; refresh and inspect recorded state before retrying"))

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle(True)

    def do_HEAD(self):
        self._handle()

    def do_OPTIONS(self):
        self._error(BridgeError("origin", "Cross-origin preflight is unsupported"))

    def handle_expect_100(self):
        self._error(BridgeError("invalid", "Expectation requests are unsupported"))
        return False


def serve(db, actor="analyst", port=8765, runtime=None):
    service = WebService(db, actor,runtime=runtime)
    try:
        server = LocalServer(port, service)
        try:
            print(f"ApprovalBridge local web app: {server.origin}", flush=True)
            print("Offline by default. Mail/live routes require operator configuration. Stop with Ctrl+C.", flush=True)
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        service.close()
