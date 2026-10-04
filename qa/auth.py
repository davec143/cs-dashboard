"""Named management accounts and revocable database-backed sessions."""
import hashlib
import re
import secrets
import time
from werkzeug.security import check_password_hash, generate_password_hash


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def create_user(store, email, password_hash, role='admin'):
    email = email.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or role not in ('admin', 'viewer'):
        raise ValueError('invalid_user')
    with store.connect() as conn:
        conn.execute('INSERT INTO users(email,password_hash,role,created) VALUES (?,?,?,?) ON CONFLICT DO NOTHING',
                     (email, password_hash, role, time.time()))


def provision_user(store, email, role, actor):
    """Create a named account; return its initial password exactly once."""
    email = str(email).strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or len(email) > 254 or role not in ('admin', 'viewer'):
        raise ValueError('invalid_user')
    password = secrets.token_urlsafe(24)
    hashed = generate_password_hash(password)
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
            return None
        conn.execute('INSERT INTO users(email,password_hash,role,created) VALUES (?,?,?,?)',
                     (email, hashed, role, time.time()))
        conn.execute('INSERT INTO audit_log(actor,action,subject,detail,created) VALUES (?,?,?,?,?)',
                     (actor, 'account.created', email, '{"role":"' + role + '"}', time.time()))
    return {'email':email, 'role':role, 'initial_password':password}


def identity(store, token):
    if not token:
        return None
    with store.connect() as conn:
        row = conn.execute('SELECT u.email,u.role,s.csrf FROM sessions s JOIN users u ON u.email=s.email '
                           'WHERE s.token_hash=? AND s.expires>? AND u.enabled=1', (digest(token), time.time())).fetchone()
    return dict(row) if row else None


def login(store, email, password, ip):
    now = time.time()
    email = str(email).strip().lower()[:254]
    # Persistent limits work across processes/restarts. Count successful attempts too.
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM login_attempts WHERE expires<=?', (now,))
        conn.execute('DELETE FROM sessions WHERE expires<=?', (now,))
        for key, limit in [('email:' + email, 5), ('ip:' + str(ip), 30)]:
            bucket = digest(key)
            row = conn.execute('SELECT count FROM login_attempts WHERE bucket=?', (bucket,)).fetchone()
            if row and row[0] >= limit:
                return None, 'rate_limited'
            conn.execute('INSERT INTO login_attempts(bucket,count,expires) VALUES (?,1,?) '
                         'ON CONFLICT(bucket) DO UPDATE SET count=login_attempts.count+1', (bucket, now + 60))
        user = conn.execute('SELECT * FROM users WHERE email=? AND enabled=1', (email,)).fetchone()
    # Hash outside the write lock. Dummy hash prevents account-existence timing disclosure.
    if not isinstance(password, str) or len(password) > 1024:
        return None, 'invalid_credentials'
    stored = user['password_hash'] if user else DUMMY_HASH
    if not check_password_hash(stored, password) or not user:
        return None, 'invalid_credentials'
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with store.connect() as conn:
        conn.execute('INSERT INTO sessions(token_hash,email,csrf,expires) VALUES (?,?,?,?)',
                     (digest(token), email, csrf, now + 8 * 3600))
    return token, None


def logout(store, token):
    with store.connect() as conn:
        conn.execute('DELETE FROM sessions WHERE token_hash=?', (digest(token),))


def change_password(store, email, old, new):
    if not isinstance(new, str) or not 16 <= len(new) <= 256:
        raise ValueError('password_must_be_16_to_256_characters')
    with store.connect() as conn:
        row = conn.execute('SELECT password_hash FROM users WHERE email=?', (email,)).fetchone()
    if not row or not isinstance(old, str) or not check_password_hash(row[0], old):
        raise ValueError('invalid_current_password')
    hashed = generate_password_hash(new)
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE users SET password_hash=? WHERE email=?', (hashed, email))
        conn.execute('DELETE FROM sessions WHERE email=?', (email,))
        conn.execute('INSERT INTO audit_log(actor,action,subject,detail,created) VALUES (?,?,?,?,?)',
                     (email, 'account.password_changed', email, '{}', time.time()))


DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32))
