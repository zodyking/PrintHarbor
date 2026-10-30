import base64
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from cryptography.fernet import Fernet
from argon2 import PasswordHasher

ROOT = Path(os.getenv('DATA_DIR', '/data'))
DB_FILE = ROOT / 'printharbor.db'
PH = PasswordHasher()
PRIVATE_KEYS = {'imap_password', 'email_pin'}
DEFAULTS = {
    'mail_enabled': '0', 'imap_host': 'imap.gmail.com', 'imap_port': '993',
    'imap_user': '', 'imap_folder': 'INBOX', 'allowed_senders': '',
    'mail_recipient': '', 'poll_seconds': os.getenv('POLL_SECONDS', '3'),
    'default_queue': '', 'default_sides': 'one-sided', 'max_pages': '40',
    'max_files': '5', 'max_mb': '12', 'print_email_body': '0',
    'reply_enabled': '0', 'smtp_from': '', 'smtp_host': 'smtp.gmail.com', 'smtp_port': '587',
}

def _key():
    secret = os.environ.get('SESSION_SECRET', '')
    if len(secret) < 32 or secret.startswith('REPLACE_'):
        raise RuntimeError('Set a unique SESSION_SECRET of at least 32 characters in .env')
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))

def _reveal(raw):
    # Older builds encrypted private settings twice. Open until the value is plaintext.
    fernet = _key()
    current = raw
    revealed = False
    for _ in range(3):
        try:
            nxt = fernet.decrypt(current.encode()).decode()
        except Exception:
            break
        current = nxt
        revealed = True
    return current if revealed else ''

@contextmanager
def connection():
    ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_FILE), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    try:
        yield c
        c.commit()
    finally:
        c.close()

def initialize():
    _key()
    password = os.environ.get('ADMIN_PASSWORD', '')
    if len(password) < 12 or password.startswith('REPLACE_'):
        raise RuntimeError('Set ADMIN_PASSWORD to a unique password of at least 12 characters in .env')
    (ROOT / 'files').mkdir(parents=True, exist_ok=True)
    (ROOT / 'tmp').mkdir(parents=True, exist_ok=True)
    with connection() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS printers (
            queue TEXT PRIMARY KEY, label TEXT NOT NULL, host TEXT NOT NULL,
            protocol TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe TEXT UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            source TEXT NOT NULL, sender TEXT DEFAULT '', filename TEXT NOT NULL,
            queue TEXT NOT NULL, pages INTEGER, copies INTEGER DEFAULT 1,
            sides TEXT DEFAULT 'one-sided', status TEXT NOT NULL,
            cups_id TEXT DEFAULT '', filepath TEXT DEFAULT '', error TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, level TEXT, message TEXT
        );
        ''')
        db.execute('INSERT OR IGNORE INTO users VALUES (?,?)',
                   (os.getenv('ADMIN_USER', 'admin'), PH.hash(password)))
        for k, v in DEFAULTS.items():
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (k, v))
        if not db.execute("SELECT 1 FROM settings WHERE key='email_pin'").fetchone():
            set_setting('email_pin', '', db=db)
        db.execute(
            "UPDATE jobs SET status='error', error=? WHERE status='preparing'",
            ('Interrupted before the document was submitted',),
        )

def verify_user(username, password):
    with connection() as db:
        row = db.execute('SELECT password_hash FROM users WHERE username=?', (username,)).fetchone()
    if not row:
        return False
    try:
        return PH.verify(row['password_hash'], password)
    except Exception:
        return False

def settings():
    with connection() as db:
        rows = db.execute('SELECT key,value FROM settings').fetchall()
    vals = {r['key']: r['value'] for r in rows}
    for name in PRIVATE_KEYS:
        if vals.get(name):
            vals[name] = _reveal(vals[name])
    return vals

def set_setting(key, value, db=None):
    stored = value
    if key in PRIVATE_KEYS and value:
        stored = _key().encrypt(str(value).encode()).decode()
    sql = 'INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value'
    if db is None:
        with connection() as conn:
            conn.execute(sql, (key, stored))
    else:
        db.execute(sql, (key, stored))

def event(level, msg):
    text = str(msg)[:600]
    with connection() as db:
        last = db.execute('SELECT id, level, message FROM events ORDER BY id DESC LIMIT 1').fetchone()
        if last and last['level'] == level and last['message'] == text:
            db.execute('UPDATE events SET created_at=CURRENT_TIMESTAMP WHERE id=?', (last['id'],))
            return
        db.execute('INSERT INTO events(level,message) VALUES (?,?)', (level, text))
        db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 250)')

def records(table, limit=60):
    if table not in ('jobs', 'printers', 'events'):
        raise ValueError('Invalid table')
    order = 'queue' if table == 'printers' else 'id DESC'
    with connection() as db:
        return [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY {order} LIMIT ?', (limit,)).fetchall()]
