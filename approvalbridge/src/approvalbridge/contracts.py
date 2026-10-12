from typing import Annotated, Literal, Any
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .util import instant

Identifier = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
Money = Annotated[int, Field(ge=0, le=10**12)]
SmallText = Annotated[str, Field(max_length=4000)]

class Strict(BaseModel):
    model_config=ConfigDict(extra="forbid", strict=True, validate_assignment=True)

class Claim(Strict):
    id: Identifier
    invoice: Identifier
    po: Identifier
    item: Identifier
    amount: Money
    period: str = "2026-10"
    currency: Literal["USD","SGD","EUR","GBP"] = "USD"

class MailProvenance(Strict):
    provider: Literal["microsoft_graph"]
    connection: Identifier
    account_ref: Annotated[str,Field(pattern=r"^[a-f0-9]{64}$")]
    message_ref: Annotated[str,Field(pattern=r"^[a-f0-9]{64}$")]
    content_hash: Annotated[str,Field(pattern=r"^[a-f0-9]{64}$")]
    imported_at: str
    sent_at: str | None = None
    received_at: str
    source_link: Annotated[str,Field(max_length=2048)] | None = None
    previous_source: Identifier | None = None
    synthetic_fixture: bool = False

    @field_validator("imported_at","sent_at","received_at")
    @classmethod
    def valid_time(cls,value):
        if value is not None:instant(value)
        return value
    @field_validator("source_link")
    @classmethod
    def safe_source_link(cls,value):
        if value is not None:
            from urllib.parse import urlsplit
            p=urlsplit(value)
            if p.scheme!="https" or p.hostname not in ("outlook.office.com","outlook.office365.com","outlook.live.com") or p.username or p.password or p.port not in (None,443):
                raise ValueError("Original mail link must use an approved Outlook HTTPS origin")
        return value

class Evidence(Strict):
    id: Identifier
    text: Annotated[str,Field(max_length=32768)]
    sender: Identifier = "unknown"
    identity: Literal["fictional_verified","unknown"] = "unknown"
    kind: Literal["email","ledger","procedure","note"] = "email"
    effective_at: str = "2026-10-01T00:00:00+00:00"
    available: bool = True
    restricted: bool = False
    origin: SmallText = "Synthetic local fixture"
    mail: MailProvenance | None = None
    @field_validator("effective_at")
    @classmethod
    def timestamp(cls,v):
        instant(v); return v
    @field_validator("text")
    @classmethod
    def bounded_text(cls,v):
        if "\x00" in v or len(v.encode("utf-8"))>32768:
            raise ValueError("text must be bounded UTF-8 without NUL")
        return v

class Authority(Strict):
    actor: Identifier
    actions: list[Literal["approval","hold","release","condition","override"]]
    claims: list[str] = Field(default_factory=lambda:["*"])
    effective_from: str = "2020-01-01T00:00:00+00:00"
    effective_to: str | None = None
    invalidates_prior: bool = False
    version: Identifier = "v1"
    currency: Literal["USD","SGD","EUR","GBP"] | None = None
    maximum_minor: Money | None = None
    source: str = "Fictional trusted register supplied by operator"
    @model_validator(mode="after")
    def interval(self):
        start=instant(self.effective_from)
        if self.effective_to and instant(self.effective_to)<=start:
            raise ValueError("authority interval is empty")
        return self

class Settlement(Strict):
    id: Identifier
    source_id: Identifier
    kind: Literal["retailer_repayment","retailer_adjustment","supplier_concession","reversal","unknown"]
    amount: Money
    currency: Literal["USD","SGD","EUR","GBP"] = "USD"
    status: Literal["posted","provisional"] = "posted"
    allocations: dict[Identifier,Money] = Field(default_factory=dict)
    candidates: list[Identifier] = Field(default_factory=list)
    reverses: Identifier | None = None
    effective_at: str = "2026-10-01T00:00:00+00:00"
    @model_validator(mode="after")
    def allocation(self):
        instant(self.effective_at)
        if sum(self.allocations.values())>self.amount:
            raise ValueError("settlement allocations exceed event amount")
        if (self.kind=="reversal") != (self.reverses is not None):
            raise ValueError("reversal must reference original event; other types cannot")
        return self

class Policy(Strict):
    version: Identifier = "fictional-v1"
    approval_required: bool = True
    precedence: Literal["unresolved_on_conflict"] = "unresolved_on_conflict"
    settlement_effects: dict[str,Literal["reduce","none","unknown"]] = Field(default_factory=lambda:{
        "retailer_repayment":"reduce","retailer_adjustment":"unknown",
        "supplier_concession":"reduce","unknown":"unknown"})
    description: SmallText = "Fictional internal review procedure. Approval is not entitlement or cash."

class ClaimCorrection(Strict):
    claim: Identifier
    previous_hash: Annotated[str,Field(pattern=r"^[0-9a-f]{64}$")]
    source_id: Identifier
    reason: Annotated[str,Field(min_length=1,max_length=1000)]

class Bundle(Strict):
    schema_version: Literal[1] = 1
    id: Identifier
    title: Annotated[str,Field(min_length=1,max_length=200)]
    synthetic: bool = True
    currency: Literal["USD","SGD","EUR","GBP"] = "USD"
    as_of: str = "2026-10-10T07:00:00+00:00"
    claims: list[Claim] = Field(min_length=1,max_length=32)
    evidence: list[Evidence] = Field(max_length=32)
    authorities: list[Authority] = Field(default_factory=list,max_length=32)
    settlements: list[Settlement] = Field(default_factory=list,max_length=64)
    withdrawn: list[Identifier] = Field(default_factory=list)
    policy: Policy = Field(default_factory=Policy)
    claim_corrections: list[ClaimCorrection] = Field(default_factory=list,max_length=32)
    @field_validator("synthetic",mode="before")
    @classmethod
    def truly_synthetic(cls,v):
        if type(v) is not bool: raise ValueError("synthetic must be an explicit boolean")
        return v
    @model_validator(mode="after")
    def integrity(self):
        instant(self.as_of)
        if any(d.mail and not d.mail.synthetic_fixture for d in self.evidence) and self.synthetic:
            raise ValueError("real imported mail cannot be labeled synthetic")
        if any(d.mail and d.identity != "unknown" for d in self.evidence):
            raise ValueError("imported mail cannot establish verified approval identity")
        for records in (self.claims,self.evidence,self.settlements):
            ids=[r.id for r in records]
            if len(ids)!=len(set(ids)):
                raise ValueError("duplicate identifiers")
        claim_ids={c.id for c in self.claims}
        docs={d.id for d in self.evidence}
        settlements={s.id for s in self.settlements}
        if sum(len(d.text.encode()) for d in self.evidence)>1024*1024:
            raise ValueError("bundle text exceeds 1 MiB")
        if not set(self.withdrawn)<=docs:
            raise ValueError("withdrawal references absent evidence")
        if len({c.claim for c in self.claim_corrections})!=len(self.claim_corrections): raise ValueError("duplicate claim correction")
        for correction in self.claim_corrections:
            if correction.claim not in claim_ids or correction.source_id not in docs: raise ValueError("claim correction reference missing")
        for c in self.claims:
            if c.currency!=self.currency: raise ValueError("mixed currencies")
        for s in self.settlements:
            if s.currency!=self.currency: raise ValueError("mixed currencies")
            if s.source_id not in docs: raise ValueError("settlement source missing")
            if not (set(s.allocations)|set(s.candidates))<=claim_ids:
                raise ValueError("unknown settlement claim")
            if s.reverses and s.reverses not in settlements:
                raise ValueError("reversal original missing")
        return self

class Observation(Strict):
    id: Identifier
    source_id: Identifier
    quote: Annotated[str,Field(min_length=1,max_length=4000)]
    kind: Literal["approval","hold","release","condition","ambiguous"]
    claims: list[Identifier] = Field(min_length=1,max_length=32)
    amount: Money | None = None
    currency: Literal["USD","SGD","EUR","GBP"] = "USD"
    basis: Literal["original","remaining_at","shared_cap","unknown"] = "unknown"
    allocations: dict[Identifier,Money] = Field(default_factory=dict)
    balance_at_basis: Money | None = None
    basis_at: str | None = None
    condition: Identifier | None = None
    supersedes: list[Identifier] = Field(default_factory=list,max_length=16)
    target: Identifier | None = None
    @model_validator(mode="after")
    def consistency(self):
        if len(set(self.claims))!=len(self.claims): raise ValueError("duplicate scope")
        if self.basis_at: instant(self.basis_at)
        if self.allocations and (self.kind!="approval" or self.basis!="shared_cap" or not set(self.allocations)<=set(self.claims) or self.amount is None or sum(self.allocations.values())>self.amount):
            raise ValueError("approval allocation requires a scoped shared cap and cannot exceed it")
        return self

class SearchArgs(Strict):
    query: Annotated[str,Field(max_length=200)] = ""
    limit: Annotated[int,Field(ge=1,le=32)] = 32
class ReadArgs(Strict):
    ids: list[Identifier] = Field(min_length=1,max_length=4)
class LookupArgs(Strict):
    subject: Literal["authority","settlement","all"] = "all"
class InquiryRequest(Strict):
    claims: list[Identifier] = Field(min_length=1,max_length=32)
    proposition: Annotated[str,Field(min_length=1,max_length=300)]
    owner: Identifier = "merchant"
    draft: Annotated[str,Field(min_length=1,max_length=2000)]
class InquiryArgs(Strict):
    requests: list[InquiryRequest] = Field(default_factory=list,max_length=8)
    claims: list[Identifier] = Field(default_factory=list,max_length=32)
    proposition: Annotated[str,Field(max_length=300)] = ""
    owner: Identifier = "merchant"
    draft: Annotated[str,Field(max_length=2000)] = ""
    @model_validator(mode="after")
    def one_form(self):
        if self.requests:
            if self.claims or self.proposition or self.draft: raise ValueError("use requests or a single inquiry, not both")
        elif not self.claims or not self.proposition or not self.draft:
            raise ValueError("inquiry requires claims, proposition and draft")
        return self
    def items(self):
        return self.requests or [InquiryRequest(claims=self.claims,proposition=self.proposition,owner=self.owner,draft=self.draft)]

class ProposalArgs(Strict):
    observations: list[Observation] = Field(default_factory=list,max_length=128)
    inquiries: list[InquiryRequest] = Field(default_factory=list,max_length=8)
class Decision(Strict):
    tool: Literal["search","read","lookup","inquire","propose"]
    arguments: dict[str,Any]
    rationale: Annotated[str,Field(max_length=1000)] = ""
