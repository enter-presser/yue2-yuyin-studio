"""SQLite transactions, encrypted credentials, immutable version snapshots."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from cryptography.fernet import Fernet

DATA = Path(os.environ.get('STUDIO_DATA', '/root/autodl-tmp/yue2-studio'))
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(DATA, 0o700)
KEY = DATA / 'master.key'
class LazyCipher:
    """Create an instance-local key only when the first credential is saved."""
    def _load(self,create=False):
        if not KEY.exists() and create:
            try:
                fd=os.open(KEY,os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd,'wb') as f: f.write(Fernet.generate_key())
        return Fernet(KEY.read_bytes())
    def encrypt(self,value): return self._load(create=True).encrypt(value)
    def decrypt(self,value): return self._load().decrypt(value)

cipher=LazyCipher()

def uid(): return secrets.token_hex(12)
def now(): return time.time()
def dump(value): return json.dumps(value, ensure_ascii=False)

@contextlib.contextmanager
def db():
    c = sqlite3.connect(DATA / 'studio.sqlite3', timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally: c.close()

def init():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT UNIQUE, password TEXT);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user TEXT, expires REAL);
        CREATE TABLE IF NOT EXISTS providers(user TEXT PRIMARY KEY, name TEXT, base_url TEXT, model TEXT, secret TEXT);
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, user TEXT, state TEXT, revision INTEGER, parent TEXT, created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, state TEXT, parent TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS suggestions(id TEXT PRIMARY KEY, project TEXT, revision INTEGER, body TEXT, applied INTEGER DEFAULT 0, created REAL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, role TEXT, body TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY, project TEXT, user TEXT, parent TEXT, name TEXT, snapshot TEXT, fingerprint TEXT, kind TEXT, status TEXT, result TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, version TEXT, user TEXT, idem TEXT, request_hash TEXT, status TEXT, stage TEXT, error TEXT, cancel INTEGER DEFAULT 0, reuse_version TEXT, created REAL, updated REAL, UNIQUE(user,idem));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, stage TEXT, detail TEXT, created REAL);
        CREATE INDEX IF NOT EXISTS project_owner ON projects(user);
        CREATE INDEX IF NOT EXISTS version_project ON versions(project);
        ''')
    os.chmod(DATA / 'studio.sqlite3', 0o600)

def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()

def check_password(password, stored):
    if not stored or ':' not in stored: return False
    return secrets.compare_digest(password_hash(password, stored.split(':')[0]), stored)

def event(c, job, stage, detail=''):
    c.execute('INSERT INTO events(job,stage,detail,created) VALUES(?,?,?,?)', (job,stage,detail,now()))

def transition(job, stage, status='running', detail='', error=None):
    with db() as c:
        c.execute('UPDATE jobs SET stage=?,status=?,error=?,updated=? WHERE id=?', (stage,status,error,now(),job))
        c.execute('UPDATE versions SET status=? WHERE id=(SELECT version FROM jobs WHERE id=?)',(status,job))
        event(c,job,stage,detail)

def artifact_dir(version):
    if not version or any(x not in '0123456789abcdef' for x in version) or len(version)!=24: raise ValueError('Invalid version')
    return DATA / 'artifacts' / version

def add_user(name,password):
    init()
    with db() as c: c.execute('INSERT INTO users VALUES(?,?,?)',(uid(),name,password_hash(password)))
