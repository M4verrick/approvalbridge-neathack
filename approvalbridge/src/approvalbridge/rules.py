"""Deliberately narrow deterministic baseline; not a language model."""
import re
from decimal import Decimal
from pydantic import ValidationError
from .contracts import Observation
from .errors import BridgeError
from .util import digest

def _observation(**values):
    try:
        return Observation(**values)
    except ValidationError:
        raise BridgeError("invalid","Recognized evidence does not fit the observation contract; review the source") from None

ID=r"[A-Za-z0-9][A-Za-z0-9_.-]*"
def extract(bundle,docs=None):
    observations=[]
    for d in docs if docs is not None else bundle.evidence:
        if not d.available or d.restricted or d.id in bundle.withdrawn: continue
        for n,line in enumerate(d.text.replace("\r\n","\n").splitlines()):
            line=line.strip()
            observation_id=f"{d.id}-{n}"
            if len(observation_id)>80: observation_id="obs-"+digest([d.id,n])[:40]
            common={"id":observation_id,"source_id":d.id,"quote":line,"currency":bundle.currency}
            # Full-line matching prevents an embedded command or unscoped number becoming a fact.
            m=re.fullmatch(r"I approve (USD|SGD|EUR|GBP) ([0-9]+\.[0-9]{2}) for claim ("+ID+r") against the original deduction(?: if ("+ID+r"))?(?:; supersedes ("+ID+r"))?\.",line)
            if m:
                common["currency"]=m[1]
                observations.append(_observation(**common,kind="approval",claims=[m[3]],amount=int(Decimal(m[2])*100),basis="original",condition=m[4],supersedes=[m[5]] if m[5] else [])); continue
            m=re.fullmatch(r"I approve (USD|SGD|EUR|GBP) ([0-9]+\.[0-9]{2}) for item ("+ID+r") on ("+ID+r") for period ([0-9]{4}-[0-9]{2}) against the original deduction\.",line)
            if m:
                matches=[c.id for c in bundle.claims if c.item==m[3] and c.po==m[4] and c.period==m[5]]
                if len(matches)==1:
                    common["currency"]=m[1]
                    observations.append(_observation(**common,kind="approval",claims=matches,amount=int(Decimal(m[2])*100),basis="original"))
                continue
            m=re.fullmatch(r"I approve a shared cap of (USD|SGD|EUR|GBP) ([0-9]+\.[0-9]{2}) for claims ("+ID+r"(?:, "+ID+r")+)\.",line)
            if m:
                common["currency"]=m[1]
                observations.append(_observation(**common,kind="approval",claims=m[3].split(", "),amount=int(Decimal(m[2])*100),basis="shared_cap")); continue
            m=re.fullmatch(r"Hold claim ("+ID+r")\.",line)
            if m:
                observations.append(_observation(**common,kind="hold",claims=[m[1]])); continue
            m=re.fullmatch(r"Release hold ("+ID+r") for claim ("+ID+r")\.",line)
            if m:
                observations.append(_observation(**common,kind="release",claims=[m[2]],target=m[1])); continue
            m=re.fullmatch(r"Condition ("+ID+r") is satisfied for claim ("+ID+r")\.",line)
            if m:
                observations.append(_observation(**common,kind="condition",claims=[m[2]],condition=m[1])); continue
            if "approved" in line.lower() and "claim" in line.lower():
                scope=[c.id for c in bundle.claims if re.search(r"\b"+re.escape(c.id)+r"\b",line)]
                if scope: observations.append(_observation(**common,kind="ambiguous",claims=scope))
    return observations
