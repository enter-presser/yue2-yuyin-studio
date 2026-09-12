import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).parent/'vendor'))
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from domain import Song, Save, Generate, Message, Revision, Rename, Provider, Login, Strict, Field, validate_song, fingerprint, sections
from store import db, init, uid, now, dump, cipher, check_password, event, artifact_dir, add_user
import worker
import assistant

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
ROOT=Path(__file__).parent
locks={}
attempts={}
CHAT_TIMEOUT_SECONDS=180
# This private image always opens the existing owner workspace.
PRIVATE_USER=os.environ.get("STUDIO_PRIVATE_USER", "creator").strip()

@asynccontextmanager
async def lifespan(app):
    init()
    if PRIVATE_USER:
        with db() as c:
            owner=c.execute("SELECT id FROM users WHERE name=?",(PRIVATE_USER,)).fetchone()
        if not owner: add_user(PRIVATE_USER,secrets.token_urlsafe(48))
    worker.start()
    yield
    worker.stopping.set()

app=FastAPI(title='余音 · YuE2 创作工作台',lifespan=lifespan,docs_url=None,redoc_url=None)

def fail(code,message,status=400): raise HTTPException(status,{'code':code,'message':message})

@app.exception_handler(RequestValidationError)
async def bad_request(request,exc):
    # Never serialize validation inputs, which could contain a provider key.
    return JSONResponse({'error':{'code':'validation','message':'输入格式或范围不正确，请检查填写内容。','fields':['.'.join(str(x) for x in e['loc']) for e in exc.errors()]}},status_code=422)

@app.exception_handler(HTTPException)
async def http_error(request,exc): return JSONResponse({'error':exc.detail},status_code=exc.status_code)

@app.exception_handler(assistant.ProviderError)
async def api_error(request,exc): return JSONResponse({'error':{'code':exc.code,'message':exc.message}},status_code=502)

def same_origin_request(request):
    # Browser Fetch Metadata describes the public page, before proxy Host rewriting.
    # Sec-Fetch-* cannot be supplied by page JavaScript. Non-browser API clients
    # still require the custom request header below. Never trust forwarded Host.
    site=request.headers.get('sec-fetch-site')
    if site=='cross-site': return False
    origin=request.headers.get('origin')
    if not origin: return True
    try:
        source=urlsplit(origin)
        if source.scheme not in ('http','https') or not source.hostname or source.username or source.password or source.path or source.query or source.fragment:
            return False
        if site=='same-origin': return True
        target=urlsplit(source.scheme+'://'+request.headers.get('host',''))
        default=443 if source.scheme=='https' else 80
        return source.hostname==target.hostname and (source.port or default)==(target.port or default)
    except ValueError:
        return False

@app.middleware('http')
async def protection(request,call_next):
    try: length=int(request.headers.get('content-length','0') or 0)
    except ValueError: length=300001
    if length>300000:
        return JSONResponse({'error':{'message':'请求过大'}},status_code=413)
    if request.method not in ('GET','HEAD','OPTIONS'):
        if not same_origin_request(request):
            logging.warning('Request rejected by browser origin validation')
            return JSONResponse({'error':{'code':'request_origin','message':'访问地址校验未通过，请从工作台入口重新打开页面后重试。'}},status_code=403)
        if request.headers.get('x-studio-request')!='1':
            return JSONResponse({'error':{'message':'缺少请求校验标记，请刷新页面'}},status_code=403)
        chunks=[]; received=0
        async for chunk in request.stream():
            received+=len(chunk)
            if received>300000: return JSONResponse({'error':{'message':'请求过大'}},status_code=413)
            chunks.append(chunk)
        request._body=b''.join(chunks)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='same-origin'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    if request.url.path.startswith('/api/'): response.headers['Cache-Control']='no-store'
    elif request.url.path=='/' or request.url.path.startswith('/static/'):
        response.headers['Cache-Control']='no-cache'
    return response

def user(request:Request):
    if PRIVATE_USER:
        with db() as c:
            row=c.execute('SELECT id,name FROM users WHERE name=?',(PRIVATE_USER,)).fetchone()
        if not row: fail('workspace_unavailable','私人工作台尚未初始化，请重启服务。',503)
        return dict(row)
    token=request.cookies.get('studio_session','')
    with db() as c:
        row=c.execute('SELECT u.id,u.name FROM sessions s JOIN users u ON u.id=s.user WHERE s.token=? AND s.expires>?',(hashlib.sha256(token.encode()).hexdigest(),now())).fetchone()
    if not row: fail('login','请先登录工作台。',401)
    return dict(row)

def own_project(c,pid,u):
    row=c.execute('SELECT * FROM projects WHERE id=? AND user=?',(pid,u['id'])).fetchone()
    if not row: fail('not_found','项目不存在。',404)
    return dict(row)

def own_version(c,vid,u):
    row=c.execute('SELECT * FROM versions WHERE id=? AND user=?',(vid,u['id'])).fetchone()
    if not row: fail('not_found','版本不存在。',404)
    return dict(row)

def ensure_revision(project,rev):
    if project['revision']!=rev: fail('stale','项目已被修改，请刷新后重新操作；不会覆盖最新内容。',409)

def save_state(c,p,state,parent=None):
    c.execute('INSERT INTO revisions(project,state,parent,created) VALUES(?,?,?,?)',(p['id'],p['state'],p['parent'],now()))
    c.execute('UPDATE projects SET state=?,parent=?,revision=revision+1,updated=? WHERE id=?',(dump(state),parent,now(),p['id']))
    return {'id':p['id'],'state':state,'revision':p['revision']+1,'parent':parent}

def view_version(v):
    v=dict(v); v['snapshot']=json.loads(v['snapshot']); v['result']=json.loads(v['result']) if v['result'] else None
    v['sections']=sections(v['snapshot']['lyrics'])
    return v

@app.get('/api/health')
def health(): return {'status':'ok','engine':'YuE2','worker':worker.health,'queue_concurrency':1}

@app.post('/api/login')
def login(body:Login,request:Request):
    address=request.client.host
    recent=[t for t in attempts.get(address,[]) if now()-t<300]
    attempts[address]=recent
    if len(recent)>=15: fail('rate_limit','登录尝试过多，请五分钟后再试。',429)
    recent.append(now())
    with db() as c:
        u=c.execute('SELECT * FROM users WHERE name=?',(body.username,)).fetchone()
        if not u or not check_password(body.password,u['password']): fail('authentication','用户名或密码不正确。',401)
        token=secrets.token_urlsafe(40)
        c.execute('DELETE FROM sessions WHERE expires<?',(now(),))
        c.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),u['id'],now()+43200))
    attempts[address]=[]
    response=JSONResponse({'name':u['name']})
    secure=request.url.scheme=='https' or request.headers.get('x-forwarded-proto')=='https'
    response.set_cookie('studio_session',token,httponly=True,samesite='strict',secure=secure,max_age=43200,path='/')
    return response

@app.post('/api/logout')
def logout(request:Request):
    with db() as c: c.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(request.cookies.get('studio_session','').encode()).hexdigest(),))
    r=JSONResponse({'ok':True}); r.delete_cookie('studio_session'); return r

@app.get('/api/me')
def me(u=Depends(user)): return {'name':'我的创作空间' if PRIVATE_USER else u['name'],'private':bool(PRIVATE_USER)}

@app.get('/api/provider')
def provider(u=Depends(user)):
    with db() as c: row=c.execute('SELECT name,base_url,model FROM providers WHERE user=?',(u['id'],)).fetchone()
    return {'configured':bool(row),**(dict(row) if row else {}),'storage':'服务器加密保存，密钥不返回浏览器，可删除。'}

@app.put('/api/provider')
async def set_provider(body:Provider,u=Depends(user)):
    url=await assistant.valid_url(body.base_url)
    with db() as c:
        old=c.execute('SELECT * FROM providers WHERE user=?',(u['id'],)).fetchone()
        if not body.api_key and not old: fail('key_required','首次配置请填写 API Key。')
        secret=cipher.encrypt(body.api_key.encode()).decode() if body.api_key else old['secret']
        # An existing credential must never be silently sent to a different origin.
        if old and old['base_url']!=url and not body.api_key: fail('key_required','更换 API 地址时请重新输入密钥，避免将原密钥发送到其他服务商。')
        c.execute('INSERT OR REPLACE INTO providers VALUES(?,?,?,?,?)',(u['id'],body.name,url,body.model,secret))
    return {'configured':True}

@app.delete('/api/provider')
def delete_provider(u=Depends(user)):
    with db() as c: c.execute('DELETE FROM providers WHERE user=?',(u['id'],))
    return {'deleted':True}

@app.post('/api/provider/test')
async def test_provider(u=Depends(user)):
    r=await assistant.completion(assistant.get_provider(u['id']),[{'role':'user','content':'连接测试，请只回复 OK。'}])
    if not r.get('content'): fail('format','服务商未返回文字，请检查模型。')
    return {'ok':True,'message':'服务商已返回有效响应。创作工具调用将在首次创作时进一步验证。'}

@app.get('/api/projects')
def projects(u=Depends(user)):
    with db() as c: rows=c.execute('SELECT id,state,revision,updated FROM projects WHERE user=? ORDER BY updated DESC',(u['id'],)).fetchall()
    return [{'id':r['id'],'title':json.loads(r['state'])['title'],'revision':r['revision'],'updated':r['updated']} for r in rows]

@app.post('/api/projects')
def create_project(body:Song,u=Depends(user)):
    pid=uid()
    with db() as c: c.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?)',(pid,u['id'],dump(body.model_dump()),0,None,now(),now()))
    return {'id':pid,'state':body.model_dump(),'revision':0,'parent':None}

@app.get('/api/projects/{pid}')
def project(pid:str,u=Depends(user)):
    with db() as c:
        p=own_project(c,pid,u)
        vs=c.execute('SELECT * FROM versions WHERE project=? ORDER BY created DESC',(pid,)).fetchall()
        ms=c.execute('SELECT role,body,created FROM messages WHERE project=? ORDER BY id',(pid,)).fetchall()
        suggestions=c.execute('SELECT * FROM suggestions WHERE project=? AND applied=0 ORDER BY created DESC LIMIT 1',(pid,)).fetchone()
    p['state']=json.loads(p['state']); p['sections']=sections(p['state']['lyrics'])
    p['versions']=[view_version(v) for v in vs]; p['messages']=[dict(m) for m in ms]
    p['suggestion']={**dict(suggestions),'body':json.loads(suggestions['body'])} if suggestions else None
    return p

@app.put('/api/projects/{pid}')
def save_project(pid:str,body:Save,u=Depends(user)):
    with db() as c:
        c.execute('BEGIN IMMEDIATE'); p=own_project(c,pid,u); ensure_revision(p,body.revision)
        return save_state(c,p,body.state.model_dump(),p['parent'])

@app.post('/api/projects/{pid}/undo')
def undo(pid:str,body:Revision,u=Depends(user)):
    with db() as c:
        c.execute('BEGIN IMMEDIATE'); p=own_project(c,pid,u); ensure_revision(p,body.revision)
        r=c.execute('SELECT * FROM revisions WHERE project=? ORDER BY id DESC LIMIT 1',(pid,)).fetchone()
        if not r: fail('no_undo','没有可撤销的修改。')
        c.execute('UPDATE projects SET state=?,parent=?,revision=revision+1,updated=? WHERE id=?',(r['state'],r['parent'],now(),pid))
        c.execute('DELETE FROM revisions WHERE id=?',(r['id'],))
    return {'ok':True}

@app.post('/api/projects/{pid}/validate')
def validate(pid:str,u=Depends(user)):
    with db() as c: p=own_project(c,pid,u)
    return validate_song(json.loads(p['state']))

@app.post('/api/projects/{pid}/chat')
async def chat(pid:str,body:Message,u=Depends(user)):
    lock=locks.setdefault(u['id'],asyncio.Lock())
    if lock.locked(): fail('busy','创作助手正在回复，请稍候。',409)
    async with lock:
        with db() as c:
            p=own_project(c,pid,u); ensure_revision(p,body.revision)
            history=c.execute('SELECT role,body FROM messages WHERE project=? ORDER BY id DESC LIMIT 8',(pid,)).fetchall()[::-1]
            versions=[{'id':r['id'],'name':r['name'],'status':r['status']} for r in c.execute('SELECT id,name,status FROM versions WHERE project=?',(pid,))]
        started=time.monotonic()
        logging.info('Creative request started project=%s',pid)
        try:
            result=await asyncio.wait_for(assistant.chat(u['id'],p,history,body.message,versions),timeout=CHAT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logging.warning('Creative request timed out project=%s',pid)
            raise assistant.ProviderError('timeout','创作助手本次等待超过 3 分钟，已停止等待。手稿未修改，也未提交音乐生成；可以重试或在设置中检查模型连接。')
        except assistant.ProviderError as exc:
            logging.warning('Creative request failed project=%s code=%s',pid,exc.code)
            raise
        logging.info('Creative request completed project=%s seconds=%.1f',pid,time.monotonic()-started)
        sid=uid()
        with db() as c:
            c.execute('BEGIN IMMEDIATE'); latest=own_project(c,pid,u)
            # A proposal is a separate artifact, never a write to the live draft.
            # Keep completed work even if an autosave occurred during the API call.
            outdated=latest['revision']!=body.revision
            c.execute('INSERT INTO messages(project,role,body,created) VALUES(?,?,?,?)',(pid,'user',body.message,now()))
            c.execute('INSERT INTO messages(project,role,body,created) VALUES(?,?,?,?)',(pid,'assistant',result['message'],now()))
            c.execute('INSERT INTO suggestions VALUES(?,?,?,?,0,?)',(sid,pid,p['revision'],dump(result),now()))
        return {'id':sid,'revision':p['revision'],'body':result,'outdated':outdated}

class Apply(Revision):
    choice: int = Field(ge=0,le=2)

@app.post('/api/projects/{pid}/suggestions/{sid}/apply')
def apply_suggestion(pid:str,sid:str,body:Apply,u=Depends(user)):
    with db() as c:
        c.execute('BEGIN IMMEDIATE'); p=own_project(c,pid,u); ensure_revision(p,body.revision)
        s=c.execute('SELECT * FROM suggestions WHERE id=? AND project=?',(sid,pid)).fetchone()
        if not s or s['applied']: fail('suggestion','建议已应用或不存在。',409)
        if s['revision']!=p['revision']: fail('stale','你已编辑过项目。这条建议基于旧内容，请让助手根据最新内容重新建议。',409)
        choices=json.loads(s['body'])['choices']
        if body.choice>=len(choices): fail('choice','方向不存在。')
        state=Song.model_validate(choices[body.choice]['state']).model_dump()
        check=validate_song(state)
        if not check['valid']: fail('input','；'.join(check['errors']))
        result=save_state(c,p,state,p['parent'])
        c.execute('UPDATE suggestions SET applied=1 WHERE id=?',(sid,))
    return result

@app.post('/api/projects/{pid}/generate')
def generate(pid:str,body:Generate,u=Depends(user)):
    with db() as c:
        c.execute('BEGIN IMMEDIATE'); p=own_project(c,pid,u); ensure_revision(p,body.revision)
        state=json.loads(p['state']); checked=validate_song(state)
        if not checked['valid']: fail('input','；'.join(checked['errors']))
        if body.kind=='plan' and state['params']['cot']=='off': fail('mode','直接生成模式没有音乐方案，请选择完整或旋律规划。')
        fp=fingerprint(state)
        reqhash=hashlib.sha256(dump([pid,p['revision'],fp,body.kind,body.reuse_version]).encode()).hexdigest()
        old=c.execute('SELECT * FROM jobs WHERE user=? AND idem=?',(u['id'],body.idempotency_key)).fetchone()
        if old:
            if old['request_hash']!=reqhash: fail('idempotency_conflict','这次提交标识已用于其他输入，请刷新后重试。',409)
            return {'job':old['id'],'version':old['version'],'duplicate':True}
        old=c.execute("SELECT j.id,j.version FROM jobs j JOIN versions v ON v.id=j.version WHERE v.project=? AND v.fingerprint=? AND v.kind=? AND j.status IN ('queued','running')",(pid,fp,body.kind)).fetchone()
        if old: return {'job':old['id'],'version':old['version'],'duplicate':True}
        if c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]>=5: fail('queue_full','队列已满，请等待当前任务完成。',429)
        if body.reuse_version:
            source=own_version(c,body.reuse_version,u)
            if source['project']!=pid or source['fingerprint']!=fp: fail('reuse_mismatch','源音乐方案与当前输入不匹配，请重新规划。')
            if not (artifact_dir(source['id'])/'plan_manifest.json').is_file(): fail('no_plan','该版本还没有可复用的音乐方案。')
        vid,jid=uid(),uid()
        number=c.execute('SELECT COUNT(*) FROM versions WHERE project=?',(pid,)).fetchone()[0]+1
        name=f"V{number} · {state['title']}"+(' · 音乐方案' if body.kind=='plan' else '')
        c.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(vid,pid,u['id'],p['parent'],name,dump(state),fp,body.kind,'queued',None,now()))
        c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(jid,vid,u['id'],body.idempotency_key,reqhash,'queued','queued',None,0,body.reuse_version,now(),now()))
        event(c,jid,'queued','已进入单卡队列')
    worker.signal.set()
    return {'job':jid,'version':vid,'duplicate':False}

@app.get('/api/jobs')
def jobs(u=Depends(user)):
    with db() as c:
        rows=c.execute('SELECT j.*,v.name,v.project,v.kind FROM jobs j JOIN versions v ON v.id=j.version WHERE j.user=? ORDER BY j.created DESC LIMIT 100',(u['id'],)).fetchall()
        result=[]
        for r in rows:
            item=dict(r); item.pop('idem'); item.pop('request_hash'); item['error']=json.loads(item['error']) if item['error'] else None
            item['events']=[dict(e) for e in c.execute('SELECT stage,detail,created FROM events WHERE job=? ORDER BY id',(r['id'],))]
            result.append(item)
    return result

@app.post('/api/jobs/{jid}/cancel')
def cancel(jid:str,u=Depends(user)):
    with db() as c:
        c.execute('BEGIN IMMEDIATE'); j=c.execute('SELECT * FROM jobs WHERE id=? AND user=?',(jid,u['id'])).fetchone()
        if not j: fail('not_found','任务不存在。',404)
        if j['status']=='queued':
            c.execute("UPDATE jobs SET status='cancelled',stage='cancelled',cancel=1,updated=? WHERE id=?",(now(),jid))
            c.execute("UPDATE versions SET status='cancelled' WHERE id=?",(j['version'],)); event(c,jid,'cancelled','已移出队列，没有执行推理')
            return {'message':'已取消排队'}
        if j['status']=='running':
            c.execute('UPDATE jobs SET cancel=1 WHERE id=?',(jid,)); event(c,jid,'cancel_requested','已请求协作式取消，将在安全检查点停止；解码阶段需要等待结束')
            return {'message':'已请求停止运行，将在安全检查点生效'}
        return {'message':'任务已经结束'}

@app.post('/api/versions/{vid}/continue')
def continue_version(vid:str,body:Revision,u=Depends(user)):
    with db() as c:
        c.execute('BEGIN IMMEDIATE'); v=own_version(c,vid,u); p=own_project(c,v['project'],u); ensure_revision(p,body.revision)
        state=json.loads(v['snapshot']); result=json.loads(v['result']) if v['result'] else {}
        if result.get('abc'): state['abc']=result['abc']
        return save_state(c,p,state,vid)

@app.put('/api/versions/{vid}/name')
def rename(vid:str,body:Rename,u=Depends(user)):
    with db() as c: own_version(c,vid,u); c.execute('UPDATE versions SET name=? WHERE id=?',(body.name,vid))
    return {'ok':True}

@app.get('/api/versions/{vid}/artifacts/{filename}')
def artifact(vid:str,filename:str,u=Depends(user)):
    allowed={'preview.mp3':'audio/mpeg','audio.flac':'audio/flac','score.abc':'text/plain','request.json':'application/json','config.json':'application/json','result.json':'application/json','input.json':'application/json','score-check.json':'application/json'}
    if filename not in allowed: fail('not_found','产物不存在。',404)
    with db() as c: own_version(c,vid,u)
    path=artifact_dir(vid)/filename
    if not path.is_file() or path.is_symlink(): fail('not_found','产物尚未生成。',404)
    return FileResponse(path,media_type=allowed[filename],filename=filename,content_disposition_type='inline' if filename.endswith(('.mp3','.flac')) else 'attachment')

@app.get('/')
def index(): return FileResponse(ROOT/'static/index.html')

app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
