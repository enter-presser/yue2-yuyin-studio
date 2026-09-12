'use strict';
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let project=null,provider=null,jobs=[],selected=null,saveTimer=null,saveChain=Promise.resolve(),busyAI=false,submitting=false,polling=false,lastJobState='',lastPlayerState='';
const fields=['title','intent','constraints','language','mood','vocal','summary','style','lyrics','abc'];
const stages={running:'生成中',queued:'排队中',loading:'模型加载中',planning:'音乐规划中',semantic:'演唱与伴奏生成中',synthesis:'音频合成中',decoding:'音频解码中',complete:'已完成',failed:'生成失败',interrupted:'服务重启中断',cancelled:'已取消',cancel_requested:'等待安全停止点'};
const scenes={毕业:'想写一首送给毕业好友的中文歌，温暖一点，不要太伤感，有一起走过青春的感觉。',告白:'想写一首温柔的中文告白歌，像日常聊天一样自然，写那些一起度过的小瞬间。',旅行:'想写一首适合公路旅行的中文歌，轻快、自由，有窗外阳光和远方的感觉。',生日:'想写一首送给朋友的生日歌，温暖又轻快，感谢陪伴，祝愿新的一岁勇敢快乐。',个人故事:'想写一首关于重新出发的中文歌，讲述走过低谷、慢慢找回自己的故事，真诚但不悲伤。'};
function toast(text){$('toast').textContent=text;$('toast').style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').style.display='none',6500)}
async function api(path,data,method){
 const controller=new AbortController();
 const limit=path.endsWith('/chat')?190000:path==='/provider/test'?90000:20000;
 const timer=setTimeout(()=>controller.abort(),limit);
 try{
  const r=await fetch('/api'+path,{method:method||(data===undefined?'GET':'POST'),headers:{'Content-Type':'application/json','X-Studio-Request':'1'},body:data===undefined?undefined:JSON.stringify(data),signal:controller.signal});
  let result;try{result=await r.json()}catch(e){if(e.name==='AbortError')throw e;throw Error('服务器返回异常，请检查服务并重试。')}
  if(!r.ok){const e=Error(result.error?.message||'操作未完成');e.code=result.error?.code;e.status=r.status;throw e}
  return result;
 }catch(e){
  if(e.name==='AbortError')throw Error(path.endsWith('/chat')?'等待创作回复超时。请刷新查看是否已有建议，再重试。':'连接超时，请检查网络后重试。');
  if(e instanceof TypeError)throw Error('连接中断，请检查网络后重试。');
  throw e;
 }finally{clearTimeout(timer)}
}
function safe(fn){return async(...args)=>{try{await fn(...args)}catch(e){toast(e.message)}}}
function stateFromUI(){const state={};for(const f of fields)state[f]=$(f).value;state.params={cot:$('cot').value,seed:Number($('seed').value),cfg_scale:$('cfg_scale').value===''?null:Number($('cfg_scale').value)};return state}
function fill(state){for(const f of fields)$(f).value=state[f]??'';$('cot').value=state.params.cot;$('seed').value=state.params.seed;$('cfg_scale').value=state.params.cfg_scale??'';updateHeading()}
function changed(){return project&&JSON.stringify(stateFromUI())!==JSON.stringify(project.state)}
function updateHeading(){$('breadcrumb-title').textContent=$('title').value||'未命名的歌';$('page-title').textContent=project?.parent?'好作品，值得再写一版。':'把你的故事，写成一首歌。';$('page-subtitle').textContent=project?.parent?'正在从已有版本继续创作。每一次生成都会保留之前的录音。':'不必懂乐理。从你想表达的那句话开始。';$('step1').className=$('lyrics').value?'':'current';$('step2').className=$('lyrics').value?'current':'';$('step3').className=project?.versions?.some(x=>x.status==='complete')?'current':''}
function scheduleSave(){$('save-status').textContent='有未保存的修改';clearTimeout(saveTimer);saveTimer=setTimeout(()=>flush().catch(e=>toast(e.message)),900);updateHeading()}
function flush(){clearTimeout(saveTimer);saveChain=saveChain.catch(()=>{}).then(async()=>{if(!project||!changed())return;const pid=project.id;const state=stateFromUI();$('save-status').textContent='正在保存…';try{const r=await api('/projects/'+pid,{revision:project.revision,state},'PUT');if(project.id===pid){Object.assign(project,r);$('save-status').textContent=changed()?'有未保存的修改':'已保存';updateHeading();renderSuggestions();await refreshList()}}catch(e){$('save-status').textContent='保存失败 · 请重试';throw e}});return saveChain}
async function refreshList(){const list=await api('/projects');$('project-count').textContent=list.length;$('project-list').innerHTML=list.map(p=>`<button data-project="${p.id}" class="${p.id===project?.id?'active':''}">${esc(p.title||'未命名的歌')}</button>`).join('')}
async function openProject(id){await flush();project=await api('/projects/'+id);selected=project.versions.find(v=>v.result?.preview)?.id||project.versions[0]?.id||null;history.replaceState(null,'','#'+id);fill(project.state);$('save-status').textContent='已保存';$('validation').textContent='';renderChat();renderSuggestions();renderVersions();renderTasks();await refreshList();lastPlayerState='';renderPlayer();renderAIStatus()}
async function createProject(){await flush();const p=await api('/projects',{});await openProject(p.id)}
async function boot(){try{await api('/me');$('avatar').textContent='我';await loadProvider();const list=await api('/projects');const hash=location.hash.slice(1);if(list.length)await openProject(list.some(x=>x.id===hash)?hash:list[0].id);else await createProject();await poll()}catch(e){$('save-status').textContent='连接失败 · 请刷新重试';$('engine-status').textContent='工作台连接失败';toast(e.message)}}
for(const f of [...fields,'cot','seed','cfg_scale'])$(f).addEventListener('input',scheduleSave);
$('save').onclick=safe(async()=>{await flush();toast('手稿已保存')});$('new-project').onclick=safe(createProject);
$('project-list').onclick=safe(async e=>{const b=e.target.closest('[data-project]');if(b)await openProject(b.dataset.project)});
$('templates').onclick=safe(async e=>{const b=e.target.closest('[data-scene]');if(b){$('intent').value=scenes[b.dataset.scene];scheduleSave()}});
$('format-lyrics').onclick=()=>{let s=$('lyrics').value.trim();if(!s){s='[Verse]\n\n[Chorus]\n'}else{s=s.replace(/【主歌】|\[主歌\]/g,'[Verse]').replace(/【副歌】|\[副歌\]/g,'[Chorus]').replace(/【桥段】|\[桥段\]/g,'[Bridge]');if(!s.startsWith('['))s='[Verse]\n'+s}$('lyrics').value=s;scheduleSave();toast('已整理段落标记，歌词内容保留。')};
$('undo').onclick=safe(async()=>{await flush();await api('/projects/'+project.id+'/undo',{revision:project.revision});await openProject(project.id);toast('已撤销上一次保存的修改')});
$('validate').onclick=safe(async()=>{await flush();const r=await api('/projects/'+project.id+'/validate',{});$('validation').textContent=r.valid?'✓ 输入检查通过'+(r.score?`\n乐谱：${r.score.bpm} BPM · 结构时长约 ${Math.round(r.score.nominal_duration_seconds)} 秒（不代表最终音频时长）`:''):'请调整：\n'+r.errors.join('\n');if(r.warnings.length)$('validation').textContent+='\n'+r.warnings.join('\n')});
function renderChat(){$('chat-history').innerHTML=project.messages.length?project.messages.map(x=>`<div class="chat-message ${x.role==='user'?'user':''}">${esc(x.body)}</div>`).join(''):'<p class="assistant-note">写下你的想法，我会给你几个容易比较的方向。已有歌词也可以一起打磨。</p>';$('chat-history').scrollTop=$('chat-history').scrollHeight}
const labels={title:'歌名',intent:'创作意图',constraints:'创作约束',language:'语言',mood:'情绪',vocal:'人声',summary:'创作概要',style:'风格提示',lyrics:'歌词',abc:'乐谱',params:'生成参数'};
function diff(before,after){return Object.keys(after).filter(k=>JSON.stringify(before[k])!==JSON.stringify(after[k])).map(k=>`<h4>${esc(labels[k]||k)}</h4><pre class="before">原内容\n${esc(typeof before[k]==='object'?JSON.stringify(before[k]):before[k]||'（空）')}</pre><pre class="after">建议内容\n${esc(typeof after[k]==='object'?JSON.stringify(after[k]):after[k]||'（空）')}</pre>`).join('')||'<p>项目内容没有变化。</p>'}
function renderSuggestions(){const s=project.suggestion;$('suggestion').innerHTML=!s||!s.body.choices?.length?'':s.body.choices.map((c,i)=>`<article class="suggestion-card"><h3>${esc(c.label)}</h3><p>${esc(c.explanation)}</p><details><summary>查看完整建议与修改差异</summary><div class="diff">${diff(project.state,c.state)}</div>${c.invariants?`<p>旋律与节奏校验：${c.invariants.match?'保持一致（允许速度改变）':esc(c.invariants.differences?.join('；')||'有变化')}。此检查不代表真实音频保持不变。</p>`:''}</details><button class="secondary" data-apply="${i}" ${s.revision!==project.revision?'disabled':''}>${s.revision!==project.revision?'手稿已改变，请重新获取建议':'应用这个'+(s.body.choices.length>1?'方向':'修改')}</button></article>`).join('')}
let aiNotice=null;
function renderAIStatus(){
 const current=aiNotice?.pid===project?.id?aiNotice:null;
 for(const id of ['send','develop']){$(id).disabled=busyAI;$(id).setAttribute('aria-busy',String(busyAI))}
 $('develop').textContent=busyAI?'正在构思…':'一起构思 ↗';
 $('send').textContent=busyAI?'正在构思…':'发送给创作伙伴 ↗';
 for(const id of ['idea-status','assistant-status']){
  const box=$(id);box.hidden=!current;if(!current)continue;
  box.dataset.state=current.state;
  const elapsed=Math.floor((Date.now()-current.started)/1000);
  const detail=current.state==='waiting'?`正在等待创作助手回复 · 已等待 ${elapsed} 秒。${elapsed>=30?'服务商回复较慢，请稍候；无需重复点击。':'方向和歌词准备好后会显示在下方。'}`:current.message;
  box.innerHTML=`<span>${esc(detail)}</span>${current.state==='complete'?'<button data-ai-view>查看创作回复 ↓</button>':current.state==='failed'?'<button data-ai-retry>重试构思 ↗</button>':''}`;
 }
}
for(const id of ['idea-status','assistant-status'])$(id).onclick=safe(async e=>{
 if(e.target.closest('[data-ai-view]')){document.querySelector('.copilot').scrollIntoView({behavior:'smooth',block:'start'})}
 if(e.target.closest('[data-ai-retry]')&&aiNotice?.pid===project?.id)await ask(aiNotice.text);
});
async function ask(text){
 if(busyAI)return;
 if(!project){toast('手稿尚未载入，请稍候或刷新页面。');return}
 if(!provider?.configured){$('settings').showModal();toast('请连接创作大模型；也可以直接填写歌词和音乐方向。');return}
 if(!text.trim()){toast('先写下一句想法。');return}
 const pid=project.id;busyAI=true;
 aiNotice={pid,text,state:'saving',started:Date.now(),message:'正在保存最新手稿…'};renderAIStatus();
 const timer=setInterval(renderAIStatus,1000);
 try{
  await flush();if(project.id!==pid)throw Error('已切换歌曲，请在当前歌曲重新发起构思。');
  aiNotice.state='waiting';renderAIStatus();
  const r=await api('/projects/'+pid+'/chat',{message:text,revision:project.revision});
  aiNotice.state='complete';aiNotice.message=r.outdated?'创作回复已保存。等待期间手稿有更新，建议供参考，最新内容不会被覆盖。':r.body.choices?.length?'创作建议已准备好，查看后选择一个方向应用。':'创作助手已回复，请查看下方内容。';
  if(project.id===pid){project.messages.push({role:'user',body:text},{role:'assistant',body:r.body.message});project.suggestion=r;$('feedback').value='';renderChat();renderSuggestions();document.querySelector('.copilot').scrollIntoView({behavior:'smooth',block:'start'})}
  toast(aiNotice.message);
 }catch(e){aiNotice.state='failed';aiNotice.message=e.message;toast(e.message)}
 finally{clearInterval(timer);busyAI=false;renderAIStatus()}
}
$('develop').onclick=safe(() =>ask($('intent').value));$('send').onclick=safe(()=>ask($('feedback').value));

document.querySelector('.feedback-chips').onclick=safe(async e=>{const b=e.target.closest('[data-feedback]');if(b){$('feedback').value=b.dataset.feedback;$('feedback').focus()}});
$('suggestion').onclick=safe(async e=>{const b=e.target.closest('[data-apply]');if(!b)return;await flush();await api('/projects/'+project.id+'/suggestions/'+project.suggestion.id+'/apply',{revision:project.revision,choice:Number(b.dataset.apply)});await openProject(project.id);toast('已应用建议，随时可以撤销。确认歌词后点击生成。')});
async function generate(kind='audio',reuse=null){if(submitting)return;submitting=true;try{await flush();$('generate').disabled=$('plan').disabled=true;let key;const storageKey='studio-submit:'+project.id+':'+project.revision+':'+kind+':'+(reuse||'');key=sessionStorage.getItem(storageKey)||crypto.randomUUID();sessionStorage.setItem(storageKey,key);const result=await api('/projects/'+project.id+'/generate',{revision:project.revision,idempotency_key:key,kind,reuse_version:reuse});sessionStorage.removeItem(storageKey);selected=result.version;toast(result.duplicate?'已找到对应任务，没有重复提交。':'已加入生成队列，可以继续编辑下一版。');await poll();$('tasks').scrollIntoView({behavior:'smooth',block:'center'})}finally{submitting=false;$('generate').disabled=$('plan').disabled=false}}
$('generate').onclick=safe(()=>generate());$('plan').onclick=safe(()=>generate('plan'));
function renderTasks(){const mine=jobs.filter(j=>j.project===project?.id);const active=mine.filter(j=>j.status==='running'||j.status==='queued');const relevant=[...active,...mine.filter(j=>j.status!=='running'&&j.status!=='queued').slice(0,2)];$('tasks').innerHTML=relevant.map(j=>`<article class="task ${j.status==='failed'?'failed':''}"><div><strong>${esc(stages[j.stage]||j.stage)} · ${esc(j.name)}</strong><p>${esc(j.error?.message||j.events.at(-1)?.detail||'已记录任务')}</p><details><summary>任务记录 · ${j.id.slice(0,8)}</summary>${j.events.map(e=>`<p>${new Date(e.created*1000).toLocaleTimeString('zh-CN')} · ${esc(stages[e.stage]||e.stage)} · ${esc(e.detail)}</p>`).join('')}</details></div>${['queued','running'].includes(j.status)?`<button data-cancel="${j.id}" ${j.cancel?'disabled':''}>${j.cancel?'等待取消生效':j.status==='queued'?'取消排队':'停止此任务'}</button>`:j.status==='failed'||j.status==='cancelled'?`<button data-retry="${j.version}">载入此版重试</button>`:''}</article>`).join('');}
$('tasks').onclick=safe(async e=>{const cancel=e.target.closest('[data-cancel]');if(cancel){const r=await api('/jobs/'+cancel.dataset.cancel+'/cancel',{});toast(r.message);await poll()}const retry=e.target.closest('[data-retry]');if(retry){await continueVersion(retry.dataset.retry);toast('已载入该版本。确认内容后点击生成，创建独立的新任务。')}});
function renderVersions(){$('version-count').textContent=project.versions.length+' 个版本';$('versions').innerHTML=project.versions.length?project.versions.map(v=>`<button class="version ${v.id===selected?'active':''}" data-version="${v.id}"><strong>${esc(v.name)}</strong><small>${esc(stages[v.status]||v.status)} · ${v.result?.audio_seconds?Math.round(v.result.audio_seconds)+' 秒 · ':''}${new Date(v.created*1000).toLocaleDateString('zh-CN')}${v.parent?' · 从已有版本续作':''}</small></button>`).join(''):'<p class="small muted" style="padding:20px">你的版本会按创作顺序保存在这里。</p>'}
$('versions').onclick=e=>{const b=e.target.closest('[data-version]');if(b){selected=b.dataset.version;renderVersions();lastPlayerState='';renderPlayer()}};
function renderPlayer(){const v=project?.versions.find(v=>v.id===selected);if(!v){$('player').innerHTML='<div class="empty-disc">♪</div><h3>第一首歌，从这里响起。</h3><p class="muted">生成完成后，在这里试听、下载，或继续写下一版。</p>';lastPlayerState='';return}const signature=JSON.stringify([v.id,v.status,v.name,v.result]);if(signature===lastPlayerState)return;lastPlayerState=signature;const r=v.result;let media='';if(r?.preview){const url='/api/versions/'+v.id+'/artifacts/';media=`<div class="waveform" aria-label="根据真实音频提取的振幅波形">${r.peaks.map(n=>`<i style="height:${Math.max(2,Math.min(56,n*56))}px"></i>`).join('')}</div><audio controls preload="metadata" src="${url+r.preview}"></audio><p>${Math.round(r.audio_seconds)} 秒 · 48 kHz 立体声 · YuE2-Vae 试听解码器</p>${Object.values(r.truncated||{}).some(Boolean)?'<p class="error">模型触及生成上限，这段音频可能没有完整结束。请试听后决定是否修改重试。</p>':''}<div class="player-actions"><a href="${url}audio.flac" download="${esc(v.name)}.flac">↓ 下载无损音频</a><button data-continue="${v.id}">从此版继续修改 ↗</button><button data-rename="${v.id}">重命名</button></div>`}else media=`<div class="empty-disc">${v.kind==='plan'?'♮':'♪'}</div><p>${v.status==='complete'?'音乐方案已完成。可以直接复用，或载入手稿检查与编辑。':'此版本尚未生成音频，请查看上方任务状态。'}</p><div class="player-actions"><button data-continue="${v.id}">载入此版手稿</button>${v.status==='complete'&&v.kind==='plan'?`<button data-render-plan="${v.id}">使用此方案生成歌曲</button>`:''}</div>`;$('player').innerHTML=`<span class="eyebrow">${v.kind==='plan'?'MUSIC PLAN':'YOUR RECORDING'}</span><h3>${esc(v.name)}</h3>${media}<details><summary>查看此版本的歌词、音乐方案与生成记录</summary><p>${esc(v.snapshot.summary)}</p><pre>${esc(v.snapshot.lyrics)}</pre><p>${esc(v.snapshot.style)}</p><pre>${esc(r?.abc||v.snapshot.abc||'此版本没有乐谱')}</pre><pre>${esc(JSON.stringify({params:v.snapshot.params,truncated:r?.truncated,model:r?.weights,source_version:v.parent,version:v.id},null,2))}</pre><a href="/api/versions/${v.id}/artifacts/input.json" download>下载输入快照</a></details>`}
async function continueVersion(vid){await flush();await api('/versions/'+vid+'/continue',{revision:project.revision});await openProject(project.id);selected=vid;renderVersions();renderPlayer();$('lyrics').scrollIntoView({behavior:'smooth',block:'center'})}
$('player').onclick=safe(async e=>{const next=e.target.closest('[data-continue]');if(next){await continueVersion(next.dataset.continue);toast('已载入此版本的歌词与乐谱。可以直接提出修改。')}const rename=e.target.closest('[data-rename]');if(rename){const v=project.versions.find(x=>x.id===rename.dataset.rename);const name=prompt('给这个版本起个名字',v.name);if(name?.trim()){await api('/versions/'+v.id+'/name',{name:name.trim()},'PUT');await poll()}}const plan=e.target.closest('[data-render-plan]');if(plan){const v=project.versions.find(x=>x.id===plan.dataset.renderPlan);await flush();const r=await api('/projects/'+project.id,{revision:project.revision,state:v.snapshot},'PUT');Object.assign(project,r);fill(r.state);await generate('audio',v.id)}});
async function poll(){if(polling||$('workspace').hidden||!project)return;polling=true;const pid=project.id;try{const [h,js]=await Promise.all([api('/health'),api('/jobs')]);jobs=js;$('engine-status').textContent=h.worker.state==='loading'?'模型加载中':h.worker.active_job?'YuE2 正在创作':h.worker.state==='ready'?'YuE2 已就绪':'YuE2 · 按需加载';const key=JSON.stringify(jobs);if(key!==lastJobState){lastJobState=key;const p=await api('/projects/'+pid);if(project.id!==pid)return;project.versions=p.versions;if(!selected)selected=p.versions[0]?.id;renderTasks();renderVersions();renderPlayer();updateHeading()}}finally{polling=false}}
setInterval(()=>poll().catch(e=>{if(e.status!==401)$('engine-status').textContent='连接中断 · 自动重连中'}),3000);
async function loadProvider(){provider=await api('/provider');$('api-hint').textContent=provider.configured?'创作助手已连接：'+provider.name+' · '+provider.model:'还未连接创作助手？可在设置中配置 API，或先粘贴歌词手动创作。';$('provider-name').value=provider.name||'我的创作助手';$('provider-url').value=provider.base_url||'';$('provider-model').value=provider.model||'';$('provider-key').value='';$('provider-key').placeholder=provider.configured?'密钥已保存，留空保持不变':'粘贴 API Key'}
$('settings-open').onclick=safe(async()=>{await loadProvider();$('provider-result').textContent='';$('settings').showModal()});$('settings-close').onclick=()=>{$('provider-key').value='';$('settings').close()};$('settings').addEventListener('close',()=>$('provider-key').value='');
$('provider-form').addEventListener('submit',async e=>{e.preventDefault();e.submitter.disabled=true;try{await api('/provider',{name:$('provider-name').value,base_url:$('provider-url').value,model:$('provider-model').value,api_key:$('provider-key').value||null},'PUT');await loadProvider();$('provider-result').textContent='配置已加密保存。点击测试连接以验证真实 API。'}catch(e){$('provider-result').textContent=e.message}finally{e.submitter.disabled=false}});
$('provider-test').onclick=async()=>{$('provider-test').disabled=true;$('provider-result').textContent='正在请求服务商…';try{const r=await api('/provider/test',{});$('provider-result').textContent=r.message}catch(e){$('provider-result').textContent=e.message}finally{$('provider-test').disabled=false}};
$('provider-delete').onclick=safe(async()=>{await api('/provider',undefined,'DELETE');await loadProvider();$('provider-result').textContent='已删除服务商配置和加密密钥。'});
boot();
