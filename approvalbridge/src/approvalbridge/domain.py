"""Deterministic accounting and evidence guards, not general semantic entailment."""
from collections import defaultdict
from .contracts import Bundle, Observation
from .util import instant,text_hash

def authorized(bundle,doc,kind,claims,amount=None):
    if not bundle.synthetic or doc.identity!="fictional_verified" or doc.mail is not None: return False
    when=instant(doc.effective_at)
    if any(a.actor==doc.sender and a.invalidates_prior and kind in a.actions and instant(a.effective_from)<=instant(bundle.as_of) and ("*" in a.claims or bool(set(claims)&set(a.claims))) for a in bundle.authorities): return False
    for a in bundle.authorities:
        if a.actor!=doc.sender or a.invalidates_prior or kind not in a.actions: continue
        if a.currency and a.currency!=bundle.currency: continue
        if kind in ("approval","override") and a.maximum_minor is not None and (amount is None or amount>a.maximum_minor): continue
        if when<instant(a.effective_from) or (a.effective_to and when>=instant(a.effective_to)): continue
        if "*" not in a.claims and not set(claims)<=set(a.claims): continue
        return True
    return False

def reconcile(bundle: Bundle, observations: list[Observation]):
    docs={d.id:d for d in bundle.evidence}
    claims={c.id:c for c in bundle.claims}
    errors=[]; rejected=[]; issues=[]; facts=[]
    seen=set()
    for o in observations:
        d=docs.get(o.source_id)
        reason=None
        if o.id in seen: reason="duplicate observation ID"
        elif not set(o.claims)<=set(claims): reason="unknown claim"
        elif not d or not d.available or d.restricted or d.id in bundle.withdrawn: reason="unavailable source"
        elif o.quote not in d.text.replace("\r\n","\n").replace("\r","\n"): reason="quote not in exact source"
        elif o.currency!=bundle.currency: reason="currency mismatch"
        if reason:
            errors.append({"observation":o.id,"reason":reason}); continue
        seen.add(o.id)
        if instant(d.effective_at)>instant(bundle.as_of):
            rejected.append({"observation":o.id,"reason":"not yet effective"}); continue
        if o.kind=="ambiguous":
            issues.append({"claims":o.claims,"reason":"ambiguous approval scope","source":d.id}); continue
        if not authorized(bundle,d,o.kind,o.claims,o.amount if o.kind=="approval" else None):
            rejected.append({"observation":o.id,"reason":"unsupported identity or authority"})
            issues.append({"claims":o.claims,"reason":"authority unresolved","source":d.id}); continue
        if o.kind=="approval":
            if o.amount is None or o.basis=="unknown":
                rejected.append({"observation":o.id,"reason":"amount or basis unresolved"})
                issues.append({"claims":o.claims,"reason":"amount basis unresolved","source":d.id}); continue
            if o.basis=="remaining_at" and (o.balance_at_basis is None or not o.basis_at):
                errors.append({"observation":o.id,"reason":"remaining basis requires dated balance"}); continue
            maximum=sum(claims[x].amount for x in o.claims)
            if o.basis=="remaining_at":
                historical=reconcile(bundle.model_copy(update={"as_of":o.basis_at}),[])
                matches=[r for r in historical["rows"] if r["claim"] in o.claims]
                supported=historical["valid"] and all(r["remaining_recorded_minor"] is not None for r in matches)
                if not supported or instant(o.basis_at)>instant(d.effective_at) or sum(r["remaining_recorded_minor"] for r in matches)!=o.balance_at_basis:
                    issues.append({"claims":o.claims,"reason":"historical remaining basis is not independently supported","source":d.id})
                    rejected.append({"observation":o.id,"reason":"historical balance not verified"}); continue
                maximum=o.balance_at_basis
            if o.amount>maximum:
                errors.append({"observation":o.id,"reason":"approval exceeds stated basis"}); continue
            if len(o.claims)>1 and o.basis!="shared_cap":
                errors.append({"observation":o.id,"reason":"multi-claim approval requires shared cap"}); continue
        facts.append(o)
    # Supersession is explicit, source-scoped, authority checked, and order independent.
    suppressed=defaultdict(set)
    bad=set()
    for o in facts:
        for target in o.supersedes:
            old=docs.get(target); new=docs[o.source_id]
            prior=[p for p in facts if p.source_id==target and p.kind==o.kind]
            if not old or not prior or target==o.source_id or instant(new.effective_at)<=instant(old.effective_at):
                errors.append({"observation":o.id,"reason":"invalid supersession"}); bad.add(o.id); continue
            if not set(o.claims)<=set().union(*(set(p.claims) for p in prior)):
                errors.append({"observation":o.id,"reason":"supersession claim scope unsupported"}); bad.add(o.id); continue
            if old.sender!=new.sender and not authorized(bundle,new,"override",o.claims,o.amount):
                errors.append({"observation":o.id,"reason":"cross-actor override unauthorized"}); bad.add(o.id); continue
            if any(p.basis=="shared_cap" and set(p.claims)!=set(o.claims) for p in prior):
                errors.append({"observation":o.id,"reason":"cannot partially reallocate a shared cap"}); bad.add(o.id); continue
    facts=[f for f in facts if f.id not in bad]
    for o in facts:
        for target in o.supersedes: suppressed[(target,o.kind)].update(o.claims)
    active=[f for f in facts if not set(f.claims)<=suppressed[(f.source_id,f.kind)]]
    conditions={(f.condition,c) for f in active if f.kind=="condition" and f.condition for c in f.claims if c not in suppressed[(f.source_id,f.kind)]}
    releases=set()
    for f in active:
        if f.kind!="release": continue
        targets=[h for h in active if h.kind=="hold" and h.source_id==f.target]
        source=docs[f.source_id]
        if not targets or any(instant(source.effective_at)<=instant(docs[h.source_id].effective_at) or not set(f.claims)<=set(h.claims) or (source.sender!=docs[h.source_id].sender and not authorized(bundle,source,"override",f.claims)) for h in targets):
            errors.append({"observation":f.id,"reason":"release target, scope, time or override unsupported"}); continue
        releases.update((f.target,c) for c in f.claims)
    coverage=defaultdict(list); conditional=defaultdict(list); holds=defaultdict(list); caps=[]
    support=[]
    for o in facts:
        scope=[c for c in o.claims if c not in suppressed[(o.source_id,o.kind)]]
        if not scope: continue
        d=docs[o.source_id]
        canonical=d.text.replace("\r\n","\n").replace("\r","\n")
        support.append({"observation":o.id,"source":d.id,"source_hash":text_hash(d.text),
                        "quote":o.quote,"start":canonical.index(o.quote),"end":canonical.index(o.quote)+len(o.quote),
                        "start_line":canonical[:canonical.index(o.quote)].count("\n")+1,"canonical_hash":text_hash(canonical),"canonicalization":"newline-v1", "authority_versions":[a.version for a in bundle.authorities if a.actor==d.sender],"procedure_version":bundle.policy.version})
        if o.kind=="hold":
            for c in scope:
                if (o.source_id,c) not in releases: holds[c].append(o.source_id)
        if o.kind!="approval": continue
        if o.basis=="shared_cap":
            caps.append({"source":o.source_id,"claims":scope,"cap_minor":o.amount,"condition":o.condition,
                         "condition_met":o.condition is None or all((o.condition,c) in conditions for c in scope),"allocations":dict(o.allocations),"unallocated_minor":o.amount-sum(o.allocations.values())})
            for c,amount in o.allocations.items():
                if amount>claims[c].amount:
                    errors.append({"observation":o.id,"reason":"shared-cap allocation exceeds claim amount"});continue
                target=conditional if o.condition and (o.condition,c) not in conditions else coverage
                target[c].append(o.model_copy(update={"amount":amount,"basis":"original"}))
            continue
        for c in scope:
            target=conditional if o.condition and (o.condition,c) not in conditions else coverage
            target[c].append(o)
    # Only typed, posted, supported ledger events alter the recorded balance.
    net=defaultdict(int); uncertain=set(); applied={}
    events=sorted(bundle.settlements,key=lambda s:(instant(s.effective_at),s.kind=="reversal",s.id))
    for s in events:
        scope=set(s.allocations)|set(s.candidates) or set(claims)
        d=docs.get(s.source_id)
        if instant(s.effective_at)>instant(bundle.as_of): continue
        if not d or not d.available or d.restricted or d.id in bundle.withdrawn:
            uncertain.update(scope); continue
        if s.status!="posted":
            uncertain.update(scope); issues.append({"claims":sorted(scope),"reason":"provisional settlement","source":s.source_id}); continue
        if s.kind=="reversal":
            original=applied.get(s.reverses)
            if not original or sum(s.allocations.values())!=s.amount or not s.allocations:
                errors.append({"settlement":s.id,"reason":"reversal original/allocation unsupported"}); continue
            if any(v>original.get(k,0) for k,v in s.allocations.items()):
                errors.append({"settlement":s.id,"reason":"reversal exceeds unreversed allocation"}); continue
            for k,v in s.allocations.items(): original[k]-=v; net[k]-=v
            applied[s.id]={}
            continue
        effect=bundle.policy.settlement_effects.get(s.kind,"unknown")
        if effect=="none": continue
        if effect!="reduce" or sum(s.allocations.values())!=s.amount:
            uncertain.update(scope)
            issues.append({"claims":sorted(scope),"reason":"settlement type or allocation unresolved","source":s.source_id})
            continue
        applied[s.id]=dict(s.allocations)
        for c,a in s.allocations.items(): net[c]+=a
    rows=[]
    def scalar(entries,c):
        values={(o.amount,o.basis,o.balance_at_basis,o.basis_at) for o in entries}
        if len(values)>1:
            issues.append({"claims":[c],"reason":"overlapping conflicting approvals","source":None})
            return None
        return entries[0].amount if entries else None
    for c in bundle.claims:
        approved=scalar(coverage[c.id],c.id); pending=scalar(conditional[c.id],c.id)
        if net[c.id]>c.amount or net[c.id]<0:
            errors.append({"claim":c.id,"reason":"settlement balance outside claim bounds"})
        remaining=None if c.id in uncertain else c.amount-net[c.id]
        groups=[g for g in caps if c.id in g["claims"]]
        rowissues=[i["reason"] for i in issues if c.id in i["claims"] and not ((approved is not None or pending is not None or groups) and i["reason"]=="ambiguous approval scope")]
        state="approval_scope_supported" if approved is not None else "approval_evidence_missing"
        if pending is not None: state="conditional_approval"
        if any(c.id not in g["allocations"] for g in groups): state="shared_cap_unallocated"
        if rowissues: state="conflicting_or_incomplete_evidence"
        if remaining==0: state="matched_settlement_recorded"
        rows.append({"claim":c.id,"invoice":c.invoice,"face_minor":c.amount,"currency":c.currency,
                     "approval_minor":approved,"conditional_minor":pending,"shared_caps":groups,
                     "settlement_minor":net[c.id],"remaining_recorded_minor":remaining,
                     "holds":holds[c.id],"issues":rowissues,"state":state,
                     "approval_sources":[o.source_id for o in coverage[c.id]+conditional[c.id]],
                     "action_eligibility":"not_assessed_no_external_actions"})
    inquiries=[]
    for r in rows:
        reasons=[]
        if r["approval_minor"] is None and r["conditional_minor"] is None and r["remaining_recorded_minor"]!=0 and not r["shared_caps"]:
            reasons.append("claim-specific approval scope and amount")
        if r["conditional_minor"] is not None: reasons.append("evidence satisfying the approval condition")
        if r["shared_caps"]:
            if any(r["claim"] not in g["allocations"] for g in r["shared_caps"]):
                reasons.append("allocation of shared approval cap")
            if any(g["condition"] and not g["condition_met"] for g in r["shared_caps"]):
                reasons.append("evidence satisfying the approval condition")
        if r["remaining_recorded_minor"] is None: reasons.append("settlement meaning and claim allocation")
        if r["holds"]: reasons.append("authorized release or clarification of the hold")
        reasons.extend(x for x in r["issues"] if x not in ("ambiguous approval scope","settlement type or allocation unresolved","provisional settlement"))
        if reasons:
            inquiries.append({"claim":r["claim"],"owner":"merchant_or_AR_analyst","propositions":sorted(set(reasons)),
                              "status":"draft_not_sent"})
    return {"schema_version":1,"case":bundle.id,"title":bundle.title,"synthetic":True,"currency":bundle.currency,
            "rows":rows,"shared_constraints":caps,"support":support,"inquiries":inquiries,
            "evidence_manifest":[{"id":d.id,"sha256":text_hash(d.text),"kind":d.kind} for d in bundle.evidence if d.available and not d.restricted and d.id not in bundle.withdrawn],
            "settlement_history":[dict(s.model_dump(),source_hash=text_hash(docs[s.source_id].text),applied_to_recorded_balance=s.id in applied) for s in events if docs[s.source_id].available and not docs[s.source_id].restricted and s.source_id not in bundle.withdrawn],
            "issues":issues,"rejected_observations":rejected,"validation_errors":errors,
            "valid":not errors,"purpose":"internal_reconciliation_review",
            "semantic_limit":"Source references and structural checks do not prove natural-language entailment. Analyst review required.",
            "completion":"complete_for_internal_review" if not errors else "invalid_proposal"}
