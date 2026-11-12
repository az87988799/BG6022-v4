'use strict';
const $ = id => document.getElementById(id);
const pretty = value => JSON.stringify(value, null, 2);
const labels = {ready:'已就绪',running:'运行中',paused:'已暂停',cancelled:'已取消',completed:'已完成',failed:'失败',unknown:'状态待核对',budget_exhausted:'预算已耗尽',waiting_user:'等待补充',unreadable:'记录不可读'};
const checkLabels = {passed:'通过',failed:'未通过',unverified:'未验证',partially_checked:'部分检查',not_applicable:'不适用',source_unverified:'来源未通过核对'};
let loaded=null, historyExpanded=false;
let token, current = localStorage.getItem('orca:selected'), polling = false, runPage = 0, artifactPage = 0, messagePage = 0;
function notice(text='') { $('notice').textContent = text; }
function element(tag, text, cls) { const node=document.createElement(tag); if(text!==undefined) node.textContent=text; if(cls) node.className=cls; return node; }
function action(label, fn) { const b=element('button',label); b.type='button'; b.onclick=()=>Promise.resolve().then(fn).catch(e=>notice(e.message)); return b; }
async function api(path, body) {
  const response=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:{'X-Local-Token':token||'','Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json(); if(!response.ok) throw new Error(data.error==='local_token_required'?'服务已重启，请刷新页面重新连接；已保存任务不会自动恢复。':data.message||data.error||'本地服务请求失败'); return data;
}
function select(id) { leaveAcceptance(); $('ask').hidden=!id; current=id;loaded=null;historyExpanded=false; if(id) localStorage.setItem('orca:selected',id); else localStorage.removeItem('orca:selected'); $('task').hidden=true; $('welcome').hidden=!!id; $('input-label').textContent=id?'补充条件或追问当前任务':'描述新的计算目标'; $('send').textContent=id?'发送消息 ↑':'提交任务 ↑'; $('send-hint').textContent=id?'消息保存在当前任务中；协调者停止时，点击“继续处理”。费用和预算不会重置。':'提交将按本地配置中的许可与有限预算执行。支持受管理的代码计算与分析；上传暂未开放。'; $('artifacts').replaceChildren(); $('evidence-navigation').replaceChildren(); $('evidence').hidden=true; $('more-artifacts').hidden=true; $('conversation').replaceChildren(); messagePage=0; artifactPage=0; notice(); $('runs').querySelectorAll('button').forEach(b=>b.classList.toggle('selected',b.dataset.runId===id)); if(id){notice('正在读取所选任务…');refresh().catch(e=>notice(e.message));} }
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

function recordedState(value) { return labels[value]||({intent:'已登记待启动',reserved:'已预约',timed_out:'超时',not_started:'未启动'}[value])||value; }
function renderExecution(state, report) {
 const view=state.execution||{}, active=view.current_actions||[];
 $('current-action').textContent=active.length?active.map(item=>`${item.tool||item.kind} · ${item.request_key||item.step_id||item.call_id||item.attempt_id} · ${recordedState(item.state)}`).join('\n'):(state.coordinator_active?'协调者正在处理；当前尚无运行中的工具或计算记录。':'协调者未运行；当前显示已保存进度。');
 const counts=view.internal_call_counts||{}, progress=$('execution-progress');progress.replaceChildren();
 progress.append(element('p',`内部 ORCA：已登记 ${view.internal_calls?.length??report.execution?.progress?.total??0} 次；完成 ${counts.completed??0} 次；失败 ${(counts.failed||0)+(counts.timed_out||0)} 次；待核对 ${counts.unknown??0} 次。`));
 progress.append(element('p',`脚本/计算尝试 ${view.attempts?.length??state.attempts?.length??0} 次；检查点 ${view.checkpoint_count??0} 个。未来计算次数由后续决策确定，当前记录不代表总搜索范围。`,'muted'));
 if(view.latest_checkpoint?.requires_decision)progress.append(element('p','检查点等待反馈决策：'+(view.latest_checkpoint.reason||'原因未记录'),'question'));
 $('internal-calls').textContent=pretty({internal_calls:view.internal_calls||report.execution?.internal_calls||[],checkpoints:report.execution?.checkpoints||[],latest_checkpoint:view.latest_checkpoint});
}
async function downloadArtifact(id, artifact) {
 const artifactId=artifact.artifact_id||artifact.id;
 const response=await fetch(`/api/runs/${id}/artifacts/${artifactId}/download`,{headers:{'X-Local-Token':token}});
 if(!response.ok)throw new Error('文件未通过完整性核对、无当前任务访问许可或超过下载上限');
 const url=URL.createObjectURL(await response.blob()),link=element('a');link.href=url;link.download=(artifact.path||artifactId).split(/[\\/]/).pop();link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function artifactRow(id, artifact, label) {
 const artifactId=artifact.artifact_id||artifact.id,row=element('div',undefined,'file'),info=element('div'),controls=element('div');
 info.append(element('span',(label||artifact.role||'文件')+' · '+(artifact.path||artifactId)),element('small',artifactId+' · '+(artifact.status||artifact.integrity?.status||'完整性状态未知')),element('small','SHA256 '+(artifact.sha256||'未知')));
 if(artifactId)controls.append(action('查看前 80 行',()=>readEvidence(id,'evidence.text',{artifact_id:artifactId,start_line:1,lines:80})),action('下载',()=>downloadArtifact(id,artifact)));
 row.append(info,controls);return row;
}
function renderScripts(id, report) {
 const scripts=$('scripts');scripts.replaceChildren();
 const files=new Map();for(const item of report.generated_scripts||[])files.set(item.artifact_id||item.id,item);
 for(const item of report.artifact_index||[]){if(item.purposes?.includes('actual_input')||['orca_input','native_input','input'].includes(item.role))files.set(item.artifact_id||item.id,item);}
 for(const item of files.values())scripts.append(artifactRow(id,item,item.language==='orca'?'ORCA 输入':item.role==='code'||item.language==='python'?'Python 脚本':item.role));
 if(!files.size)scripts.append(element('p','尚无已登记的生成脚本或实际输入。历史文件可通过下方原始证据清单读取。','muted'));
}
function renderObservations(report, target='observations') {
 const container=$(target);container.replaceChildren();let count=0;
 for(const result of report.results||[]){
  const records=result.observation_records||[],validation=result.output_validation||[];
  if(validation.length){const table=element('table'),head=element('tr');for(const label of ['输出 / 结果','检查状态','科学资格'])head.append(element('th',label));table.append(head);
   for(const output of validation){const row=element('tr');row.append(element('td',output.name+' · '+result.result_id),element('td',checkLabels[output.status]||output.status||'未知'),element('td',output.scientific_qualification===true?'仅此输出通过适用科学检查':'未授予科学资格'));const details=element('details');details.append(element('summary','检查覆盖与来源'),element('pre',pretty({checks:output.checks,source_integrity:output.source_integrity})));row.children[1].append(details);table.append(row);}container.append(table);count+=validation.length;}
  for(const observation of records){const box=element('details',undefined,'observation');box.open=true;box.append(element('summary',observation.name+' · '+(checkLabels[observation.validation_status]||'未验证')));
   box.append(element('p',(observation.value===null?'值未知':pretty(observation.value))+' · '+(observation.unit||'单位未知'),'observation-value'),element('p','通用观察；来源或格式检查不等于完整科学检查。','muted'));
   if(observation.formula)box.append(element('p','公式：'+observation.formula));
   box.append(element('pre',pretty({result_id:result.result_id,attempt_id:result.attempt_id,actual_conditions:observation.actual_conditions,sources:observation.sources,code_sha256:observation.code_sha256,checks:observation.checks,source_integrity:observation.source_integrity,limitations:observation.limitations})));
   container.append(box);count++;}
  if(!records.length&&Object.keys(result.observations?.data||{}).length){const box=element('details');box.append(element('summary','已有原始观察 · '+result.result_id+' · 未宣称科学资格'),element('pre',pretty(result.observations.data)));container.append(box);count++;}
 }
 if(!count)container.append(element('p','尚无逐项分析记录。缺失的数值、单位、来源或检查保持未知。','muted'));
}
function renderDiagnostics(state, report) {
 const container=$('diagnostics');container.replaceChildren();const failures=[...(report.diagnostics||[])];
 for(const result of report.results||[])for(const item of result.diagnostics||[])failures.push({...item,result_id:result.result_id});
 for(const item of failures.slice(-10))container.append(element('div',(item.category||'诊断')+'：'+(item.message==='bounded proposal correction or transport retry exhausted'?'模型建议连续未通过校验，已达到纠正上限；任务停止，已有证据保留。':item.message||pretty(item))+(item.result_id?' · '+item.result_id:''),'question'));
 const stop=report.stop_reason;if(stop&&['failed','budget_exhausted','unknown','cancelled','paused'].includes(state.state)){const box=element('details');box.append(element('summary','停止原因与模型失败记录'),element('pre',pretty(stop)));container.append(box);}
}

async function refresh() {
 if(!current||polling)return;polling=true;const id=current;
 try {const [state, report]=await Promise.all([api(`runs/${id}`),api(`runs/${id}/report`)]);if(id!==current)return;loaded=id;if($('notice').textContent==='正在读取所选任务…')notice();
 $('task').hidden=false;$('welcome').hidden=true;$('task-title').textContent=report.request?.original_text||'已有任务';$('run-id').textContent=id;$('state').textContent=labels[state.state]||state.state;
 $('activity').textContent=state.coordinator_active?'协调者正在处理 · 每 4 秒更新':state.control_requested?'已收到'+(state.control_requested==='pause'?'暂停':'取消')+'请求；实际状态见上方':'协调者未运行；只读展示已保存事实';
 if(state.web_operation?.status==='failed')notice(state.web_operation.message);
 const knowledgeOnly=report.goal_facts?.length&&report.goal_facts.every(f=>f.port==='knowledge_answer'); const boundedAnswer=report.goal_facts?.some(f=>f.answer?.observation?.limitations?.length); $('goal-state').textContent=report.user_goal_complete?(knowledgeOnly?(boundedAnswer?'回答已交付 · 范围有限':'回答已交付'):'已满足'):'尚未全部满足';$('science-cost').textContent=`${state.usage.orca_starts_actual} / ${state.budget.orca_starts}`;$('model-cost').textContent=`${state.usage.model_calls} 次 / ${state.usage.model_tokens_used} tokens`;
 const money=report.budget?.model_cost;$('money').textContent=money?.known_cost??'—';
 $('steps').textContent=pretty({steps:state.steps,attempts:state.attempts});$('conditions').textContent=pretty(report.request);$('report').textContent=pretty(report);
 const deliveryLabels={complete:'完整',partial:'部分完成',pending:'待生成',rendered:'已生成',failed:'失败',not_recorded:'未记录'};$('delivery-state').textContent='用户目标：'+(report.user_goal_complete?'已满足':'尚未全部满足')+'；交付内容：'+(deliveryLabels[report.report_delivery_status]||report.report_delivery_status||'未知')+'；报告文件：'+(deliveryLabels[report.report_artifact?.status]||report.report_artifact?.status||'未知')+'。科学检查见每个输出。';
 renderExecution(state,report);renderScripts(id,report);renderObservations(report);renderDiagnostics(state,report); $('questions').replaceChildren();for(const q of [...(report.communication?.questions||[]),...(report.communication?.notices||[])])$('questions').append(element('div',q,'question'));
 if(state.clarification?.questions)for(const q of state.clarification.questions)$('questions').append(element('div',q,'question'));
 const table=element('table');const head=element('tr');for(const t of ['目标 / 物理量','值与单位','资格与来源'])head.append(element('th',t));table.append(head);
 const facts=report.goal_facts||[];for(const fact of facts){const row=element('tr');row.append(element('td',fact.goal_id));const answer=fact.answer;if(answer?.kind==='qualified_scientific_output'){row.append(element('td',(answer.value===null?'结构文件 '+answer.artifact_id:pretty(answer.value)+' '+(answer.unit||'单位未知'))),element('td','科学检查通过 · '+(answer.check_versions||[]).join(', ')));}else if(answer?.observation?.status==='answered'){row.append(element('td',answer.observation.answer),element('td','知识解释 · 非科学验证'));for(const limitation of answer.observation.limitations||[])row.children[1].append(element('p','范围说明：'+limitation,'muted'));for(const source of answer.observation.sources||[]){if(source.url){const link=element('a',source.title);link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';row.lastChild.append(link);}else{const detail=element('details');detail.append(element('summary','原文件读取来源 · '+source.result_id),element('pre',pretty(source.observations)));row.lastChild.append(detail);}}const saved=answer.observation.source_facts;if(saved?.length){const detail=element('details');detail.append(element('summary','原计算的数值、结构及来源'));for(const f of saved){const v=f.answer;if(v?.kind==='qualified_scientific_output')detail.append(element('p',f.port+'：'+(v.value===null?v.artifact_id:v.value+' '+v.unit)+'；结构 '+f.source_geometry_artifact_id));}row.children[1].append(detail);}}else if(answer){row.append(element('td',pretty(answer.observation)),element('td','原始证据观察 · 单位 '+(answer.unit||'未知')+' · 未宣称科学资格'));}else{row.append(element('td','暂无可交付结果'),element('td','证据不足或尚未完成'));}table.append(row);}
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
// Historical acceptance data has its own selection and GET-only routes.
let acceptanceTasks=[], acceptanceCurrent=null, acceptanceGeneration=0;
let acceptanceFiles=[], acceptanceFileGeneration=0, acceptancePageOffset=0;
let acceptanceNextOffset=null, acceptanceFileTotal=0, acceptanceTextGeneration=0;
const deliveryLabels={complete:'完整交付',partial:'部分交付',pending:'待生成',failed:'交付失败',not_recorded:'未记录'};
const goalLabels={satisfied:'已满足',unsatisfied:'未满足',pending:'待完成',unverified:'未验证',partial:'部分完成',blocked:'受阻'};
const acceptanceKey=selection=>selection?selection.task+'/'+selection.variant:'';
function acceptancePath(selection=acceptanceCurrent) {
 return 'acceptance/tasks/'+encodeURIComponent(selection.task)+'/runs/'+encodeURIComponent(selection.variant);
}
function leaveAcceptance() {
 acceptanceCurrent=null;acceptanceGeneration++;acceptanceFileGeneration++;acceptanceTextGeneration++;
 $('acceptance').hidden=true;$('composer').hidden=false;
 $('acceptance-tasks').querySelectorAll('button').forEach(button=>{button.classList.remove('selected');button.removeAttribute('aria-current');});
 if(location.hash.startsWith('#acceptance/'))window.history.replaceState(null,'',location.pathname+location.search);
}
function acceptanceRoute() {
 const match=location.hash.match(/^#acceptance\/([^/]+)(?:\/([^/]+))?$/);
 if(!match)return null;
 try{return {task:decodeURIComponent(match[1]),variant:match[2]?decodeURIComponent(match[2]):null};}catch{return null;}
}
function openAcceptance(task,variant) {
 const hash='#acceptance/'+encodeURIComponent(task)+(variant?'/'+encodeURIComponent(variant):'');
 if(location.hash===hash)return applyAcceptanceRoute();
 location.hash=hash;
}
async function listAcceptanceTasks() {
 const data=await api('acceptance/tasks');acceptanceTasks=data.tasks||[];
 const navigation=$('acceptance-tasks');navigation.replaceChildren();
 for(const task of acceptanceTasks){
  const button=action('',()=>openAcceptance(task.id));button.dataset.acceptanceTask=task.id;
  button.append(element('b',task.id),element('span',task.title));button.title=task.id+' · '+task.title;
  navigation.append(button);
 }
 if(!acceptanceTasks.length)navigation.append(element('p','暂无可用的测试索引。','muted'));
}
function acceptanceNotes(container, notes) {
 if(notes===null||notes===undefined)return;
 for(const item of Array.isArray(notes)?notes:[notes])container.append(element('div',typeof item==='string'?item:pretty(item),'question'));
}
function acceptanceAnswer(answer) {
 if(!answer)return '尚无可交付结果';
 if(answer.kind==='qualified_scientific_output'||answer.kind==='qualified_input_artifact')return answer.value===null?'结构 / 文件 '+(answer.artifact_id||'未知'):pretty(answer.value)+' '+(answer.unit||'单位未知');
 const observation=answer.observation;
 if(observation?.status==='answered')return observation.answer;
 const value=observation&&typeof observation==='object'&&Object.prototype.hasOwnProperty.call(observation,'value')?observation.value:observation;
 return (value===undefined?'尚无记录值':value===null?'值未知':typeof value==='string'?value:pretty(value))+(answer.unit||observation?.unit?' '+(answer.unit||observation.unit):'');
}
function renderAcceptanceGoals(report) {
 const container=$('acceptance-results');container.replaceChildren();
 const facts=new Map((report.goal_facts||[]).map(fact=>[fact.goal_id,fact]));
 const goals=report.goals?.length?report.goals:(report.goal_facts||[]);
 if(!goals.length){container.append(element('p','本次历史尝试尚无逐目标结果，已取得的文件仍可在下方查阅。','muted'));return;}
 const table=element('table'),head=element('tr');
 for(const title of ['目标 / 物理量','历史结果','证据与验证'])head.append(element('th',title));table.append(head);
 for(const goal of goals){
  const fact=facts.get(goal.goal_id)||goal, answer=fact.answer, row=element('tr');
  const name=element('td',goal.goal_id);if(goal.port||fact.port)name.append(element('small',goal.port||fact.port,'muted'));
  const value=element('td');value.append(element('div',acceptanceAnswer(answer),'acceptance-result-value'));
  if(answer?.observation&&typeof answer.observation==='object'){const detail=element('details');detail.append(element('summary','完整观察、来源及验证说明'),element('pre',pretty(answer.observation)));value.append(detail);}
  const evidence=element('td');
  const state=goal.recorded_status||goal.report_status||goal.evidence_status;
  if(state)evidence.append(element('p',goalLabels[state]||checkLabels[state]||state));
  evidence.append(element('p',answer?.kind==='qualified_scientific_output'?'此输出通过其适用科学检查':answer?.kind==='qualified_input_artifact'?'已检查输入；不代表科学计算通过':'未授予科学资格','muted'));
  const gaps=goal.gaps||[];if(gaps.length){const detail=element('details');detail.append(element('summary','证据缺口'),element('pre',pretty(gaps)));evidence.append(detail);}
  if(fact.source_conditions||fact.current_conditions){const detail=element('details');detail.append(element('summary','条件与来源'),element('pre',pretty({current_conditions:fact.current_conditions,source_conditions:fact.source_conditions,answer})));evidence.append(detail);}
  row.append(name,value,evidence);table.append(row);
 }
 container.append(table);
 const stop=report.stop_reason;
 if(stop?.categories?.length){const detail=element('details');detail.append(element('summary','停止原因与诊断'),element('pre',pretty(stop)));container.append(detail);}
}
async function applyAcceptanceRoute() {
 const route=acceptanceRoute();if(!route)return false;
 const task=acceptanceTasks.find(item=>item.id===route.task);
 const variant=task?.variants?.find(item=>item.id===route.variant)||(route.variant?null:task?.variants?.find(item=>item.available!==false)||task?.variants?.[0]);
 const generation=++acceptanceGeneration;acceptanceFileGeneration++;acceptanceTextGeneration++;
 current=null;loaded=null;acceptanceCurrent=variant?{task:task.id,variant:variant.id}:null;
 $('welcome').hidden=true;$('task').hidden=true;$('composer').hidden=true;$('acceptance').hidden=false;
 $('acceptance').scrollIntoView({block:'start',behavior:'auto'});
 $('runs').querySelectorAll('button').forEach(button=>button.classList.remove('selected'));
 $('acceptance-tasks').querySelectorAll('button').forEach(button=>{const selected=button.dataset.acceptanceTask===route.task;button.classList.toggle('selected',selected);if(selected)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});
 $('acceptance-title').textContent=task?task.id+' · '+task.title:'未找到这项测试';
 $('acceptance-run-id').textContent=variant?.run_id||'';
 for(const id of ['acceptance-delivery','acceptance-verification'])$(id).textContent='—';
 for(const id of ['acceptance-summary','acceptance-original','acceptance-conditions','acceptance-report','acceptance-report-basis','acceptance-files-count'])$(id).textContent='';
 for(const id of ['acceptance-results','acceptance-limitations','acceptance-observations','acceptance-files','acceptance-variants'])$(id).replaceChildren();
 $('acceptance-viewer').hidden=true;$('acceptance-files-prev').hidden=true;$('acceptance-files-next').hidden=true;
 $('acceptance-category').value='all';$('acceptance-search').value='';acceptanceFiles=[];acceptancePageOffset=0;acceptanceNextOffset=null;
 if(!task||!variant){notice('测试或历史尝试不存在，请从左侧七题测试重新选择。');return true;}
 for(const item of task.variants||[]){const button=action(item.label||item.id,()=>openAcceptance(task.id,item.id));button.classList.toggle('selected',item.id===variant.id);button.setAttribute('aria-pressed',String(item.id===variant.id));button.disabled=item.available===false;$('acceptance-variants').append(button);}
 if(variant.available===false){notice('该历史尝试的数据当前不可用，未启动任何计算。');acceptanceNotes($('acceptance-limitations'),task.limitations);return true;}
 notice('正在读取历史测试证据…');
 try{
  const data=await api(acceptancePath());if(generation!==acceptanceGeneration)return true;
  const report=data.report||{};
  $('acceptance-title').textContent=task.id+' · '+(data.title||task.title);
  $('acceptance-run-id').textContent=(data.variant?.label||variant.label||variant.id)+' · '+(data.variant?.run_id||variant.run_id||'Run 未记录');
  const delivery=data.delivery_status??report.recorded_delivery_status??report.report_delivery_status;$('acceptance-delivery').textContent=deliveryLabels[delivery]||delivery||'未记录';
  $('acceptance-verification').textContent=report.user_goal_complete?'已满足（原记录）':report.user_obligations_verification==='unverified'?'仍有未验证义务（原记录）':'尚未全部满足（原记录）';
  const summary=data.summary??task.summary;$('acceptance-summary').textContent=typeof summary==='string'?summary:pretty(summary);
  acceptanceNotes($('acceptance-limitations'),data.limitations??task.limitations);
  $('acceptance-limitations').append(element('p','运行完成、报告交付与科学验证分别记录；这些尝试来自各自历史版本，不能合并为同一冻结版本七题通过。','muted'));
  $('acceptance-original').textContent=data.original_text||report.request?.original_text||'原文未记录';
  $('acceptance-report-basis').textContent=data.report_basis||'已保存的历史报告；文件当前完整性以逐次读取和下载核验为准。';
  $('acceptance-conditions').textContent=pretty(report.request||{});$('acceptance-report').textContent=pretty(report);
  renderAcceptanceGoals(report);renderObservations(report,'acceptance-observations');notice();
  await showAcceptanceFiles(0);
 }catch(error){if(generation===acceptanceGeneration){notice(error.message);$('acceptance-results').append(element('p','历史记录暂时无法读取。现有数据没有被修改。','muted'));}}
 return true;
}
function renderAcceptanceFiles() {
 const query=$('acceptance-search').value.trim().toLocaleLowerCase();
 const files=acceptanceFiles.filter(file=>!query||[file.path,file.role,file.id].filter(Boolean).join(' ').toLocaleLowerCase().includes(query));
 const container=$('acceptance-files');container.replaceChildren();
 $('acceptance-files-count').textContent='本分类共 '+acceptanceFileTotal+' 个文件；当前页 '+acceptanceFiles.length+' 个'+(query?'，筛选匹配 '+files.length+' 个':'')+'。文件名与角色搜索仅筛选当前页。';
 for(const file of files){
  const row=element('div',undefined,'file'),info=element('div'),controls=element('div');
  const integrity=file.integrity?.status==='verified'?'hash核验通过':checkLabels[file.integrity?.status]||file.integrity?.status||'完整性状态未知';
  info.append(element('span',file.path||file.id),element('small',(file.role||'角色未知')+' · '+(file.size??'未知')+' bytes · '+integrity),element('small',file.id+' · SHA256 '+(file.sha256||'未知')));
  controls.append(action('查看文本',()=>readAcceptanceText(file,1)));
  const download=action('下载原件',()=>downloadAcceptanceFile(file));download.disabled=file.downloadable===false;
  if(download.disabled)download.title='文件未通过下载条件核对或超过下载上限';controls.append(download);
  row.append(info,controls);container.append(row);
 }
 if(!files.length)container.append(element('p',query?'当前页没有匹配文件，可清除筛选或翻页查看。':'此分类当前没有可列出的文件。','muted'));
 $('acceptance-files-prev').hidden=acceptancePageOffset===0;$('acceptance-files-next').hidden=acceptanceNextOffset===null;
}
async function showAcceptanceFiles(offset=0) {
 if(!acceptanceCurrent)return;
 const selection={...acceptanceCurrent},generation=++acceptanceFileGeneration;
 acceptanceFiles=[];$('acceptance-files').replaceChildren();$('acceptance-files-count').textContent='正在读取文件清单…';
 $('acceptance-files-prev').hidden=true;$('acceptance-files-next').hidden=true;
 const category=$('acceptance-category').value;
 const page=await api(acceptancePath(selection)+'/artifacts?offset='+offset+'&limit=40&category='+encodeURIComponent(category));
 if(generation!==acceptanceFileGeneration||acceptanceKey(selection)!==acceptanceKey(acceptanceCurrent))return;
 acceptanceFiles=page.artifacts||[];acceptanceFileTotal=page.total??acceptanceFiles.length;acceptancePageOffset=offset;acceptanceNextOffset=page.next_offset??null;
 renderAcceptanceFiles();
}
async function readAcceptanceText(file,start) {
 if(!acceptanceCurrent)return;
 const selection={...acceptanceCurrent},generation=++acceptanceTextGeneration;
 $('acceptance-viewer').hidden=false;$('acceptance-file-title').textContent=file.path||file.id;
 $('acceptance-file-text').textContent='正在读取已有文件…';$('acceptance-file-navigation').replaceChildren();$('acceptance-file-coverage').textContent='';$('acceptance-file-metadata').textContent='';
 try{
  const data=await api(acceptancePath(selection)+'/artifacts/'+encodeURIComponent(file.id)+'/text?start_line='+start+'&lines=80');
  if(generation!==acceptanceTextGeneration||acceptanceKey(selection)!==acceptanceKey(acceptanceCurrent))return;
  const lines=data.lines||[],actualStart=data.coverage?.returned?.start??start,stop=data.coverage?.returned?.stop??actualStart+lines.length;
  $('acceptance-file-text').textContent=lines.length?lines.map(line=>String(line.line).padStart(5,' ')+'  '+line.text).join('\n'):'此读取窗口没有文本行。二进制文件请下载原件。';
  $('acceptance-file-coverage').textContent=(lines.length?'仅显示第 '+actualStart+'—'+Math.max(actualStart,stop-1)+' 行的读取窗口；不代表全文。':'此读取窗口未返回文本行。')+(data.status==='partial'?'本次返回达到读取大小上限。':'');
  const metadata={...data};delete metadata.lines;$('acceptance-file-metadata').textContent=pretty(metadata);
  const navigation=$('acceptance-file-navigation');
  if(actualStart>1)navigation.append(action('前 80 行',()=>readAcceptanceText(file,Math.max(1,actualStart-80))));
  if(stop>actualStart&&stop<=10000&&(lines.length===80||data.status==='partial'))navigation.append(action('后续行',()=>readAcceptanceText(file,stop)));
  $('acceptance-file-text').scrollTop=0;
 }catch(error){if(generation===acceptanceTextGeneration)$('acceptance-file-text').textContent='无法读取文本：'+error.message+'。可核对文件类型与完整性，或下载可用原件。';}
}
async function downloadAcceptanceFile(file) {
 if(!acceptanceCurrent)return;
 const response=await fetch('/api/'+acceptancePath()+'/artifacts/'+encodeURIComponent(file.id)+'/download',{method:'GET',headers:{'X-Local-Token':token||''}});
 if(!response.ok)throw new Error('原件无法下载：请核对历史文件完整性、来源许可与下载上限。');
 const url=URL.createObjectURL(await response.blob()),link=element('a');
 link.href=url;link.download=(file.path||file.id).split(/[\\/]/).pop();link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
$('acceptance-category').onchange=()=>showAcceptanceFiles(0).catch(error=>notice(error.message));
$('acceptance-search').oninput=renderAcceptanceFiles;
$('acceptance-files-prev').onclick=()=>showAcceptanceFiles(Math.max(0,acceptancePageOffset-40)).catch(error=>notice(error.message));
$('acceptance-files-next').onclick=()=>{if(acceptanceNextOffset!==null)showAcceptanceFiles(acceptanceNextOffset).catch(error=>notice(error.message));};
$('acceptance-file-close').onclick=()=>{acceptanceTextGeneration++;$('acceptance-viewer').hidden=true;};
$('acceptance-jump-results').onclick=()=>$('acceptance-results-panel').scrollIntoView({block:'start',behavior:'smooth'});
$('acceptance-jump-files').onclick=()=>$('acceptance-files-panel').scrollIntoView({block:'start',behavior:'smooth'});
window.addEventListener('hashchange',()=>{if(acceptanceRoute())applyAcceptanceRoute().catch(error=>notice(error.message));else if(!$('acceptance').hidden)select(localStorage.getItem('orca:selected'));});
if(acceptanceRoute()){current=null;$('welcome').hidden=true;$('task').hidden=true;$('composer').hidden=true;$('acceptance').hidden=false;}

$('composer').onsubmit=async event=>{event.preventDefault();if(!$('acceptance').hidden)return;const text=$('text').value.trim();if(!text)return;const id=current;const recordKey='orca:pending:'+ (id||'new');let saved;try{saved=JSON.parse(localStorage.getItem(recordKey));}catch{}if(!saved||saved.text!==text)saved={text,id:'web_'+crypto.randomUUID().replaceAll('-','')};localStorage.setItem(recordKey,JSON.stringify(saved));$('send').disabled=true;
try{if(id){await api(`runs/${id}/messages`,{text,message_id:saved.id});notice('消息已保存。若协调者未运行，点击“继续处理”消费这条消息。');}else{const result=await api('runs',{text,submission_id:saved.id});select(result.run_id);if(result.start_error)notice(result.start_error);}localStorage.removeItem(recordKey);historyExpanded=false;$('text').value='';await listRuns();await refresh();}catch(e){notice(e.message+'；重试同一条消息会沿用提交编号。');}finally{$('send').disabled=false;}};
for(const command of ['resume','pause','cancel'])$(command).onclick=async()=>{if(!current||loaded!==current)return;try{await api(`runs/${current}/control`,{action:command});notice('请求已保存，实际处理结果请查看状态。');await refresh();}catch(e){notice(e.message);}};
$('new').onclick=()=>{select(null);$('text').focus();};$('refresh').onclick=()=>listRuns().catch(e=>notice(e.message));$('more-runs').onclick=()=>listRuns(true).catch(e=>notice(e.message));$('more-messages').onclick=()=>history(true).catch(e=>notice(e.message));$('load-evidence').onclick=()=>showEvidence().catch(e=>notice(e.message));$('more-artifacts').onclick=()=>showEvidence(true).catch(e=>notice(e.message));document.querySelectorAll('[data-example]').forEach(b=>b.onclick=()=>{$('text').value=b.dataset.example;$('text').focus();});
(async()=>{try{const data=await api('bootstrap');token=data.token;await listRuns();try{await listAcceptanceTasks();}catch(error){$('acceptance-tasks').replaceChildren(element('p','测试索引暂时无法读取。','muted'));if(acceptanceRoute())throw error;}if(!await applyAcceptanceRoute()){const selected=current;select(selected);if(!data.text_enabled)notice('当前配置未启用文本请求，可以查看历史结果。请按 README 启用本地文本 profile。');else if(!data.model_key_present)notice('后端当前进程未读取到 DeepSeek 密钥，可查看历史结果。');}setInterval(()=>{if(!document.hidden)refresh().catch(e=>notice(e.message));},4000);}catch(e){notice(e.message);}})();

$('water-sp').onclick=async()=>{const key='orca:pending:water-sp';let id=localStorage.getItem(key);if(!id){id='web_'+crypto.randomUUID().replaceAll('-','');localStorage.setItem(key,id);}$('water-sp').disabled=true;try{const result=await api('presets/water-sp',{submission_id:id});localStorage.removeItem(key);select(result.run_id);if(result.start_error)notice(result.start_error);await listRuns();await refresh();}catch(e){notice(e.message+'；重试沿用同一提交编号。');}finally{$('water-sp').disabled=false;}};

$('ask').onclick=async()=>{const text=$('text').value.trim();if(!text||!current)return;const key='orca:question:'+current;let saved;try{saved=JSON.parse(localStorage.getItem(key));}catch{}if(!saved||saved.text!==text)saved={text,submission_id:'query_'+crypto.randomUUID().replaceAll('-','')};localStorage.setItem(key,JSON.stringify(saved));$('ask').disabled=true;try{const result=await api(`runs/${current}/questions`,saved);localStorage.removeItem(key);select(result.run_id);if(result.start_error)notice(result.start_error);$('text').value='';await listRuns();await refresh();}catch(e){notice(e.message);}finally{$('ask').disabled=false;}};
