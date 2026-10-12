"""SQLite persistence. Short transactions never surround network calls."""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from .contracts import Bundle
from .errors import BridgeError
from .util import canonical, digest, now

def ident(prefix):
    return prefix+"-"+uuid.uuid4().hex[:16]

class Store:
    def __init__(self,path):
        self.path=Path(path)
        if self.path.is_symlink(): raise BridgeError("path","Database cannot be a symlink")
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=10,isolation_level=None)
        os.chmod(self.path,0o600)
        self.db.row_factory=sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version=self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0,1,2,3): raise BridgeError("schema","Unsupported database version")
        self.db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY,owner TEXT NOT NULL,revision INTEGER NOT NULL,epoch INTEGER NOT NULL DEFAULT 1,revoked INTEGER NOT NULL DEFAULT 0,fence INTEGER NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS snapshots(case_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(case_id,revision),FOREIGN KEY(case_id) REFERENCES cases(id));
        CREATE TABLE IF NOT EXISTS receipts(key TEXT PRIMARY KEY,case_id TEXT NOT NULL,request_hash TEXT NOT NULL,result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS investigations(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,limits TEXT NOT NULL,tools INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,inquiries INTEGER NOT NULL DEFAULT 0,active_ms INTEGER NOT NULL DEFAULT 0,reserved_micro INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,investigation TEXT NOT NULL,case_id TEXT NOT NULL,revision INTEGER NOT NULL,epoch INTEGER NOT NULL,fence INTEGER NOT NULL,variant TEXT NOT NULL,mode TEXT NOT NULL,status TEXT NOT NULL,checkpoint TEXT NOT NULL,started TEXT NOT NULL,last_tick REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS steps(run_id TEXT NOT NULL,seq INTEGER NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,payload TEXT,PRIMARY KEY(run_id,seq));
        CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,case_id TEXT NOT NULL,revision INTEGER NOT NULL,epoch INTEGER NOT NULL,payload TEXT NOT NULL,hash TEXT NOT NULL,valid INTEGER NOT NULL,status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS dossiers(case_id TEXT NOT NULL,revision INTEGER NOT NULL,proposal_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,hash TEXT NOT NULL,status TEXT NOT NULL,PRIMARY KEY(case_id,revision));
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,trace_id TEXT);
        CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,dossier_revision INTEGER NOT NULL,path TEXT NOT NULL,hash TEXT NOT NULL,status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS grants(id TEXT PRIMARY KEY,config_hash TEXT NOT NULL,limit_micro INTEGER NOT NULL,reserved_micro INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS snapshot_metadata(case_id TEXT NOT NULL,revision INTEGER NOT NULL,recorded_at TEXT NOT NULL,provenance TEXT NOT NULL,PRIMARY KEY(case_id,revision));
        CREATE TABLE IF NOT EXISTS run_metadata(run_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS simulation_sessions(investigation TEXT PRIMARY KEY,case_id TEXT NOT NULL,map_hash TEXT NOT NULL,rounds INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS simulation_rounds(id TEXT PRIMARY KEY,investigation TEXT NOT NULL,case_id TEXT NOT NULL,request_hash TEXT NOT NULL,input_revision INTEGER NOT NULL,output_revision INTEGER NOT NULL,payload TEXT NOT NULL,recorded_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS comparisons(id TEXT PRIMARY KEY,actor TEXT NOT NULL,plan_hash TEXT NOT NULL,plan TEXT NOT NULL,status TEXT NOT NULL,model_attempts INTEGER NOT NULL DEFAULT 0,reserved_micro INTEGER NOT NULL DEFAULT 0,max_model_attempts INTEGER NOT NULL,max_reserved_micro INTEGER NOT NULL,report TEXT);
        CREATE TABLE IF NOT EXISTS comparison_jobs(id TEXT PRIMARY KEY,comparison_id TEXT NOT NULL,case_id TEXT NOT NULL,investigation TEXT UNIQUE,scenario TEXT NOT NULL,variant TEXT NOT NULL,repetition INTEGER NOT NULL,position INTEGER NOT NULL,status TEXT NOT NULL,result TEXT,FOREIGN KEY(comparison_id) REFERENCES comparisons(id));
        CREATE TABLE IF NOT EXISTS trace_receipts(event_id TEXT PRIMARY KEY,project TEXT NOT NULL,region TEXT NOT NULL,acknowledged_at TEXT NOT NULL,transport TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT,recorded_at TEXT NOT NULL,event TEXT NOT NULL,actor_ref TEXT NOT NULL,case_id TEXT,revision INTEGER,run_id TEXT,tool TEXT,outcome TEXT NOT NULL,reason TEXT,details TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'Audit events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'Audit events are append-only'); END;
        CREATE TABLE IF NOT EXISTS mail_connections(id TEXT PRIMARY KEY,account_ref TEXT NOT NULL,actor_ref TEXT NOT NULL,epoch INTEGER NOT NULL DEFAULT 1,revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS mail_previews(id TEXT PRIMARY KEY,actor_ref TEXT NOT NULL,case_id TEXT NOT NULL,revision INTEGER NOT NULL,connection TEXT NOT NULL,epoch INTEGER NOT NULL,payload TEXT NOT NULL,expires_at REAL NOT NULL);
        PRAGMA user_version=3;
        COMMIT;
        """)
    def audit(self,event,actor="operator",**kwargs):
        from .audit import record
        record(self,event,actor,**kwargs)
    def close(self): self.db.close()
    @contextmanager
    def tx(self):
        nested=self.db.in_transaction
        marker="sp_"+uuid.uuid4().hex
        self.db.execute("SAVEPOINT "+marker if nested else "BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("RELEASE SAVEPOINT "+marker if nested else "COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK TO SAVEPOINT "+marker if nested else "ROLLBACK")
            if nested: self.db.execute("RELEASE SAVEPOINT "+marker)
            raise
    def one(self,sql,args=()):
        row=self.db.execute(sql,args).fetchone()
        return dict(row) if row else None
    def case(self,case_id,actor):
        row=self.one("SELECT * FROM cases WHERE id=?",(case_id,))
        if not row: raise BridgeError("not_found","Case not found")
        if row["owner"]!=actor or row["revoked"]: raise BridgeError("access","Case access denied")
        return row
    def receipt(self,key,actor):
        row=self.one("SELECT * FROM receipts WHERE key=?",(key,))
        if not row: raise BridgeError("not_found","Operation receipt not found")
        self.case(row["case_id"],actor)
        return json.loads(row["result"])
    def review_writable(self,case_id,actor):
        if self.one("SELECT id FROM cases WHERE id=?",(case_id,)): self.case(case_id,actor)
        if self.one("SELECT id FROM comparison_jobs WHERE case_id=?",(case_id,)):
            raise BridgeError("comparison_input","Frozen comparison cases are read-only; import a new case to explore changes")
    def _replay(self,key,case_id,request):
        row=self.one("SELECT * FROM receipts WHERE key=?",(key,))
        if not row: return None
        if row["case_id"]!=case_id or row["request_hash"]!=digest(request):
            raise BridgeError("conflict","Operation key already used with a different payload")
        return json.loads(row["result"])
    def _receipt(self,key,case_id,request,result):
        if not key or len(key)>200: raise BridgeError("invalid","Bounded nonempty operation key required")
        self.db.execute("INSERT INTO receipts VALUES(?,?,?,?)",(key,case_id,digest(request),canonical(result)))
    def bundle(self,case_id,actor,revision=None):
        c=self.case(case_id,actor)
        row=self.one("SELECT * FROM snapshots WHERE case_id=? AND revision=?",(case_id,revision or c["revision"]))
        if not row: raise BridgeError("not_found","Snapshot not found")
        if digest(json.loads(row["payload"]))!=row["hash"]: raise BridgeError("integrity","Snapshot hash mismatch")
        return Bundle.model_validate(json.loads(row["payload"]))
    def import_bundle(self,bundle,actor,key):
        payload=bundle.model_dump(); request={"action":"import","bundle":payload}
        with self.tx():
            c=self.one("SELECT * FROM cases WHERE id=?",(bundle.id,))
            if c: self.case(bundle.id,actor)
            old_receipt=self.one("SELECT * FROM receipts WHERE key=?",(key,))
            if old_receipt and old_receipt["case_id"]==bundle.id and old_receipt["request_hash"]!=digest(request):
                recorded=json.loads(old_receipt["result"])
                if "input_revision" in recorded and "hash" in recorded:
                    old_snapshot=self.bundle(bundle.id,actor,recorded["input_revision"])
                    if old_snapshot.model_dump()==payload: return recorded
            prior=self._replay(key,bundle.id,request)
            if prior: return prior
            if c:
                old=self.bundle(bundle.id,actor)
                if not set(old.withdrawn)<=set(bundle.withdrawn):
                    raise BridgeError("immutable","Withdrawn evidence must stay withdrawn; restoration requires a separate explicit contract")
                if {c.id for c in old.claims}!={c.id for c in bundle.claims} or old.currency!=bundle.currency:
                    raise BridgeError("immutable","Claim inventory and currency are immutable; use a new case for new scope")
                previous={c.id:c for c in old.claims}; corrections={x.claim:x for x in bundle.claim_corrections}
                for claim in bundle.claims:
                    if claim==previous[claim.id]: continue
                    correction=corrections.get(claim.id)
                    source=next((d for d in bundle.evidence if correction and d.id==correction.source_id),None)
                    if not correction or correction.previous_hash!=digest(previous[claim.id].model_dump()) or not source or not source.available or source.restricted or source.id in bundle.withdrawn:
                        raise BridgeError("immutable","Claim changes require explicit current predecessor hash, reason and available correction source")
                newdocs={d.id:d for d in bundle.evidence}
                for d in old.evidence:
                    if d.id not in newdocs or d!=newdocs[d.id]:
                        raise BridgeError("immutable","Evidence is immutable; append a correction and explicitly withdraw if needed")
                oldevents={s.id:s for s in old.settlements}; newevents={s.id:s for s in bundle.settlements}
                if any(newevents.get(k)!=v for k,v in oldevents.items()):
                    raise BridgeError("immutable","Settlement events are immutable; append reversals")
                revision=c["revision"]+1
                self.db.execute("UPDATE cases SET revision=?,fence=fence+1,lease_until=0 WHERE id=?",(revision,bundle.id))
                self.db.execute("UPDATE proposals SET status='stale' WHERE case_id=? AND status='review'",(bundle.id,))
                self.db.execute("UPDATE dossiers SET status='superseded' WHERE case_id=?",(bundle.id,))
                self.db.execute("UPDATE runs SET status='stale' WHERE case_id=? AND status='running'",(bundle.id,))
            else:
                revision=1
                self.db.execute("INSERT INTO cases(id,owner,revision) VALUES(?,?,?)",(bundle.id,actor,revision))
            self.db.execute("INSERT INTO snapshots VALUES(?,?,?,?)",(bundle.id,revision,canonical(payload),digest(payload)))
            provenance={d.id:{"raw_sha256":__import__("hashlib").sha256(d.text.encode()).hexdigest(),"canonical_sha256":__import__("hashlib").sha256(d.text.replace("\r\n","\n").replace("\r","\n").encode()).hexdigest(),"canonicalization":"newline-v1","origin":d.origin} for d in bundle.evidence}
            self.db.execute("INSERT INTO snapshot_metadata VALUES(?,?,?,?)",(bundle.id,revision,now(),canonical(provenance)))
            result={"case":bundle.id,"input_revision":revision,"hash":digest(payload)}
            self._receipt(key,bundle.id,request,result)
            self.audit("evidence.import",actor,case=bundle.id,revision=revision,details={"request_hash":digest(request),"result_hash":digest(result)})
            return result
    def revoke(self,case_id,actor):
        with self.tx():
            self.case(case_id,actor)
            self.db.execute("UPDATE cases SET revoked=1,epoch=epoch+1,fence=fence+1,lease_until=0 WHERE id=?",(case_id,))
            self.db.execute("UPDATE runs SET status='revoked' WHERE case_id=? AND status='running'",(case_id,))
            self.db.execute("UPDATE proposals SET status='stale' WHERE case_id=?",(case_id,))
            self.db.execute("UPDATE dossiers SET status='superseded' WHERE case_id=?",(case_id,))
            self.audit("case.revoke",actor,case=case_id,outcome="revoked")
    def investigation_history_access(self,iid,actor):
        inv=self.one("SELECT case_id FROM investigations WHERE id=?",(iid,))
        if not inv:raise BridgeError("not_found","Investigation not found")
        current=self.bundle(inv["case_id"],actor)
        allowed={d.id for d in current.evidence if d.available and not d.restricted and d.id not in current.withdrawn}
        revisions={r[0] for r in self.db.execute("SELECT revision FROM runs WHERE investigation=? UNION SELECT input_revision FROM simulation_rounds WHERE investigation=?",(iid,iid))}
        for revision in revisions:
            earlier=self.bundle(inv["case_id"],actor,revision)
            visible={d.id for d in earlier.evidence if d.available and not d.restricted and d.id not in earlier.withdrawn}
            if not visible<=allowed:
                raise BridgeError("access","Historical inquiry context may contain currently unavailable evidence; start a new investigation without that context")

    def new_investigation(self,case_id,actor,tool_limit=20,attempt_limit=8,inquiry_limit=2):
        if not 1<=tool_limit<=20 or not 1<=attempt_limit<=8 or not 0<=inquiry_limit<=2:
            raise BridgeError("invalid","Investigation limits exceed application ceilings")
        with self.tx():
            self.case(case_id,actor); iid=ident("inv")
            limits={"tools":tool_limit,"attempts":attempt_limit,"inquiries":inquiry_limit,"active_ms":600000}
            self.db.execute("INSERT INTO investigations(id,case_id,limits) VALUES(?,?,?)",(iid,case_id,canonical(limits)))
            return iid
    def start_run(self,inv,actor,variant,mode):
        if variant not in ("A","B","C") or mode not in ("offline","instrumented","live"):
            raise BridgeError("invalid","Unknown variant or mode")
        if variant=="A" and mode=="live": raise BridgeError("invalid","Rules baseline has no model route")
        with self.tx():
            investigation=self.one("SELECT * FROM investigations WHERE id=?",(inv,))
            if not investigation: raise BridgeError("not_found","Investigation not found")
            c=self.case(investigation["case_id"],actor); tick=time.time()
            if c["lease_until"]>tick: raise BridgeError("busy","Case lease is held by another run")
            if self.one("SELECT id FROM runs WHERE case_id=? AND status='running'",(c["id"],)):
                raise BridgeError("recovery","Expired run requires explicit recover or abandon")
            fence=c["fence"]+1; rid=ident("run")
            self.db.execute("UPDATE cases SET fence=?,lease_until=? WHERE id=?",(fence,tick+120,c["id"]))
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid,inv,c["id"],c["revision"],c["epoch"],fence,variant,mode,"running",canonical({"messages":[],"observations":[],"read_ids":[],"inquiries":[]}),now(),tick))
            return self.run(rid,actor)
    def run(self,rid,actor):
        r=self.one("SELECT * FROM runs WHERE id=?",(rid,))
        if not r: raise BridgeError("not_found","Run not found")
        self.case(r["case_id"],actor)
        r["checkpoint"]=json.loads(r["checkpoint"])
        return r
    def _fresh(self,rid,actor,fence):
        r=self.run(rid,actor); c=self.case(r["case_id"],actor)
        if r["revision"]!=c["revision"] or r["epoch"]!=c["epoch"]:
            raise BridgeError("stale","Run snapshot is stale")
        if r["fence"]!=fence or c["fence"]!=fence or c["lease_until"]<=time.time() or r["status"]!="running":
            raise BridgeError("lease","Run lease is expired or fenced")
        return r
    def check(self,rid,actor,fence):
        return self._fresh(rid,actor,fence)
    def recover(self,rid,actor):
        with self.tx():
            r=self.run(rid,actor); c=self.case(r["case_id"],actor)
            if c["lease_until"]>time.time(): raise BridgeError("busy","Wait for the worker lease to expire")
            if r["status"]!="running" or (r["revision"],r["epoch"])!=(c["revision"],c["epoch"]):
                raise BridgeError("stale","Only an unchanged interrupted run can be recovered")
            if self.one("SELECT seq FROM steps WHERE run_id=? AND status='reserved'",(rid,)):
                self.db.execute("UPDATE steps SET status='uncertain' WHERE run_id=? AND status='reserved'",(rid,))
            fence=c["fence"]+1
            self.db.execute("UPDATE cases SET fence=?,lease_until=? WHERE id=?",(fence,time.time()+120,c["id"]))
            # A lost worker's unobserved lease time remains conservatively charged.
            charge=min(120000,max(0,int((time.time()-r["last_tick"])*1000)))
            self.db.execute("UPDATE investigations SET active_ms=active_ms+? WHERE id=?",(charge,r["investigation"]))
            self.db.execute("UPDATE runs SET fence=?,last_tick=? WHERE id=?",(fence,time.time(),rid))
            self.audit("run.recover",actor,case=c["id"],revision=c["revision"],run=rid,outcome="recovered",details={"elapsed_ms":charge})
            return self.run(rid,actor)
    def cancel(self,rid,actor,reason):
        if not reason or len(reason)>1000: raise BridgeError("invalid","Cancellation reason required")
        with self.tx():
            r=self.run(rid,actor); c=self.case(r["case_id"],actor)
            if r["status"]!="running": raise BridgeError("conflict","Only an active or interrupted run can be cancelled")
            if r["fence"]!=c["fence"]: raise BridgeError("lease","Run no longer owns the case fence")
            charge=min(120000,max(0,int((time.time()-r["last_tick"])*1000)))
            self.db.execute("UPDATE investigations SET active_ms=active_ms+? WHERE id=?",(charge,r["investigation"]))
            self.db.execute("UPDATE cases SET fence=fence+1,lease_until=0 WHERE id=?",(r["case_id"],))
            self.db.execute("UPDATE runs SET status=? WHERE id=?",("cancelled: "+reason,rid))
            self.db.execute("UPDATE steps SET status='uncertain' WHERE run_id=? AND status='reserved'",(rid,))
            self._queue(r["case_id"],{"name":"cancelled investigation","kind":"WORKFLOW","status":"ERROR","metadata":{"run_id":rid,"synthetic":self.bundle(r["case_id"],actor).synthetic,"outcome":"cancelled","inflight_billing":"reservation_retained"}})
            self.audit("run.cancel",actor,case=r["case_id"],revision=r["revision"],run=rid,outcome="cancelled",details={"request_hash":digest(reason)})
            return {"run":rid,"status":"cancelled","reservations":"retained"}
    def reserve_step(self,rid,actor,fence,kind,grant=None):
        if kind not in ("tool","attempt","inquiry"): raise BridgeError("invalid","Unknown reservation kind")
        with self.tx():
            r=self._fresh(rid,actor,fence)
            inv=self.one("SELECT * FROM investigations WHERE id=?",(r["investigation"],))
            limits=json.loads(inv["limits"]); field={"tool":"tools","attempt":"attempts","inquiry":"inquiries"}[kind]
            elapsed=max(0,int((time.time()-r["last_tick"])*1000))
            if inv[field]>=limits[field] or inv["active_ms"]+elapsed>=limits["active_ms"]:
                raise BridgeError("budget","Investigation budget exhausted")
            job=self.one("SELECT c.* FROM comparisons c JOIN comparison_jobs j ON j.comparison_id=c.id WHERE j.investigation=?",(inv["id"],))
            if kind=="attempt" and job:
                reserve=grant[3] if grant else 0
                if job["model_attempts"]>=job["max_model_attempts"] or job["reserved_micro"]+reserve>job["max_reserved_micro"]:
                    raise BridgeError("comparison_budget","Comparison-wide model or credit reserve exhausted")
                self.db.execute("UPDATE comparisons SET model_attempts=model_attempts+1,reserved_micro=reserved_micro+? WHERE id=?",(reserve,job["id"]))
            if grant:
                config_hash,grant_id,limit,cost=grant
                self.db.execute("INSERT OR IGNORE INTO grants VALUES(?,?,?,0)",(grant_id,config_hash,limit))
                g=self.one("SELECT * FROM grants WHERE id=?",(grant_id,))
                if (g["config_hash"],g["limit_micro"])!=(config_hash,limit):
                    raise BridgeError("grant","Grant configuration changed")
                if cost<=0 or g["reserved_micro"]+cost>limit: raise BridgeError("budget","Organizer grant budget exhausted")
                self.db.execute("UPDATE grants SET reserved_micro=reserved_micro+? WHERE id=?",(cost,grant_id))
                self.db.execute("UPDATE investigations SET reserved_micro=reserved_micro+? WHERE id=?",(cost,inv["id"]))
            self.db.execute(f"UPDATE investigations SET {field}={field}+1,active_ms=active_ms+? WHERE id=?",(elapsed,inv["id"]))
            self.db.execute("UPDATE runs SET last_tick=? WHERE id=?",(time.time(),rid))
            self.db.execute("UPDATE cases SET lease_until=? WHERE id=?",(time.time()+120,r["case_id"]))
            seq=self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM steps WHERE run_id=?",(rid,)).fetchone()[0]
            self.db.execute("INSERT INTO steps VALUES(?,?,?,'reserved',NULL)",(rid,seq,kind))
            after=self.one("SELECT * FROM investigations WHERE id=?",(inv["id"],))
            self.audit("budget.reserve",actor,case=r["case_id"],revision=r["revision"],run=rid,outcome=kind,details={"before":inv,"after":after,"seq":seq})
            return seq
    def checkpoint(self,rid,actor,fence,seq,checkpoint,result):
        with self.tx():
            self._fresh(rid,actor,fence)
            changed=self.db.execute("UPDATE steps SET status='complete',payload=? WHERE run_id=? AND seq=? AND status='reserved'",
                                    (canonical(result),rid,seq)).rowcount
            if not changed: raise BridgeError("conflict","Step already completed or uncertain")
            self.db.execute("UPDATE runs SET checkpoint=? WHERE id=?",(canonical(checkpoint),rid))
    def attempt_outcome(self,rid,actor,fence,seq,outcome,payload):
        with self.tx():
            self._fresh(rid,actor,fence)
            self.db.execute("UPDATE steps SET status=?,payload=? WHERE run_id=? AND seq=? AND status='reserved'",(outcome,canonical(payload),rid,seq))

    def propose(self,rid,actor,fence,dossier):
        with self.tx():
            r=self._fresh(rid,actor,fence); pid=ident("proposal")
            metadata=self.one("SELECT payload FROM run_metadata WHERE run_id=?",(rid,))
            snapshot=self.one("SELECT recorded_at,provenance FROM snapshot_metadata WHERE case_id=? AND revision=?",(r["case_id"],r["revision"]))
            bundle=self.bundle(r["case_id"],actor)
            provenance="Synthetic data" if bundle.synthetic else "Mixed data" if any(not d.mail for d in bundle.evidence) else "Imported email"
            dossier=dict(dossier,data_provenance=provenance,run_id=rid,input_revision=r["revision"],access_epoch=r["epoch"],mode=r["mode"],variant=r["variant"],run_metadata=json.loads(metadata["payload"]) if metadata else {},recorded_at=snapshot["recorded_at"] if snapshot else "legacy_unknown")
            dossier["changes"]=[];dossier["changes_unavailable"]=False
            previous=self.one("SELECT proposal_id FROM dossiers WHERE case_id=? ORDER BY revision DESC LIMIT 1",(r["case_id"],))
            if previous:
                try:
                    before={x["claim"]:x for x in self.proposal(previous["proposal_id"],actor)["payload"]["rows"]}
                    for row in dossier["rows"]:
                        for field in ("face_minor","approval_minor","conditional_minor","settlement_minor","remaining_recorded_minor","holds","state","approval_sources"):
                            if before[row["claim"]][field]!=row[field]:
                                dossier["changes"].append({"claim":row["claim"],"field":field,"before":before[row["claim"]][field],"after":row[field]})
                except BridgeError as error:
                    if error.code!="access":raise
                    dossier["changes_unavailable"]=True
            payload=canonical(dossier); h=digest(dossier)
            self.db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?)",
                            (pid,rid,r["case_id"],r["revision"],r["epoch"],payload,h,int(dossier["valid"]),"review"))
            return {"proposal":pid,"hash":h,"valid":dossier["valid"]}
    def proposal(self,pid,actor):
        p=self.one("SELECT * FROM proposals WHERE id=?",(pid,))
        if not p: raise BridgeError("not_found","Proposal not found")
        self.case(p["case_id"],actor); p["payload"]=json.loads(p["payload"])
        current=self.bundle(p["case_id"],actor)
        allowed={d.id for d in current.evidence if d.available and not d.restricted and d.id not in current.withdrawn}
        referenced={x["source"] for x in p["payload"].get("support",[])}
        referenced.update(x["source_id"] for x in p["payload"].get("settlement_history",[]))
        referenced.update(x["id"] for x in p["payload"].get("evidence_manifest",[]))
        if not referenced<=allowed:
            raise BridgeError("access","Historical proposal references currently unavailable evidence; only metadata history is accessible")
        return p
    def accept(self,pid,expected_hash,actor,key):
        request={"action":"accept","proposal":pid,"hash":expected_hash}
        with self.tx():
            p=self.proposal(pid,actor); c=self.case(p["case_id"],actor)
            self.review_writable(p["case_id"],actor)
            replay=self._replay(key,p["case_id"],request)
            if replay: return replay
            if p["hash"]!=expected_hash or digest(p["payload"])!=expected_hash:
                raise BridgeError("conflict","Expected proposal hash does not match")
            existing=self.one("SELECT revision FROM dossiers WHERE proposal_id=?",(pid,))
            if existing: raise BridgeError("conflict","Proposal already accepted; recover its original receipt")
            if (p["revision"],p["epoch"])!=(c["revision"],c["epoch"]) or p["status"]!="review":
                raise BridgeError("stale","Proposal is stale or not reviewable")
            if not p["valid"]: raise BridgeError("invalid","Invalid proposal cannot be accepted")
            revision=self.db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM dossiers WHERE case_id=?",(p["case_id"],)).fetchone()[0]
            payload=dict(p["payload"],dossier_revision=revision,accepted_by=actor,accepted_at=now(),proposal_id=pid,proposal_hash=expected_hash)
            self.db.execute("UPDATE dossiers SET status='superseded' WHERE case_id=?",(p["case_id"],))
            self.db.execute("INSERT INTO dossiers VALUES(?,?,?,?,?,?)",(p["case_id"],revision,pid,canonical(payload),digest(payload),"current"))
            self.db.execute("UPDATE proposals SET status='accepted' WHERE id=?",(pid,))
            result={"case":p["case_id"],"dossier_revision":revision,"hash":digest(payload),"purpose":"internal_review_only"}
            self._receipt(key,p["case_id"],request,result)
            self._queue(p["case_id"],{"name":"acceptance","kind":"WORKFLOW","metadata":{"synthetic":self.bundle(p["case_id"],actor).synthetic,"operation_key_hash":digest(key),"dossier_revision":revision},"status":"OK"})
            self.audit("dossier.accept",actor,case=p["case_id"],revision=p["revision"],run=p["run_id"],outcome="accepted",details={"proposal_hash":expected_hash,"result_hash":digest(result)})
            return result
    def reject(self,pid,actor,reason):
        if not reason or len(reason)>1000: raise BridgeError("invalid","Rejection reason required")
        with self.tx():
            p=self.proposal(pid,actor)
            self.review_writable(p["case_id"],actor)
            if p["status"]!="review": raise BridgeError("conflict","Proposal not reviewable")
            self.db.execute("UPDATE proposals SET status=? WHERE id=?",("rejected: "+reason,pid))
            self.audit("proposal.reject",actor,case=p["case_id"],revision=p["revision"],run=p["run_id"],outcome="rejected",details={"request_hash":digest(reason),"proposal_hash":p["hash"]})
    def _queue(self,case_id,payload):
        event=ident("event")
        payload=dict(payload); payload.setdefault("metadata",{})["local_event_id"]=event
        self.db.execute("INSERT INTO outbox(id,case_id,payload,status) VALUES(?,?,?,'pending')",(event,case_id,canonical(payload)))
    def finish(self,rid,actor,fence,status):
        with self.tx():
            r=self._fresh(rid,actor,fence)
            elapsed=max(0,int((time.time()-r["last_tick"])*1000))
            self.db.execute("UPDATE investigations SET active_ms=active_ms+? WHERE id=?",(elapsed,r["investigation"]))
            self.db.execute("UPDATE runs SET status=? WHERE id=?",(status,rid))
            self.db.execute("UPDATE cases SET lease_until=0 WHERE id=?",(r["case_id"],))
            steps=[dict(x) for x in self.db.execute("SELECT seq,kind,status,payload FROM steps WHERE run_id=? ORDER BY seq",(rid,))]
            synthetic=self.bundle(r["case_id"],actor).synthetic
            trace={"name":"ApprovalBridge "+r["variant"],"kind":"WORKFLOW","start":r["started"],"end":now(),"status":"OK" if status=="review" else "ERROR",
                "input":{"case":r["case_id"],"variant":r["variant"],"mode":r["mode"],"revision":r["revision"]},"output":{"outcome":status},
                "attributes":{"neatlogs.session.id":r["investigation"]},
                "metadata":{"synthetic":synthetic,"mode":r["mode"],"run_id":rid,"outcome":status,"remote_inference":r["mode"]=="live"},
                "children":[self._span(x,synthetic) for x in steps]}
            if status!="review": trace["error"]=status
            self._queue(r["case_id"],trace)
    @staticmethod
    def _span(step,synthetic=False):
        data=json.loads(step["payload"]) if step["payload"] else {}
        span={"name":str(step["seq"])+" "+data.get("tool",step["kind"]),"kind":"LLM" if step["kind"]=="attempt" else "TOOL","status":"OK" if step["status"]=="complete" and not data.get("error") else "ERROR","metadata":{"outcome":step["status"],"result_hash":digest(data)}}
        if step["status"]!="complete" or data.get("error"): span["error"]=": ".join(str(x) for x in (data.get("error") or step["status"],data.get("failure")) if x)
        if data.get("started") and data.get("ended"): span["start"],span["end"]=data["started"],data["ended"]
        if type(data.get("latency_ms")) in (int,float): span["duration_ms"]=data["latency_ms"]
        if data.get("finish_reason"): span["attributes"]={"neatlogs.llm.finish_reason":str(data["finish_reason"])}
        # Raw content only for synthetic cases; imported mail stays hash-only.
        if synthetic:
            content=(data.get("request"),data.get("output")) if step["kind"]=="attempt" else (data.get("arguments"),data.get("result"))
            for field,value in zip(("input","output"),content):
                if value is not None: span[field]=value
        if data.get("tool"): span["tool_name"]=data["tool"]
        if data.get("provider_model"): span["model"]=data["provider_model"]
        usage=data.get("usage")
        if isinstance(usage,dict):
            mapped={k:usage[v] for k,v in {"prompt":"prompt_tokens","completion":"completion_tokens","total":"total_tokens"}.items() if type(usage.get(v)) is int and usage[v]>=0}
            if mapped: span["tokens"]=mapped
        return span
    def dossier(self,case_id,actor,revision=None):
        self.case(case_id,actor)
        row=self.one("SELECT * FROM dossiers WHERE case_id=? "+("AND revision=?" if revision else "ORDER BY revision DESC LIMIT 1"),
                     (case_id,revision) if revision else (case_id,))
        if not row: raise BridgeError("not_found","No accepted dossier")
        if row["status"]!="current": raise BridgeError("stale","Dossier superseded; inspect its proposal history instead")
        row["payload"]=json.loads(row["payload"])
        if digest(row["payload"])!=row["hash"]: raise BridgeError("integrity","Dossier hash mismatch")
        return row
    def backup(self,destination,actor="analyst"):
        for row in self.db.execute("SELECT id FROM cases"):
            self.case(row["id"],actor)
        path=Path(destination)
        if path.exists(): raise BridgeError("conflict","Backup destination exists")
        target=sqlite3.connect(path)
        try:
            self.db.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0]!="ok": raise BridgeError("integrity","Backup integrity failed")
        finally: target.close()
        os.chmod(path,0o600)
        check=Store(path)
        try:
            for row in check.db.execute("SELECT payload,hash FROM snapshots UNION ALL SELECT payload,hash FROM dossiers"):
                if digest(json.loads(row["payload"]))!=row["hash"]: raise BridgeError("integrity","Restored payload hash failed")
            for row in check.db.execute("SELECT result FROM receipts"): json.loads(row["result"])
        finally: check.close()
        return {"backup":str(path),"restore_verified":True}
