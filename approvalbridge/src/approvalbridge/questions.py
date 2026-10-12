"""Finite, documented inquiry-intent resolver; never generates an answer."""
import re

def normalize(text):
    return " ".join(re.findall(r"[a-z0-9]+",text.casefold())).replace("sign off","signoff")

def intents(text):
    value=normalize(text); words=set(value.split()); result=[]
    if words & {"settlement","settled","repayment","paid","payment","credit","ledger","cash"}:
        result.append("settlement")
    if words & {"hold","release","pause","paused","resume"}:
        result.append("hold")
    if words & {"authority","authorized","authorised","identity","sender","permission"}:
        result.append("authority")
    if words & {"condition","conditional","prerequisite","proof","satisfying","fulfilled","satisfied"}:
        result.append("condition")
    elif words & {"cap","shared","joint","split"}:
        result.append("cap")
    elif words & {"conflicting","contradiction","conflict","supersede","correction"}:
        result.append("conflict")
    elif words & {"historical","basis","remaining"}:
        result.append("basis")
    elif words & {"approval","approve","approved","signoff","covered","coverage"}:
        result.append("approval")
    return sorted(set(result))

def identity(question):
    subjects=intents(question["proposition"])
    return (tuple(sorted(set(question["claims"]))),tuple(subjects) if subjects else (normalize(question["proposition"]),))

def fact_keys(question):
    return [(tuple(sorted(set(question["claims"]))),intent) for intent in (intents(question["proposition"]) or ["unknown:"+normalize(question["proposition"])])]
