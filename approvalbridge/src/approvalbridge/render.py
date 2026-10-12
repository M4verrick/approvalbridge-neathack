import html
import os
import tempfile
from pathlib import Path
from .errors import BridgeError
from .util import canonical,digest,text_hash

def safe(value):
    s=html.escape(str(value),quote=True).replace("\n"," ").replace("\r"," ")
    for char in ("\\","|","*","_","[","]","(",")","`","#","!"):
        s=s.replace(char,"\\"+char)
    return s

def money(value,currency):
    return "Unknown" if value is None else f"{currency} {value//100:,}.{value%100:02d}"

def table(headers,rows):
    return ["| "+" | ".join(headers)+" |","|"+"---|"*len(headers)]+["| "+" | ".join(safe(v) for v in row)+" |" for row in rows]

def markdown(dossier):
    d=dossier;accepted="accepted_by" in d
    lines=["# ApprovalBridge — internal reconciliation review","",
        "**"+safe(d.get("data_provenance","Synthetic data"))+" · "+safe(d["mode"])+" · variant "+safe(d["variant"])+"**","",
        f"Case: {safe(d['case'])} · Input revision: {d['input_revision']} · Dossier: {d.get('dossier_revision','not accepted')}",
        f"Accepted by {safe(d['accepted_by'])} at {safe(d['accepted_at'])}." if accepted else "**Unaccepted proposal. A human must review and accept its exact hash.**",
        "Approval evidence does not establish cash receipt, entitlement, or permission for external action.",
        "This downloaded snapshot cannot update itself when newer evidence arrives.",""]
    lines+=table(["Claim","Face","Approval","Conditional","Recorded settlement","Remaining","Holds"],[
        [r["claim"],money(r["face_minor"],d["currency"]),money(r["approval_minor"],d["currency"]),money(r["conditional_minor"],d["currency"]),
         money(r["settlement_minor"],d["currency"]),money(r["remaining_recorded_minor"],d["currency"]),", ".join(r["holds"]) or "None recorded"] for r in d["rows"]])
    lines+=["","## Shared constraints",""]
    lines+=table(["Source","Claims","Cap","Explicit allocations","Unallocated","Condition"],[
        [x["source"],", ".join(x["claims"]),money(x["cap_minor"],d["currency"]),"; ".join(k+": "+money(v,d["currency"]) for k,v in x.get("allocations",{}).items()) or "Unknown",
         money(x.get("unallocated_minor",x["cap_minor"]),d["currency"]),x.get("condition") or "None"] for x in d["shared_constraints"]]) if d["shared_constraints"] else ["None recorded."]
    lines+=["","## Open inquiries (drafts; not sent)",""]
    lines+=["- "+safe(x["claim"])+": "+safe("; ".join(x["propositions"])) for x in d["inquiries"]] or ["None recorded."]
    if d.get("drafted_inquiries"):
        lines+=["","### Proposed requests",""]+["- "+safe(", ".join(x["claims"]))+" → "+safe(x["owner"])+": "+safe(x["draft"]) for x in d["drafted_inquiries"]]
    lines+=["","## Evidence and interpretation",""]
    for x in d["support"]:
        lines+=["- "+safe(x["source"])+" · SHA-256 "+safe(x["source_hash"])+" · line "+str(x.get("start_line","unknown"))+" · character span "+str(x["start"])+":"+str(x["end"]),"> "+safe(x["quote"]),""]
    lines+=["## Settlement history",""]
    events=d.get("settlement_history",[])
    lines+=table(["Event","Type","Amount","Allocation","Reverses","Effective","Recorded effect"],[
        [x["id"],x["kind"],money(x["amount"],d["currency"]),"; ".join(k+": "+money(v,d["currency"]) for k,v in x["allocations"].items()) or "Unknown",
         x["reverses"] or "—",x["effective_at"],"Applied" if x["applied_to_recorded_balance"] else "Unapplied/uncertain"] for x in events]) if events else ["None recorded."]
    lines+=["","## Changes from previous accepted dossier",""]
    if d.get("changes_unavailable"):lines+=["Previous comparison unavailable under current evidence access."]
    elif d.get("changes"):
        lines+=table(["Claim","Field","Previously","Current"],[[x["claim"],x["field"],x["before"],x["after"]] for x in d["changes"]])
    else:lines+=["No earlier accepted dossier, or no changed claim findings."]
    lines+=["","## Source manifest",""]+table(["Source","Type","SHA-256"],[[x["id"],x["kind"],x["sha256"]] for x in d.get("evidence_manifest",[])])
    lines+=["","## Validation","",safe(d["semantic_limit"]),"","Valid proposal: "+str(d["valid"])]
    for label,key in (("Open issue","issues"),("Rejected observation","rejected_observations"),("Validation error","validation_errors")):
        for x in d[key]:lines.append("- "+label+": "+safe(x.get("reason","unknown"))+" · "+safe(x.get("claim") or ", ".join(x.get("claims",[])) or x.get("observation") or x.get("settlement") or "case"))
    lines+=["","Input recorded at: "+safe(d.get("recorded_at","unknown")),"Runtime: "+safe(d.get("run_metadata",{}).get("execution","legacy_unknown")),""]
    return "\n".join(lines)

def review_markdown(proposal):
    return markdown(proposal["payload"])+"\nProposal: "+safe(proposal["id"])+"\n\nExact acceptance hash: "+safe(proposal["hash"])+"\n\nStatus: "+safe(proposal["status"])+"\n"

def comparison_markdown(report):
    lines=["# ApprovalBridge comparison","",
        f"Mode: {safe(report['mode'])} · Execution: {safe(report['execution'])} · Status: {safe(report['status'])}",
        "Receipt SHA-256: "+safe(report["receipt_hash"]),"Plan SHA-256: "+safe(report["plan_hash"]),"",
        "All planned jobs, including failed and unexecuted jobs, appear below.",""]
    lines+=table(["Scenario","Variant","Repeat","Quality","Tools","Model attempts","Missing facts","Extra facts","Reserved µUSD","Outcome"],[
        [j["scenario"],j["variant"],j["repetition"],"Pass" if j["quality_passed"] else "Fail/unexecuted",j.get("tools","—"),j["model_attempts"],
         len(j.get("missing_requests",[])),len(j.get("unnecessary_requests",[])),j["reserved_micro_usd"],", ".join(j.get("failures",[])) or j.get("outcome",j["status"])] for j in report["jobs"]])
    lines+=["","Actual billed cost: unknown. Conservative reserves are not spend.","",safe(report["retention_decision"]),""]+["- "+safe(x) for x in report["limitations"]]
    return "\n".join(lines)+"\n"

def comparison_export(store,cid,actor,directory):
    from .comparison import get_report
    report=get_report(store,cid,actor);root=Path(directory)
    if root.is_symlink():raise BridgeError("path","Report directory cannot be a symlink")
    root.mkdir(parents=True,exist_ok=True);paths=[]
    for suffix,content in (("json",canonical(report)+"\n"),("md",comparison_markdown(report))):
        target=root/(cid+"-"+report["receipt_hash"][:12]+"."+suffix)
        with store.tx():
            get_report(store,cid,actor)
            if target.is_symlink() or (target.exists() and target.read_text()!=content):raise BridgeError("conflict","Report output already contains different bytes")
            fd,tmp=tempfile.mkstemp(prefix=".comparison-",dir=root)
            try:
                with os.fdopen(fd,"w",encoding="utf-8") as f:f.write(content);f.flush();os.fsync(f.fileno())
                os.replace(tmp,target)
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
        if target.read_text()!=content:raise BridgeError("integrity","Report readback mismatch")
        paths.append(str(target))
    return {"files":paths,"receipt_hash":report["receipt_hash"]}

def export(store,case_id,actor,directory,revision=None,fail_before_replace=False):
    store.review_writable(case_id,actor)
    dossier=store.dossier(case_id,actor,revision); d=dossier["payload"]
    root=Path(directory)
    if root.is_symlink(): raise BridgeError("path","Export directory must not be a symlink")
    root.mkdir(parents=True,exist_ok=True)
    outputs={f"{case_id}-v{d['dossier_revision']}.json":canonical(d)+"\n",
             f"{case_id}-v{d['dossier_revision']}.md":markdown(d)}
    paths=[]
    for name,content in outputs.items():
        target=root/name
        if target.is_symlink(): raise BridgeError("path","Export target must not be a symlink")
        eid=digest({"case":case_id,"revision":d["dossier_revision"],"path":str(target.absolute())})
        h=text_hash(content)
        with store.tx():
            store.dossier(case_id,actor,d["dossier_revision"])
            prior=store.one("SELECT * FROM exports WHERE id=?",(eid,))
            if prior and prior["hash"]!=h: raise BridgeError("integrity","Export intent changed")
            store.db.execute("INSERT OR IGNORE INTO exports VALUES(?,?,?,?,?,'pending')",(eid,case_id,d["dossier_revision"],str(target.absolute()),h))
        with store.tx():
            store.dossier(case_id,actor,d["dossier_revision"])
            if target.exists() and text_hash(target.read_text())!=h:
                raise BridgeError("conflict","Export target contains different bytes; choose another directory")
            # A short local write transaction excludes concurrent revocation or input change.
            fd,tmp=tempfile.mkstemp(prefix=".approvalbridge-",dir=root)
            try:
                with os.fdopen(fd,"w",encoding="utf-8") as f:
                    f.write(content); f.flush(); os.fsync(f.fileno())
                if fail_before_replace: raise OSError("simulated export interruption")
                os.replace(tmp,target)
                dirfd=os.open(root,os.O_RDONLY)
                try: os.fsync(dirfd)
                finally: os.close(dirfd)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
            if text_hash(target.read_text())!=h: raise BridgeError("integrity","Export read-back differs")
            store.db.execute("UPDATE exports SET status='complete' WHERE id=?",(eid,))
        paths.append(str(target))
    store.audit("export.complete",actor,case=case_id,revision=d["input_revision"],outcome="complete",details={"content_hash":dossier["hash"],"count":len(paths)})
    return {"files":paths,"hash":dossier["hash"],"raw_sources_included":False}
