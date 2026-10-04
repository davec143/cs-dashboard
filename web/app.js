'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const num = n => Number(n).toLocaleString();
let people = [], systemStatus = null;
let data, session, view = 'overview', reviewSource = 'pilot', overviewSource = 'management', page = 0;
let filters = {agent:'',issue:'',query:''};
const titles = {
 overview:['Quality you can act on.','See the health of your data before judging the quality of your calls.'],
 review:['Every score needs a source.','Review flagged history and inspect evidence from the new evaluation workflow.'],
 coaching:['Turn insight into practice.','Give each opportunity an owner, a concrete behavior, and a follow-up.'],
 users:['People & access.','Manage individual accounts and their access to this workspace.'],
 health:['Know what is getting through.','Track delivery, processing, and validation independently from agent performance.']};
async function api(path, body) {
 const r = await fetch(path,body ? {method:'POST',headers:{'Content-Type':'application/json','X-QA-CSRF':session?.csrf || ''},body:JSON.stringify(body)} : {});
 const result = await r.json(); if (!r.ok) { if(r.status===401 && path!=='/api/login'){data=null;people=[];systemStatus=null;$('#workspace').hidden=true;$('#login').hidden=false;$('#detail').close();$('#detail-body').replaceChildren();} throw Error((result.error || 'Request failed').replaceAll('_',' ')); } return result;
}
function metric(label, value, foot, warn=false) {return `<div class="card"><div class="metric-label">${label}</div><div class="metric-value">${value}</div><div class="metric-foot ${warn?'warn':''}">${foot}</div></div>`;}
function uniqueLegacy() {
 const dup = new Set(data.audit.duplicatedCalls.map(x=>x[0]));
 const math = new Set(data.audit.totalDisagreement.map(x=>x.callId));
 const junk = new Set(data.audit.junkRows.map(x=>x.call_id));
 const missing = new Set(data.audit.evaluatedMissingTotal);
 const grouped = new Map();
 for(const row of data.legacy) {
  if(!grouped.has(row.call_id)) grouped.set(row.call_id,{...row,rows:[],issues:[]});
  grouped.get(row.call_id).rows.push(row.row);
 }
 for(const r of grouped.values()) {
  if(dup.has(r.call_id)) r.issues.push('duplicate');
  if(math.has(r.call_id)) r.issues.push('total mismatch');
  if(junk.has(r.call_id)) r.issues.push('invalid output');
  if(missing.has(r.call_id)) r.issues.push('missing scores');
  if(r.flags.includes('hannahMention')) r.issues.push('Hannah mentioned');
 }
 return [...grouped.values()];
}
function historicalOverview() {
 const a=data.audit;
 if(!a.rows) return `<div class="card empty">Historical audit records have not been imported. New calls will appear in the management overview after the pipeline is connected.</div>`;
 const bars=Object.entries(a.monthly).map(([month,count])=>`<div class="bar-group"><span class="count">${count}</span><div class="bar" data-count="${count}"></div><span class="bar-label">${new Date(month+'-02').toLocaleDateString('en-US',{month:'short'})}</span></div>`).join('');
 const agents=Object.entries(a.agents).sort((a,b)=>b[1].rows-a[1].rows).map(([name,s])=>`<tr><td><strong>${esc(name)}</strong></td><td>${s.rows}</td><td>${s.evaluated}</td><td class="score score-muted">${s.mean.toFixed(1)}%</td><td><span class="tag">Legacy · needs validation</span></td></tr>`).join('');
 return `<div class="metrics">${metric('Unique calls in history',num(a.uniqueCalls),`${num(a.rows)} rows · ${num(a.duplicateRows)} duplicate rows`)}${metric('Marked evaluated',num(a.types.Evaluated),'Classification has not been verified')}${metric('Totals that disagree',a.totalDisagreementCount,'Verify the original assessment',true)}${metric('Latest historical call',esc(a.latestCallDate||'—'),'Historical snapshot',true)}</div>
 <div class="grid-two"><div class="card"><div class="section-head"><div><h2>Call volume over time</h2><p>Raw rows per active month</p></div><span class="pill">Legacy history</span></div><div class="bar-chart" aria-label="Monthly call row counts">${bars}</div></div>
 <div class="card"><div class="section-head"><div><h2>What needs attention</h2><p>Data quality is part of service quality.</p></div><span class="pill">Audit findings</span></div>
 <div class="issue"><div class="issue-number">${a.totalDisagreementCount}</div><div><strong>Totals differ from the stated rubric</strong><p>Calculate scores in code before they reach a report.</p></div></div>
 <div class="issue"><div class="issue-number">${a.junkScreen.junkUnion}</div><div><strong>Outputs flagged as unusable</strong><p>Heuristic screen: refusals, missing-input requests, null files.</p></div></div>
 <div class="issue"><div class="issue-number">${data.legacy.filter(r=>r.flags.includes('hannahMention')).length}</div><div><strong>AI assistant mentioned</strong><p>Verify human attribution before assigning coaching.</p></div></div></div></div>
 <div class="card wide"><div class="section-head"><div><h2>Team coverage</h2><p>Historical averages are shown for audit comparison. They are not a calibrated ranking.</p></div><button class="link-button" data-go="review">Explore call history →</button></div><div class="table-wrap"><table><thead><tr><th>Agent</th><th>Raw rows</th><th>Marked evaluated</th><th>Historical average</th><th>Confidence in score</th></tr></thead><tbody>${agents}</tbody></table></div><div class="callout"><p><b>Management view:</b> interpret quality alongside coverage, call purpose, sample size, and review status. The new pipeline counts only validated evaluations.</p></div></div>`;
}
function managementOverview() {
 const p=data.pilot,s=p.summary,total=Object.values(p.states).reduce((a,b)=>a+b,0),checked=p.validated_count;
 const dims=Object.entries(s.dimensions).map(([name,x])=>`<tr><td>${esc(name.replaceAll('_',' '))}</td><td>${x.mean.toFixed(1)}%</td><td>${x.count}</td></tr>`).join('');
 const teams=Object.entries(s.agents).sort((a,b)=>a[0].localeCompare(b[0])).map(([name,x])=>`<tr><td><strong>${esc(name)}</strong></td><td>${x.count}</td><td>${x.mean.toFixed(1)}%</td><td><span class="tag">Model estimate · calibration pending</span></td></tr>`).join('');
 const themes=Object.entries(s.themes).sort((a,b)=>b[1]-a[1]).map(([name,n])=>`<div class="issue"><div class="issue-number">${n}</div><div><strong>${esc(name.replaceAll('_',' '))}</strong><p>Evidence-backed feedback items. Inspect the conversation before assigning practice.</p></div></div>`).join('');
 const days=Object.entries(s.daily).sort((a,b)=>a[0].localeCompare(b[0])).slice(-14).map(([day,x])=>`<tr><td>${day}</td><td>${x.count}</td><td>${x.mean.toFixed(1)}%</td></tr>`).join('');
 return `<div class="metrics">${metric('Calls received',total,data.demo?'3 synthetic workflow examples':'All stored call IDs')}${metric('Evidence checks passed',checked,total?((checked/total*100).toFixed(1)+'% of received calls'):'No calls received')}${metric('Needs review / failed',(p.states.review||0)+(p.states.error||0),'Review separately from agent quality',true)}${metric('Model quality estimate',s.mean===null?'—':s.mean+'%',data.demo?'Synthetic fixture · not a live score':'Applicable behavior dimensions only')}</div>
 <div class="grid-two"><div class="card"><div class="section-head"><div><h2>Where practice can help</h2><p>Recurring feedback from evaluations that passed evidence checks.</p></div><button class="link-button" data-go="coaching">Open coaching →</button></div>${themes||'<p class="empty">No evidenced opportunities yet.</p>'}</div><div class="card"><div class="section-head"><div><h2>Behavior dimensions</h2><p>Different dimensions have different applicable sample sizes.</p></div></div><table><thead><tr><th>Behavior</th><th>Average</th><th>Applicable calls</th></tr></thead><tbody>${dims||'<tr><td colspan="3" class="empty">No validated dimensions yet.</td></tr>'}</tbody></table></div></div>
 <div class="card wide"><div class="section-head"><div><h2>Team coaching coverage</h2><p>Compare like call purposes and time periods. These all-time model estimates need human calibration.</p></div><button class="link-button" id="overview-pilot">Inspect evidence →</button></div><table><thead><tr><th>Agent</th><th>Checked calls</th><th>Estimate</th><th>Interpretation</th></tr></thead><tbody>${teams||'<tr><td colspan="4" class="empty">No evidence-checked calls yet.</td></tr>'}</tbody></table></div>
 <div class="grid-two"><div class="card"><h2>Daily quality and sample size</h2><p class="small">Latest 14 active dates · UTC · checked evaluations only</p><table><thead><tr><th>Call date</th><th>Calls</th><th>Estimate</th></tr></thead><tbody>${days||'<tr><td colspan="3">Awaiting calls</td></tr>'}</tbody></table></div><div class="card"><h2>Call mix and freshness</h2><p class="small">Call purpose affects the applicable rubric.</p>${Object.entries(s.call_types).map(([kind,n])=>`<p>${esc(kind.replaceAll('_',' '))}: <strong>${n}</strong></p>`).join('')}<h3>Latest checked evaluation</h3><p>${when(s.latest_evaluation)}</p><h3>Coaching actions</h3><p>Open / practicing: ${(s.task_states.open||0)+(s.task_states.practicing||0)} · Verified: ${s.task_states.verified||0}</p><div class="callout"><p>Passing an evidence check verifies quoted text and score arithmetic. Human review is still needed to validate whether the interpretation is fair.</p></div></div></div>`;
}
function overview() {return `<div class="source-tabs"><button id="management-tab" class="${overviewSource==='management'?'active':''}">Management overview</button><button id="history-overview-tab" class="${overviewSource==='legacy'?'active':''}">Historical audit</button></div>${overviewSource==='management'?managementOverview():historicalOverview()}`;}
function legacyReview() {
 let rows=uniqueLegacy().filter(r=>(!filters.agent||r.agent===filters.agent)&&(!filters.issue||r.issues.includes(filters.issue))&&(!filters.query||r.call_id.includes(filters.query)));
 rows.sort((a,b)=>b.issues.length-a.issues.length||b.date.localeCompare(a.date));
 const visible=rows.slice(page*20,page*20+20);
 return `<div class="card"><div class="section-head"><div><h2>Historical review queue</h2><p>One entry per call ID. Conflicting rows are preserved for investigation.</p></div><span class="pill">${rows.length} matching calls</span></div>
 <div class="filters"><label>Agent<select id="agent-filter"><option value="">All agents</option>${Object.keys(data.audit.agents).map(a=>`<option ${filters.agent===a?'selected':''}>${esc(a)}</option>`).join('')}</select></label><label>Finding<select id="issue-filter"><option value="">All findings</option>${['duplicate','total mismatch','invalid output','missing scores','Hannah mentioned'].map(i=>`<option ${filters.issue===i?'selected':''}>${i}</option>`).join('')}</select></label><label>Call ID<input id="call-search" value="${esc(filters.query)}" placeholder="Search by call ID"></label></div>
 <div class="table-wrap"><table><thead><tr><th>Call / date</th><th>Agent</th><th>Legacy total</th><th>Audit findings</th><th></th></tr></thead><tbody>${visible.map(r=>`<tr><td><strong>${r.call_id}</strong><br><span class="small">${r.date}</span></td><td>${esc(r.agent)}</td><td class="score-muted">${r.legacy_total===null?'—':r.legacy_total+'%'}</td><td>${r.issues.length?r.issues.map(i=>`<span class="tag">${i}</span>`).join(''):'<span class="small">Not screened as defective</span>'}</td><td><button class="link-button" data-legacy-id="${r.call_id}">Inspect →</button></td></tr>`).join('')||'<tr><td colspan="5" class="empty">No calls match these filters.</td></tr>'}</tbody></table></div>
 <div class="nav-page"><span>${rows.length?`${page*20+1}–${Math.min((page+1)*20,rows.length)}`:'0'} of ${rows.length}</span><div><button class="plain" id="prev" ${page===0?'disabled':''}>Previous</button> <button class="plain" id="next" ${(page+1)*20>=rows.length?'disabled':''}>Next</button></div></div></div>`;
}
function pilotReview() {
 const p=data.pilot;
 return `<div class="card"><div class="section-head"><div><h2>${data.demo?'Synthetic workflow examples':'Shadow pilot calls'}</h2><p>${data.demo?'These three test records demonstrate validation, review, and exclusion. They are not real calls.':'Latest 500 updated calls shown. Aggregates cover all stored calls; only checked evaluations contribute to averages.'}</p></div></div><div class="table-wrap"><table><thead><tr><th>Call</th><th>Agent</th><th>Status</th><th>Total</th><th></th></tr></thead><tbody>${p.calls.map(c=>{const e=p.evaluations.find(e=>e.call_id===c.id);return `<tr><td>${c.id}${c.metadata.source_url?`<br><a class="link-button" href="${esc(c.metadata.source_url)}" target="_blank" rel="noopener noreferrer">Open Aircall source ↗</a>`:''}</td><td>${esc(c.metadata.agent)}</td><td><span class="tag ${c.state==='validated'?'good':''}">${esc(c.state)}</span>${e?.human_review?`<br><span class="tag">${esc(e.human_review.decision)}</span>`:''}<br><span class="small">${esc(c.last_error||'')}</span></td><td>${e&&e.eligible?e.total+'%':'—'}</td><td>${e&&session.role==='admin'?`<button class="link-button" data-eval-id="${e.id}">View evidence →</button>`:''}${session.role==='admin'&&data.processing_enabled&&['error','review'].includes(c.state)?`<button class="link-button" data-retry-id="${esc(c.id)}">Retry processing</button>`:''}</td></tr>`;}).join('')||'<tr><td colspan="5" class="empty">No pilot calls yet. Connect and validate the shadow workflow first.</td></tr>'}</tbody></table></div></div>`;
}
function review() {return `<div class="source-tabs"><button id="legacy-tab" class="${reviewSource==='legacy'?'active':''}">Historical audit</button><button id="pilot-tab" class="${reviewSource==='pilot'?'active':''}">${data.demo?'Sample workflow':'Shadow pilot'}</button></div>${reviewSource==='legacy'?legacyReview():pilotReview()}`;}
function coaching() {
 const p=data.pilot, valid=p.evaluations.filter(e=>e.eligible), ts=p.summary.task_states;
 const themes={}; for(const e of valid) for(const i of e.result?.improvements||[]) themes[i.theme]=(themes[i.theme]||0)+1;
 return `<div class="metrics">${metric('Validated evaluations',p.validated_count,data.demo?'Synthetic example only':'Evidence gate passed')}${metric('Open coaching actions',(ts.open||0)+(ts.practicing||0),'Assigned to a lead for follow-through')}${metric('Verified actions',ts.verified||0,'Lead confirmed completion')}${metric('Top opportunity',Object.keys(themes).sort((a,b)=>themes[b]-themes[a])[0]||'—',data.demo?'From the synthetic example':'From validated call feedback')}</div><div class="card wide"><div class="section-head"><div><h2>Start with the conversation</h2><p>Inspect the source and create one focused action for the next call.</p></div><button class="link-button" id="go-pilot">View ${data.demo?'sample':'pilot'} calls →</button></div><p class="small">Latest 500 actions shown; counts cover all actions. Creating an action does not send a message to an agent.</p></div><div class="task-grid">${p.tasks.map(t=>`<div class="card task-card"><span class="pill">${esc(t.status)}</span><h3>${esc(t.owner)}</h3><p>${esc(t.action)}</p><div class="due">Due ${esc(t.due)} · Evaluation ${t.evaluation_id}</div>${t.eligible===false?'<p class="notice">Source assessment is disputed or no longer current. Review the call before further coaching.</p>':''}<label>Progress<select data-task-id="${t.id}">${(t.eligible===false?[t.status,...(t.status==='dismissed'?[]:['dismissed'])]:['open','practicing','verified','dismissed']).map(s=>`<option ${s===t.status?'selected':''}>${s}</option>`).join('')}</select></label></div>`).join('')||'<div class="card empty">No coaching actions yet. Open a validated call to assign a next step.</div>'}</div>`;
}
const when = n => n?new Date(Number(n)*1000).toLocaleString('en-US'):'Not connected';
function health() {
 const p=data.pilot;
 return `${systemHealth()}<div class="status-grid"><div class="card"><div class="metric-label">Processing</div><div class="status-text">${data.demo?'Synthetic demo':data.processing_enabled?'Enabled':'Paused'}</div><p class="small">${data.demo?'Local demonstration only.':data.processing_enabled?'Verify receipt, worker freshness, and completed evaluations below.':'Provider setup and controlled call verification are required before activation.'}</p></div><div class="card"><div class="metric-label">Calls requiring attention</div><div class="status-text">${(p.states.error||0)+(p.states.review||0)}</div><p class="small">Failed processing and calls awaiting review remain visible.</p></div><div class="card"><div class="metric-label">Historical audit</div><div class="status-text">${num(data.audit.uniqueCalls)} unique calls</div><p class="small">Historical scores need source verification before coaching.</p></div></div>
 <div class="card wide"><div class="section-head"><div><h2>New pipeline health</h2><p>${data.demo?'Demo is isolated from Aircall and cannot accept real-call webhooks.':'Webhook receipts, durable jobs, and reconciliation are independently visible.'}</p></div><span class="pill">${data.demo?'Demo':'Shadow'}</span></div><table><tbody><tr><td>Last accepted event</td><td>${data.demo?'Synthetic seed':when(p.last_event)}</td></tr><tr><td>Worker heartbeat</td><td>${when(p.worker_heartbeat)}</td></tr><tr><td>Last completed reconciliation</td><td>${when(p.last_reconcile)}</td></tr><tr><td>Reconciliation error</td><td>${esc(p.reconcile_error||'None recorded')}</td></tr><tr><td>Calls by processing state</td><td>${Object.entries(p.states).map(([s,n])=>`${esc(s)}: ${n}`).join(' · ')||'No calls received'}</td></tr><tr><td>Oldest queued job</td><td>${when(p.oldest_queued)}</td></tr></tbody></table><div class="callout"><p>Receiving a webhook is not the same as evaluating a call. Monitor expected working hours, compare Aircall call IDs with stored IDs, and alert on a stale worker or incomplete reconciliation.</p></div></div>
 <div class="card"><h2>Release gates</h2><p class="sub">Keep the replacement in shadow mode until these are verified.</p><div class="issue"><div class="issue-number">01</div><div><strong>End-to-end delivery</strong><p>A new Aircall call reaches one stored record, including webhook retries.</p></div></div><div class="issue"><div class="issue-number">02</div><div><strong>Human calibration</strong><p>Two reviewers score a balanced sample; compare evidence, applicability, and repeatability.</p></div></div><div class="issue"><div class="issue-number">03</div><div><strong>Access and recovery</strong><p>Private management access, tested backup restore, monitored worker, and rollback.</p></div></div></div>`;
}
function render() {
 if(!data) return;
 $('#page-title').textContent=titles[view][0]; $('#page-subtitle').textContent=titles[view][1];
 document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
 $('#content').innerHTML=({overview,review,coaching,health,users})[view](); bind();
}
function legacyDetail(cid) {
 const r=uniqueLegacy().find(r=>r.call_id===cid);
 const mismatches=data.audit.totalDisagreement.filter(x=>x.callId===cid);
 $('#detail-body').innerHTML=`<div class="eyebrow">HISTORICAL AUDIT · UNVALIDATED</div><h1>Call ${cid}</h1><p>${esc(r.agent)} · ${r.date}</p><div class="notice"><div><b>Review the original before coaching</b>Automated findings identify records to investigate; they do not establish agent performance.</div></div><h3>Source rows</h3><p>Sheet2 rows ${r.rows.join(', ')}. All historical entries are preserved.</p><h3>Audit findings</h3><p>${r.issues.map(esc).join(' · ')||'No heuristic finding. This is not evidence of a valid evaluation.'}</p>${mismatches.map(m=>`<p>Reported total: ${m.reported}%. Rounded mean of the six dimensions: ${m.calculated}%.</p>`).join('')}<h3>Next step</h3><p>Verify the canonical transcript, human speaker attribution, and call purpose. Re-evaluate with the versioned rubric in a separate destination.</p><p class="small">Use the recorded source row references in your private historical workbook.</p>`; $('#detail').showModal();
}
async function evalDetail(id) {
 const e=data.pilot.evaluations.find(e=>e.id===id),r=e.result;
 if(!r) return;
 const feedback=(list,label)=>`<h3>${label}</h3>${list.map(i=>`<p><strong>${esc(i.behavior)}</strong></p>${i.evidence.map(q=>`<div class="quote">Turn ${q.turn_id}: “${esc(q.quote)}”</div>`).join('')}<p class="small">Next action: ${esc(i.next_action)}</p>`).join('')||'<p class="small">None recorded.</p>'}`;
 $('#detail-body').innerHTML=`<div class="eyebrow">${data.demo?'SYNTHETIC EXAMPLE':'EVIDENCE-CHECKED SOURCE'} · ${esc(e.rubric)}</div><h1>Call ${e.call_id}</h1><p>${esc(r.context)}</p><p class="small">Model: ${esc(e.model)} · Transcript fingerprint: ${e.fingerprint.slice(0,12)} · ${esc(e.status)}</p>${!e.current&&!data.demo?'<p class="notice">Historical assessment. The current call is queued, unresolved, or has a newer assessment.</p>':''}${(r.gate?.review_reasons||r.review_reasons||[]).length?`<div class="notice"><div><strong>Review reasons</strong>${(r.gate?.review_reasons||r.review_reasons||[]).map(x=>`<p>${esc(x)}</p>`).join('')}</div></div>`:''}<h3>Behavior dimensions</h3><table>${Object.entries(r.dimensions).map(([d,s])=>`<tr><td>${esc(d.replaceAll('_',' '))}</td><td>${s.anchor===null?'N/A':s.anchor*25+'%'}</td><td>${esc(s.reason)}${(s.evidence||[]).map(q=>`<div class="quote">Turn ${esc(q.turn_id)}: “${esc(q.quote)}”</div>`).join('')}</td></tr>`).join('')}</table>${feedback(r.strengths,'What worked')}${feedback(r.improvements,'Coaching opportunity')}<details><summary>Source conversation</summary>${(e.turns||[]).map(t=>`<div class="turn"><span class="tag">${esc(t.role)}</span> <b>${t.id}.</b> ${esc(t.text)}</div>`).join('')}</details>${humanReviewForm(e)}${e.eligible&&session.role==='admin'?`<h3>Assign one practice action</h3><form id="coaching-form" class="coaching-form"><label>Owner / CS lead<input name="owner" required placeholder="Name of lead"></label><label>Follow-up date<input name="due" type="date" required></label><label class="full">Behavior to practice<textarea name="action" required>${esc(r.improvements[0]?.next_action||'')}</textarea></label><div class="full"><button class="primary">Create coaching action</button><span id="task-message" class="small"></span></div></form>`:''}`;
 $('#detail').showModal();
 bindReview(e);
 $('#coaching-form')?.addEventListener('submit',async ev=>{ev.preventDefault();const f=new FormData(ev.target);try{await api('/api/coaching',{evaluation_id:e.id,owner:f.get('owner'),due:f.get('due'),action:f.get('action')});$('#task-message').textContent=' Saved in management workspace.';data=await api('/api/dashboard');}catch(err){($('#task-message')||$('#message')).textContent=err.message;}});
}
function bind() {
 bindUsers();
 document.querySelectorAll('[data-retry-id]').forEach(b=>b.onclick=async()=>{if(!confirm('Retry this call after fixing its processing issue? This may incur evaluation usage.'))return;b.disabled=true;try{await api('/api/replay',{call_id:b.dataset.retryId});await load();}catch(err){$('#message').textContent=err.message;b.disabled=false;}});
 document.querySelectorAll('.bar[data-count]').forEach(b=>b.style.height=Math.max(3,Number(b.dataset.count)/645*135)+'px');
 document.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>{view=b.dataset.go;render();});
 document.querySelectorAll('[data-legacy-id]').forEach(b=>b.onclick=()=>legacyDetail(b.dataset.legacyId));
 document.querySelectorAll('[data-eval-id]').forEach(b=>b.onclick=()=>evalDetail(Number(b.dataset.evalId)));
 $('#management-tab')?.addEventListener('click',()=>{overviewSource='management';render();});
 $('#history-overview-tab')?.addEventListener('click',()=>{overviewSource='legacy';render();});
 $('#overview-pilot')?.addEventListener('click',()=>{reviewSource='pilot';view='review';render();});
 $('#legacy-tab')?.addEventListener('click',()=>{reviewSource='legacy';render();});
 $('#pilot-tab')?.addEventListener('click',()=>{reviewSource='pilot';render();});
 $('#go-pilot')?.addEventListener('click',()=>{reviewSource='pilot';view='review';render();});
 $('#agent-filter')?.addEventListener('change',ev=>{filters.agent=ev.target.value;page=0;render();});
 $('#issue-filter')?.addEventListener('change',ev=>{filters.issue=ev.target.value;page=0;render();});
 $('#call-search')?.addEventListener('change',ev=>{filters.query=ev.target.value;page=0;render();});
 $('#prev')?.addEventListener('click',()=>{page--;render();}); $('#next')?.addEventListener('click',()=>{page++;render();});
 document.querySelectorAll('[data-task-id]').forEach(s=>s.onchange=async()=>{try{await api('/api/coaching/status',{id:Number(s.dataset.taskId),status:s.value});await load();}catch(err){$('#message').textContent=err.message;}});
}
async function load() {
 try {session=await api('/api/session');const named=session.auth_mode==='named';$('#email-label').hidden=!named;$('#email-label input').required=named;$('#password-label').textContent=named?'Password':'Access token';$('#login-description').textContent=named?'Sign in with your individual management account.':'Enter your private viewer or lead access token.';$('#logout').hidden=!named||!session.authenticated;$('#account-button').hidden=!named||!session.authenticated;$('#login').hidden=session.authenticated;$('#workspace').hidden=!session.authenticated;$('#users-nav').hidden=!named||session.role!=='admin';if(!session.authenticated){data=null;people=[];systemStatus=null;$('#content').replaceChildren();$('#detail').close();$('#detail-body').replaceChildren();return;}if(view==='users'&&session.role!=='admin')view='overview';
 data=await api('/api/dashboard');if(named&&session.role==='admin'){const extra=await Promise.all([api('/api/users'),api('/api/system')]);people=extra[0].users;systemStatus=extra[1];}$('#mode-label').textContent=data.demo?'Local demo · synthetic examples':'Shadow pilot';$('#mode-badge').textContent=data.demo?'Audit + synthetic demo':'Shadow pilot';$('#snapshot-date').textContent=data.audit.latestCallDate||'Not imported';
 $('#global-banner').innerHTML=`<div><b>${data.demo?'Demonstration workspace':data.processing_enabled?'Shadow pilot · calibration pending':'Setup in progress · processing paused'}</b>${data.demo?'Coaching examples use synthetic test data.':data.processing_enabled?'Review evidence and calibrate assessments before using scores for team decisions.':'The dashboard is ready. Connect provider credentials and verify a controlled call before enabling processing.'}</div><button class="plain" id="banner-health">View system health →</button>`;
 $('#banner-health').onclick=()=>{view='health';render();};$('#message').textContent='';render();
 }catch(err){$('#message').textContent='Unable to load: '+err.message;}
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{view=b.dataset.view;render();});
$('#close-detail').onclick=()=>$('#detail').close(); $('#detail').addEventListener('close',()=>{if(!$('#detail').open)$('#detail-body').replaceChildren();}); $('#refresh').onclick=load;
$('#login-form').onsubmit=async ev=>{ev.preventDefault();try{await api('/api/login',session.auth_mode==='named'?{email:new FormData(ev.target).get('email'),password:new FormData(ev.target).get('token')}:{token:new FormData(ev.target).get('token')});ev.target.reset();await load();}catch(err){$('#message').textContent=err.message;}};
$('#logout').onclick=async()=>{try{await api('/api/logout',{});data=null;await load();}catch(err){$('#message').textContent=err.message;}};
$('#account-button').onclick=()=>{
 $('#detail-body').innerHTML=`<h1>Your account</h1><p>${esc(session.email)}</p><form id="password-form" class="coaching-form"><label>Current password<input name="current_password" type="password" autocomplete="current-password" required></label><label>New password · at least 16 characters<input name="new_password" type="password" autocomplete="new-password" minlength="16" maxlength="256" required></label><button class="primary">Change password and sign out</button><p id="password-message" role="status"></p></form>`;
 $('#detail').showModal();$('#password-form').onsubmit=async ev=>{ev.preventDefault();try{const f=new FormData(ev.target);await api('/api/password',Object.fromEntries(f));$('#detail').close();data=null;await load();$('#message').textContent='Password changed. Sign in again.';}catch(err){($('#password-message')||$('#message')).textContent=err.message;}};
};
load();

function systemHealth() {
 if(!systemStatus) return '';
 const c=systemStatus.worker_configuration||{};
 const checks=[['Aircall API',c.aircall_configured],['OpenAI API',c.openai_configured],['Evaluation model',c.model_configured],['Agent scope',c.agents_configured],['Webhook token',systemStatus.webhook_configured]];
 return `<div class="card wide"><h2>Connection checklist</h2><p>Configuration presence is shown here. A real call is still needed to verify delivery and scoring.</p><table><tbody>${checks.map(([label,ok])=>`<tr><td>${label}</td><td><span class="tag ${ok?'good':''}">${ok?'Configured':'Setup needed'}</span></td></tr>`).join('')}<tr><td>Last verified backup</td><td>${when(systemStatus.backup_completed_at)}</td></tr><tr><td>Last restore drill</td><td>${when(systemStatus.restore_verified_at)}</td></tr></tbody></table>${systemStatus.alerts.map(a=>`<p class="notice">${esc(a)}</p>`).join('')}</div>`;
}
function users() {
 if(session.role!=='admin') return '<p>Administrator access is required.</p>';
 return `<div class="card wide"><h2>Create an account</h2><p>Admins can inspect call evidence, manage coaching, and administer accounts. Viewers see management summaries without transcripts or review notes.</p><form id="create-user-form" class="coaching-form"><label>Email<input name="email" type="email" required maxlength="254" autocomplete="off"></label><label>Access<select name="role"><option value="viewer">Viewer</option><option value="admin">Administrator</option></select></label><button class="primary">Create account</button><p id="users-message" role="status"></p></form></div><div class="card wide"><h2>Workspace accounts</h2><p>Access changes and password resets sign the account out of all sessions. Your own access is protected.</p><div class="table-wrap"><table><thead><tr><th>Email</th><th>Access</th><th>Status</th><th>Actions</th></tr></thead><tbody>${people.map(u=>`<tr><td>${esc(u.email)}</td><td>${esc(u.role)}</td><td>${u.enabled?'Active':'Disabled'}</td><td>${u.email===session.email?'Your account':`<button class="link-button" data-edit-user="${esc(u.email)}">Edit access</button> <button class="link-button" data-reset-user="${esc(u.email)}">Reset password</button>`}</td></tr>`).join('')}</tbody></table></div></div>`;
}
function showInitialPassword(user) {
 $('#detail-body').innerHTML=`<h1>Account ready</h1><p>${esc(user.email)} · ${esc(user.role)}</p><p>This initial password is shown once. Share it privately with the account owner and ask them to change it under Account after signing in. This screen does not send an email.</p><label>Initial password<input type="text" readonly id="initial-password" autocomplete="off"></label><p class="small">Closing this window removes the password from this page.</p>`;
 $('#initial-password').value=user.initial_password;$('#detail').showModal();
}
function bindUsers() {
 $('#create-user-form')?.addEventListener('submit',async ev=>{
  ev.preventDefault();const button=ev.target.querySelector('button');button.disabled=true;
  try{const user=await api('/api/users',Object.fromEntries(new FormData(ev.target)));await load();showInitialPassword(user);}catch(err){($('#users-message')||$('#message')).textContent=err.message;button.disabled=false;}
 });
 document.querySelectorAll('[data-reset-user]').forEach(b=>b.onclick=async()=>{
  if(!confirm(`Reset the password for ${b.dataset.resetUser}? Their current sessions will be signed out.`))return;
  b.disabled=true;try{const user=await api('/api/users/reset-password',{email:b.dataset.resetUser});await load();showInitialPassword(user);}catch(err){$('#message').textContent=err.message;b.disabled=false;}
 });
 document.querySelectorAll('[data-edit-user]').forEach(b=>b.onclick=()=>{
  const u=people.find(x=>x.email===b.dataset.editUser);
  $('#detail-body').innerHTML=`<h1>Edit account access</h1><p>${esc(u.email)}</p><form id="edit-user-form" class="coaching-form"><label>Access<select name="role"><option value="viewer" ${u.role==='viewer'?'selected':''}>Viewer</option><option value="admin" ${u.role==='admin'?'selected':''}>Administrator</option></select></label><label>Status<select name="enabled"><option value="true" ${u.enabled?'selected':''}>Active</option><option value="false" ${u.enabled?'':'selected'}>Disabled</option></select></label><button class="primary">Save access</button><p id="access-message" role="status"></p></form>`;$('#detail').showModal();
  $('#edit-user-form').onsubmit=async ev=>{ev.preventDefault();const f=new FormData(ev.target);const button=ev.target.querySelector('button');button.disabled=true;try{await api('/api/users/access',{email:u.email,role:f.get('role'),enabled:f.get('enabled')==='true'});$('#detail').close();await load();}catch(err){($('#access-message')||$('#message')).textContent=err.message;button.disabled=false;}};
 });
}
function humanReviewForm(e) {
 if(session.role!=='admin'||data.demo||!e.current)return '';
 const r=e.human_review;
 return `<h3>Human review</h3><p>${r?`${esc(r.decision)} · ${esc(r.actor)} · ${when(r.created)}`:'No human review recorded.'}</p>${r?`<p>${esc(r.note)}</p>`:''}<p class="small">Disputing or excluding this assessment removes it from quality estimates and prevents new coaching actions. Original scores and all review decisions are preserved. Approval requires the evidence checks to pass.</p><form id="review-form" class="coaching-form"><label>Decision<select name="decision"><option value="disputed">Dispute assessment</option><option value="excluded">Exclude from quality estimates</option>${e.status==='validated'?'<option value="approved">Approve assessment</option>':''}</select></label><label class="full">Reason and evidence<textarea name="note" required minlength="5" maxlength="2000" placeholder="Explain your decision and reference the relevant conversation turns."></textarea></label><button class="primary">Save review</button><p id="review-message" role="status"></p></form><details><summary>Review history</summary><div id="review-history">Loading…</div></details>`;
}
function bindReview(e) {
 const form=$('#review-form');if(!form)return;
 const target=$('#review-history');api(`/api/reviews/${e.id}`).then(result=>{if(!target?.isConnected)return;target.innerHTML=result.reviews.map(r=>`<p><strong>${esc(r.decision)}</strong> · ${esc(r.actor)} · ${when(r.created)}<br>${esc(r.note)}</p>`).join('')||'<p>No decisions yet.</p>';}).catch(()=>{if(target?.isConnected)target.textContent='Unable to load review history.';});
 form.onsubmit=async ev=>{ev.preventDefault();const button=ev.target.querySelector('button');button.disabled=true;try{await api('/api/reviews',{evaluation_id:e.id,...Object.fromEntries(new FormData(ev.target))});$('#detail').close();await load();await evalDetail(e.id);}catch(err){($('#review-message')||$('#message')).textContent=err.message;button.disabled=false;}};
}
