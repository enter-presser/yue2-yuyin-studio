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
            self.assertEqual(anon.post('/api/projects',json={},headers={'Origin':'https://evil.example'}).status_code,403)
        with patch.object(server,'PRIVATE_USER','bob'):
            self.assertEqual(anon.get('/api/projects/'+self.p['id']).status_code,404)
        with patch.object(server,'PRIVATE_USER','missing'):
            self.assertEqual(anon.get('/api/projects').status_code,503)
    def test_assistant_endpoints_removed(self):
        self.assertEqual(self.c.get('/api/provider').status_code,404)
        self.assertEqual(self.c.post('/api/provider/test',json={}).status_code,404)
        self.assertEqual(self.c.post('/api/projects/'+self.p['id']+'/chat',json={'message':'hello','revision':0}).status_code,404)
        self.assertNotIn('messages',self.c.get('/api/projects/'+self.p['id']).json())

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
