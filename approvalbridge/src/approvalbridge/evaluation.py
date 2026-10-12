"""Frozen synthetic oracle checks. Offline results do not measure model quality."""
import json
import tempfile
import time
from pathlib import Path
from .contracts import Bundle
from .errors import BridgeError
from .store import Store
from .runner import run_investigation
from .util import digest

def evaluate(fixtures_dir):
    root=Path(fixtures_dir)
    oracle=json.loads((root/"oracle.json").read_text())
    inquiry_oracle=json.loads((root/"inquiry-oracle.json").read_text())
    manifest=json.loads((root/"manifest.json").read_text())
    reports=[]
    for name,expected in oracle.items():
        packet=json.loads((root/(name+".json")).read_text())
        if digest(packet)!=manifest[name]: raise BridgeError("fixture","Frozen scenario hash changed")
        bundle=Bundle.model_validate(packet)
        for variant in ("A","B","C"):
            with tempfile.TemporaryDirectory(prefix="approvalbridge-eval-") as tmp:
                s=Store(Path(tmp)/"state.db"); s.import_bundle(bundle,"evaluator","import")
                inv=s.new_investigation(bundle.id,"evaluator")
                r=s.start_run(inv,"evaluator",variant,"offline"); start=time.monotonic()
                result=run_investigation(s,r["id"],"evaluator")
                dossier=s.proposal(result["proposal"],"evaluator")["payload"]
                rows={x["claim"]:x for x in dossier["rows"]}; failures=[]
                if set(rows)!=set(expected): failures.append("claim_inventory")
                for cid,fields in expected.items():
                    for field,value in fields.items():
                        if rows[cid][field]!=value: failures.append(cid+"."+field)
                wanted={(cid,p) for cid,propositions in inquiry_oracle[name].items() for p in propositions}
                asked={(x["claim"],p) for x in dossier["inquiries"] for p in x["propositions"]}
                missing=sorted(wanted-asked); unnecessary=sorted(asked-wanted)
                if missing: failures.append("missing_inquiry_propositions")
                if unnecessary: failures.append("unnecessary_inquiry_propositions")
                if not dossier["valid"]: failures.append("validation")
                usage=s.one("SELECT * FROM investigations WHERE id=?",(inv,))
                reports.append({"scenario":name,"split":"development" if name.startswith("D") else "designated_H_regression",
                    "variant":variant,"passed":not failures,"failures":failures,"tool_operations":usage["tools"],
                    "model_calls":usage["attempts"],"latency_ms":round((time.monotonic()-start)*1000,2),
                    "missing_propositions":missing,"unnecessary_propositions":unnecessary,"necessary_proposition_recall":1.0 if not wanted else len(wanted&asked)/len(wanted),
                    "live_cost":None,"mode":"offline_deterministic_simulation","inquiry_propositions":sum(len(x["propositions"]) for x in dossier["inquiries"])})
                s.close()
    return {"passed":all(x["passed"] for x in reports),"runs":reports,"claim":"Contract and recovery harness only; no inference quality, token, cost, or latency advantage established"}
