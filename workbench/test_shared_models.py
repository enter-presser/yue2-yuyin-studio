import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
import models
from shared_models import SharedAssets, SharedModelError


def packed(header, data):
    raw = json.dumps(header, separators=(',', ':')).encode()
    return len(raw).to_bytes(8, 'little') + raw + data


class SharedPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = b'abcdefgh'
        tensor = {'dtype': 'BF16', 'shape': [2, 2], 'data_offsets': [0, 8]}
        self.original = packed({'llm2vae.weight': tensor}, self.data)
        self.shared = self.root / 'shared.safetensors'
        self.shared.write_bytes(packed({'model.diffusion_model.llm2vae.weight': tensor}, self.data))
        self.file = {'name': 'model.safetensors', 'size': len(self.original), 'algorithm': 'sha256', 'digest': hashlib.sha256(self.original).hexdigest()}
        self.catalog = [{'repo': 'm-a-p/YuE2-3B', 'revision': 'a' * 40, 'files': [self.file]}]
        self.assets = self.root / 'assets'
        header = self.assets / 'm-a-p--YuE2-3B/model.safetensors.header'
        header.parent.mkdir(parents=True)
        header.write_bytes(self.original[:-8])
        self.manager = models.ModelManager(self.root / 'cache', self.catalog, shared=SharedAssets(self.shared, self.assets), shared_wait_seconds=0)

    def run_prepare(self):
        with patch('models.httpx.Client', side_effect=AssertionError('Offline preparation must not access network')):
            self.manager.launch(source='platform')
            self.manager.thread.join(10)
            self.assertFalse(self.manager.thread.is_alive())
        return self.manager.snapshot()

    def test_offline_exact_original_and_reuse_without_shared_mount(self):
        s = self.run_prepare()
        self.assertTrue(s['ready'])
        path, blob = self.manager.paths(self.catalog[0], self.file)
        self.assertEqual(path.read_bytes(), self.original)
        stamp = blob.stat().st_mtime_ns
        self.shared.unlink()
        self.assertTrue(self.run_prepare()['ready'])
        self.assertEqual(blob.stat().st_mtime_ns, stamp)

    def test_missing_mount_never_downloads_or_reports_ready(self):
        self.shared.unlink()
        s = self.run_prepare()
        self.assertFalse(s['ready'])
        self.assertEqual(s['error']['code'], 'shared_missing')

    def test_corruption_is_not_installed(self):
        data = self.shared.read_bytes()
        self.shared.write_bytes(data[:-1] + b'x')
        s = self.run_prepare()
        self.assertEqual(s['error']['code'], 'shared_checksum')
        path, blob = self.manager.paths(self.catalog[0], self.file)
        self.assertFalse(path.exists())
        self.assertFalse(blob.exists())
        self.assertFalse(list(blob.parent.glob('*.shared-part')))

    def test_truncated_source_rejected(self):
        self.shared.write_bytes(self.shared.read_bytes()[:-2])
        s = self.run_prepare()
        self.assertEqual(s['error']['code'], 'shared_format')

    def test_cancel_discards_only_incomplete_output_then_retry_succeeds(self):
        path, blob = self.manager.paths(self.catalog[0], self.file)
        blob.parent.mkdir(parents=True)
        cancelled = False
        def checkpoint():
            if cancelled:
                raise InterruptedError()
        def progress(n):
            nonlocal cancelled
            cancelled = True
        with self.assertRaises(InterruptedError):
            self.manager.shared.prepare(self.catalog[0], self.file, blob, checkpoint, progress)
        self.assertTrue(self.shared.exists())
        self.assertFalse(blob.exists())
        self.assertFalse(list(blob.parent.glob('*.shared-part')))
        self.assertTrue(self.run_prepare()['ready'])

    def test_missing_small_configuration_is_actionable(self):
        file = {'name': 'config.json', 'size': 2, 'algorithm': 'sha256', 'digest': hashlib.sha256(b'{}').hexdigest()}
        path, blob = self.manager.paths(self.catalog[0], file)
        blob.parent.mkdir(parents=True)
        with self.assertRaises(SharedModelError) as caught:
            self.manager.shared.prepare(self.catalog[0], file, blob, lambda: None, lambda n: None)
        self.assertEqual(caught.exception.code, 'assets_missing')

    def test_auto_uses_shared_without_network_and_reuses_cache_after_mount_removed(self):
        for remove_mount in (False, True):
            if remove_mount: self.shared.unlink()
            with patch('models.httpx.Client', side_effect=AssertionError('Valid cache/shared model must stay offline')):
                self.manager.launch(source='auto')
                self.manager.thread.join(10)
            self.assertFalse(self.manager.thread.is_alive())
            self.assertTrue(self.manager.ready())
            self.assertEqual(self.manager.snapshot()['source'], 'cache' if remove_mount else 'platform')

    def test_auto_new_instance_downloads_without_mount_and_resumes_partial(self):
        self.shared.unlink()
        path, blob = self.manager.paths(self.catalog[0], self.file)
        blob.parent.mkdir(parents=True)
        blob.with_name(blob.name+'.studio-part').write_bytes(self.original[:43])
        real_client = httpx.Client
        requests = []
        def get(request):
            requests.append(request)
            self.assertEqual(request.url.host, 'hf-mirror.com')
            self.assertEqual(request.headers['range'], 'bytes=43-')
            return httpx.Response(206, content=self.original[43:], headers={
                'content-range': f'bytes 43-{len(self.original)-1}/{len(self.original)}'})
        with patch('models.httpx.Client', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(get), **kw)):
            self.manager.launch(source='auto')
            self.manager.thread.join(10)
        self.assertTrue(self.manager.ready())
        self.assertEqual(len(requests), 1)
        self.assertEqual(path.read_bytes(), self.original)
        self.assertEqual(self.manager.snapshot()['source'], 'mirror')
        self.assertIn('自动', self.manager.snapshot()['source_note'])

    def test_auto_corrupt_shared_falls_back_to_verified_network_file(self):
        self.shared.write_bytes(self.shared.read_bytes()[:-1]+b'x')
        real_client = httpx.Client
        with patch('models.httpx.Client', side_effect=lambda **kw: real_client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, content=self.original)), **kw)):
            self.manager.launch(source='auto')
            self.manager.thread.join(10)
        self.assertTrue(self.manager.ready())
        self.assertEqual(self.manager.snapshot()['source'], 'mirror')
        self.assertEqual(self.manager.snapshot()['shared_diagnostic']['code'], 'shared_checksum')
        self.assertIn('shared_checksum', self.manager.snapshot()['source_note'])

    def test_auto_missing_mount_network_failure_is_not_reported_as_mount_error(self):
        self.shared.unlink()
        real_client = httpx.Client
        def get(request): raise httpx.ConnectError('private-address')
        with patch('models.httpx.Client', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(get), **kw)):
            self.manager.launch(source='auto')
            self.manager.thread.join(10)
        state = self.manager.snapshot()
        self.assertFalse(state['ready'])
        self.assertEqual(state['error']['code'], 'connection')
        self.assertNotIn('private-address', json.dumps(state))

    def test_auto_waits_for_late_platform_mount_without_network(self):
        data=self.shared.read_bytes()
        self.shared.unlink()
        self.manager.shared_wait_seconds=2
        mounted=threading.Timer(0.1,lambda:self.shared.write_bytes(data))
        with patch('models.httpx.Client',side_effect=AssertionError('Late mount must be used without network')):
            mounted.start()
            try:
                self.manager.launch(source='auto')
                self.manager.thread.join(5)
            finally:mounted.join()
        self.assertTrue(self.manager.ready())
        self.assertEqual(self.manager.snapshot()['source'],'platform')
        self.assertEqual(self.manager.snapshot()['shared_diagnostic']['code'],'available')

    def test_wait_for_mount_can_be_cancelled_without_network(self):
        self.shared.unlink()
        self.manager.shared_wait_seconds=30
        with patch('models.httpx.Client',side_effect=AssertionError('Cancelled wait must not download')):
            self.manager.launch(source='auto')
            for _ in range(100):
                if self.manager.snapshot().get('shared_diagnostic'):break
                time.sleep(0.01)
            self.manager.cancel()
            self.manager.thread.join(2)
        self.assertFalse(self.manager.thread.is_alive())
        self.assertEqual(self.manager.snapshot()['state'],'paused')

    def test_missing_bundled_config_records_exact_fallback_reason(self):
        data=b'{}'
        entry={'name':'config.json','size':2,'algorithm':'sha256','digest':hashlib.sha256(data).hexdigest()}
        manager=models.ModelManager(self.root/'small', [{**self.catalog[0],'files':[entry]}],
                                    shared=self.manager.shared,shared_wait_seconds=0)
        real_client=httpx.Client
        with patch('models.httpx.Client',side_effect=lambda **kw:real_client(
                transport=httpx.MockTransport(lambda r:httpx.Response(200,content=data)),**kw)):
            manager.launch(source='auto');manager.thread.join(5)
        self.assertTrue(manager.ready())
        self.assertEqual(manager.snapshot()['shared_diagnostic']['code'],'assets_missing')


if __name__ == '__main__':
    unittest.main()
