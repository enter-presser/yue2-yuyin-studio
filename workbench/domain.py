"""Validated project state. No model, shell, or network access."""
import hashlib
import json
import os
import re
import sys
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, os.environ.get('YUE_ROOT', '/root/YuE') + '/skills/yue2-music/scripts')
from abc_tools import parse_abc, report, compare

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Params(Strict):
    cot: Literal['full', 'melody', 'off'] = 'full'
    seed: int = Field(default=831001, ge=0, le=2147483647)
    cfg_scale: float | None = Field(default=None, ge=0, le=20)

class Song(Strict):
    title: str = Field(default='未命名的歌', max_length=100)
    intent: str = Field(default='', max_length=4000)
    constraints: str = Field(default='', max_length=2000)
    language: str = Field(default='中文', max_length=100)
    mood: str = Field(default='温暖、明亮', max_length=300)
    vocal: str = Field(default='自然、亲近的人声', max_length=300)
    summary: str = Field(default='', max_length=2000)
    style: str = Field(default='Mandarin, warm acoustic pop, natural intimate vocal, acoustic guitar, piano, gentle drums, hopeful, 96 BPM', max_length=3000)
    lyrics: str = Field(default='', max_length=8000)
    abc: str = Field(default='', max_length=60000)
    params: Params = Field(default_factory=Params)

def fingerprint(song):
    s = Song.model_validate(song)
    return hashlib.sha256(json.dumps({'style': s.style, 'lyrics': s.lyrics, 'abc': s.abc, 'params': s.params.model_dump()}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def sections(lyrics):
    out = []
    for line in lyrics.splitlines():
        m = re.fullmatch(r'\s*\[([^\]]+)\]\s*', line)
        if m:
            out.append({'name': m[1], 'lines': []})
        elif line.strip():
            if not out:
                out.append({'name': 'Verse', 'lines': []})
            out[-1]['lines'].append(line)
    return out

def validate_song(song, generation=True):
    s = Song.model_validate(song)
    errors, warnings, score = [], [], None
    if generation:
        if not s.style.strip(): errors.append('请填写音乐方向。')
        if not s.lyrics.strip(): errors.append('请先写下歌词。')
        elif not re.match(r'^\s*\[(Verse|Chorus|Intro|Outro|Bridge|Pre-Chorus|Interlude|Instrumental|verse|chorus|intro|outro|bridge)[^\]]*\]', s.lyrics):
            errors.append('歌词请以 [Verse] 或 [Chorus] 等段落标记开始。可使用“整理歌词段落”。')
        if s.lyrics and not any(x['lines'] for x in sections(s.lyrics)):
            errors.append('段落中还没有要演唱的歌词。')
    if s.abc.strip():
        if s.params.cot == 'off': errors.append('直接生成模式不能同时使用乐谱，请切换模式或清空乐谱。')
        try:
            parsed = parse_abc(s.abc)
            # The official report contains Fraction values in chord/bar event grids.
            score = json.loads(json.dumps(report(parsed), default=str))
            if s.params.cot == 'melody' and any(v.chords for v in parsed.voices.values()):
                errors.append('自由伴奏模式需要不带和弦的旋律乐谱；可切换到完整规划。')
        except (ValueError, KeyError, IndexError) as exc:
            errors.append('乐谱未通过 YuE2 原生格式检查（也可能使用了暂不支持的写法）：' + str(exc)[:500])
    if len(s.lyrics) > 1800: warnings.append('歌词较长，可能触及模型上下文上限。建议先做较短的完整段落。')
    return {'valid': not errors, 'errors': errors, 'warnings': warnings, 'score': score, 'sections': sections(s.lyrics)}

class Save(Strict):
    revision: int
    state: Song

class Generate(Strict):
    revision: int
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
    kind: Literal['audio', 'plan'] = 'audio'
    reuse_version: str | None = None

class Message(Strict):
    message: str = Field(min_length=1, max_length=4000)
    revision: int

class Revision(Strict):
    revision: int

class Rename(Strict):
    name: str = Field(min_length=1, max_length=100)

class Provider(Strict):
    protocol: Literal['openai', 'anthropic'] = 'openai'
    name: str = Field(default='我的创作助手', min_length=1, max_length=100)
    base_url: str = Field(max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=1000)

class Login(Strict):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)
