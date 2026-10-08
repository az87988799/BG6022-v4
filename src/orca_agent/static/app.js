'use strict';
const $ = id => document.getElementById(id);
const pretty = value => JSON.stringify(value, null, 2);
const labels = {ready:'已就绪',running:'运行中',paused:'已暂停',cancelled:'已取消',completed:'已完成',failed:'失败',unknown:'状态待核对',budget_exhausted:'预算已耗尽',waiting_user:'等待补充',unreadable:'记录不可读'};
let loaded=null, historyExpanded=false;
let token, current = localStorage.getItem('orca:selected'), polling = false, runPage = 0, artifactPage = 0, messagePage = 0;
function notice(text='') { $('notice').textContent = text; }
function element(tag, text, cls) { const node=document.createElement(tag); if(text!==undefined) node.textContent=text; if(cls) node.className=cls; return node; }
function action(label, fn) { const b=element('button',label); b.type='button'; b.onclick=()=>Promise.resolve().then(fn).catch(e=>notice(e.message)); return b; }
async function api(path, body) {
  const response=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:{'X-Local-Token':token||'','Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json(); if(!response.ok) throw new Error(data.error==='local_token_required'?'服务已重启，请刷新页面重新连接；已保存任务不会自动恢复。':data.message||data.error||'本地服务请求失败'); return data;
}
function select(id) { current=id;loaded=null;historyExpanded=false; if(id) localStorage.setItem('orca:selected',id); else localStorage.removeItem('orca:selected'); $('task').hidden=true; $('welcome').hidden=!!id; $('input-label').textContent=id?'补充条件或追问当前任务':'描述新的计算目标'; $('send').textContent=id?'发送消息 ↑':'提交任务 ↑'; $('send-hint').textContent=id?'消息保存在当前任务中；协调者停止时，点击“继续处理”。费用和预算不会重置。':'提交将按本地配置中的许可与有限预算执行。上传、偶极矩与知识检索尚未开放。'; $('artifacts').replaceChildren(); $('evidence-navigation').replaceChildren(); $('evidence').hidden=true; $('more-artifacts').hidden=true; $('conversation').replaceChildren(); messagePage=0; artifactPage=0; notice(); $('runs').querySelectorAll('button').forEach(b=>b.classList.toggle('selected',b.dataset.runId===id)); if(id){notice('正在读取所选任务…');refresh().catch(e=>notice(e.message));} }
async function listRuns(append=false) { const data=await api('runs?offset='+(append?runPage:0)); if(!append) $('runs').replaceChildren(); for(const run of data.items){const b=action('',()=>select(run.run_id));b.append(element('span',run.text||run.run_id),element('small',(labels[run.state]||run.state)+' · '+new Date(run.created_at).toLocaleString())); b.dataset.runId=run.run_id;if(run.run_id===current)b.classList.add('selected');$('runs').append(b);}runPage=data.next_offset;$('more-runs').hidden=runPage===null; }
async function history(older=false) {
 const id=current;let page;
 if(older){const offset=Math.max(0,messagePage-100);page=await api(`runs/${id}/messages?offset=${offset}&limit=${Math.min(100,messagePage)}`);historyExpanded=true;}
 else{page=await api(`runs/${id}/messages?offset=0&limit=100`);if(page.total>100)page=await api(`runs/${id}/messages?offset=${page.total-100}&limit=100`);}
 if(id!==current)return;if(!older)$('conversation').replaceChildren();
 const nodes=[];for(const m of page.items){const n=element('div',m.text,'message');n.append(element('small',m.processed?'已处理 · '+m.id:'已保存，等待处理 · '+m.id));nodes.push(n);}
 if(older)$('conversation').prepend(...nodes);else $('conversation').append(...nodes);
 if(!page.total)$('conversation').append(element('p','结构化任务，没有自然语言消息。','muted'));
 messagePage=page.offset;$('more-messages').hidden=messagePage===0;$('more-messages').textContent='读取更早消息（共 '+page.total+' 条）';
}

async function refresh() {
 if(!current||polling)return;polling=true;const id=current;
 try {const [state, report]=await Promise.all([api(`runs/${id}`),api(`runs/${id}/report`)]);if(id!==current)return;loaded=id;if($('notice').textContent==='正在读取所选任务…')notice();
 $('task').hidden=false;$('welcome').hidden=true;$('task-title').textContent=report.request?.original_text||'已有任务';$('run-id').textContent=id;$('state').textContent=labels[state.state]||state.state;
 $('activity').textContent=state.coordinator_active?'协调者正在处理 · 每 4 秒更新':state.control_requested?'已收到'+(state.control_requested==='pause'?'暂停':'取消')+'请求；实际状态见上方':'协调者未运行；只读展示已保存事实';
 if(state.web_operation?.status==='failed')notice(state.web_operation.message);
 $('goal-state').textContent=report.user_goal_complete?'已满足':'尚未全部满足';$('science-cost').textContent=`${state.usage.orca_starts_actual} / ${state.budget.orca_starts}`;$('model-cost').textContent=`${state.usage.model_calls} 次 / ${state.usage.model_tokens_used} tokens`;
 const money=report.budget?.model_cost;$('money').textContent=money?.known_cost??'—';
 $('steps').textContent=pretty({steps:state.steps,attempts:state.attempts});$('conditions').textContent=pretty(report.request);$('report').textContent=pretty(report);
 $('diagnostics').replaceChildren();if(['failed','budget_exhausted','unknown'].includes(state.state)){const failures=report.diagnostics||[];for(const d of failures.slice(-5))$('diagnostics').append(element('div',(d.category||'诊断')+'：'+(d.message==='bounded proposal correction or transport retry exhausted'?'模型建议连续未通过校验，已达到纠正上限；任务停止，已有证据保留。':d.message||'模型建议未通过程序校验；原始记录已保留。'),'question'));} $('questions').replaceChildren();for(const q of [...(report.communication?.questions||[]),...(report.communication?.notices||[])])$('questions').append(element('div',q,'question'));
 if(state.clarification?.questions)for(const q of state.clarification.questions)$('questions').append(element('div',q,'question'));
 const table=element('table');const head=element('tr');for(const t of ['目标 / 物理量','值与单位','资格与来源'])head.append(element('th',t));table.append(head);
 const facts=report.goal_facts||[];for(const fact of facts){const row=element('tr');row.append(element('td',fact.goal_id));const answer=fact.answer;if(answer?.kind==='qualified_scientific_output'){row.append(element('td',(answer.value===null?'结构文件 '+answer.artifact_id:pretty(answer.value)+' '+(answer.unit||'单位未知'))),element('td','科学检查通过 · '+(answer.check_versions||[]).join(', ')));}else if(answer){row.append(element('td',pretty(answer.observation)),element('td','原始证据观察 · 单位 '+(answer.unit||'未知')+' · 未宣称科学资格'));}else{row.append(element('td','暂无可交付结果'),element('td','证据不足或尚未完成'));}table.append(row);}
 $('results').replaceChildren(facts.length?table:element('p','尚无结果。执行失败或模型不可用时，已保存的科学证据仍会保留。','muted'));
 for(const r of report.results||[]){for(const [port,output] of Object.entries(r.qualified_outputs||{})){const box=element('details');box.append(element('summary','历史合格输出 · '+port+' · 仅适用于原条件与原规则'));box.append(element('pre',pretty({result_id:r.result_id,attempt_id:r.attempt_id,port,...output})));$('results').append(box);}}$('resume').disabled=state.coordinator_active;if(!historyExpanded)await history(false);
 } finally {polling=false;if(current&&current!==id)refresh().catch(e=>notice(e.message));}
}
async function showEvidence(append=false){const id=current;const page=await api(`runs/${id}/artifacts?offset=${append?artifactPage:0}`);if(id!==current)return;if(!append)$('artifacts').replaceChildren();for(const a of page.artifacts){const row=element('div',undefined,'file'), info=element('div'), controls=element('div');info.append(element('span',a.role+' · '+a.path),element('small',a.id+' · '+a.size+' bytes · '+a.integrity.status),element('small','SHA256 '+a.sha256));controls.append(action('查看',async()=>{await readEvidence(id,'evidence.text',{artifact_id:a.id,start_line:1,lines:80});}),action('发现字段',async()=>{await readEvidence(id,'evidence.discover',{artifact_id:a.id});}),action('下载',async()=>{const r=await fetch(`/api/runs/${id}/artifacts/${a.id}/download`,{headers:{'X-Local-Token':token}});if(!r.ok)throw new Error('文件未通过完整性核对或超过下载上限');const url=URL.createObjectURL(await r.blob());const link=element('a');link.href=url;link.download=a.path.split(/[\\/]/).pop();link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}));row.append(info,controls);$('artifacts').append(row);}artifactPage=page.next_offset;$('more-artifacts').hidden=artifactPage===null;if(!page.total)$('artifacts').append(element('p','当前任务尚无登记文件。','muted'));}
async function readEvidence(id,tool,parameters){
 const data=await api(`runs/${id}/evidence`,{tool,parameters});if(id!==current)return;
 $('evidence').hidden=false;$('evidence').textContent=pretty(data);const navigation=$('evidence-navigation');navigation.replaceChildren(element('p','证据观察：仅覆盖返回范围。字段路径来自文件发现；单位未知时不推断，也不启动补算。','muted'));
 const bar=element('div',undefined,'toolbar');
 if(tool==='evidence.discover'){
   const path=data.path||[];if(path.length)bar.append(action('上一级字段',()=>readEvidence(id,tool,{artifact_id:data.artifact_id,path:path.slice(0,-1)})));
   if(data.next_offset!==null&&data.next_offset!==undefined)bar.append(action('下一页字段',()=>readEvidence(id,tool,{artifact_id:data.artifact_id,path,offset:data.next_offset})));
   navigation.append(bar);
   for(const entry of data.entries||[]){if(!entry.addressable)continue;const name=entry.path.map(p=>p.kind==='key'?p.key:'['+p.index+']').join(' / ');navigation.append(action(name+' · '+entry.type,()=>readEvidence(id,['array','object'].includes(entry.type)?'evidence.discover':'evidence.value',{artifact_id:data.artifact_id,path:entry.path})));}
   if(!data.entries?.length&&data.status==='observed')navigation.append(action('读取该值',()=>readEvidence(id,'evidence.value',{artifact_id:data.artifact_id,path})));
 }else if(tool==='evidence.value'){
   bar.append(action('返回字段列表',()=>readEvidence(id,'evidence.discover',{artifact_id:data.artifact_id,path:(data.path||[]).slice(0,-1)})));navigation.append(bar);
 }else if(tool==='evidence.text'){
   const start=parameters.start_line||1,stop=data.coverage?.returned?.stop;
   if(start>1)bar.append(action('前 80 行',()=>readEvidence(id,tool,{artifact_id:data.artifact_id,start_line:Math.max(1,start-80),lines:80})));
   if(stop>start&&stop<=10000&&(data.lines?.length===80||data.status==='partial'))bar.append(action('继续读取后续行',()=>readEvidence(id,tool,{artifact_id:data.artifact_id,start_line:stop,lines:80})));
   navigation.append(bar);
 }
}
$('composer').onsubmit=async event=>{event.preventDefault();const text=$('text').value.trim();if(!text)return;const id=current;const recordKey='orca:pending:'+ (id||'new');let saved;try{saved=JSON.parse(localStorage.getItem(recordKey));}catch{}if(!saved||saved.text!==text)saved={text,id:'web_'+crypto.randomUUID().replaceAll('-','')};localStorage.setItem(recordKey,JSON.stringify(saved));$('send').disabled=true;
try{if(id){await api(`runs/${id}/messages`,{text,message_id:saved.id});notice('消息已保存。若协调者未运行，点击“继续处理”消费这条消息。');}else{const result=await api('runs',{text,submission_id:saved.id});select(result.run_id);if(result.start_error)notice(result.start_error);}localStorage.removeItem(recordKey);historyExpanded=false;$('text').value='';await listRuns();await refresh();}catch(e){notice(e.message+'；重试同一条消息会沿用提交编号。');}finally{$('send').disabled=false;}};
for(const command of ['resume','pause','cancel'])$(command).onclick=async()=>{if(!current||loaded!==current)return;try{await api(`runs/${current}/control`,{action:command});notice('请求已保存，实际处理结果请查看状态。');await refresh();}catch(e){notice(e.message);}};
$('new').onclick=()=>{select(null);$('text').focus();};$('refresh').onclick=()=>listRuns().catch(e=>notice(e.message));$('more-runs').onclick=()=>listRuns(true).catch(e=>notice(e.message));$('more-messages').onclick=()=>history(true).catch(e=>notice(e.message));$('load-evidence').onclick=()=>showEvidence().catch(e=>notice(e.message));$('more-artifacts').onclick=()=>showEvidence(true).catch(e=>notice(e.message));document.querySelectorAll('[data-example]').forEach(b=>b.onclick=()=>{$('text').value=b.dataset.example;$('text').focus();});
(async()=>{try{const data=await api('bootstrap');token=data.token;$('scope').textContent=data.scope;$('profile').textContent=pretty({默认条件:data.defaults,许可:data.permission,预算:data.budget});$('limitations').textContent=data.limitations.join(' ');if(!data.text_enabled)notice('当前配置未启用文本请求。可以浏览历史；启用方式见 README 的本地网页说明。');else if(!data.model_key_present)notice('后端当前进程未读取到 DeepSeek 密钥。可查看已有结果；新模型请求需要先配置进程环境。');const selected=current;select(selected);if(!data.text_enabled)notice('当前配置未启用文本请求，可以查看历史结果。请按 README 启用本地文本 profile。');else if(!data.model_key_present)notice('后端当前进程未读取到 DeepSeek 密钥，可查看历史结果。');await listRuns();setInterval(()=>{if(!document.hidden)refresh().catch(e=>notice(e.message));},4000);}catch(e){notice(e.message);}})();

$('water-sp').onclick=async()=>{const key='orca:pending:water-sp';let id=localStorage.getItem(key);if(!id){id='web_'+crypto.randomUUID().replaceAll('-','');localStorage.setItem(key,id);}$('water-sp').disabled=true;try{const result=await api('presets/water-sp',{submission_id:id});localStorage.removeItem(key);select(result.run_id);if(result.start_error)notice(result.start_error);await listRuns();await refresh();}catch(e){notice(e.message+'；重试沿用同一提交编号。');}finally{$('water-sp').disabled=false;}};
