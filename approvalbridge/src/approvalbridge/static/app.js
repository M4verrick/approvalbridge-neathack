"use strict";
/* Local review desk. All source material is rendered as text, never HTML. */
const $ = (q) => document.querySelector(q);
const app = $("#app"), dialog = $("#dialog");
const S = {state:null, view:null, route:[], busy:false, serial:0, variant:"C", mode:"offline", executionContext:null, selectedInvestigation:null, poll:null, announcedJob:null, renderedRoute:null};
const active = j => ["queued","running"].includes(j.status);
const label = v => v == null ? "Unknown" : String(v).replaceAll("_"," ");
const short = v => v ? String(v).slice(0,12) : "—";
function el(tag, attrs={}, ...children) {
  const n=document.createElement(tag);
  for(const [k,v] of Object.entries(attrs)) {
    if(v==null || (v===false&&!k.startsWith("aria-"))) continue;
    if(k==="class") n.className=v;
    else if(k.startsWith("on")) n.addEventListener(k.slice(2),v);
    else if(["value","checked","disabled","open"].includes(k)) n[k]=v;
    else n.setAttribute(k,k.startsWith("aria-")?String(v):v===true?"":v);
  }
  for(const c of children.flat(Infinity)) if(c!=null && c!==false) n.append(c instanceof Node?c:document.createTextNode(String(c)));
  return n;
}
const p=(text,cls="")=>el("p",{class:cls},text);
const tag=(text,cls="")=>el("span",{class:"tag "+cls},label(text));
const valueForField=(field,value,currency)=>field.endsWith("_minor")?money(value,currency):Array.isArray(value)?value.map(label).join(", ")||"None":label(value);
const money=(n,c="USD")=> n==null?"Unknown":new Intl.NumberFormat("en-US",{style:"currency",currency:c}).format(n/100);
const link=(text,href,cls="")=>el("a",{href,class:cls},text);
const note=(text,cls="")=>el("div",{class:"note "+cls},text);
const section=(title,body,subtitle)=>el("section",{class:"sheet section"},el("div",{class:"sheet-heading"},el("div",{},el("h2",{},title),subtitle?p(subtitle):null)),el("div",{class:"sheet-body"},body));
const details=(title,value)=>el("details",{class:"source"},el("summary",{},title),el("div",{class:"source-content"},el("pre",{class:"mono json"},JSON.stringify(value,null,2))));
function button(text,fn,cls="",disabled=false) {
  return el("button",{type:"button",class:"button "+cls,disabled,onclick:()=>action(fn), "data-mutation":true, "data-focus":text},text);
}
function toast(message,error=false) {
  $("#notices").replaceChildren(el("div",{class:"toast"+(error?" error":"")},el("span",{},message),el("button",{class:"close-button","aria-label":"Dismiss notification",onclick:()=>$("#notices").replaceChildren()},"×")));
}
async function api(path,body) {
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),15000);
  try {
    const response=await fetch(path,{method:body?"POST":"GET",cache:"no-store",signal:controller.signal,headers:body?{"Content-Type":"application/json","X-CSRF-Token":S.state?.csrf||""}:{},body:body?JSON.stringify(body):undefined});
    let data; try {data=await response.json();} catch {throw Error("Local server returned an unreadable response. Refresh to inspect the saved state.");}
    if(!response.ok) {const err=Error(data.error?.message||"Local request failed.");err.code=data.error?.code;throw err;}
    return data;
  } catch(error) {
    if(error.name==="AbortError")throw Error(body?"Response timed out; whether the operation saved is unknown. Inspect saved state before retrying. The operation key is retained.":"Loading timed out. Refresh saved state to try again.");
    throw error;
  } finally {clearTimeout(timer);}
}
async function keyed(path,body) {
  const bytes=new TextEncoder().encode(path+JSON.stringify(body));
  const hash=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes)),n=>n.toString(16).padStart(2,"0")).join("");
  const slot="approvalbridge:"+hash;
  let key=sessionStorage.getItem(slot);
  if(!key){key="web-"+crypto.randomUUID();sessionStorage.setItem(slot,key);}
  const result=await api(path,{...body,key});
  sessionStorage.removeItem(slot);
  return result;
}
async function action(fn) {
  if(S.busy) return;
  S.busy=true;
  const buttons=[...document.querySelectorAll("[data-mutation]")].map(n=>[n,n.disabled]);
  buttons.forEach(([n])=>n.disabled=true);
  try {await fn();} catch(e) {toast(e.message,true);}
  finally {S.busy=false;buttons.forEach(([n,d])=>{if(n.isConnected)n.disabled=d;});}
}
function heading(title,description,actions=[]) {
  return el("div",{class:"page-heading"},el("div",{},el("h1",{},title),description?p(description):null),el("div",{class:"heading-actions"},actions));
}
function shell(content,breadcrumb="Casework") {
  const nav=[["Casework","#/","▤"],["Comparisons","#/comparisons","≋"],["Readiness","#/readiness","◉"]];
  const current=S.route[0]||"";
  app.replaceChildren(el("div",{class:"shell"},el("aside",{class:"rail"},link([el("span",{class:"mark"},"a"),"ApprovalBridge"],"#/","brand"),
    el("nav",{"aria-label":"Main navigation"},nav.map(([name,href,icon])=>{const selected=name==="Casework"?!["comparisons","readiness"].includes(current):current===name.toLowerCase();return el("a",{href,class:"nav-link"+(selected?" active":""),"aria-current":selected?"page":null},el("span",{class:"nav-symbol","aria-hidden":true},icon),name);})),
    el("div",{class:"rail-foot"},el("div",{},el("span",{class:"local-dot"}),"Local review desk"),p("Local operator · "+(S.state?.actor||"analyst")),p("Imported evidence stays local until you run its configured live investigation."))),
    el("div",{class:"workspace"},el("header",{class:"topbar"},el("div",{class:"crumb"},link("Workspace","#/"),el("span",{},"/"),el("span",{},breadcrumb)),el("span",{class:"mode"},S.view&&S.route[0]==="case"&&S.view.runs[0]?.mode==="live"?"Live route · "+label(S.view.runs[0].metadata.execution):"Offline · no model calls")),
      el("main",{id:"main",tabindex:"-1"},content))));
}
function modal(title,body,submitText,submit,options={}) {
  const error=el("p",{id:"dialog-error",class:"inline-error",role:"alert"});
  const footer=el("div",{class:"dialog-footer"});
  const cancel=el("button",{type:"button",class:"button quiet",onclick:()=>dialog.close()},"Cancel");
  let submitButton;
  if(submit) submitButton=el("button",{type:"button",class:"button "+(options.danger?"danger":"primary"),disabled:!!options.disabled,onclick:async()=>{
    if(S.busy)return;S.busy=true;submitButton.disabled=true;cancel.disabled=false;error.textContent="";
    try{await submit();dialog.close();await refresh();}catch(e){error.textContent=e.message;if(["stale","conflict","csrf"].includes(e.code))error.append(el("button",{type:"button",class:"button small quiet",onclick:()=>{dialog.close();refresh();}},"Refresh saved state"));}
    finally{S.busy=false;if(submitButton.isConnected)submitButton.disabled=options.enabled?!options.enabled():false;cancel.disabled=false;}
  }},submitText);
  footer.append(cancel);if(submitButton)footer.append(submitButton);
  const close=el("button",{type:"button",class:"close-button","aria-label":"Close dialog",onclick:()=>{dialog.close();}},"×");
  const content=el("div",{class:"dialog-content"},body,error);
  dialog.replaceChildren(el("div",{class:"dialog-heading"},el("h2",{id:"dialog-title"},title),close),content,footer);
  S.returnFocus=focusReference(document.activeElement);
  for(const field of dialog.querySelectorAll("input,textarea,select"))field.setAttribute("aria-describedby","dialog-error");
  dialog.showModal();
  requestAnimationFrame(()=>{
    const field=[...content.querySelectorAll("input,select,textarea")].find(n=>!n.disabled&&n.getAttribute("type")!=="hidden");
    (field||(options.danger?cancel:close)).focus();
  });
  return {submitButton,error};
}
function focusReference(node) {return node?.id?{id:node.id}:node?.getAttribute?.("data-focus")?{name:node.getAttribute("data-focus")}:node?.getAttribute?.("href")?{href:node.getAttribute("href")}:null;}
function restoreFocus(ref) {
  const target=ref?.id?document.getElementById(ref.id):[...document.querySelectorAll("[data-focus],a")].find(n=>ref?.name?n.getAttribute("data-focus")===ref.name:ref?.href&&n.getAttribute("href")===ref.href);
  (target||$("#main"))?.focus();
}
dialog.addEventListener("close",()=>queueMicrotask(()=>restoreFocus(S.returnFocus)));
$(".skip").addEventListener("click",event=>{event.preventDefault();$("#main")?.focus();$("#main")?.scrollIntoView({block:"start"});});
function strictJSON(text) {
  let at=0;
  const whitespace=()=>{while(/\s/.test(text[at]||"\0"))at++;};
  function value(depth=0) {
    if(depth>64)throw Error("JSON is nested too deeply.");
    whitespace();const start=at,ch=text[at];
    if(ch==='"'){at++;while(at<text.length){if(text[at]==='\\'){at+=2;continue;}if(text[at++]==='"')return JSON.parse(text.slice(start,at));}throw Error("Unterminated JSON string.");}
    if(ch==='{') {
      at++;whitespace();const keys=new Set();if(text[at]==='}'){at++;return;}
      while(at<text.length){whitespace();if(text[at]!=='"')throw Error("Expected a JSON key.");const key=value(depth+1);if(keys.has(key))throw Error("Duplicate JSON keys are not allowed.");keys.add(key);whitespace();if(text[at++]!==':')throw Error("Expected a colon.");value(depth+1);whitespace();const next=text[at++];if(next==='}')return;if(next!==',')throw Error("Expected a comma.");}throw Error("Unterminated JSON object.");
    }
    if(ch==='['){at++;whitespace();if(text[at]===']'){at++;return;}while(at<text.length){value(depth+1);whitespace();const next=text[at++];if(next===']')return;if(next!==',')throw Error("Expected a comma.");}throw Error("Unterminated JSON array.");}
    while(at<text.length&&!/[\s,\]}]/.test(text[at]))at++;
    const token=text.slice(start,at);if(!token)throw Error("Expected a JSON value.");const parsed=JSON.parse(token);if(typeof parsed==='number'&&!Number.isFinite(parsed))throw Error("Nonfinite numbers are not allowed.");
  }
  value();whitespace();if(at!==text.length)throw Error("Unexpected trailing JSON.");
  return JSON.parse(text);
}

function jsonInput(help) {
  const textarea=el("textarea",{id:"json-input",spellcheck:false,placeholder:"Paste a JSON object…","aria-label":"JSON object"});
  const file=el("input",{type:"file",accept:".json,application/json",id:"json-file"});
  const err=el("p",{class:"inline-error",role:"alert"});
  file.addEventListener("change",async()=>{err.textContent="";const f=file.files[0];if(!f)return;if(f.size>1100000){err.textContent="Choose a JSON file smaller than 1.1 MB.";return;}textarea.value=await f.text();});
  return {node:el("div",{},p(help),el("div",{class:"file-drop"},el("label",{for:"json-file"},"Choose a local JSON file"),file,err),el("label",{for:"json-input",class:"small"},"Or paste JSON"),textarea),read:()=>{
    if(new TextEncoder().encode(textarea.value).length>1100000)throw Error("JSON exceeds the 1.1 MB browser limit.");
    try{const v=strictJSON(textarea.value);if(!v||Array.isArray(v)||typeof v!=="object")throw Error();return v;}catch(e){throw Error("Enter a valid JSON object without duplicate keys or nonfinite numbers. No case was changed.");}
  }};
}
function importModal(view=null) {
  const input=jsonInput(view?"Import the complete amended case bundle. Existing records are immutable; append evidence or use explicit corrections. The current revision is checked before saving.":"Import an ApprovalBridge case bundle with explicit data provenance. Up to 32 claims and 32 source documents.");
  modal(view?"Import next revision":"Import case",input.node,"Validate & import",async()=>{
    const bundle=input.read();if(view&&bundle.id!==view.case.id)throw Error("The bundle ID must match this case.");
    const result=await keyed("/api/import",{bundle,expected_revision:view?view.case.revision:null});
    location.hash="#/case/"+bundle.id+"/findings";toast("Case evidence saved. Review the new revision.");
  });
}
function standardSample(view) {
  return view.bundle.synthetic && view.bundle.claims.length===3 && ["A","B","C"].every(id=>view.bundle.claims.some(c=>c.id===id)) && view.bundle.evidence.some(d=>d.id==="procedure" && d.text==="Synthetic retailer procedure: claim-specific written pricing approval is required; approval does not prove settlement.");
}
function sampleModal() {
  const field=el("input",{type:"text",id:"sample-id",value:"pricing-"+Date.now().toString(36),maxlength:80});
  modal("Open the sample case",el("div",{},p("Three fictional deductions. Start with incomplete evidence, request simulated replies, then inspect a correction and repayment reversal."),el("div",{class:"field"},el("label",{for:"sample-id"},"Case ID"),field),note("Offline parsing and frozen replies. No messages or model calls.","info")),"Create sample",async()=>{
    if(!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(field.value))throw Error("Use a case ID with letters, numbers, dots, underscores or dashes.");
    await keyed("/api/demo",{id:field.value,stage:1,expected_revision:null});location.hash="#/case/"+field.value+"/findings";
  });
}
function mailModal(view) {
  const connections=S.state.readiness.mail.filter(c=>c.enabled&&!c.revoked);
  if(!connections.length){modal("Email setup required",el("div",{},p("Configure a read-only mailbox on the server first. No mailbox connection or credential is created by this screen."),link("View runtime readiness","#/readiness", "button")));return;}
  const connection=el("select",{id:"mail-connection"},connections.map(c=>el("option",{value:c.id},c.id)));
  const folder=el("select",{id:"mail-folder"});
  function folders(){const c=connections.find(c=>c.id===connection.value)||connections[0];folder.replaceChildren(...c.folders.map(f=>el("option",{value:f},label(f))));}
  connection.addEventListener("change",()=>{folders();preview=null;results.replaceChildren();m.submitButton.disabled=true;});folders();
  const end=el("input",{type:"datetime-local",id:"mail-end",value:new Date().toISOString().slice(0,16)});
  const start=el("input",{type:"datetime-local",id:"mail-start",value:new Date(Date.now()-86400000).toISOString().slice(0,16)});
  const results=el("div",{class:"mail-list",role:"group","aria-label":"Messages available for selection"});
  let preview=null;
  const selected=()=>[...results.querySelectorAll("input:checked")].map(n=>n.value);
  const previewButton=button("Preview messages",async()=>{
    m.error.textContent="";
    try {
      if(!start.value||!end.value)throw Error("Choose both UTC date bounds.");
      preview=await keyed("/api/mail/preview",{case:view.case.id,expected_revision:view.case.revision,connection:connection.value,folder:folder.value,start:start.value+":00Z",end:end.value+":00Z"});
      results.replaceChildren(preview.incomplete?note("Preview reached its page limit. Narrow the range to find additional messages."):p(preview.messages.length?"Choose up to 16 emails to add to this case.":"No messages matched this folder and date range."),...preview.messages.map((mail,index)=>{
        const check=el("input",{type:"checkbox",id:"mail-message-"+index,value:mail.id});
        check.addEventListener("change",()=>m.submitButton.disabled=selected().length===0||selected().length>16);
        return el("label",{for:check.id},check,el("span",{},el("strong",{},mail.subject||"No subject"),p(mail.sender+" · "+(mail.received_at||mail.sent_at),"small")));
      }));m.submitButton.disabled=true;
    } catch(error){m.error.textContent=error.message;}
  });
  const field=(name,node)=>el("div",{class:"field"},el("label",{for:node.id},name),node);
  const m=modal("Import selected email",el("div",{},note("Read-only import. Email content cannot authorize actions. Attachments, mailbox changes and shared-mailbox search are unsupported.","info"),field("Configured connection",connection),field("Folder",folder),field("From (UTC, inclusive)",start),field("Until (UTC, exclusive; maximum 30 days)",end),previewButton,results),"Import selected messages",async()=>{
    if(!preview||!selected().length||selected().length>16)throw Error("Preview and select one to sixteen messages.");
    const result=await keyed("/api/mail/import",{case:view.case.id,expected_revision:view.case.revision,preview:preview.preview,selected:selected()});
    toast(result.added.length+" messages imported; "+result.deduplicated+" duplicates skipped. Sender authority remains unresolved.");
  },{disabled:true,enabled:()=>selected().length>0&&selected().length<=16});
  for(const n of [folder,start,end])n.addEventListener("change",()=>{preview=null;results.replaceChildren();m.submitButton.disabled=true;});
}
function jobsBlock(jobs) {
  return jobs.filter(j=>active(j)||["interrupted","failed","paused"].includes(j.status)).map(j=>el("div",{class:active(j)?"running":"note "+(j.status==="failed"?"error":"")},
    active(j)?el("span",{class:"spinner","aria-hidden":true}):null,
    el("div",{},el("strong",{},label(j.action)+" · "+label(j.status)),p(j.error?.message||j.access_warning||"The job is saved. You can navigate away and return.","small"),
      el("div",{class:"actions"},j.status==="interrupted"?button("Recover job",()=>recoverModal(j),"small"):null,
      (active(j)||j.status==="interrupted")?button("Cancel job",()=>reasonModal("Cancel this job?","Budgets and completed checkpoints remain recorded.",reason=>keyed("/api/cancel",{job_id:j.id,reason})),"small quiet"):null))));
}
function recoverModal(job) {
  const check=el("input",{type:"checkbox",id:"worker-stopped"});
  const m=modal("Recover interrupted work",el("div",{},p("Resume this saved job using the existing investigation and remaining budget. Completed tool steps are replayed from their receipts."),el("label",{class:"check-label",for:"worker-stopped"},check,"I confirm the previous worker has stopped.")),"Recover job",async()=>{
    if(!check.checked)throw Error("Confirm that the previous worker has stopped.");
    await keyed("/api/jobs",{action:"recover",job_id:job.id,confirm_worker_stopped:true});
  },{disabled:true,enabled:()=>check.checked});
  check.addEventListener("change",()=>m.submitButton.disabled=!check.checked);
}
function reasonModal(title,description,submit) {
  const reason=el("textarea",{id:"reason",maxlength:1000,placeholder:"Explain the reason…"});
  modal(title,el("div",{},p(description),el("label",{for:"reason"},"Reason"),reason),"Confirm",async()=>{if(!reason.value.trim())throw Error("Enter a short reason.");await submit(reason.value.trim());},{danger:true});
}
function home() {
  const cases=S.state.cases.filter(c=>!c.comparison_case);
  const content=[heading("Casework","Follow the evidence from a pricing deduction to an internal review dossier.",[button("Import case",()=>importModal()),button("Open sample",sampleModal,"primary")]),jobsBlock(S.state.jobs.filter(j=>!j.case_id))];
  if(!cases.length)content.push(el("section",{class:"sheet"},el("div",{class:"empty"},el("div",{class:"empty-glyph","aria-hidden":true},"§"),el("h2",{},"Every claim needs a paper trail"),p("Bring together approvals, conditions and settlement records. Keep what is supported clear—and keep the unknowns visible."),el("div",{class:"actions"},button("Explore the sample",sampleModal,"primary"),button("Import a case",()=>importModal())))));
  else content.push(el("div",{class:"case-list"},cases.map(c=>link([el("div",{},el("div",{class:"eyebrow"},c.id+" · Revision "+c.revision),el("h2",{},c.title),el("div",{class:"case-meta"},el("span",{},c.counts.claims+" claims"),el("span",{},c.counts.evidence+" sources"),tag(c.status,c.status==="review"?"warning":""))),el("span",{class:"arrow","aria-hidden":true},"→")],"#/case/"+c.id+"/findings","case-card"))));
  shell(content);
}
function caseHref(view,tab,detail="") {return "#/case/"+view.case.id+"/"+tab+(detail?"/"+detail:"");}
function proposalRows(view,proposal) {
  return proposal?proposal.payload.rows:view.bundle.claims.map(c=>({claim:c.id,invoice:c.invoice,face_minor:c.amount,currency:c.currency,approval_minor:null,conditional_minor:null,settlement_minor:null,remaining_recorded_minor:null,holds:[],state:"Not investigated",issues:[],approval_sources:[]}));
}
function ledger(view,proposal) {
  const rows=proposalRows(view,proposal);
  return el("section",{class:"sheet"},el("div",{class:"sheet-heading"},el("div",{},el("h2",{},"Claim ledger"),p("Approval evidence and recorded settlement stay separate.")),tag(proposal?(proposal.revision!==view.case.revision?"Historical · ":"")+proposal.status:"Awaiting investigation",proposal?.status==="accepted"&&proposal.revision===view.case.revision?"":"warning")),
    el("div",{class:"summary-strip"},el("div",{},el("div",{class:"amount"},money(rows.reduce((t,r)=>t+r.face_minor,0),view.bundle.currency)),el("div",{class:"label"},"Original deductions")),el("div",{},el("div",{class:"amount"},rows.length),el("div",{class:"label"},"Claims to review")),el("div",{},el("div",{class:"amount"},view.bundle.evidence.length),el("div",{class:"label"},"Current sources"))),
    el("div",{class:"ledger-wrap",tabindex:"0","aria-label":"Claim ledger, scroll horizontally for every amount"},el("table",{class:"ledger"},el("caption",{class:"sr-only"},"Claim ledger in "+view.bundle.currency+"; unknown amounts remain distinct from zero"),el("thead",{},el("tr",{},["Claim","Face","Approval","Conditional","Settled","Remaining","Hold"].map(c=>el("th",{scope:"col"},c)))),el("tbody",{},rows.map(r=>el("tr",{},
      el("th",{class:"claim-cell",scope:"row"},el("button",{class:"link-button claim-id",onclick:()=>claimModal(view,proposal,r)},"Claim "+r.claim),el("span",{class:"claim-note"},r.invoice),el("span",{class:"claim-note"},label(r.state))),
      ["face_minor","approval_minor","conditional_minor","settlement_minor","remaining_recorded_minor"].map(k=>el("td",{class:r[k]==null?"num-unknown":""},money(r[k],r.currency))),
      el("td",{},(r.holds||[]).length?tag("Active","warning"):proposal?"—":"Unknown")))))),
    el("div",{class:"ledger-foot"},"Scroll the ledger horizontally or focus it and use arrow keys to inspect every column. Amounts are evidence classifications for internal review. Approval is not entitlement or cash. Remaining uses recorded settlement only."));
}
function claimModal(view,proposal,row) {
  const sources=row.approval_sources||[];
  const supports=proposal?.payload.support||[];
  const all=Array.isArray(supports)?supports:Object.values(supports);
  const relevant=all.filter(s=>sources.includes(s.source)||row.holds?.includes(s.source));
  const body=el("div",{},p(row.invoice+" · "+label(row.state)),
    el("div",{class:"source-details"},["face_minor","approval_minor","conditional_minor","settlement_minor","remaining_recorded_minor"].map(k=>el("div",{},el("dt",{},label(k.replace("_minor",""))),el("dd",{},money(row[k],row.currency))))),
    (row.issues||[]).length?note(row.issues.map(label).join(" · ")):null,
    p("Action eligibility: "+label(row.action_eligibility||"Not investigated"),"small"),
    relevant.map(s=>el("div",{class:"citation"},el("strong",{},s.source),el("blockquote",{},s.quote),p("Line "+s.start_line+" · SHA-256 "+short(s.source_hash),"mono"),el("a",{href:caseHref(view,"evidence",s.source),onclick:()=>dialog.close()},"Inspect source"))),
    sources.length&&!relevant.length?sources.map(id=>link("Inspect "+id,caseHref(view,"evidence",id))):null,
    !proposal?note("Run an investigation to classify this claim and produce citations."):null);
  modal("Claim "+row.claim,body);
}
function findings(view,proposal) {
  const contents=[ledger(view,proposal)];
  if(proposal) {
    const data=proposal.payload;
    if(proposal.revision!==view.case.revision) contents.unshift(note("This proposal uses input revision "+proposal.revision+". The case is now revision "+view.case.revision+". Investigate the current evidence before acceptance.","error"));
    if(!proposal.valid)contents.unshift(note("This proposal did not pass validation. Acceptance is blocked.","error"));
    if(data.issues?.length)contents.push(section("Review cautions",data.issues.map(x=>el("div",{class:"question"},el("h3",{},typeof x==="string"?label(x):label(x.reason)),typeof x==="object"?p("Claims "+(x.claims||[]).join(", ")+" · Source "+(x.source||"not specified"),"small"):null))));
    if(data.changes?.length)contents.push(section("Changed since acceptance",el("div",{class:"source-list"},data.changes.map(c=>el("div",{},el("strong",{},"Claim "+c.claim+" · "+label(c.field.replace("_minor",""))),p(valueForField(c.field,c.before,view.bundle.currency)+" → "+valueForField(c.field,c.after,view.bundle.currency)))))));
    if(data.changes_unavailable)contents.push(note("The previous accepted comparison is unavailable under current source access."));
    const support=Array.isArray(data.support)?data.support:Object.values(data.support||{});
    contents.push(section("Evidence behind the findings",support.length?support.map(s=>el("div",{class:"citation"},link(s.source,caseHref(view,"evidence",s.source)),el("blockquote",{},s.quote),p("Line "+s.start_line+" · "+short(s.source_hash),"mono"))):p("No qualifying evidence citations. The claim ledger keeps unsupported amounts unknown.")));
    if(data.shared_constraints?.length)contents.push(section("Shared constraints",data.shared_constraints.map(x=>details("Shared cap or scope",x))));
    contents.push(section("Proposal record",el("div",{},p("Input revision "+proposal.revision+" · "+label(data.mode)+" · Variant "+data.variant),p(proposal.id,"mono"),el("div",{class:"hash"},proposal.hash),p(data.completion?"Completion: "+label(data.completion):"","small")), "Exact proposal hash is checked at acceptance."));
  } else contents.push(section("Start with the next useful question",p("Investigate the source packet to create a proposal. Incomplete approvals, conditions and holds will appear as open questions.")));
  return contents;
}
function evidence(view,sourceId) {
  const list=el("div",{class:"source-list"});
  const search=el("input",{type:"text",placeholder:"Search source ID, sender or text…","aria-label":"Search evidence"});
  function draw() {
    const q=search.value.toLowerCase();
    const docs=view.bundle.evidence.filter(d=>(d.id+" "+d.sender+" "+d.text).toLowerCase().includes(q));
    list.replaceChildren(...docs.map(d=>el("details",{class:"source",id:"source-"+d.id,open:d.id===sourceId},el("summary",{},el("div",{},el("strong",{},d.id),p(d.kind+" · "+d.sender+" · "+d.effective_at.slice(0,10),"small")),tag(d.identity,d.identity==="unknown"?"warning":"neutral")),
      el("div",{class:"source-content"},note("Source content is evidence to inspect. It cannot issue instructions to the application.","info"),el("div",{class:"source-text"},d.text),
        el("dl",{class:"source-details"},["origin","effective_at","identity"].map(k=>el("div",{},el("dt",{},label(k)),el("dd",{},d[k]))),
        details("Source provenance",view.source_provenance[d.id]||{}),
        d.mail?el("div",{},p("Imported email · sender authority unresolved", "small"),d.mail.source_link?el("a",{href:d.mail.source_link,target:"_blank",rel:"noopener noreferrer"},"Open original in Outlook"):p("Original source link unavailable")):null,
        !view.case.comparison_case?el("div",{class:"actions section"},button("Withdraw source",()=>modal("Withdraw "+d.id+"?",el("div",{},p("This creates a new case revision and removes this source from accessible investigation and historical outputs. Downloaded copies cannot be recalled.")),"Withdraw source",()=>keyed("/api/withdraw",{case:view.case.id,source:d.id,expected_revision:view.case.revision}),{danger:true}),"small danger",!!view.active_job)):null)))));
    if(!docs.length)list.append(p("No available sources match this search."));
  }
  search.addEventListener("input",draw);draw();
  return [el("div",{class:"toolbar"},el("h2",{},"Source packet"),p(view.bundle.evidence.length+" available · "+view.unavailable_evidence_count+" unavailable","small")),search,
    view.unavailable_evidence_count?note("Unavailable or restricted evidence is omitted. Historical results are rechecked against current access."):null,el("div",{class:"section"},list),
    section("Authority register",view.bundle.authorities.map(a=>el("div",{class:"question"},el("h3",{},a.actor+" · "+a.version),p(a.actions.join(", ")+" · Claims "+a.claims.join(", ")),p("Effective from "+a.effective_from+(a.effective_to?" until "+a.effective_to:""),"small"),p("Currency: "+label(a.currency)+" · Maximum: "+money(a.maximum_minor,a.currency||view.bundle.currency)+" · Invalidates prior authority: "+(a.invalidates_prior?"Yes":"No"),"small"),p(a.source,"small")))),
    section("Recorded settlement events",view.bundle.settlements.length?view.bundle.settlements.map(s=>details(s.id+" · "+label(s.kind)+" · "+money(s.amount,s.currency),s)):p("No settlement events have been recorded."))];
}
function questions(view,proposal) {
  const run=view.runs[0];
  const requests=run?.requests||[];
  const drafted=proposal?.payload.inquiries||[];
  return [note(view.bundle.synthetic?"These are internal question drafts and frozen synthetic replies. Nothing is sent to a merchant.":"These are question drafts. Obtain evidence independently and import selected replies; no email is sent.","info"),
    el("section",{class:"sheet"},el("div",{class:"sheet-heading"},el("div",{},el("h2",{},"Next questions"),p("From the latest investigation run.")),tag(requests.length+" requested facts","warning")),
      requests.length?requests.map(q=>el("article",{class:"question"},el("div",{class:"claim-meta"},"Claims "+q.claims.join(", ")+" · "+label(q.owner)),el("h3",{},q.proposition),p(q.draft))):el("div",{class:"sheet-body"},p("No pending requests on the latest run. Investigate current evidence to find unresolved facts.")),
      requests.length&&view.bundle.synthetic&&!view.case.comparison_case?el("div",{class:"sheet-body"},button("Apply frozen replies",()=>replyModal(run),"primary",!!view.active_job||run.revision!==view.case.revision)):null),
    run&&run.revision!==view.case.revision?note("These questions describe input revision "+run.revision+". Investigate the current evidence to update the drafts."):null,
    drafted.length?section("Remaining review questions",drafted.map(q=>el("div",{class:"question"},el("h3",{},"Claim "+q.claim+" · "+label(q.owner)),p((q.propositions||[]).join("; ")),tag(q.status,"neutral")))):null,
    section("Simulated reply history",view.reply_history.length?view.reply_history.map((h,i)=>details("Round "+(h.round||i+1)+" · "+label(h.status||h.outcome||"recorded"),h)):p("No replies have been applied."))];
}
function replyModal(run) {
  const input=jsonInput("Leave JSON empty to use the sample’s frozen reply map. A custom map must match this case and stays fixed within the investigation.");
  modal("Apply a reply round",input.node,"Apply replies",async()=>{
    const body={run:run.id};if($("#json-input").value.trim())body.reply_map=input.read();
    await keyed("/api/replies",body);toast("Replies recorded as a new evidence revision. Investigate again to update the findings.");
  });
}
function historyView(view) {
  const timeline=items=>el("div",{class:"timeline"},items.map(item=>el("div",{class:"timeline-item"},el("div",{class:"timeline-track"}),el("div",{},item))));
  return [section("Proposal versions",timeline(view.proposals.length?view.proposals.map(pr=>[el("h3",{},"Input revision "+pr.revision),tag(pr.status,pr.status==="review"?"warning":""),p(pr.id+" · "+short(pr.hash),"mono"),el("div",{class:"actions"},link("Inspect this proposal",caseHref(view,"findings",pr.id),"button small"))]):[[p("No proposals yet.")]])),
    section("Accepted dossier history",view.dossiers.length?timeline(view.dossiers.map(d=>[el("h3",{},"Dossier "+d.revision+" · "+d.status),p(d.hash,"mono"),p("Accepted by local operator "+label(d.accepted_by)+" at "+label(d.accepted_at),"small"),link("Inspect accepted proposal",caseHref(view,"findings",d.proposal_id))])):p("A human has not accepted a dossier.")),
    section("Investigation runs",view.runs.length?view.runs.map(r=>el("div",{class:"question"},el("h3",{},"Variant "+r.variant+" · Revision "+r.revision+" · "+label(r.status)),p(r.id,"mono"),p("Started "+r.started+" · "+label(r.mode)+" · "+label(r.metadata.execution),"small"),p("Investigation "+r.investigation,"mono"),
      r.access_warning?note(r.access_warning):null,details("Tool checkpoints ("+r.steps.length+")",r.steps),details("Run metadata",r.metadata),
      r.status==="running"&&!view.active_job&&!view.case.comparison_case?button("Recover stopped run",()=>stoppedRunModal(r),"small"):null)):p("No runs recorded.")),
    section("Evidence revisions",timeline(view.snapshots.map(s=>[el("h3",{},"Revision "+s.revision),p(s.recorded_at||"Recorded locally","small"),p(s.hash,"mono")]))),
    section("Operational audit",view.audit?.length?timeline(view.audit.map(e=>[el("h3",{},label(e.event.replaceAll("."," "))+" · "+label(e.outcome)),p(e.recorded_at+" · Revision "+label(e.revision)+" · Operator reference "+short(e.actor_ref),"small"),e.reason?p("Reason: "+label(e.reason)):null,details("Correlated hashes and budgets",e.details)])):p("No operational events recorded for this case."),"Sanitized application events. Local SQLite and hashes are not tamper-proof."),
    section("Job history",view.jobs.map(j=>details(label(j.action)+" · "+j.status+" · "+j.id,j)))];
}
function stoppedRunModal(run) {
  const check=el("input",{type:"checkbox",id:"stopped-run"});
  const m=modal("Recover a stopped run",el("label",{class:"check-label",for:"stopped-run"},check,"I confirm the original worker has stopped. Reuse its checkpoints and remaining budget."),"Recover run",async()=>{if(!check.checked)throw Error("Confirm the original worker has stopped.");await keyed("/api/jobs",{action:"recover",run:run.id,confirm_worker_stopped:true});},{disabled:true,enabled:()=>check.checked});
  check.addEventListener("change",()=>m.submitButton.disabled=!check.checked);
}
function chooseInvestigation(view) {
  return view.investigations.find(i=>i.id===S.selectedInvestigation)||view.investigations[0]||null;
}
function executionChoice(view) {
  const inv=chooseInvestigation(view), run=inv?view.runs.find(r=>r.investigation===inv.id):null;
  const context=JSON.stringify([view.case.id,inv?.id||null]);
  if(run){S.variant=run.variant;S.mode=run.mode;}
  else if(S.executionContext!==context){S.variant="C";S.mode="offline";}
  S.executionContext=context;
  return {inv,run};
}
async function startJob(view,kind,replyMap) {
  let {inv}=executionChoice(view);
  const variant=S.variant,mode=kind==="simulate"?"offline":S.mode,context=S.executionContext;
  if(!inv){const created=await keyed("/api/investigations",{case:view.case.id,expected_revision:view.case.revision});inv={id:created.investigation};if(S.executionContext===context){S.selectedInvestigation=inv.id;S.executionContext=JSON.stringify([view.case.id,inv.id]);}}
  const body={action:kind,investigation:inv.id,variant,mode,expected_revision:view.case.revision};
  if(replyMap)body.reply_map=replyMap;
  const queued=await keyed("/api/jobs",body);S.announcedJob=queued.job_id;await refresh();announceJob();
}
function sidePanel(view,proposal) {
  const {inv,run}=executionChoice(view);
  const variant=el("select",{id:"variant",disabled:!!run||!!view.active_job,onchange:e=>S.variant=e.target.value},[["C","C · Tool-driven workflow"],["B","B · Bounded baseline"],["A","A · Static baseline"]].map(([v,n])=>el("option",{value:v,selected:S.variant===v},n)));
  const switcher=el("select",{id:"investigation-choice",disabled:!!view.active_job,onchange:e=>{S.selectedInvestigation=e.target.value;refresh();}},view.investigations.map(i=>el("option",{value:i.id,selected:inv?.id===i.id},short(i.id)+" · "+i.tools+" tools used")));
  const panel=el("aside",{class:"side-panel"},el("div",{class:"eyebrow"},"Next action"),el("h2",{},proposal?"Review, then decide":"Investigate the evidence"),p("A bounded workflow reads case sources, checks authority and drafts useful questions. Human review remains required."));
  if(view.case.comparison_case) {panel.append(note("This is a frozen comparison case. Open a separate sample to explore changes."));return panel;}
  panel.append(el("div",{class:"field"},el("label",{for:"variant"},"Workflow variant"),variant));
  const mode=el("select",{id:"execution-mode",disabled:!!run||!!view.active_job,onchange:e=>{S.mode=e.target.value;}},el("option",{value:"offline",selected:S.mode==="offline"},"Offline · deterministic"),el("option",{value:"live",selected:S.mode==="live",disabled:!S.state.readiness.model_calls_enabled},"Live · configured model"));
  panel.append(el("div",{class:"field"},el("label",{for:"execution-mode"},"Execution mode"),mode),p(S.state.readiness.inference,"small"),p("Live B/C uses the selected case evidence at the configured provider. Choose a new investigation to change mode or variant.","small"));
  if(inv) panel.append(el("div",{class:"field"},el("label",{for:"investigation-choice"},"Investigation"),switcher));
  panel.append(el("div",{class:"actions"},
    button("Investigate current evidence",()=>startJob(view,"run"),"primary",!!view.active_job),
    button("Run with simulated replies",()=>{const input=jsonInput("Leave JSON empty to use the sample reply map. The workflow may apply up to two frozen reply rounds using this investigation’s remaining budget.");modal("Investigate with replies",input.node,"Start simulation",async()=>startJob(view,"simulate",$("#json-input").value.trim()?input.read():null));},"",!!view.active_job||!view.bundle.synthetic||S.mode==="live")));
  if(inv) panel.append(el("hr",{class:"divider"}),el("div",{class:"eyebrow"},"Remaining budget"),el("div",{class:"counter-group"},[["Tools","tools"],["Model attempts","attempts"],["Inquiry rounds","inquiries"]].map(([title,key])=>el("div",{},el("div",{class:"counter-row"},el("span",{},title),el("strong",{},Math.max(0,inv.limits[key]-inv[key])+" / "+inv.limits[key])),el("progress",{max:inv.limits[key],value:Math.max(0,inv.limits[key]-inv[key]),"aria-label":title+" remaining"})))),
    p("Active time remaining: "+Math.max(0,Math.ceil((inv.limits.active_ms-inv.active_ms)/1000))+" seconds. Reserved allowance: "+inv.reserved_micro+" µUSD. Actual billed cost unknown."),p("Budgets persist across replies, revisions and retries."),button("Start a new investigation",()=>modal("Start a separate investigation?",p("This creates a separate budget and preserves the full history of earlier investigations. It does not resume interrupted work."),"Create investigation",async()=>{const result=await keyed("/api/investigations",{case:view.case.id,expected_revision:view.case.revision});S.selectedInvestigation=result.investigation;S.variant="C";S.mode="offline";}),"small quiet",!!view.active_job));
  if(proposal) {
    const accepted=proposal.status==="accepted";
    const eligible=proposal.valid&&proposal.revision===view.case.revision&&proposal.status==="review"&&!view.active_job;
    panel.append(el("hr",{class:"divider"}),el("h2",{},"Human review"),p("Accept only the exact proposal you have inspected. This records an internal review, not a business authorization."),
      el("div",{class:"actions"},button(accepted&&proposal.revision===view.case.revision?"Dossier accepted":proposal.revision!==view.case.revision?"Historical proposal":"Review & accept",()=>acceptModal(view,proposal),"primary",!eligible),
        button("Reject proposal",()=>reasonModal("Reject this proposal","Record why this proposal needs another investigation.",reason=>keyed("/api/reject",{proposal:proposal.id,reason})),"quiet",!eligible)));
  }
  const dossier=view.dossiers.find(d=>d.status==="current");
  if(dossier) panel.append(el("hr",{class:"divider"}),el("h2",{},"Accepted dossier"),p("Export the currently accepted internal review."),el("div",{class:"actions"},button("Download Markdown",()=>download(view,"markdown",dossier.revision)),button("Download JSON",()=>download(view,"json",dossier.revision))));
  return panel;
}
function acceptModal(view,proposal) {
  const check=el("input",{id:"accept-check",type:"checkbox"});
  const body=el("div",{},p("Accept proposal "+short(proposal.id)+" from input revision "+proposal.revision+" for internal review."),
    note("Unresolved questions and holds remain part of the dossier. This does not approve a payment, settle a claim or contact anyone.","info"),
    proposal.payload.rows.map(r=>p("Claim "+r.claim+": approval "+money(r.approval_minor,r.currency)+" · settlement "+money(r.settlement_minor,r.currency)+" · "+label(r.state))),
    el("div",{class:"hash"},proposal.hash),el("label",{class:"check-label",for:"accept-check"},check,"I inspected the findings, citations and unresolved items, and accept this exact proposal hash."));
  const m=modal("Accept this internal dossier",body,"Accept exact proposal",async()=>{if(!check.checked)throw Error("Confirm you inspected this exact proposal.");await keyed("/api/accept",{proposal:proposal.id,hash:proposal.hash});toast("Dossier accepted. The reviewed version and hash are saved.");},{disabled:true,enabled:()=>check.checked});
  check.addEventListener("change",()=>m.submitButton.disabled=!check.checked);
}
async function download(view,format,revision) {
  const record=await api("/api/export",{case:view.case.id,format,revision});
  if(!/^\/api\/download\/[a-f0-9]{64}$/.test(record.download_url))throw Error("Unexpected local download reference.");
  const response=await fetch(record.download_url,{cache:"no-store"});if(!response.ok){const err=await response.json();throw Error(err.error?.message||"Export no longer available.");}
  const blob=await response.blob(),url=URL.createObjectURL(blob);
  const a=el("a",{href:url,download:view.case.id+"-v"+record.dossier_revision+(format==="json"?".json":".md")});document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);toast("Download requested; verify browser save completion · SHA-256 "+short(record.hash));
}
async function casePage(id,tab="findings",detail) {
  const view=await api("/api/cases/"+id);
  let proposal=view.latest_proposal;
  if(tab==="findings"&&detail){proposal=await api("/api/proposals/"+detail);if(proposal.case_id!==id)throw Error("This proposal belongs to a different case.");}

  const tabs=["findings","evidence","questions","history"];
  if(!tabs.includes(tab))throw Error("Unknown case view.");
  const content=[heading(view.case.title,"Case "+id+" · Revision "+view.case.revision+" · "+view.bundle.currency+" · "+(view.bundle.synthetic?"Synthetic evidence":"Imported / mixed evidence"),
    view.case.comparison_case?[]:[button("Import next revision",()=>importModal(view),"",!!view.active_job),button("Import selected email",()=>mailModal(view),"",!!view.active_job),standardSample(view)?button("Sample correction",()=>modal("Append the sample correction",el("div",{},p("For sample cases: append the lower approval for A, B’s satisfied condition, a repayment and its reversal. Previously accepted findings become stale."),note("This only applies when existing sample records match. Custom cases should import an amended bundle.")),"Import correction",()=>keyed("/api/demo",{id,stage:3,expected_revision:view.case.revision})),"quiet",!!view.active_job):null]),
    el("div",{class:"steps"},["Evidence","Investigate","Review","Export"].map((s,i)=>el("span",{class:i===(view.dossiers.some(d=>d.status==="current")?3:proposal&&proposal.revision===view.case.revision&&proposal.status==="review"?2:1)?"current":""},el("b",{},i+1),s))),
    note(view.bundle.policy?.description||"Approval meaning follows the supplied case procedure. Internal dossier acceptance does not authorize an external action.","info"),view.warnings.map(w=>note(w,"error")),jobsBlock(view.jobs),
    el("nav",{class:"tabs","aria-label":"Case views"},tabs.map(t=>el("a",{href:caseHref(view,t),class:"tab"+(tab===t?" active":""),"aria-current":tab===t?"page":null},t[0].toUpperCase()+t.slice(1)))),
    el("div",{class:"case-layout"},el("div",{class:"case-primary"},tab==="findings"?findings(view,proposal):tab==="evidence"?evidence(view,detail):tab==="questions"?questions(view,proposal):historyView(view)),sidePanel(view,proposal))];
  return {content,view,breadcrumb:id+" / "+label(tab)};
}
function readiness() {
  shell([heading("Readiness","The offline workflow is ready. Live inference and mail access require explicit server configuration."),
    el("div",{class:"readiness-grid"},[["Local workflow","Ready","Offline parsing, bounded tools, simulated replies and human dossier review."],["Runtime inference",S.state.readiness.model_calls_enabled?"Configured":"Blocked",S.state.readiness.inference],["neatlogs","Unverified",S.state.readiness.neatlogs],["Entire","Unverified",S.state.readiness.entire],["cfo.ai","Separate business-plan tool","cfo.ai is separate hypothetical business planning. No ApprovalBridge connection is needed; it is not a runtime integration."],["Email ingestion",S.state.readiness.mail.some(c=>c.enabled&&!c.revoked)?"Configured":"Not configured","Only the configured Microsoft own mailbox is implemented: inbox and sent items, with delegated User.Read and Mail.Read access. Gmail, shared mailboxes, attachments, sending and mailbox changes are unsupported."]].map(([title,status,desc])=>el("section",{class:"sheet readiness-item"},tag(status,status==="Ready"?"":"warning"),el("h2",{},title),p(desc)))),
    section("Mailbox connections",S.state.readiness.mail.length?S.state.readiness.mail.map(c=>el("div",{class:"question"},el("h3",{},c.id),p((c.revoked?"Disconnected; configure a new connection ID":c.enabled?"Server configuration present; access not yet verified":"Server setup required")+" · folders: "+c.folders.join(", ")),button("Disconnect and restrict imported sources",()=>reasonModal("Disconnect mailbox?","Existing imported sources will become unavailable. Saved downloads cannot be recalled.",()=>keyed("/api/mail/disconnect",{connection:c.id})),"small danger",c.revoked))):p("No mailbox is configured. Operator setup stays on the server.")),
    section("Organizer model setup",p("neatHack documents a separate shared $20 LiteLLM allowance for each checked-in team or solo participant, over 30 days. The team leader must claim it using the check-in email. No key is claimed or route enabled here; verify actual remaining allocation, expiry and per-call price bounds, plus neatlogs acknowledgement, before live use.")),
    section("Input coverage",p("Case JSON import is available. Email is one evidence channel; retailer/distributor portal access, ERP/AR, EDI and trade-promotion integrations are not implemented. Raw invoice PDFs, CSV/Excel files and attachments are not automatically parsed. Manually normalized records remain hypothetical or owner-supplied evidence, not verified business authority.")),
    section("Inference allowance",p(S.state.readiness.allowance?"Reserved allowance remaining: "+S.state.readiness.allowance.remaining_micro+" µUSD · worst-case per call: "+S.state.readiness.allowance.call_micro+" µUSD. Provider billing and other clients' spend are not observed.":"No verified organizer inference allowance is configured.")),
    section("What these results demonstrate",p("A local workflow with author-inspected synthetic regression cases. Offline variants use deterministic parsing; comparisons do not establish model intelligence, production accuracy, partner delivery or actual model cost."))],"Readiness");
}
function comparisonModal() {
  const id=el("input",{id:"comparison-id",type:"text",value:"web-"+Date.now().toString(36),maxlength:64,required:true});
  modal("Run the frozen comparison",el("div",{},p("Six author-inspected scenarios × three variants. Every planned job remains in the report, including failures. All jobs run offline using frozen inputs, reply maps and evaluation oracles."),note("This is a regression comparison, not a blind holdout. No proposals are automatically accepted.","info"),el("div",{class:"field"},el("label",{for:"comparison-id"},"Comparison ID"),id)),"Run 18 offline jobs",async()=>{if(!/^[A-Za-z0-9_-]{1,64}$/.test(id.value))throw Error("Use a bounded comparison ID with letters, numbers, dashes or underscores.");await keyed("/api/jobs",{action:"compare",comparison_id:id.value});location.hash="#/comparisons";});
}
async function comparisons(id) {
  let report=null,error=null;
  if(id){try{report=await api("/api/comparisons/"+id);}catch(e){error=e.message;}}
  const nodes=[heading("Comparisons","Inspect correctness, missing facts and bounded tool use across the same frozen cases.",[button("Run frozen comparison",comparisonModal,"primary")]),jobsBlock(S.state.jobs.filter(j=>j.comparison_id)),error?note(error,"error"):null];
  if(report) {
    nodes.push(el("div",{class:"toolbar"},el("h2",{},report.comparison),tag(report.status,report.passed?"":"warning")),
      note(report.retention_decision,"info"),el("section",{class:"sheet"},el("div",{class:"summary-strip"},
        p(report.jobs.filter(j=>j.quality_passed).length+" / "+report.planned_jobs+" jobs passed"),
        p(report.model_attempts+" model attempts"),p("Reserved "+money(report.reserved_micro_usd/10000)+" · actual billed unknown"))),
      section("Every planned job",el("div",{class:"ledger-wrap",tabindex:"0","aria-label":"Comparison results"},el("table",{class:"ledger comparison-table"},el("caption",{class:"visually-hidden"},"Frozen comparison results; every planned scenario and variant"),el("thead",{},el("tr",{},["Case","Variant","Status","Quality","Tools","Attempts","Inquiries","Missing / extra"].map(x=>el("th",{scope:"col"},x)))),el("tbody",{},report.jobs.map(j=>el("tr",{},el("th",{scope:"row",class:"claim-cell"},j.scenario+" / "+j.variant),el("td",{},j.variant),el("td",{},j.status),el("td",{class:j.quality_passed?"pass":"fail"},j.quality_passed?"Passed":"Failed"),el("td",{},j.tools??"—"),el("td",{},j.model_attempts??"—"),el("td",{},j.inquiry_rounds??"—"),el("td",{},(j.missing_requests?.length??0)+" / "+(j.unnecessary_requests?.length??0)))))))),
      section("Job evidence and failures",report.jobs.map(j=>details(j.scenario+" / "+j.variant+" · "+(j.quality_passed?"passed":"failed")+" · "+label(j.status),j))),
      section("Limitations",report.limitations.map(x=>p(x))),section("Frozen protocol receipt",el("div",{},p("Execution: "+label(report.execution)+" · model: "+label(report.model)),p("Plan "+report.plan_hash,"mono"),p("Receipt "+report.receipt_hash,"mono"))));
  }
  nodes.push(section("Saved comparisons",S.state.comparisons.length?el("div",{class:"case-list"},S.state.comparisons.map(c=>link([el("div",{},el("h3",{},c.id),p(c.status+" · "+c.model_attempts+" attempts","small")),el("span",{class:"arrow"},"→")],"#/comparisons/"+c.id,"case-card"))):p("No comparisons recorded. Run the frozen suite to inspect all three variants.")));
  return {content:nodes,breadcrumb:"Comparisons"};
}
async function refresh() {
  const serial=++S.serial, previousFocus=focusReference(document.activeElement);
  $("#main")?.setAttribute("aria-busy","true");
  const route=location.hash.replace(/^#\/?/,"").split("/").filter(Boolean);
  S.route=route;
  try {
    const state=await api("/api/state");if(serial!==S.serial)return;S.state=state;
    if(route[0]==="case") {
      if(!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(route[1]||""))throw Error("Invalid case ID.");
      const result=await casePage(route[1],route[2]||"findings",route[3]);if(serial!==S.serial)return;S.view=result.view;shell(result.content,result.breadcrumb);if(route[2]==="evidence"&&route[3])document.getElementById("source-"+route[3])?.scrollIntoView({block:"start"});
    } else if(route[0]==="comparisons") {const result=await comparisons(route[1]);if(serial!==S.serial)return;shell(result.content,result.breadcrumb);}
    else if(route[0]==="readiness")readiness();
    else if(!route[0])home();
    else throw Error("This page does not exist.");
    announceJob();
    const routeKey=route.join("/");if(S.renderedRoute!==null){if(S.renderedRoute!==routeKey)$("#main")?.focus();else if(previousFocus&&!dialog.open)restoreFocus(previousFocus);}S.renderedRoute=routeKey;
    document.title=(S.view&&route[0]==="case"?S.view.case.title:"ApprovalBridge")+" · Local review";
  } catch(e) {if(serial===S.serial)shell(el("div",{class:"error-page"},el("h1",{},"This view is unavailable"),p(e.message),el("div",{class:"actions section"},button("Refresh saved state",refresh,"primary"),link("Back to casework","#/","button"))),"Unavailable");}
  $("#main")?.setAttribute("aria-busy","false");
  schedulePoll();
}
function announceJob(){
  if(!S.announcedJob)return;
  const job=S.state?.jobs.find(j=>j.id===S.announcedJob);if(!job)return;
  if(active(job))toast("Investigation "+job.status+". Checkpoints and budgets are saved.");
  else {toast(job.status==="complete"?"Investigation complete. Review the saved findings.":"Investigation "+label(job.status)+". Inspect saved state before retrying.",job.status==="failed");S.announcedJob=null;}
}
function schedulePoll() {
  clearTimeout(S.poll);
  if(!S.state?.jobs.some(active))return;
  S.poll=setTimeout(async()=>{
    try{
      const state=await api("/api/state");
      const changed=JSON.stringify(state.jobs.map(j=>[j.id,j.status]))!==JSON.stringify(S.state.jobs.map(j=>[j.id,j.status]));
      if(changed&&!dialog.open&&!S.busy){await refresh();return;}
      S.state={...S.state,csrf:state.csrf};
    }catch(e){toast("Connection interrupted. Saved work remains on the server; refresh before retrying.",true);}
    schedulePoll();
  },1200);
}
window.addEventListener("hashchange",()=>{if(dialog.open)dialog.close();refresh();});
window.addEventListener("pageshow",e=>{if(e.persisted)refresh();});
refresh();
