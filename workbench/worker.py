"""One persistent GPU worker, durable stage events, safe exact-plan reuse."""
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from store import db, now, dump, transition, artifact_dir, DATA
from domain import Song, fingerprint, validate_song
from models import CACHE, manager

log = logging.getLogger('studio.worker')
MODEL = 'm-a-p/YuE2-3B'
MODEL_REV = '1a96eca688d6ae5d7f0feb88573fec89920fcd19'
VAE = 'm-a-p/YuE2-Vae'
VAE_REV = '95535e72a97bc0f09b8ada125d26b4009428c0e8'
CODE_REV = '92a73cc7652fcc1f937855e4b765e0a0edd7ff2e'
health = {'state': 'idle', 'detail': '模型将在第一次生成时加载', 'active_job': None}
signal = threading.Event()
stopping = threading.Event()

def write(path,value):
    path.write_text(dump(value),encoding='utf-8')

def classify(exc):
    name = type(exc).__name__
    if isinstance(exc, InterruptedError): return 'cancelled', '任务已取消，已完成的阶段记录已保留。'
    if 'OutOfMemory' in name or 'out of memory' in str(exc).lower():
        return 'gpu_memory', '显存不足。当前版本已保留；请检查是否有其他 GPU 程序，释放显存后重试。不会自动降低生成设置。'
    if isinstance(exc, (FileNotFoundError, OSError)): return 'storage_or_model', '模型或产物文件不可用。请检查缓存、磁盘空间及服务日志后重试。'
    if isinstance(exc, ValueError): return 'input_or_artifact', '模型输入或阶段产物未通过检查。请检查歌词与乐谱；修改后生成新版本。'
    return 'inference_failed', '音乐生成失败。输入和完成的阶段已保留，可重试；如再次失败，请根据任务编号查看服务日志。'

def run_job(job, pipe):
    import numpy as np
    import soundfile as sf
    from yue2 import SymbolicPlan
    from yue2.pipeline import SongResult
    from yue2.storage import identity
    from yue2.protocol import SongRequest
    version = job['version']
    with db() as c: row = dict(c.execute('SELECT * FROM versions WHERE id=?',(version,)).fetchone())
    state = json.loads(row['snapshot'])
    song = Song.model_validate(state)
    out = artifact_dir(version)
    out.mkdir(parents=True, exist_ok=False)
    write(out/'input.json',state)
    def cancelled():
        if stopping.is_set(): return True
        with db() as c: return bool(c.execute('SELECT cancel FROM jobs WHERE id=?',(job['id'],)).fetchone()[0])
    def checkpoint():
        if cancelled(): raise InterruptedError('Cancelled')
    request = SongRequest(style=song.style, lyrics=song.lyrics, abc=song.abc or None,
                          cot=song.params.cot, seed=song.params.seed, cfg_scale=song.params.cfg_scale, id=version)
    checkpoint()
    start = time.perf_counter()
    plan = None
    if job['reuse_version']:
        with db() as c:
            source = c.execute('SELECT * FROM versions WHERE id=? AND user=?',(job['reuse_version'],job['user'])).fetchone()
        if not source or source['fingerprint'] != row['fingerprint']: raise ValueError('Reuse input mismatch')
        source_path = artifact_dir(source['id'])
        provenance = json.loads((source_path/'stage-provenance.json').read_text())
        if provenance['weights'] != pipe.weights or provenance['runtime'] != pipe.runtime_sha256:
            raise ValueError('Reuse model/runtime mismatch')
        plan = SymbolicPlan.load(source_path)
        expected = request.to_dict(); restored = plan.request.to_dict()
        expected.pop('id'); restored.pop('id')
        if expected != restored: raise ValueError('Reuse request mismatch')
        transition(job['id'],'planning',detail='输入、模型和文件校验通过，复用精确音乐方案；重新生成演唱与伴奏')
    else:
        transition(job['id'],'planning',detail='使用已有乐谱' if song.abc else ('此模式不生成乐谱' if song.params.cot=='off' else '正在规划旋律与和弦'))
        plan = pipe.plan(request=request, cancelled=cancelled)
    plan.save(out)
    write(out/'stage-provenance.json',{'weights':pipe.weights,'runtime':pipe.runtime_sha256,'input_fingerprint':row['fingerprint'],'code_commit':CODE_REV,'reuse_version':job['reuse_version']})
    if plan.abc:
        validation = validate_song({**state,'abc':plan.abc},generation=False)
        write(out/'score-check.json',validation)
        if not validation['valid']:
            # Generated native notation can exceed the bounded portable parser; retain the warning.
            transition(job['id'],'planning',detail='模型生成的乐谱有格式检查警告，已保留报告；高级编辑时必须校验通过')
    checkpoint()
    if job['kind']=='plan':
        result={'abc':plan.abc,'truncated':{'abc':plan.truncated},'weights':pipe.weights,'kind':'plan'}
        with db() as c: c.execute('UPDATE versions SET result=? WHERE id=?',(dump(result),version))
        transition(job['id'],'complete','complete','音乐方案已保存，可检查后生成歌曲')
        return
    transition(job['id'],'semantic',detail='正在生成演唱与伴奏内容')
    semantic = pipe.generate_semantic(plan,cancelled=cancelled)
    np.save(out/'semantic.npy',np.asarray(semantic.tokens,dtype=np.int32))
    write(out/'semantic-stage.json',{'timing':semantic.timing,'truncated':semantic.truncated})
    checkpoint()
    transition(job['id'],'synthesis',detail='正在合成音频')
    t=time.perf_counter()
    latent=pipe.synthesize(semantic,cancelled=cancelled)
    nar_seconds=time.perf_counter()-t
    np.save(out/'latent.npy',latent.astype(np.float32))
    checkpoint()
    transition(job['id'],'decoding',detail='正在解码为可试听音频，此阶段取消会在解码结束后生效')
    t=time.perf_counter()
    audio=pipe.decode(latent)
    checkpoint()
    config=pipe.effective_config(plan.request)
    timing={'abc':plan.timing,'semantic':semantic.timing,'nar_seconds':nar_seconds,'vae_seconds':time.perf_counter()-t,'load':dict(pipe.load_timing),'e2e_seconds':time.perf_counter()-start}
    request_id=identity({'request':plan.request.to_dict(),'config':config,'weights':pipe.weights})
    result=SongResult(audio,48000,semantic,latent,config,pipe.weights,timing,request_id).save_artifacts(out)
    preview='audio.flac'
    try:
        sf.write(out/'preview.mp3',audio,48000)
        preview='preview.mp3'
    except Exception:
        pass
    # Binned peaks come exclusively from the generated waveform.
    mono=np.max(np.abs(audio),axis=1)
    peaks=[float(np.max(x)) for x in np.array_split(mono,min(180,len(mono))) if len(x)]
    result.update({'abc':plan.abc,'preview':preview,'peaks':peaks,'kind':'audio','code_commit':CODE_REV})
    with db() as c: c.execute('UPDATE versions SET result=? WHERE id=?',(dump(result),version))
    transition(job['id'],'complete','complete','歌曲已完成；请试听判断效果' + ('。注意：模型触及 token 上限，作品可能未完整结束' if any(result['truncated'].values()) else ''))

def recover_interrupted():
    with db() as c:
        rows=c.execute("SELECT id FROM jobs WHERE status IN ('queued','running')").fetchall()
    for row in rows:
        transition(row['id'],'interrupted','failed','服务已重启，任务不会自动重放，请检查后手动重试',dump({'code':'service_restart','message':'服务重启中断了任务。为避免重复推理，请手动重试。'}))

def loop():
    pipe=None
    # Process-wide lock prevents accidental two-server GPU contention.
    lock=open(DATA/'gpu.lock','a')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        health.update(state='error',detail='另一个工作台进程正在使用 GPU 队列')
        return
    recover_interrupted()
    while not stopping.is_set():
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            job=c.execute("SELECT j.*,v.kind FROM jobs j JOIN versions v ON v.id=j.version WHERE j.status='queued' ORDER BY j.created LIMIT 1").fetchone()
            if job:
                job=dict(job)
                c.execute("UPDATE jobs SET status='running',updated=? WHERE id=?",(now(),job['id']))
        if not job:
            signal.wait(1); signal.clear(); continue
        health['active_job']=job['id']
        try:
            if not manager.ready(): raise FileNotFoundError('Models are not verified or have changed')
            if pipe is None:
                health.update(state='loading',detail='正在校验本地权重并载入模型')
                transition(job['id'],'loading',detail=health['detail'])
                from yue2 import YuE2Pipeline
                pipe=YuE2Pipeline.from_pretrained(MODEL,revision=MODEL_REV,vae=VAE,vae_revision=VAE_REV,device='cuda',cache_dir=str(CACHE),local_files_only=True,progress=False)
                pipe._load_model()  # Installed 0.1.6 lazily loads. Explicitly finish loading before reporting planning.
                health.update(state='ready',detail='YuE2 已加载 · 单卡串行生成')
            elif pipe._model is not None and next(pipe._model.parameters()).device != pipe.device:
                transition(job['id'],'loading',detail='复用已加载的模型权重，重新载入显存')
                pipe._load_model()
            run_job(job,pipe)
        except Exception as exc:
            code,message=classify(exc)
            log.error('job=%s code=%s exception=%s',job['id'],code,type(exc).__name__)
            # GPU errors contain no API credentials; dedicated diagnostics never include HTTP provider bodies.
            import traceback
            (DATA/'diagnostics').mkdir(exist_ok=True)
            (DATA/'diagnostics'/f"{job['id']}.log").write_text(traceback.format_exc())
            transition(job['id'],'cancelled' if code=='cancelled' else 'failed','cancelled' if code=='cancelled' else 'failed',message,dump({'code':code,'message':message}))
            if pipe:
                pipe.close(); pipe=None
            health.update(state='idle',detail='下一次任务会重新加载模型')
        finally: health['active_job']=None
    if pipe: pipe.close()

def start():
    threading.Thread(target=loop,name='yue2-gpu',daemon=True).start()
