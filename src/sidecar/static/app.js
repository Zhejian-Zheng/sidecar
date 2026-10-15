// 手机页面控制逻辑：只提交与查询任务，实际 Agent 在 Mac 的独立 worker 中运行。
const $ = id => document.getElementById(id);
// 配对令牌保存在当前浏览器会话中；后续 API 请求携带它完成鉴权。
let token = sessionStorage.getItem('sidecar-token') || '';
let current = null, config = null, imageUrl = null, busy = false;
let showFrame = false;
let lastLiveFrame=0;
let chatHistory=[];
let chatGeneration=0;
try {
  const saved=JSON.parse(sessionStorage.getItem('sidecar-chat')||'[]');
  if(Array.isArray(saved)) chatHistory=saved.filter(m=>m && ['user','assistant'].includes(m.role) && typeof m.content==='string').slice(-12);
} catch (_) { /* Ignore an incomplete browser-session record. */ }
function renderChat(){
  $('chat-log').replaceChildren();
  for(const message of chatHistory){
    const row=document.createElement('p');
    row.textContent=(message.role==='user'?'你：':'伴行：')+message.content;
    $('chat-log').append(row);
  }
}
function clearChat(){
  chatGeneration++;chatHistory=[];sessionStorage.removeItem('sidecar-chat');renderChat();
}
renderChat();
function selectTask(id){
  if(current!==id){
    lastLiveFrame=0;
    $('frame').removeAttribute('src');$('frame').hidden=true;$('frame-empty').hidden=false;
    if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=null;
  }
  current=id;
}
// Keep uncertain submissions stable until the server acknowledges them.
const pendingSubmissions = new Map();
// 记住上次看到的状态，用于生成页面内提示；这里不提供系统推送。
const seen = new Map();
// 后端状态到中文标签的映射；排队、执行和等待交接需要分别展示。
const labels = {queued:'排队中',executing:'准备执行',running:'执行中',paused:'已暂停',waiting_user:'等待你处理',interrupted:'执行中断',completed:'已完成',failed:'未完成',cancelled:'已取消'};
const terminal = ['completed','failed','cancelled'];
// 统一把请求错误显示到页面，避免失败后用户只看到没有变化的按钮。
function fail(error) { $('error').textContent = error.message || String(error); $('error').hidden = false; }
// 统一附加配对令牌并解析响应；204 表示请求成功但没有内容。
async function api(path, options={}) {
  const response = await fetch('/api'+path, {...options, headers:{'Content-Type':'application/json',Authorization:'Bearer '+token,...options.headers}});
  if (!response.ok) { const body = await response.json().catch(()=>({})); throw new Error(typeof body.detail==='string'?body.detail:'请检查输入或连接状态'); }
  return response.status===204 ? null : response.json();
}
// 将多行待办或完成标准转换为数组，去掉空行和两端空白。
function lines(id) { return $(id).value.split('\n').map(x=>x.trim()).filter(Boolean); }
// 验证配对码，读取执行模式，再显示控制台和对应的任务输入区域。
async function connect() {
  config = await api('/config'); sessionStorage.setItem('sidecar-token',token);
  showFrame=false;$('screen-panel').hidden=true;$('toggle-preview').textContent='查看执行画面';
  $('toggle-preview').setAttribute('aria-expanded','false');$('frame').removeAttribute('src');
  if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=null;
  $('pairing').hidden=true; $('console').hidden=false; $('error').hidden=true;
  $('connection').textContent=config.worker_online?'执行端已连接':'执行端未启动';
  $('mode-note').textContent=config.mode==='demo'?'离线演示 · 验证任务流程，不操作真实 App':(config.hidden_display?'隐藏执行屏 · ':'Android 执行端 · ')+config.package;
  $('verification-input').hidden=config.mode!=='android';
  $('demo-input').hidden=config.mode!=='demo'; $('criteria-input').hidden=config.mode==='demo';
  const allowed=config.allowed_packages||[];
  $('app-input').hidden=!(config.mode==='android'&&allowed.length>1);
  $('app').replaceChildren(...allowed.map(name=>{const option=document.createElement('option');option.value=name;option.textContent=name;return option;}));
  await refresh();
}
// 轮询任务列表与执行端状态；busy 防止前一次刷新未结束时重复请求。
async function refresh() {
  if (!config || busy) return;
  busy=true;
  try {
    const jobs = await api('/tasks');
    $('task-list').replaceChildren();
    if (!jobs.length) { const p=document.createElement('p');p.className='hint';p.textContent='还没有任务。从上方开始。';$('task-list').append(p); }
    for (const job of jobs) {
      const prior=seen.get(job.id);
      if (prior && prior!==job.status && ['completed','failed','waiting_user','interrupted'].includes(job.status)) {
        $('notice').textContent=(labels[job.status]||job.status)+'：'+job.request.goal; $('notice').hidden=false;
      }
      seen.set(job.id,job.status);
      const button=document.createElement('button');button.className='task-row'+(job.id===current?' selected':'');
      const title=document.createElement('span');title.className='task-goal';title.textContent=job.request.goal;
      const status=document.createElement('small');status.textContent=labels[job.status]||job.status;
      button.append(title,status);button.onclick=()=>{selectTask(job.id);refresh();};$('task-list').append(button);
    }
    if (!current && jobs.length) selectTask(jobs[0].id);
    if (current) await detail(current);
    const health=await api('/config');$('connection').textContent=health.worker_online?'执行端已连接':'执行端未启动，任务保留在队列';
  } catch(e) { fail(e); } finally {busy=false;}
}
// 展示所选任务的状态、操作记录和证据；按钮随终态与交接状态变化。
async function detail(id) {
  const job=await api('/tasks/'+id);
  if(current!==id)return;
  $('task-title').textContent=job.request.goal;$('task-status').textContent=labels[job.status]||job.status;
  $('reason').textContent=job.report || (job.pending_control?('已请求'+(job.pending_control==='pause'?'暂停，请等待确认后再接管':'取消，请等待当前步骤结束')):(job.reason||'任务已保存，等待执行。'));
  $('controls').hidden=terminal.includes(job.status);$('pause').hidden=['paused','waiting_user','interrupted'].includes(job.status);
  $('resume-form').hidden=!['paused','waiting_user','interrupted'].includes(job.status);
  $('events').replaceChildren();
  for(const event of job.events||[]) {
    let text='';
    if(event.kind==='decision')text=event.data.reason;
    if(event.kind==='uncertain_action')text='操作结果不确定，请核对设备。';
    if(event.kind==='model_retry')text='模型连接暂时中断，正在重试。';
    if(event.kind==='user_resumed')text='你已交还控制：'+event.data.note;
    if(text){const li=document.createElement('li');li.textContent=text;$('events').append(li);}
  }
  $('evidence').replaceChildren();
  for(const check of (job.report?[]:job.state?.verification?.checks||[])) {
    const p=document.createElement('p');p.textContent=(check.passed?'✓ ':'○ ')+check.criterion+'：'+check.evidence;$('evidence').append(p);
  }
  const media=await api('/tasks/'+id+'/video');
  if(current!==id)return;
  $('download-recording').hidden=!media.recording_available;
  $('video-status').textContent=media.status==='live'?(lastLiveFrame&&Date.now()-lastLiveFrame<3000?'后台实时画面':'后台采集中，等待新画面'):
    media.status==='stalled'?'画面采集延迟，不能据此确认当前状态':
    media.status==='stopped'?'本轮画面采集已结束'+(media.recording_complete?'':'，录像可能不完整'):
    '暂无实时视频；可查看最近一次任务观察';
  $('screen-panel').hidden=!showFrame;
}
let frameBusy=false;
async function refreshFrame(){
  if(frameBusy||!showFrame||!current||!config||document.hidden)return;
  frameBusy=true;
  const id=current, auth=token;
  try{
    let response=await fetch('/api/tasks/'+id+'/live-frame',{headers:{Authorization:'Bearer '+auth}});
    const live=response.status===200;
    if(response.status===204)response=await fetch('/api/tasks/'+id+'/frame',{headers:{Authorization:'Bearer '+auth}});
    const blob=response.status===200?await response.blob():null;
    if(!showFrame||current!==id||token!==auth||!config)return;
    if(blob){
      const next=URL.createObjectURL(blob);$('frame').src=next;
      if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=next;
      $('frame').hidden=false;$('frame-empty').hidden=true;
      lastLiveFrame=live?Date.now():0;
      $('frame').alt=live?'后台实时画面':'最近观察截图（非实时）';
      $('video-status').textContent=live?'后台实时画面':'最近观察截图（非实时）';
    }else{$('frame').hidden=true;$('frame-empty').hidden=false;}
  }catch(e){
    if(config&&current===id&&token===auth){
      lastLiveFrame=0;
      $('video-status').textContent='连接中断，最后画面非实时';
      $('frame').alt='连接中断，最后画面非实时';fail(e);
    }
  }finally{frameBusy=false;}
}
$('download-recording').onclick=async()=>{
  const id=current;
  try{
    const response=await fetch('/api/tasks/'+id+'/recording',{headers:{Authorization:'Bearer '+token}});
    if(!response.ok)throw new Error('录像暂不可下载，请等待本轮执行结束');
    const url=URL.createObjectURL(await response.blob());
    const link=document.createElement('a');link.href=url;link.download=(response.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/)?.[1]||id+'.mkv';link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(e){fail(e);}
};
setInterval(()=>{
  if(lastLiveFrame&&Date.now()-lastLiveFrame>3000){
    lastLiveFrame=0;
    $('video-status').textContent='画面更新超时，最后画面非实时';
    $('frame').alt='画面更新超时，最后画面非实时';
  }
  refreshFrame();
},500);
$('toggle-preview').onclick=async()=>{
  showFrame=!showFrame;
  $('toggle-preview').textContent=showFrame?'隐藏执行画面':'查看执行画面';
  $('toggle-preview').setAttribute('aria-expanded',String(showFrame));
  $('screen-panel').hidden=!showFrame;
  if(!showFrame){$('frame').removeAttribute('src');if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=null;}
  else if(current){try{await detail(current);await refreshFrame();}catch(e){fail(e);}}
};
// 配对表单：阻止整页提交，连接成功后留在当前页面更新内容。
$('pair-form').onsubmit=async event=>{event.preventDefault();token=$('token').value.trim();try{await connect();}catch(e){fail(e);}};
// 断开只清除页面会话，不取消已经交给 worker 的任务。
$('disconnect').onclick=()=>{sessionStorage.removeItem('sidecar-token');clearChat();token='';config=null;current=null;seen.clear();$('token').value='';$('pairing').hidden=false;$('console').hidden=true;$('connection').textContent='尚未连接';};
// Conversation replies render directly; only returned task IDs select an execution view.
$('chat-form').onsubmit=async event=>{
  event.preventDefault();
  const content=$('chat-message').value.trim();
  if(!content || $('chat-send').disabled)return;
  const generation=chatGeneration;
  $('chat-send').disabled=true;$('error').hidden=true;
  const messages=[...chatHistory,{role:'user',content}];
  const key=JSON.stringify(['chat',token,messages]);
  if(!pendingSubmissions.has(key))pendingSubmissions.set(key,crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random());
  try{
    const result=await api('/chat',{method:'POST',body:JSON.stringify({messages,request_id:pendingSubmissions.get(key)})});
    if(generation!==chatGeneration)return;
    pendingSubmissions.delete(key);
    const answer=result.message+(result.task_id && !result.message.includes(result.task_id)?'\n任务编号：'+result.task_id:'');
    chatHistory=[...messages,{role:'assistant',content:answer}].slice(-12);
    sessionStorage.setItem('sidecar-chat',JSON.stringify(chatHistory));
    renderChat();$('chat-message').value='';
    if(result.task_id){selectTask(result.task_id);await refresh();}
  }catch(e){if(generation===chatGeneration)fail(e);}
  finally{$('chat-send').disabled=false;}
};
// 提交目标及模式对应的输入，拿到任务编号后展示执行进度。
$('task-form').onsubmit=async event=>{
  event.preventDefault();
  if ($('submit').disabled) return;
  $('submit').disabled=true;$('error').hidden=true;
  try{
    const request={goal:$('goal').value,items:config.mode==='demo'?lines('items'):[],criteria:config.mode==='android'?lines('criteria'):[],app:config.mode==='android'?($('app').value||null):null};
    if(config.mode==='android' && (request.app||config.package)==='com.google.android.deskclock' && $('alarm-time').value){
      request.requirement={kind:'clock_alarm_enabled',expected_time:$('alarm-time').value};
    }
    const key=JSON.stringify([token,config.mode,request]);
    if(!pendingSubmissions.has(key))pendingSubmissions.set(key,crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random());
    request.request_id=pendingSubmissions.get(key);
    const job=await api('/tasks',{method:'POST',body:JSON.stringify(request)});pendingSubmissions.delete(key);selectTask(job.id);await refresh();
  }catch(e){fail(e);}finally{$('submit').disabled=false;}
};
// 暂停或取消只是提交控制请求，页面须等待执行端确认状态。
for(const action of ['pause','cancel']) $(action).onclick=async()=>{try{await api('/tasks/'+current+'/'+action,{method:'POST'});await refresh();}catch(e){fail(e);}};
// 用户核对设备后填写备注并交还控制；恢复会由后台重新观察现场。
$('resume-form').onsubmit=async event=>{event.preventDefault();try{await api('/tasks/'+current+'/resume',{method:'POST',body:JSON.stringify({note:$('note').value})});$('note').value='';await refresh();}catch(e){fail(e);}};
// 重新打开页面时尝试恢复会话；前台定时查询，切回页面时立即刷新。
if(token)connect().catch(fail);
setInterval(refresh,2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
