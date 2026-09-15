"""Pinned, hash-verified public model downloads. No GPU imports or user URLs.

Writes native HF snapshots so the existing offline inference path stays intact.
All progress comes from verified files and bytes actually written to disk.
"""
import errno
import fcntl
import hashlib
from contextlib import ExitStack
from shared_models import SharedAssets, SharedModelError
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

import httpx

CATALOG = json.loads((Path(__file__).parent / 'model-catalog.json').read_text())
SOURCES = {'official': 'https://huggingface.co', 'mirror': 'https://hf-mirror.com'}
CACHE = Path(os.environ.get('STUDIO_MODEL_CACHE', '/root/autodl-tmp/huggingface/hub'))
ACTIVE = {'checking', 'preparing', 'downloading', 'verifying', 'cancelling'}
log = logging.getLogger('studio.models')


class ModelError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    os.replace(tmp, path)


class ModelManager:
    def __init__(self, cache=CACHE, catalog=CATALOG, shared=None, shared_wait_seconds=30):
        self.cache, self.catalog = Path(cache), catalog
        self.mutex = threading.RLock()
        self.cancelled = threading.Event()
        self.shared = shared or SharedAssets()
        self.shared_wait_seconds = shared_wait_seconds
        self.thread = None
        self.validated = {}
        self.state = {'state': 'checking', 'ready': False, 'completed_bytes': 0,
                      'total_bytes': sum(f['size'] for m in catalog for f in m['files']),
                      'current_file': '', 'detail': '正在检查本地模型', 'error': None,
                      'source': 'platform', 'updated': time.time()}
        # The manifest is bundled, never supplied by API clients.
        for m in catalog:
            if not re.fullmatch(r'[\w-]+/[\w.-]+', m['repo']) or not re.fullmatch('[0-9a-f]{40}', m['revision']):
                raise ValueError('Invalid bundled model identity')
            for f in m['files']:
                if Path(f['name']).is_absolute() or '..' in Path(f['name']).parts:
                    raise ValueError('Invalid bundled model path')

    def paths(self, m, f):
        base = self.cache / ('models--' + m['repo'].replace('/', '--'))
        return (base / 'snapshots' / m['revision'] / f['name'], base / 'blobs' / f['digest'])

    @staticmethod
    def stamp(path):
        s = path.stat()
        return [str(path.resolve()), s.st_size, s.st_mtime_ns, s.st_ctime_ns]

    def update(self, **values):
        with self.mutex:
            self.state.update(values, updated=time.time())

    def snapshot(self):
        with self.mutex:
            if self.state['ready'] and not self.ready():
                self.update(state='missing',ready=False,detail='本地模型文件发生变化，请重新检查或下载。')
            result = dict(self.state)
            result['models'] = [{'repo': m['repo'], 'revision': m['revision'],
                                 'bytes': sum(f['size'] for f in m['files'])} for m in self.catalog]
            result['cache_dir'] = str(self.cache)
            result['can_cancel'] = bool(self.thread and self.thread.is_alive())
            return result

    def ready(self):
        with self.mutex:
            if not self.state['ready']: return False
            for m in self.catalog:
                for f in m['files']:
                    path, _ = self.paths(m, f)
                    try:
                        if self.validated.get(str(path)) != self.stamp(path): return False
                    except OSError: return False
            return True

    def checkpoint(self):
        if self.cancelled.is_set(): raise InterruptedError()

    def digest_ok(self, path, f):
        if not path.is_file() or path.stat().st_size != f['size']: return False
        h = hashlib.sha256() if f['algorithm'] == 'sha256' else hashlib.sha1()
        if f['algorithm'] == 'git-sha1': h.update(f"blob {f['size']}\0".encode())
        with path.open('rb') as stream:
            while block := stream.read(4 * 1024 * 1024):
                self.checkpoint()
                h.update(block)
        return h.hexdigest() == f['digest']

    def launch(self, download=False, source='official', force=False):
        if source not in {*SOURCES, 'platform', 'auto'}: raise ModelError('source', '请选择支持的下载来源。')
        with self.mutex:
            if self.thread and self.thread.is_alive(): return {**self.snapshot(), 'duplicate': True}
            if download and self.ready(): return {**self.snapshot(), 'duplicate': True}
            self.cache.mkdir(parents=True, exist_ok=True)
            lock = (self.cache / 'studio-download.lock').open('a')
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                raise ModelError('download_busy', '另一个工作台进程正在检查或下载模型，请稍后重试。')
            self.cancelled.clear()
            self.update(state='checking', ready=False, current_file='', error=None,
                        detail='正在检查已有模型，通过校验的文件会直接复用', source=source, source_note='',
                        shared_diagnostic=None)
            self.thread = threading.Thread(target=self.run, args=(lock, download, source, force),
                                           name='model-download', daemon=True)
            self.thread.start()
            return {**self.snapshot(), 'duplicate': False}

    def cancel(self):
        with self.mutex:
            if self.thread and self.thread.is_alive():
                self.cancelled.set()
                detail = '正在暂停；已完成文件会保留，未完成的转换文件会在下次重新准备。' if self.state['source'] == 'platform' else '正在暂停；已下载的部分会保留，可继续下载'
                self.update(state='cancelling', detail=detail)
            return self.snapshot()

    def select_automatic_source(self):
        # Platform FUSE mounts may become available after the workbench process starts.
        deadline = time.monotonic() + self.shared_wait_seconds
        while True:
            self.checkpoint()
            try:
                available = self.shared.path.is_file()
                reason = 'shared_missing'
            except OSError:
                available, reason = False, 'shared_unreadable'
            diagnostic = {'code': 'available' if available else reason,
                          'path': str(self.shared.path), 'checked_at': time.time()}
            self.update(shared_diagnostic=diagnostic)
            if available: return 'platform'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.update(source_note=f'等待共享模型后仍未能读取文件（{reason}），已自动使用 HF-Mirror 下载。')
                log.warning('shared_probe code=%s; selecting mirror', reason)
                return 'mirror'
            self.update(state='checking', detail='正在等待平台共享模型就绪；可继续编辑歌词，暂未开始网络下载。')
            self.cancelled.wait(min(0.5, remaining))

    def run(self, lock, download, source, force):
        automatic = source == 'auto'
        try:
            receipt = self.cache / 'studio-model-verification.json'
            try: known = json.loads(receipt.read_text()) if not force else {}
            except (OSError, ValueError): known = {}
            valid, missing, completed = {}, [], 0
            for m in self.catalog:
                for f in m['files']:
                    self.checkpoint()
                    path, blob = self.paths(m, f)
                    self.update(current_file=m['repo'] + '/' + f['name'])
                    good = False
                    if path.is_file():
                        good = known.get(str(path)) == self.stamp(path) or self.digest_ok(path, f)
                    if good:
                        valid[str(path)] = self.stamp(path)
                        completed += f['size']
                    else:
                        missing.append((m, f))
                    self.update(completed_bytes=completed)
            self.validated = valid
            atomic_json(receipt, valid)
            if automatic:
                # A saved image does not include the previous instance's external mount.
                # Resolve after checking the cache so a ready installation stays offline.
                source = self.select_automatic_source() if missing else 'cache'
                download = True
                self.update(source=source)
            if missing and not download and source != 'platform':
                self.update(state='missing', ready=False, detail='需要下载音乐模型。你可以先构思和编辑歌词。')
                return
            remaining = sum(f['size'] for _, f in missing)
            partial_bytes = 0
            for m, f in missing:
                _, blob = self.paths(m, f)
                partial = blob.with_name(blob.name + '.studio-part')
                if source != 'platform' and partial.is_file(): partial_bytes += min(partial.stat().st_size, f['size'])
            free = shutil.disk_usage(self.cache).free
            self.update(free_bytes=free)
            if missing and free < remaining - partial_bytes + 1024**3:
                raise ModelError('disk_full', f'磁盘空间不足。请至少腾出 {(remaining-partial_bytes+1024**3)/1024**3:.1f} GiB 再准备模型。')
            # Explicitly disable ambient HF credentials; this is a public download.
            with ExitStack() as clients:
                client = None
                for m, f in missing:
                    self.checkpoint()
                    path, blob = self.paths(m, f)
                    blob.parent.mkdir(parents=True, exist_ok=True)
                    if not self.digest_ok(blob, f):
                        if source == 'platform':
                            self.update(state='preparing', detail='正在从平台共享模型准备文件，并校验官方版本', current_file=m['repo']+'/'+f['name'])
                            try:
                                self.shared.prepare(m, f, blob, self.checkpoint,
                                    lambda n: self.update(completed_bytes=completed+n))
                            except (SharedModelError, FileNotFoundError) as exc:
                                if not automatic: raise
                                self.checkpoint()
                                source = 'mirror'
                                code = exc.code if isinstance(exc, SharedModelError) else 'shared_file_missing'
                                message = exc.message if isinstance(exc, SharedModelError) else '共享源或配套文件在读取时不存在。'
                                log.warning('shared_prepare code=%s file=%s/%s; selecting mirror', code, m['repo'], f['name'])
                                self.update(source=source, completed_bytes=completed,
                                    shared_diagnostic={'code':code,'path':str(self.shared.path),'file':m['repo']+'/'+f['name'],'checked_at':time.time()},
                                    source_note=f'共享模型准备失败（{code}）：{message} 已自动切换到 HF-Mirror 下载官方固定版本。')
                        if source != 'platform':
                            if client is None:
                                client = clients.enter_context(httpx.Client(
                                    follow_redirects=True, timeout=httpx.Timeout(30, connect=15),
                                    headers={'User-Agent': 'Yue-Studio/1.2', 'Accept-Encoding': 'identity'}))
                            self.fetch(client, m, f, blob, source, completed)
                    self.checkpoint()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_name(path.name + '.studio-link')
                    if tmp.is_symlink() or tmp.exists(): tmp.unlink()
                    tmp.symlink_to(os.path.relpath(blob, path.parent))
                    os.replace(tmp, path)
                    valid[str(path)] = self.stamp(path)
                    self.validated = dict(valid)
                    atomic_json(receipt, valid)
                    completed += f['size']
                    self.update(completed_bytes=completed)
            self.update(state='ready', ready=True, completed_bytes=self.state['total_bytes'],
                        current_file='', detail='音乐模型已就绪并通过官方文件校验。点击生成歌曲后才会加载 GPU。', error=None)
        except InterruptedError:
            detail = '已暂停。继续准备会复用已完成文件；正在转换的单个文件会重新准备。' if source == 'platform' else '已暂停。点击继续准备，可复用已完成文件并续传。'
            self.update(state='paused', ready=False, detail=detail)
        except Exception as exc:
            if isinstance(exc, (ModelError, SharedModelError)): code, message = exc.code, exc.message
            elif isinstance(exc, httpx.TimeoutException): code, message = 'timeout', '下载连接超时。请点击继续下载，或切换下载来源后重试。'
            elif isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code
                code = 'rate_limit' if status == 429 else 'remote_unavailable'
                message = f'模型下载服务返回 HTTP {status}。请稍后继续下载，或切换来源。'
            elif isinstance(exc, httpx.RequestError): code, message = 'connection', '无法连接模型下载服务。请检查云实例网络，或切换下载来源后重试。'
            elif isinstance(exc, OSError) and exc.errno == errno.ENOSPC: code, message = 'disk_full', '磁盘已满。请释放空间后继续下载，已下载部分会保留。'
            else: code, message = 'download_failed', '模型准备失败。请检查目录写入权限并重试。'
            log.warning('model_download code=%s exception=%s', code, type(exc).__name__)
            self.update(state='failed', ready=False, detail=message, error={'code': code, 'message': message})
        finally:
            try: atomic_json(self.cache / 'studio-download-state.json', self.snapshot())
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()

    def fetch(self, client, m, f, blob, source, completed):
        partial = blob.with_name(blob.name + '.studio-part')
        offset = partial.stat().st_size if partial.exists() else 0
        if offset >= f['size']:
            if offset == f['size'] and self.digest_ok(partial, f):
                os.replace(partial, blob)
                return
            partial.unlink()
            offset = 0
        url = f"{SOURCES[source]}/{m['repo']}/resolve/{m['revision']}/{f['name']}"
        headers = {'Range': f'bytes={offset}-'} if offset else {}
        self.update(state='downloading', detail='正在下载音乐模型，可继续编辑歌词',
                    current_file=m['repo'] + '/' + f['name'], completed_bytes=completed+offset)
        with client.stream('GET', url, headers=headers) as response:
            response.raise_for_status()
            if response.status_code == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('content-range', ''))
                if not match or int(match[1]) != offset or int(match[3]) != f['size']:
                    raise ModelError('range_invalid', '下载来源的续传响应异常，请切换来源后重试。')
            elif response.status_code == 200:
                offset = 0  # This source ignored Range; safely restart this file.
            else:
                raise ModelError('download_response', '下载来源返回了无效文件响应，请重试。')
            with partial.open('ab' if offset else 'wb') as stream:
                for block in response.iter_bytes(256 * 1024):
                    self.checkpoint()
                    if offset + len(block) > f['size']:
                        raise ModelError('size_mismatch', '文件大小与官方固定版本不符，请切换来源后重试。')
                    stream.write(block)
                    offset += len(block)
                    self.update(completed_bytes=completed+offset)
                stream.flush()
                os.fsync(stream.fileno())
        self.checkpoint()
        self.update(state='verifying', detail='正在校验文件完整性，校验结束前不会加载模型')
        if not self.digest_ok(partial, f):
            # Only remove this downloader's invalid temporary file, never user files.
            partial.unlink()
            raise ModelError('checksum_mismatch', '文件完整性校验失败。已丢弃损坏的临时文件，请重新下载该文件。')
        os.replace(partial, blob)


manager = ModelManager()
