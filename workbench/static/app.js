'use strict';
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let modelState=null,modelAction=false;
let project=null,jobs=[],selected=null,saveTimer=null,saveChain=Promise.resolve(),submitting=false,polling=false,lastJobState='',lastPlayerState='';
const fields=['title','language','mood','vocal','summary','style','lyrics','abc'];
const stages={running:'生成中',queued:'排队中',loading:'模型加载中',planning:'音乐规划中',semantic:'演唱与伴奏生成中',synthesis:'音频合成中',decoding:'音频解码中',complete:'已完成',failed:'生成失败',interrupted:'服务重启中断',cancelled:'已取消',cancel_requested:'等待安全停止点'};
function toast(text){$('toast').textContent=text;$('toast').style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').style.display='none',6500)}
async function api(path,data,method){
 const controller=new AbortController();
 const limit=20000;
 const timer=setTimeout(()=>controller.abort(),limit);
 try{
  const r=await fetch('/api'+path,{method:method||(data===undefined?'GET':'POST'),headers:{'Content-Type':'application/json','X-Studio-Request':'1'},body:data===undefined?undefined:JSON.stringify(data),signal:controller.signal});
  let result;try{result=await r.json()}catch(e){if(e.name==='AbortError')throw e;throw Error('服务器返回异常，请检查服务并重试。')}
  if(!r.ok){const e=Error(result.error?.message||'操作未完成');e.code=result.error?.code;e.status=r.status;throw e}
  return result;
 }catch(e){
  if(e.name==='AbortError')throw Error('连接超时，请检查网络后重试。');
  if(e instanceof TypeError)throw Error('连接中断，请检查网络后重试。');
  throw e;
 }finally{clearTimeout(timer)}
}
function safe(fn){return async(...args)=>{try{await fn(...args)}catch(e){toast(e.message)}}}
function stateFromUI(){const state={...project.state};for(const f of fields)state[f]=$(f).value;state.params={...project.state.params,cot:$('cot').value,seed:Number($('seed').value)};return state}
function fill(state){for(const f of fields)$(f).value=state[f]??'';$('cot').value=state.params.cot;$('seed').value=state.params.seed;updateHeading()}
function changed(){return project&&JSON.stringify(stateFromUI())!==JSON.stringify(project.state)}
function updateHeading(){$('breadcrumb-title').textContent=$('title').value||'未命名的歌';$('page-title').textContent=project?.parent?'好作品，值得再写一版。':'把你的故事，写成一首歌。';$('page-subtitle').textContent=project?.parent?'正在从已有版本继续创作。每一次生成都会保留之前的录音。':'填入音乐风格与歌词，让 YuE2 直接生成歌曲。';$('step1').className=$('lyrics').value?'':'current';$('step2').className=$('lyrics').value?'current':'';$('step3').className=project?.versions?.some(x=>x.status==='complete')?'current':''}
function scheduleSave(){$('save-status').textContent='有未保存的修改';clearTimeout(saveTimer);saveTimer=setTimeout(()=>flush().catch(e=>toast(e.message)),900);updateHeading()}
function flush(){clearTimeout(saveTimer);saveChain=saveChain.catch(()=>{}).then(async()=>{if(!project||!changed())return;const pid=project.id;const state=stateFromUI();$('save-status').textContent='正在保存…';try{const r=await api('/projects/'+pid,{revision:project.revision,state},'PUT');if(project.id===pid){Object.assign(project,r);$('save-status').textContent=changed()?'有未保存的修改':'已保存';updateHeading();await refreshList()}}catch(e){$('save-status').textContent='保存失败 · 请重试';throw e}});return saveChain}
async function refreshList(){const list=await api('/projects');$('project-count').textContent=list.length;$('project-list').innerHTML=list.map(p=>`<button data-project="${p.id}" class="${p.id===project?.id?'active':''}">${esc(p.title||'未命名的歌')}</button>`).join('')}
async function openProject(id){await flush();project=await api('/projects/'+id);selected=project.versions.find(v=>v.result?.preview)?.id||project.versions[0]?.id||null;history.replaceState(null,'','#'+id);fill(project.state);$('save-status').textContent='已保存';$('validation').textContent='';renderVersions();renderTasks();await refreshList();lastPlayerState='';renderPlayer();}
async function createProject(){await flush();const p=await api('/projects',{});await openProject(p.id)}
async function boot(){try{await api('/me');$('avatar').textContent='我';const list=await api('/projects');const hash=location.hash.slice(1);if(list.length)await openProject(list.some(x=>x.id===hash)?hash:list[0].id);else await createProject();await poll()}catch(e){$('save-status').textContent='连接失败 · 请刷新重试';$('engine-status').textContent='工作台连接失败';toast(e.message)}}
for(const f of [...fields,'cot','seed'])$(f).addEventListener('input',scheduleSave);
$('save').onclick=safe(async()=>{await flush();toast('手稿已保存')});$('new-project').onclick=safe(createProject);
$('project-list').onclick=safe(async e=>{const b=e.target.closest('[data-project]');if(b)await openProject(b.dataset.project)});
$('format-lyrics').onclick=()=>{let s=$('lyrics').value.trim();if(!s){s='[Verse]\n\n[Chorus]\n'}else{s=s.replace(/【主歌】|\[主歌\]/g,'[Verse]').replace(/【副歌】|\[副歌\]/g,'[Chorus]').replace(/【桥段】|\[桥段\]/g,'[Bridge]');if(!s.startsWith('['))s='[Verse]\n'+s}$('lyrics').value=s;scheduleSave();toast('已整理段落标记，歌词内容保留。')};
$('undo').onclick=safe(async()=>{await flush();await api('/projects/'+project.id+'/undo',{revision:project.revision});await openProject(project.id);toast('已撤销上一次保存的修改')});
$('validate').onclick=safe(async()=>{await flush();const r=await api('/projects/'+project.id+'/validate',{});$('validation').textContent=r.valid?'✓ 输入检查通过'+(r.score?`\n乐谱：${r.score.bpm} BPM · 结构时长约 ${Math.round(r.score.nominal_duration_seconds)} 秒（不代表最终音频时长）`:''):'请调整：\n'+r.errors.join('\n');if(r.warnings.length)$('validation').textContent+='\n'+r.warnings.join('\n')});
async function generate(kind='audio',reuse=null){if(submitting)return;if(modelState&&!modelState.ready){await openModels();toast('先准备音乐模型，当前手稿会自动保留。');return;}submitting=true;try{await flush();$('generate').disabled=$('plan').disabled=true;let key;const storageKey='studio-submit:'+project.id+':'+project.revision+':'+kind+':'+(reuse||'');key=sessionStorage.getItem(storageKey)||crypto.randomUUID();sessionStorage.setItem(storageKey,key);const result=await api('/projects/'+project.id+'/generate',{revision:project.revision,idempotency_key:key,kind,reuse_version:reuse});sessionStorage.removeItem(storageKey);selected=result.version;toast(result.duplicate?'已找到对应任务，没有重复提交。':'已加入生成队列，可以继续编辑下一版。');await poll();$('tasks').scrollIntoView({behavior:'smooth',block:'center'})}finally{submitting=false;$('generate').disabled=$('plan').disabled=false}}
$('generate').onclick=safe(()=>generate());$('plan').onclick=safe(()=>generate('plan'));
function renderTasks(){const mine=jobs.filter(j=>j.project===project?.id);const active=mine.filter(j=>j.status==='running'||j.status==='queued');const relevant=[...active,...mine.filter(j=>j.status!=='running'&&j.status!=='queued').slice(0,2)];$('tasks').innerHTML=relevant.map(j=>`<article class="task ${j.status==='failed'?'failed':''}"><div><strong>${esc(stages[j.stage]||j.stage)} · ${esc(j.name)}</strong><p>${esc(j.error?.message||j.events.at(-1)?.detail||'已记录任务')}</p><details><summary>任务记录 · ${j.id.slice(0,8)}</summary>${j.events.map(e=>`<p>${new Date(e.created*1000).toLocaleTimeString('zh-CN')} · ${esc(stages[e.stage]||e.stage)} · ${esc(e.detail)}</p>`).join('')}</details></div>${['queued','running'].includes(j.status)?`<button data-cancel="${j.id}" ${j.cancel?'disabled':''}>${j.cancel?'等待取消生效':j.status==='queued'?'取消排队':'停止此任务'}</button>`:j.status==='failed'||j.status==='cancelled'?`<button data-retry="${j.version}">载入此版重试</button>`:''}</article>`).join('');}
$('tasks').onclick=safe(async e=>{const cancel=e.target.closest('[data-cancel]');if(cancel){const r=await api('/jobs/'+cancel.dataset.cancel+'/cancel',{});toast(r.message);await poll()}const retry=e.target.closest('[data-retry]');if(retry){await continueVersion(retry.dataset.retry);toast('已载入该版本。确认内容后点击生成，创建独立的新任务。')}});
function renderVersions(){$('version-count').textContent=project.versions.length+' 个版本';$('versions').innerHTML=project.versions.length?project.versions.map(v=>`<button class="version ${v.id===selected?'active':''}" data-version="${v.id}"><strong>${esc(v.name)}</strong><small>${esc(stages[v.status]||v.status)} · ${v.result?.audio_seconds?Math.round(v.result.audio_seconds)+' 秒 · ':''}${new Date(v.created*1000).toLocaleDateString('zh-CN')}${v.parent?' · 从已有版本续作':''}</small></button>`).join(''):'<p class="small muted" style="padding:20px">你的版本会按创作顺序保存在这里。</p>'}
$('versions').onclick=e=>{const b=e.target.closest('[data-version]');if(b){selected=b.dataset.version;renderVersions();lastPlayerState='';renderPlayer()}};
function renderPlayer(){const v=project?.versions.find(v=>v.id===selected);if(!v){$('player').innerHTML='<div class="empty-disc">♪</div><h3>第一首歌，从这里响起。</h3><p class="muted">生成完成后，在这里试听、下载，或继续写下一版。</p>';lastPlayerState='';return}const signature=JSON.stringify([v.id,v.status,v.name,v.result]);if(signature===lastPlayerState)return;lastPlayerState=signature;const r=v.result;let media='';if(r?.preview){const url='/api/versions/'+v.id+'/artifacts/';media=`<div class="waveform" aria-label="根据真实音频提取的振幅波形">${r.peaks.map(n=>`<i style="height:${Math.max(2,Math.min(56,n*56))}px"></i>`).join('')}</div><audio controls preload="metadata" src="${url+r.preview}"></audio><p>${Math.round(r.audio_seconds)} 秒 · 48 kHz 立体声 · YuE2-Vae 试听解码器</p>${Object.values(r.truncated||{}).some(Boolean)?'<p class="error">模型触及生成上限，这段音频可能没有完整结束。请试听后决定是否修改重试。</p>':''}<div class="player-actions"><a href="${url}audio.flac" download="${esc(v.name)}.flac">↓ 下载无损音频</a><button data-continue="${v.id}">从此版继续修改 ↗</button><button data-rename="${v.id}">重命名</button></div>`}else media=`<div class="empty-disc">${v.kind==='plan'?'♮':'♪'}</div><p>${v.status==='complete'?'音乐方案已完成。可以直接复用，或载入手稿检查与编辑。':'此版本尚未生成音频，请查看上方任务状态。'}</p><div class="player-actions"><button data-continue="${v.id}">载入此版手稿</button>${v.status==='complete'&&v.kind==='plan'?`<button data-render-plan="${v.id}">使用此方案生成歌曲</button>`:''}</div>`;$('player').innerHTML=`<span class="eyebrow">${v.kind==='plan'?'MUSIC PLAN':'YOUR RECORDING'}</span><h3>${esc(v.name)}</h3>${media}<details><summary>查看此版本的歌词、音乐方案与生成记录</summary><p>${esc(v.snapshot.summary)}</p><pre>${esc(v.snapshot.lyrics)}</pre><p>${esc(v.snapshot.style)}</p><pre>${esc(r?.abc||v.snapshot.abc||'此版本没有乐谱')}</pre><pre>${esc(JSON.stringify({params:v.snapshot.params,truncated:r?.truncated,model:r?.weights,source_version:v.parent,version:v.id},null,2))}</pre><a href="/api/versions/${v.id}/artifacts/input.json" download>下载输入快照</a></details>`}
async function continueVersion(vid){await flush();await api('/versions/'+vid+'/continue',{revision:project.revision});await openProject(project.id);selected=vid;renderVersions();renderPlayer();$('lyrics').scrollIntoView({behavior:'smooth',block:'center'})}
$('player').onclick=safe(async e=>{const next=e.target.closest('[data-continue]');if(next){await continueVersion(next.dataset.continue);toast('已载入此版本的歌词与乐谱。编辑手稿后可生成新版本。')}const rename=e.target.closest('[data-rename]');if(rename){const v=project.versions.find(x=>x.id===rename.dataset.rename);const name=prompt('给这个版本起个名字',v.name);if(name?.trim()){await api('/versions/'+v.id+'/name',{name:name.trim()},'PUT');await poll()}}const plan=e.target.closest('[data-render-plan]');if(plan){const v=project.versions.find(x=>x.id===plan.dataset.renderPlan);await flush();const r=await api('/projects/'+project.id,{revision:project.revision,state:v.snapshot},'PUT');Object.assign(project,r);fill(r.state);await generate('audio',v.id)}});
async function poll(){if(polling||$('workspace').hidden||!project)return;polling=true;const pid=project.id;try{const [h,js]=await Promise.all([api('/health'),api('/jobs')]);jobs=js;renderModels(h.models);$('engine-status').textContent=!h.models?.ready?'YuE2 · 模型待准备':h.worker.state==='loading'?'模型加载中':h.worker.active_job?'YuE2 正在创作':h.worker.state==='ready'?'YuE2 已就绪':'YuE2 · 按需加载';const key=JSON.stringify(jobs);if(key!==lastJobState){lastJobState=key;const p=await api('/projects/'+pid);if(project.id!==pid)return;project.versions=p.versions;if(!selected)selected=p.versions[0]?.id;renderTasks();renderVersions();renderPlayer();updateHeading()}}finally{polling=false}}
setInterval(()=>poll().catch(e=>{if(e.status!==401)$('engine-status').textContent='连接中断 · 自动重连中'}),3000);
function formatBytes(n){return (n/1e9).toFixed(2)+' GB'}
function renderModels(s){
 if(!s)return;modelState=s;
 const active=['checking','preparing','downloading','verifying','cancelling'].includes(s.state);
 $('model-banner').hidden=s.ready;
 $('model-banner-title').textContent=s.state==='missing'?'等待准备音乐模型':s.state==='ready'?'音乐模型已就绪':s.state==='failed'?'模型准备需要处理':s.state==='paused'?'模型准备已暂停':'正在准备音乐模型';
 const detail=[s.source_note,s.detail].filter(Boolean).join(' ');
 $('model-banner-detail').textContent=detail;
 $('models-banner-open').textContent=active?'查看进度':'查看模型状态';
 $('model-status').textContent=detail;
 if(s.source==='mirror'||s.source==='official'||s.state==='failed')$('model-network-fallback').open=true;
 $('model-progress').value=Math.min(s.completed_bytes,s.total_bytes);$('model-progress').max=s.total_bytes||1;
 $('model-bytes').textContent=formatBytes(s.completed_bytes)+' / '+formatBytes(s.total_bytes)+(s.state==='verifying'?' · 正在校验':'');
 $('model-file').textContent=s.current_file||'';
 $('model-download').disabled=active||s.ready||modelAction;
 $('model-prepare').disabled=active||s.ready||modelAction;
 $('model-prepare').textContent=s.ready?'模型已就绪':active?(s.source==='mirror'||s.source==='official'?'正在网络下载…':'正在自动准备…'):'继续准备模型';
 $('model-download').textContent=s.ready?'模型已就绪':'使用网络下载';
 $('model-cancel').hidden=!s.can_cancel;$('model-cancel').disabled=s.state==='cancelling'||modelAction;
 $('model-check').disabled=active||modelAction;
 $('model-source').disabled=active||modelAction;
 $('model-cancel').textContent=s.state==='cancelling'?'正在暂停…':'暂停准备';
 $('model-manifest').textContent=s.models.map(m=>m.repo+'\n'+m.revision).join('\n\n')+'\n\n'+s.cache_dir;
}
async function openModels(){await flush();$('models-dialog').showModal();renderModels(await api('/models'))}
$('models-open').onclick=safe(openModels);$('models-banner-open').onclick=safe(openModels);
$('models-close').onclick=()=>$('models-dialog').close();
async function modelOperation(action){if(modelAction)return;modelAction=true;renderModels(modelState);try{renderModels(await api('/models/'+action,action==='download'?{source:$('model-source').value}:{}))}finally{modelAction=false;renderModels(modelState)}}
$('model-download').onclick=safe(()=>modelOperation('download'));
$('model-prepare').onclick=safe(()=>modelOperation('prepare'));
$('model-check').onclick=safe(()=>modelOperation('check'));
$('model-cancel').onclick=safe(()=>modelOperation('cancel'));
boot();
