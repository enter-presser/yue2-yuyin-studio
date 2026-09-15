"""Offline preparation from the platform's public combined YuE2 checkpoint.

Never execute checkpoint metadata. Only install files matching the pinned official
digest; shared source stays untouched. This module does not import torch.
"""
import hashlib
import json
import math
import os
from pathlib import Path

DEFAULT_SHARED = '/.autodl/ac/d6/61/acd661ae90fcf20f8955a84088c01cce'
ASSETS = Path(__file__).parent / 'model-assets'


class SharedModelError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def read_header(stream):
    prefix = stream.read(8)
    size = int.from_bytes(prefix, 'little')
    if len(prefix) != 8 or not 2 <= size <= 16 * 1024 * 1024:
        raise SharedModelError('shared_format', '平台共享模型格式不匹配，请检查挂载的模型版本。')
    raw = prefix + stream.read(size)
    try:
        header = json.loads(raw[8:])
        if len(raw) != size + 8 or not isinstance(header, dict):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise SharedModelError('shared_format', '平台共享模型文件头不完整，请检查挂载。')
    return raw, header


def tensor_source(name, target, headers, vae):
    if vae:
        source_name = 'vae.' + name
    elif '.nar_' in name:
        source_name = 'model.diffusion_model.' + name.replace('.nar_input_layernorm.', '.input_layernorm.').replace('.nar_pre_mlp_layernorm.', '.post_attention_layernorm.').replace('.nar_mlp.', '.mlp.').replace('.nar_self_attn.', '.self_attn.')
    elif name == 'lm_head.weight':
        source_name = 'text_encoders.model.lm_head.weight'
    elif name.startswith('model.'):
        source_name = 'text_encoders.' + name
    else:
        source_name = 'model.diffusion_model.' + name
    offset = 0
    if source_name not in headers:
        for projection, rows in [('q_proj', 0), ('k_proj', 2048), ('v_proj', 3072), ('gate_proj', 0), ('up_proj', 6144)]:
            if '.' + projection + '.' in source_name:
                fused = 'qkv_proj' if projection in ('q_proj', 'k_proj', 'v_proj') else 'gate_up_proj'
                source_name = source_name.replace('.' + projection + '.', '.' + fused + '.')
                offset = rows * 2048 * 2
                break
    source = headers.get(source_name)
    if not isinstance(source, dict) or source.get('dtype') != target.get('dtype'):
        raise SharedModelError('shared_format', '共享模型的张量结构与当前 YuE2 版本不匹配。')
    begin, end = source['data_offsets']
    size = target['data_offsets'][1] - target['data_offsets'][0]
    width = {'BF16': 2, 'F32': 4}.get(target['dtype'])
    if not width or size != math.prod(target['shape']) * width or begin < 0 or size < 0 or begin + offset + size > end:
        raise SharedModelError('shared_format', '共享模型的张量范围不正确，未使用该文件。')
    if offset == 0 and size == end - begin and source['shape'] != target['shape']:
        raise SharedModelError('shared_format', '共享模型的张量形状不匹配。')
    return begin + offset, size


class SharedAssets:
    def __init__(self, path=None, assets=ASSETS):
        self.path = Path(path or os.environ.get('STUDIO_SHARED_MODEL', DEFAULT_SHARED))
        self.assets = Path(assets)

    def prepare(self, model, entry, blob, checkpoint, progress):
        asset = self.assets / model['repo'].replace('/', '--') / entry['name']
        temp = blob.with_name(blob.name + '.shared-part')
        try:
            h = hashlib.sha256() if entry['algorithm'] == 'sha256' else hashlib.sha1()
            if entry['algorithm'] == 'git-sha1':
                h.update(f"blob {entry['size']}\0".encode())
            with temp.open('wb') as target:
                if entry['name'] != 'model.safetensors':
                    if not asset.is_file():
                        raise SharedModelError('assets_missing', '本地模型配置文件缺失，请修复安装或使用备用下载。')
                    with asset.open('rb') as source:
                        while block := source.read(1024 * 1024):
                            checkpoint()
                            target.write(block)
                            h.update(block)
                            progress(target.tell())
                else:
                    if not self.path.is_file():
                        if self.path == Path(DEFAULT_SHARED) and not Path('/.autodl').is_dir():
                            raise SharedModelError('shared_missing', '当前实例未挂载 AutoDL.Art 公共模型盘。镜像只保留接入配置，不包含平台挂载。请使用下方备用网络下载；完成后自动复用本地模型。重复点击“重新接入”不能创建平台挂载。')
                        raise SharedModelError('shared_missing', '未检测到平台共享模型。请在平台挂载 YuE2 公共模型后点击“重新接入”，或展开备用下载。')
                    with asset.with_name(asset.name + '.header').open('rb') as f:
                        raw, official = read_header(f)
                    with self.path.open('rb') as source:
                        shared_raw, shared = read_header(source)
                        data_start = len(shared_raw)
                        source_size = os.fstat(source.fileno()).st_size
                        tensors = sorted(((k, v) for k, v in official.items() if k != '__metadata__'), key=lambda x: x[1]['data_offsets'][0])
                        target.write(raw)
                        h.update(raw)
                        position = 0
                        for name, metadata in tensors:
                            checkpoint()
                            if metadata['data_offsets'][0] != position:
                                raise SharedModelError('shared_format', '模型文件布局不正确。')
                            offset, size = tensor_source(name, metadata, shared, model['repo'].endswith('YuE2-Vae'))
                            if data_start + offset + size > source_size:
                                raise SharedModelError('shared_format', '平台共享模型文件不完整。')
                            source.seek(data_start + offset)
                            remaining = size
                            while remaining:
                                checkpoint()
                                block = source.read(min(8 * 1024 * 1024, remaining))
                                if not block:
                                    raise SharedModelError('shared_format', '平台共享模型读取不完整。')
                                target.write(block)
                                h.update(block)
                                remaining -= len(block)
                                progress(target.tell())
                            position = metadata['data_offsets'][1]
                target.flush()
                os.fsync(target.fileno())
            checkpoint()
            if temp.stat().st_size != entry['size'] or h.hexdigest() != entry['digest']:
                raise SharedModelError('shared_checksum', '共享模型与固定官方版本校验值不一致，未接入。可尝试备用下载。')
            os.replace(temp, blob)
        finally:
            # Only this operation's disposable output; never shared source/user data.
            temp.unlink(missing_ok=True)
