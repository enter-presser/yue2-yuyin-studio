"""Fault-injection tests for download safety; not simulated production downloads."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
import models

DATA = b'public model bytes' * 100
FILE = {'name': 'model.safetensors', 'size': len(DATA), 'algorithm': 'sha256', 'digest': hashlib.sha256(DATA).hexdigest()}
CATALOG = [{'repo': 'test/model', 'revision': 'a'*40, 'files': [FILE]}]

class ModelDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = models.ModelManager(self.tmp.name, CATALOG)
        self.real_client = httpx.Client

    def wait(self, m=None):
        m = m or self.m
        m.thread.join(10)
        self.assertFalse(m.thread.is_alive())
        return m.snapshot()

    def network(self, handler):
        return patch('models.httpx.Client', side_effect=lambda **kw: self.real_client(transport=httpx.MockTransport(handler), **kw))

    def test_missing_does_not_download_or_import_gpu(self):
        with self.network(lambda r: self.fail('Startup must not access network')):
            self.m.launch(); s = self.wait()
        self.assertEqual(s['state'], 'missing'); self.assertFalse(self.m.ready())

    def test_verified_download_offline_restart_and_deleted_file(self):
        with self.network(lambda r: httpx.Response(200,content=DATA)):
            self.m.launch(download=True); s = self.wait()
        self.assertTrue(s['ready']); self.assertTrue(self.m.ready())
        self.assertEqual(s['completed_bytes'],len(DATA))
        next_manager = models.ModelManager(self.tmp.name,CATALOG)
        with self.network(lambda r: self.fail('Must reuse local model')):
            next_manager.launch(); self.wait(next_manager)
        self.assertTrue(next_manager.ready())
        path,_ = self.m.paths(CATALOG[0],FILE);path.unlink()
        self.assertFalse(next_manager.ready())

    def test_resume_range_and_wrong_range(self):
        path,blob = self.m.paths(CATALOG[0],FILE)
        blob.parent.mkdir(parents=True)
        partial=blob.with_name(blob.name+'.studio-part');partial.write_bytes(DATA[:43])
        def get(r):
            self.assertEqual(r.headers['range'],'bytes=43-')
            self.assertNotIn('authorization',r.headers)
            return httpx.Response(206,headers={'content-range':f'bytes 43-{len(DATA)-1}/{len(DATA)}'},content=DATA[43:])
        with self.network(get):self.m.launch(download=True);self.wait()
        self.assertTrue(self.m.ready());self.assertEqual(path.read_bytes(),DATA)
        path.unlink();blob.unlink();partial.write_bytes(DATA[:43])
        with self.network(lambda r:httpx.Response(206,headers={'content-range':f'bytes 0-{len(DATA)-1}/{len(DATA)}'},content=DATA)):
            self.m.launch(download=True);s=self.wait()
        self.assertEqual(s['error']['code'],'range_invalid');self.assertEqual(partial.read_bytes(),DATA[:43])

    def test_server_ignoring_range_restarts_file_safely(self):
        path,blob=self.m.paths(CATALOG[0],FILE);blob.parent.mkdir(parents=True)
        blob.with_name(blob.name+'.studio-part').write_bytes(DATA[:31])
        with self.network(lambda r:httpx.Response(200,content=DATA)):
            self.m.launch(download=True);self.wait()
        self.assertTrue(self.m.ready());self.assertEqual(path.read_bytes(),DATA)

    def test_corrupt_download_never_becomes_ready(self):
        with self.network(lambda r:httpx.Response(200,content=b'x'*len(DATA))):
            self.m.launch(download=True);s=self.wait()
        self.assertEqual(s['error']['code'],'checksum_mismatch');self.assertFalse(self.m.ready())
        path,blob=self.m.paths(CATALOG[0],FILE)
        self.assertFalse(path.exists());self.assertFalse(blob.exists())
        self.assertFalse(blob.with_name(blob.name+'.studio-part').exists())

    def test_duplicate_and_cancel(self):
        waiting,release=threading.Event(),threading.Event()
        def get(r):
            waiting.set();release.wait(5);return httpx.Response(200,content=DATA)
        with self.network(get):
            self.m.launch(download=True);self.assertTrue(waiting.wait(5))
            self.assertTrue(self.m.launch(download=True)['duplicate'])
            self.m.cancel();release.set();s=self.wait()
        self.assertEqual(s['state'],'paused');self.assertFalse(self.m.ready())

    def test_connection_timeout_rate_limit_and_disk_full(self):
        for response,code in [(httpx.ReadTimeout('secret-address'),'timeout'),(httpx.ConnectError('secret-token'),'connection'),(429,'rate_limit')]:
            def get(r):
                if isinstance(response,Exception):raise response
                return httpx.Response(response)
            with self.network(get):self.m.launch(download=True);s=self.wait()
            self.assertEqual(s['error']['code'],code);self.assertNotIn('secret',json.dumps(s))
        with patch('models.shutil.disk_usage',return_value=type('Disk',(),{'free':0})()),self.network(lambda r:self.fail('disk full must not download')):
            self.m.launch(download=True);s=self.wait()
        self.assertEqual(s['error']['code'],'disk_full')

    def test_wrong_same_size_cached_weight_is_repaired(self):
        path,blob=self.m.paths(CATALOG[0],FILE)
        path.parent.mkdir(parents=True);path.write_bytes(b'x'*len(DATA))
        with self.network(lambda r:httpx.Response(200,content=DATA)):
            self.m.launch(download=True);self.wait()
        self.assertTrue(self.m.ready());self.assertEqual(path.read_bytes(),DATA)

    def test_git_blob_hash_for_config(self):
        data=b'{"model_type":"yue2"}'
        f={'name':'config.json','size':len(data),'algorithm':'git-sha1','digest':hashlib.sha1(f'blob {len(data)}\0'.encode()+data).hexdigest()}
        m=models.ModelManager(self.tmp.name,[{**CATALOG[0],'files':[f]}])
        with self.network(lambda r:httpx.Response(200,content=data)):
            m.launch(download=True);self.wait(m)
        self.assertTrue(m.ready())

if __name__=='__main__':unittest.main()
