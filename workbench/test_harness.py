"""Isolated regression tests. Fault injection is never used for demo music."""
import os,tempfile,sys,unittest,json,asyncio
from pathlib import Path
os.environ['STUDIO_PRIVATE_USER']=''
os.environ['STUDIO_DATA']=tempfile.mkdtemp(prefix='yue-studio-test-')
sys.path.insert(0,str(Path(__file__).parent/'vendor'))
from unittest.mock import patch
from fastapi.testclient import TestClient
import server,store,assistant
from domain import Song,validate_song

class HarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init();store.add_user('alice','test-password-alice');store.add_user('bob','test-password-bob')
        cls.client=TestClient(server.app)
    def setUp(self):
        self.model_ready=patch('server.models.manager.ready',return_value=True)
        self.model_ready.start();self.addCleanup(self.model_ready.stop)
        self.c=TestClient(server.app);self.c.headers['X-Studio-Request']='1'
        self.c.post('/api/login',json={'username':'alice','password':'test-password-alice'})
        self.p=self.c.post('/api/projects',json={'lyrics':'[Verse]\n一起走过夏天','title':'test'}).json()
    def test_model_gate_no_job_and_schema(self):
        url='/api/projects/'+self.p['id']+'/generate'
        with patch('server.models.manager.ready',return_value=False):
            result=self.c.post(url,json={'revision':0,'idempotency_key':store.uid()})
        self.assertEqual(result.status_code,409)
        self.assertEqual(result.json()['error']['code'],'models_missing')
        self.assertEqual(self.c.get('/api/projects/'+self.p['id']).json()['versions'],[])
        self.assertEqual(self.c.post('/api/models/download',json={'source':'http://127.0.0.1'}).status_code,422)
        self.assertEqual(self.c.post('/api/models/download',json={'source':'official','command':'anything'}).status_code,422)
        anon=TestClient(server.app)
        self.assertEqual(anon.get('/api/models').status_code,401)

    def test_auth_isolation(self):
        anon=TestClient(server.app);self.assertEqual(anon.get('/api/projects').status_code,401)
        bob=TestClient(server.app,headers={'X-Studio-Request':'1'});bob.post('/api/login',json={'username':'bob','password':'test-password-bob'})
        self.assertEqual(bob.get('/api/projects/'+self.p['id']).status_code,404)
    def test_private_workspace_without_session(self):
        anon=TestClient(server.app,headers={'X-Studio-Request':'1'})
        with patch.object(server,'PRIVATE_USER','alice'):
            self.assertTrue(anon.get('/api/me').json()['private'])
            self.assertEqual(anon.get('/api/projects/'+self.p['id']).status_code,200)
            self.assertFalse(anon.cookies)
            with patch('assistant.valid_url',return_value='https://api.example.com/v1'):
                self.c.put('/api/provider',json={'base_url':'https://api.example.com/v1','model':'test','api_key':'private-mode-secret'})
            result=anon.get('/api/provider')
            self.assertTrue(result.json()['configured']);self.assertNotIn('private-mode-secret',result.text)
            self.assertEqual(anon.post('/api/projects',json={},headers={'Origin':'https://evil.example'}).status_code,403)
            anon.delete('/api/provider')
        with patch.object(server,'PRIVATE_USER','bob'):
            self.assertEqual(anon.get('/api/projects/'+self.p['id']).status_code,404)
        with patch.object(server,'PRIVATE_USER','missing'):
            self.assertEqual(anon.get('/api/projects').status_code,503)
    def test_revision_and_undo(self):
        s=self.p['state'];s['lyrics']='[Verse]\n这是手动修改'
        url='/api/projects/'+self.p['id'];r=self.c.put(url,json={'revision':0,'state':s});self.assertEqual(r.status_code,200)
        self.assertEqual(self.c.put(url,json={'revision':0,'state':s}).status_code,409)
        self.assertEqual(self.c.post(url+'/undo',json={'revision':1}).status_code,200)
        self.assertEqual(self.c.get(url).json()['state']['lyrics'],'[Verse]\n一起走过夏天')
    def test_idempotent_and_cancel(self):
        url='/api/projects/'+self.p['id']+'/generate';body={'revision':0,'idempotency_key':store.uid()}
        one=self.c.post(url,json=body).json();two=self.c.post(url,json=body).json();self.assertEqual(one['job'],two['job']);self.assertTrue(two['duplicate'])
        self.assertEqual(self.c.post(url,json={**body,'kind':'plan'}).status_code,409)
        self.c.post('/api/jobs/'+one['job']+'/cancel',json={});r=next(x for x in self.c.get('/api/jobs').json() if x['id']==one['job']);self.assertEqual(r['status'],'cancelled')
    def test_stale_suggestion(self):
        pid=self.p['id'];sid=store.uid();s=self.p['state']
        with store.db() as c:c.execute('INSERT INTO suggestions VALUES(?,?,?,?,0,?)',(sid,pid,0,store.dump({'message':'suggestion','choices':[{'label':'choice','explanation':'why','state':s}]}),store.now()))
        s['title']='manual';self.c.put('/api/projects/'+pid,json={'revision':0,'state':s})
        r=self.c.post(f'/api/projects/{pid}/suggestions/{sid}/apply',json={'revision':1,'choice':0});self.assertEqual(r.status_code,409)
    def test_chat_preserves_reply_when_draft_changes(self):
        pid=self.p['id'];original=self.p['state']
        proposal={'message':'完成的建议','choices':[{'label':'建议方向','explanation':'说明','state':original}]}
        async def editing_during_reply(*args):
            with store.db() as c:
                live={**original,'title':'用户刚刚编辑的歌名'}
                c.execute('UPDATE projects SET state=?,revision=revision+1 WHERE id=?',(store.dump(live),pid))
            return proposal
        with patch('assistant.chat',side_effect=editing_during_reply):
            r=self.c.post('/api/projects/'+pid+'/chat',json={'revision':0,'message':'构思'})
        self.assertEqual(r.status_code,200);self.assertTrue(r.json()['outdated'])
        latest=self.c.get('/api/projects/'+pid).json()
        self.assertEqual(latest['state']['title'],'用户刚刚编辑的歌名')
        self.assertEqual(latest['suggestion']['revision'],0)
        self.assertEqual(latest['messages'][-1]['body'],'完成的建议')
        sid=r.json()['id']
        self.assertEqual(self.c.post(f'/api/projects/{pid}/suggestions/{sid}/apply',json={'revision':1,'choice':0}).status_code,409)
    def test_validation_and_csrf(self):
        self.assertFalse(validate_song(Song(lyrics='no section').model_dump())['valid'])
        self.assertFalse(validate_song(Song(lyrics='[Verse]\nwords',abc='invalid').model_dump())['valid'])
        r=self.c.post('/api/projects',json={'lyrics':'secret-value','unknown':'api-secret'});self.assertEqual(r.status_code,422);self.assertNotIn('api-secret',r.text)
        r=self.c.post('/api/projects',json={},headers={'Origin':'https://evil.example'});self.assertEqual(r.status_code,403)
    def test_proxy_origin_validation(self):
        url='/api/projects/'+self.p['id']+'/validate'
        # A same-origin browser fetch can arrive with the proxy's internal Host.
        r=self.c.post(url,json={},headers={'Origin':'https://studio.example:8443','Host':'127.0.0.1:6006','Sec-Fetch-Site':'same-origin'})
        self.assertEqual(r.status_code,200)
        # Older clients without Fetch Metadata require a matching authority.
        r=self.c.post(url,json={},headers={'Origin':'https://studio.example','Host':'studio.example:443'})
        self.assertEqual(r.status_code,200)
        for headers in [
            {'Origin':'https://evil.example','Host':'internal:6006','Sec-Fetch-Site':'cross-site'},
            {'Origin':'http://testserver','Sec-Fetch-Site':'cross-site'},
            {'Origin':'https://evil.example','X-Forwarded-Host':'evil.example'},
            {'Origin':'null','Sec-Fetch-Site':'same-origin'},
            {'Origin':'http://['},
        ]:
            self.assertEqual(self.c.post(url,json={},headers=headers).status_code,403)
        # The proxy compatibility path does not bypass the custom-header guard.
        c=TestClient(server.app)
        self.assertEqual(c.post(url,json={},headers={'Origin':'https://studio.example','Sec-Fetch-Site':'same-origin'}).status_code,403)
    def test_key_isolation_and_masking(self):
        with patch('assistant.valid_url',return_value='https://api.example.com/v1'):
            r=self.c.put('/api/provider',json={'base_url':'https://api.example.com/v1','model':'model','api_key':'test-private-key-123456'});self.assertEqual(r.status_code,200)
        r=self.c.get('/api/provider');self.assertNotIn('test-private-key',r.text)
        with store.db() as c:raw=c.execute('SELECT secret FROM providers').fetchone()[0]
        self.assertNotIn('test-private-key',raw);self.assertEqual(store.cipher.decrypt(raw.encode()).decode(),'test-private-key-123456')
        self.c.delete('/api/provider');self.assertFalse(self.c.get('/api/provider').json()['configured'])
    def test_clean_image_key_created_only_when_saving_credentials(self):
        key=Path(tempfile.mkdtemp(prefix='studio-key-test-'))/'master.key'
        with patch.object(store,'KEY',key):
            local=store.LazyCipher()
            self.assertFalse(key.exists())
            encrypted=local.encrypt(b'new-instance-secret')
            self.assertTrue(key.exists())
            self.assertEqual(local.decrypt(encrypted),b'new-instance-secret')
            self.assertEqual(key.stat().st_mode & 0o777,0o600)
        self.assertFalse(store.check_password('old-password','!disabled'))
    def test_private_url_rejected(self):
        with self.assertRaises(assistant.ProviderError):asyncio.run(assistant.valid_url('https://127.0.0.1/v1'))
        with self.assertRaises(assistant.ProviderError):asyncio.run(assistant.valid_url('http://api.example.com'))
    def test_provider_faults(self):
        import httpx
        original=httpx.AsyncClient
        p={'base_url':'https://api.example.com/v1','model':'test','secret':store.cipher.encrypt(b'test-key').decode()}
        async def invoke(handler):
            with patch('assistant.valid_url',return_value=p['base_url']),patch('assistant.httpx.AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler),**kw)):
                return await assistant.completion(p,[{'role':'user','content':'test'}])
        for status,body,expected in [(401,{},'authentication'),(429,{},'rate_limit'),(400,{'error':{'code':'1211'}},'model_unavailable'),(200,{'oops':True},'format'),(200,{'choices':[{'message':{'tool_calls':[{}]}}]},'format')]:
            with self.assertRaises(assistant.ProviderError) as e:asyncio.run(invoke(lambda req:httpx.Response(status,json=body)))
            self.assertEqual(e.exception.code,expected)
        def timeout(req):raise httpx.ReadTimeout('injected')
        with self.assertRaises(assistant.ProviderError) as e:asyncio.run(invoke(timeout))
        self.assertEqual(e.exception.code,'timeout')
    def test_partial_tool_preserves_manual_state(self):
        state=self.p['state'];state['constraints']='保留手工歌词';state['params']['seed']=1234
        response={'role':'assistant','tool_calls':[{'id':'call_1','function':{'name':'propose_changes','arguments':json.dumps({'message':'建议','choices':[{'label':'改歌名','explanation':'只改歌名','state':{'title':'新的歌名'}}]})}}]}
        with patch('assistant.get_provider',return_value={}),patch('assistant.completion',return_value=response):
            r=asyncio.run(assistant.chat('user',{'state':json.dumps(state),'revision':3,'parent':None},[],'只改歌名',[]))
        suggested=r['choices'][0]['state']
        self.assertEqual(suggested['lyrics'],state['lyrics']);self.assertEqual(suggested['params']['seed'],1234);self.assertEqual(suggested['constraints'],'保留手工歌词')
    def test_chat_total_timeout_releases_lock(self):
        async def slow(*args):
            await asyncio.sleep(1)
            return {'message':'late','choices':[]}
        url='/api/projects/'+self.p['id']+'/chat'
        with patch.object(server,'CHAT_TIMEOUT_SECONDS',0.01),patch('assistant.chat',side_effect=slow):
            r=self.c.post(url,json={'revision':0,'message':'test'})
            self.assertEqual(r.status_code,502);self.assertEqual(r.json()['error']['code'],'timeout')
        self.assertEqual(self.c.get('/api/projects/'+self.p['id']).json()['messages'],[])
        with patch('assistant.chat',return_value={'message':'Ready','choices':[]}):
            r=self.c.post(url,json={'revision':0,'message':'retry'})
            self.assertEqual(r.status_code,200)
    def test_inference_faults(self):
        import worker
        self.assertEqual(worker.classify(RuntimeError('CUDA out of memory'))[0],'gpu_memory')
        self.assertEqual(worker.classify(InterruptedError())[0],'cancelled')
        self.assertEqual(worker.classify(ValueError('tampered plan'))[0],'input_or_artifact')
    def test_restart_recovery(self):
        import worker
        r=self.c.post('/api/projects/'+self.p['id']+'/generate',json={'revision':0,'idempotency_key':store.uid()}).json()
        with store.db() as c:c.execute("UPDATE jobs SET status='running',stage='semantic' WHERE id=?",(r['job'],))
        worker.recover_interrupted()
        j=next(x for x in self.c.get('/api/jobs').json() if x['id']==r['job'])
        self.assertEqual(j['status'],'failed');self.assertEqual(j['error']['code'],'service_restart')

if __name__=='__main__':unittest.main(verbosity=2)
