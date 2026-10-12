"""Single-operator CLI; stdout is machine-readable JSON."""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from pydantic import ValidationError
from .errors import BridgeError
from .evaluation import evaluate
from .fixtures import demo
from .ingestion import load_bundle,visible
from .provider import Grant,ChatProvider
from .render import export,review_markdown,comparison_markdown,comparison_export
from .runner import run_investigation
from .store import Store
from .telemetry import flush,ensure_ready
from .comparison import compare,load_protocol,get_report
from .simulation import load_reply_map,apply_replies,history as reply_history
from .workflow import simulate,pending_requests

def parser():
    p=argparse.ArgumentParser(prog="approvalbridge",description="Synthetic local approval-evidence reconciliation; no external business actions")
    p.add_argument("--db",default=".local/approvalbridge.db")
    p.add_argument("--actor",default="analyst")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("doctor")
    q=sub.add_parser("serve",help="Open the local offline browser app");q.add_argument("--port",type=int,default=8765);q.add_argument("--runtime",help="Private operator-owned runtime configuration; optional")
    d=sub.add_parser("demo"); d.add_argument("--stage",type=int,choices=(1,2,3),default=1); d.add_argument("--case",default="demo")
    imp=sub.add_parser("import"); imp.add_argument("bundle"); imp.add_argument("--key",required=True)
    for name in ("inspect","new-investigation","history","revoke","traces"):
        q=sub.add_parser(name); q.add_argument("case")
    r=sub.add_parser("run"); r.add_argument("investigation"); r.add_argument("--variant",choices=("A","B","C"),default="C")
    r.add_argument("--mode",choices=("offline","instrumented","live"),default="offline")
    r.add_argument("--grant"); r.add_argument("--allow-organizer-inference",action="store_true")
    rr=sub.add_parser("recover"); rr.add_argument("run"); rr.add_argument("--grant"); rr.add_argument("--allow-organizer-inference",action="store_true")
    q=sub.add_parser("cancel"); q.add_argument("run"); q.add_argument("--reason",required=True)
    q=sub.add_parser("review"); q.add_argument("proposal");q.add_argument("--format",choices=("json","markdown"),default="json")
    q=sub.add_parser("accept"); q.add_argument("proposal"); q.add_argument("--hash",required=True); q.add_argument("--key",required=True)
    q=sub.add_parser("reject"); q.add_argument("proposal"); q.add_argument("--reason",required=True)
    q=sub.add_parser("receipt"); q.add_argument("key")
    q=sub.add_parser("export"); q.add_argument("case"); q.add_argument("--out",default=".local/exports"); q.add_argument("--revision",type=int)
    q=sub.add_parser("backup"); q.add_argument("destination")
    q=sub.add_parser("evaluate"); q.add_argument("--fixtures",default="fixtures")
    q=sub.add_parser("trace-recover"); q.add_argument("case"); q.add_argument("event"); q.add_argument("--confirm-worker-stopped",action="store_true")
    q=sub.add_parser("trace-flush"); q.add_argument("case"); q.add_argument("--region",choices=("us","eu"),required=True); q.add_argument("--project")
    q.add_argument("--allow-neatlogs",action="store_true"); q.add_argument("--retry-uncertain",action="store_true")
    for name in ("simulate","compare"):
        q=sub.add_parser(name)
        q.add_argument("investigation" if name=="simulate" else "protocol")
        if name=="simulate":q.add_argument("--replies",required=True);q.add_argument("--variant",choices=("A","B","C"),default="C")
        else:q.add_argument("--id",required=True);q.add_argument("--out")
        q.add_argument("--mode",choices=("offline","instrumented","live"),default="offline")
        q.add_argument("--grant");q.add_argument("--allow-organizer-inference",action="store_true")
        q.add_argument("--region",choices=("us","eu"));q.add_argument("--project");q.add_argument("--allow-neatlogs",action="store_true")
        q.add_argument("--retry-uncertain-traces",action="store_true")
        q.add_argument("--recover-stopped-worker",action="store_true",help="Confirm the original worker has stopped before recovering interrupted state")
    q=sub.add_parser("inquiries");q.add_argument("run")
    q=sub.add_parser("reply");q.add_argument("run");q.add_argument("--replies",required=True);q.add_argument("--key",required=True)
    q=sub.add_parser("comparison");q.add_argument("id");q.add_argument("--format",choices=("json","markdown"),default="json");q.add_argument("--out")
    return p

def configured_provider(args,mode):
    if mode!="live":
        if getattr(args,"grant",None) or getattr(args,"allow_organizer_inference",False):
            raise BridgeError("route","Inference flags require explicit live mode")
        return None
    if not args.allow_organizer_inference or not args.grant:
        raise BridgeError("route","Live mode requires a verified organizer inference grant and explicit --allow-organizer-inference")
    grant=Grant.model_validate(json.loads(Path(args.grant).read_text()))
    provider=ChatProvider(grant); provider.validate()
    if not os.environ.get(grant.api_key_env): raise BridgeError("credential","App-specific inference credential is not configured")
    return provider

def configured_tracer(args,store,actor):
    enabled=getattr(args,"allow_neatlogs",False)
    if not enabled:
        if getattr(args,"mode",None)=="live":raise BridgeError("trace_config","Automatic live workflows require explicit --allow-neatlogs, region and project")
        if getattr(args,"region",None) or getattr(args,"project",None):raise BridgeError("trace_config","Explicit --allow-neatlogs required")
        return None
    key=os.environ.get("APPROVALBRIDGE_NEATLOGS_WRITE_KEY","")
    from .telemetry import check_config
    check_config(args.region,args.project,key)
    return lambda case:ensure_ready(store,case,actor,args.region,args.project,key,retry_uncertain=args.retry_uncertain_traces)

def execute(args):
    if args.command=="serve":
        from .web import serve
        from .runtime import Runtime
        serve(args.db,args.actor,args.port,Runtime.load(args.runtime))
        return {"server":"stopped"}
    if args.command=="doctor":
        return {"local_runtime":"ready","python":sys.version.split()[0],"sqlite":__import__("sqlite3").sqlite_version,
            "entire_cli":shutil.which("entire") is not None,"neatlogs_delivery":"requires real acknowledged trace",
            "runtime_inference":"unconfigured; organizer infrastructure IAM allowance is not model inference funding",
            "default_mode":"offline","paid_fallback":False,"cfo_ai":"Separate hypothetical business planning in cfo.ai; no ApprovalBridge connection is needed"}
    if args.command=="evaluate": return evaluate(args.fixtures)
    s=Store(args.db)
    try:
        c=args.command; actor=args.actor
        if c=="demo":
            b=demo(args.stage,args.case)
            return s.import_bundle(b,actor,"demo-"+args.case+"-stage-"+str(args.stage))
        if c=="import": return s.import_bundle(load_bundle(args.bundle),actor,args.key)
        if c=="inspect":
            case=s.case(args.case,actor); b=s.bundle(args.case,actor)
            return {"case":case,"bundle":visible(b)}
        if c=="new-investigation": return {"investigation":s.new_investigation(args.case,actor),"limits":{"tools":20,"model_attempts":8,"inquiry_rounds":2,"active_minutes":10}}
        if c in ("simulate","compare"):
            provider=configured_provider(args,args.mode)
            trace=configured_tracer(args,s,actor)
            if c=="simulate":
                return simulate(s,args.investigation,actor,load_reply_map(args.replies),args.variant,args.mode,provider,trace,args.recover_stopped_worker)
            result=compare(s,args.id,actor,load_protocol(args.protocol),args.mode,provider,trace,args.recover_stopped_worker)
            if args.out:result=dict(result,export=comparison_export(s,args.id,actor,args.out))
            return result
        if c=="comparison":
            result=get_report(s,args.id,actor)
            if args.out:return comparison_export(s,args.id,actor,args.out)
            return comparison_markdown(result) if args.format=="markdown" else result
        if c=="inquiries":
            run=s.run(args.run,actor)
            return {"run":run["id"],"requests":pending_requests(s,args.run,actor),"reply_history":reply_history(s,run["investigation"],actor),"nothing_sent":True}
        if c=="reply":
            run=s.run(args.run,actor)
            return apply_replies(s,run["investigation"],actor,load_reply_map(args.replies),pending_requests(s,args.run,actor),args.key,run["revision"],args.run)
        if c=="run":
            provider=configured_provider(args,args.mode)
            r=s.start_run(args.investigation,actor,args.variant,args.mode)
            return run_investigation(s,r["id"],actor,provider)
        if c=="recover":
            previous=s.run(args.run,actor); provider=configured_provider(args,previous["mode"])
            r=s.recover(args.run,actor)
            return run_investigation(s,r["id"],actor,provider)
        if c=="cancel": return s.cancel(args.run,actor,args.reason)
        if c=="review":
            result=s.proposal(args.proposal,actor)
            previous=s.one("SELECT proposal_id FROM dossiers WHERE case_id=? ORDER BY revision DESC LIMIT 1",(result["case_id"],))
            result["previous_accepted_rows"]=None
            if previous:
                try: result["previous_accepted_rows"]=s.proposal(previous["proposal_id"],actor)["payload"]["rows"]
                except BridgeError as error:
                    if error.code!="access": raise
                    result["previous_comparison"]="unavailable under current source access"
            return review_markdown(result) if args.format=="markdown" else result
        if c=="accept": return s.accept(args.proposal,args.hash,actor,args.key)
        if c=="reject":
            s.reject(args.proposal,actor,args.reason); return {"rejected":args.proposal}
        if c=="receipt": return s.receipt(args.key,actor)
        if c=="export": return export(s,args.case,actor,args.out,args.revision)
        if c=="backup": return s.backup(args.destination,actor)
        if c=="revoke":
            s.revoke(args.case,actor); return {"case":args.case,"access":"revoked","downloaded_exports":"cannot_be_recalled"}
        if c=="history":
            s.case(args.case,actor)
            return {table:[dict(r) for r in s.db.execute(sql,(args.case,))] for table,sql in {
                "investigations":"SELECT id,limits,tools,attempts,inquiries,active_ms,reserved_micro FROM investigations WHERE case_id=?",
                "runs":"SELECT id,investigation,revision,epoch,variant,mode,status FROM runs WHERE case_id=?",
                "proposals":"SELECT id,revision,hash,status FROM proposals WHERE case_id=?",
                "dossiers":"SELECT revision,proposal_id,hash,status FROM dossiers WHERE case_id=?"}.items()}
        if c=="traces":
            s.case(args.case,actor)
            return [dict(r) for r in s.db.execute("SELECT id,payload,status,attempts,trace_id FROM outbox WHERE case_id=?",(args.case,))]
        if c=="trace-recover":
            if not args.confirm_worker_stopped: raise BridgeError("recovery","Stop the original delivery worker and explicitly confirm before recovery")
            with s.tx():
                s.case(args.case,actor)
                changed=s.db.execute("UPDATE outbox SET status='uncertain' WHERE id=? AND case_id=? AND status='dispatching'",(args.event,args.case)).rowcount
                if not changed: raise BridgeError("conflict","Event is not an interrupted dispatch")
            return {"event":args.event,"status":"uncertain","remote_duplicates_possible":True}
        if c=="trace-flush":
            if not args.allow_neatlogs: raise BridgeError("trace_config","Explicit --allow-neatlogs is required")
            key=os.environ.get("APPROVALBRIDGE_NEATLOGS_WRITE_KEY","")
            return flush(s,args.case,actor,args.region,args.project,key,retry_uncertain=args.retry_uncertain)
        raise BridgeError("command","Unknown command")
    finally: s.close()

def main():
    args=parser().parse_args()
    try:
        result=execute(args); print(result if isinstance(result,str) else json.dumps(result,indent=2,ensure_ascii=False))
        return 1 if isinstance(result,dict) and result.get("passed") is False else 0
    except BridgeError as e:
        print(json.dumps({"error":e.code,"message":str(e)}),file=sys.stderr); return 2
    except (ValidationError,ValueError,OSError) as e:
        # Input snippets and OS paths may contain credential material; do not echo them.
        print(json.dumps({"error":"invalid_input_or_local_io","message":"Input validation or local file operation failed; check the documented schema and paths.","type":type(e).__name__}),file=sys.stderr); return 2

if __name__=="__main__": raise SystemExit(main())
