"""Protocol contract tests use HTTP fixtures, never claim a live provider result."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

if 'store' not in sys.modules:
    os.environ['STUDIO_DATA'] = tempfile.mkdtemp(prefix='yue-protocol-tests-')
import httpx
from fastapi.testclient import TestClient
import assistant
import llm_protocols as wire
import server
import store
from domain import Song


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, value in [('store.DATA', Path(self.temp.name)), ('store.KEY', Path(self.temp.name)/'master.key')]:
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        store.init()
        self.provider = {'base_url':'https://api.example.com', 'model':'test-model', 'protocol':'anthropic',
                         'secret':store.cipher.encrypt(b'fixture-key').decode()}

    async def invoke(self, handler, messages=None, tools=None):
        original = httpx.AsyncClient
        with patch('assistant.valid_url', return_value=self.provider['base_url']), patch('assistant.httpx.AsyncClient',
                side_effect=lambda **kw: original(transport=httpx.MockTransport(handler), **kw)):
            return await assistant.completion(self.provider, messages or [{'role':'user','content':'hello'}], tools)

    def test_endpoint_variants(self):
        for base, protocol, expected in [
            ('https://api.example.com','openai','/v1/chat/completions'),
            ('https://api.example.com/v1','openai','/v1/chat/completions'),
            ('https://api.example.com/api/coding/paas/v4','openai','/api/coding/paas/v4/chat/completions'),
            ('https://api.example.com/v1/chat/completions','openai','/v1/chat/completions'),
            ('https://api.example.com','anthropic','/v1/messages'),
            ('https://api.example.com/v1/','anthropic','/v1/messages'),
            ('https://api.example.com/api/anthropic','anthropic','/api/anthropic/v1/messages'),
            ('https://api.example.com/v1/messages','anthropic','/v1/messages')]:
            self.assertEqual(wire.endpoint(base,protocol),'https://api.example.com'+expected)

    def test_anthropic_text_and_auth(self):
        def handler(request):
            body=json.loads(request.content)
            self.assertEqual(request.url.path,'/v1/messages')
            self.assertEqual(request.headers['x-api-key'],'fixture-key')
            self.assertEqual(request.headers['anthropic-version'],'2023-06-01')
            self.assertNotIn('authorization',request.headers)
            self.assertEqual(body['system'],'system one\n\nlatest state')
            self.assertEqual(body['messages'][0]['role'],'user')
            self.assertEqual(body['tools'][0]['input_schema'],assistant.TOOLS[0]['function']['parameters'])
            return httpx.Response(200,json={'role':'assistant','stop_reason':'end_turn','content':[{'type':'text','text':'OK'}]})
        response=asyncio.run(self.invoke(handler,[{'role':'system','content':'system one'},{'role':'system','content':'latest state'},{'role':'user','content':'hello'}],assistant.TOOLS))
        self.assertEqual(response['content'],'OK')

    def test_openai_legacy_default_and_auth(self):
        del self.provider['protocol']
        def handler(request):
            self.assertEqual(request.headers['authorization'],'Bearer fixture-key')
            self.assertNotIn('x-api-key',request.headers)
            self.assertEqual(request.url.path,'/v1/chat/completions')
            self.assertEqual(json.loads(request.content)['tool_choice'],'auto')
            return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'OK'}}]})
        self.assertEqual(asyncio.run(self.invoke(handler,tools=assistant.TOOLS))['content'],'OK')

    def test_anthropic_tool_round_trip_preserves_latest_state(self):
        state=Song(lyrics='[Verse]\nmanual lyric',constraints='Keep manual text').model_dump()
        blocks=[{'type':'thinking','thinking':'opaque reasoning','signature':'fixture-signature'},
                {'type':'tool_use','id':'read-1','name':'read_project','input':{}},
                {'type':'tool_use','id':'read-2','name':'get_versions','input':{}}]
        calls=[]
        def handler(request):
            body=json.loads(request.content); calls.append(body)
            if len(calls)==1:
                return httpx.Response(200,json={'role':'assistant','stop_reason':'tool_use','content':blocks})
            self.assertEqual(body['messages'][-2]['content'],blocks)
            results=body['messages'][-1]
            self.assertEqual(results['role'],'user')
            self.assertEqual([b['tool_use_id'] for b in results['content']],['read-1','read-2'])
            self.assertEqual(json.loads(results['content'][0]['content'])['state'],state)
            proposal={'message':'New title only','choices':[{'label':'A','explanation':'Keep all lyrics','state':{'title':'New title'}}]}
            return httpx.Response(200,json={'role':'assistant','stop_reason':'tool_use','content':[{'type':'tool_use','id':'proposal-1','name':'propose_changes','input':proposal}]})
        original=httpx.AsyncClient
        with patch('assistant.get_provider',return_value=self.provider),patch('assistant.valid_url',return_value=self.provider['base_url']),patch('assistant.httpx.AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler),**kw)):
            result=asyncio.run(assistant.chat('user',{'state':json.dumps(state),'revision':7,'parent':None},[],'new title',[]))
        self.assertEqual(result['choices'][0]['state']['lyrics'],state['lyrics'])
        self.assertEqual(result['choices'][0]['state']['constraints'],state['constraints'])
        self.assertEqual(result['choices'][0]['state']['title'],'New title')
        with store.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)

    def test_anthropic_failures(self):
        for status,expected in [(401,'authentication'),(403,'authentication'),(404,'model_or_address'),(429,'rate_limit'),(529,'overloaded'),(400,'provider_error')]:
            with self.assertRaises(assistant.ProviderError) as error:
                asyncio.run(self.invoke(lambda request:httpx.Response(status,json={'error':{'message':'fixture-key'}})))
            self.assertEqual(error.exception.code,expected)
            self.assertNotIn('fixture-key',error.exception.message)
        for error_type, expected in [(httpx.ReadTimeout,'timeout'),(httpx.ConnectError,'connection')]:
            def fail(request): raise error_type('fixture-key')
            with self.assertRaises(assistant.ProviderError) as error:
                asyncio.run(self.invoke(fail))
            self.assertEqual(error.exception.code,expected)
            self.assertNotIn('fixture-key',error.exception.message)

    def test_malformed_and_truncated_responses(self):
        for body in [[],{}, {'role':'assistant','content':[]},
            {'role':'assistant','content':[{'type':'tool_use','id':'x','name':'read_project','input':'not-object'}]},
            {'role':'assistant','content':[{'type':'text','text':'partial'}],'stop_reason':'max_tokens'},
            {'role':'assistant','content':[{'type':'thinking','thinking':'private','signature':'sig'}]}]:
            with self.assertRaises(assistant.ProviderError) as error:
                asyncio.run(self.invoke(lambda request:httpx.Response(200,json=body)))
            self.assertEqual(error.exception.code,'format')


    def test_legacy_database_migration_preserves_credentials(self):
        with store.db() as c:
            c.execute('DROP TABLE providers')
            c.execute('CREATE TABLE providers(user TEXT PRIMARY KEY,name TEXT,base_url TEXT,model TEXT,secret TEXT)')
            c.execute('INSERT INTO providers VALUES(?,?,?,?,?)',('owner','old','https://api.example.com','model','encrypted-fixture'))
        store.init(); store.init()
        with store.db() as c: row=dict(c.execute('SELECT * FROM providers').fetchone())
        self.assertEqual(row['protocol'],'openai')
        self.assertEqual(row['secret'],'encrypted-fixture')


if __name__=='__main__': unittest.main()
