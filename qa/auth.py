"""Named management accounts and revocable database-backed sessions."""
import hashlib
import json
import re
import secrets
import time
from werkzeug.security import check_password_hash, generate_password_hash


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _email(value):
    email = str(value).strip().lower()
    if len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise ValueError('invalid_user')
    return email


def _public_user(row):
    return {'email': row['email'], 'role': row['role'],
            'enabled': bool(row['enabled']), 'created': row['created']}


def _require_admin(conn, actor):
    # Recheck after obtaining the write lock: the request's session may have been
    # revoked or its account demoted since the HTTP authorization check.
    row = conn.execute('SELECT role,enabled FROM users WHERE email=?', (actor,)).fetchone()
    if not row or row['role'] != 'admin' or not row['enabled']:
        raise ValueError('administrator_required')


def list_users(store):
    """Return safe account metadata; never return password hashes or sessions."""
    with store.connect() as conn:
        return [_public_user(row) for row in conn.execute(
            'SELECT email,role,enabled,created FROM users ORDER BY email')]


def create_user(store, email, password_hash, role='admin'):
    email = email.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or role not in ('admin', 'viewer'):
        raise ValueError('invalid_user')
    with store.connect() as conn:
        conn.execute('INSERT INTO users(email,password_hash,role,created) VALUES (?,?,?,?) ON CONFLICT DO NOTHING',
                     (email, password_hash, role, time.time()))


def provision_user(store, email, role, actor):
    """Create a named account; return its initial password exactly once."""
    email = _email(email)
    actor = _email(actor)
    if role not in ('admin', 'viewer'):
        raise ValueError('invalid_user')
    password = secrets.token_urlsafe(24)
    hashed = generate_password_hash(password)
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _require_admin(conn, actor)
        if conn.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
            return None
        conn.execute('INSERT INTO users(email,password_hash,role,created) VALUES (?,?,?,?)',
                     (email, hashed, role, time.time()))
        conn.execute('INSERT INTO audit_log(actor,action,subject,detail,created) VALUES (?,?,?,?,?)',
                     (actor, 'account.created', email, '{"role":"' + role + '"}', time.time()))
    return {'email':email, 'role':role, 'initial_password':password}


def update_user_access(store, email, role, enabled, actor):
    """Change access atomically, keeping at least one enabled administrator."""
    email, actor = _email(email), _email(actor)
    if role not in ('admin', 'viewer'):
        raise ValueError('invalid_user')
    if not isinstance(enabled, bool):
        raise ValueError('invalid_enabled')
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _require_admin(conn, actor)
        row = conn.execute('SELECT email,role,enabled,created FROM users WHERE email=?', (email,)).fetchone()
        if not row:
            return None
        old = _public_user(row)
        if old['role'] == role and old['enabled'] == enabled:
            return old
        if email == actor and (role != 'admin' or not enabled):
            raise ValueError('cannot_change_own_access')
        if old['role'] == 'admin' and old['enabled'] and (role != 'admin' or not enabled):
            count = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
            if count <= 1:
                raise ValueError('last_enabled_admin')
        conn.execute('UPDATE users SET role=?,enabled=? WHERE email=?', (role, int(enabled), email))
        conn.execute('DELETE FROM sessions WHERE email=?', (email,))
        detail = json.dumps({'before': {'role': old['role'], 'enabled': old['enabled']},
                             'after': {'role': role, 'enabled': enabled}})
        conn.execute('INSERT INTO audit_log(actor,action,subject,detail,created) VALUES (?,?,?,?,?)',
                     (actor, 'account.access_changed', email, detail, time.time()))
    return {**old, 'role': role, 'enabled': enabled}


def reset_user_password(store, email, actor):
    """Issue a unique replacement password once and revoke all existing sessions."""
    email, actor = _email(email), _email(actor)
    password = secrets.token_urlsafe(24)
    hashed = generate_password_hash(password)
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _require_admin(conn, actor)
        row = conn.execute('SELECT email,role,enabled,created FROM users WHERE email=?', (email,)).fetchone()
        if not row:
            return None
        conn.execute('UPDATE users SET password_hash=? WHERE email=?', (hashed, email))
        conn.execute('DELETE FROM sessions WHERE email=?', (email,))
        conn.execute('INSERT INTO audit_log(actor,action,subject,detail,created) VALUES (?,?,?,?,?)',
                     (actor, 'account.password_reset', email, '{}', time.time()))
    return {**_public_user(row), 'initial_password': password}


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
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT password_hash,enabled FROM users WHERE email=?', (email,)).fetchone()
        # A reset/disable can occur while scrypt runs outside the lock. Never
        # resurrect a session authenticated using an obsolete credential.
        if not current or not current['enabled'] or current['password_hash'] != stored:
            return None, 'invalid_credentials'
        conn.execute('INSERT INTO sessions(token_hash,email,csrf,expires) VALUES (?,?,?,?)',
                     (digest(token), email, csrf, time.time() + 8 * 3600))
    return token, None


def logout(store, token):
    with store.connect() as conn:
        conn.execute('DELETE FROM sessions WHERE token_hash=?', (digest(token),))


def change_password(store, email, old, new):
    if not isinstance(new, str) or not 16 <= len(new) <= 256:
        raise ValueError('password_must_be_16_to_256_characters')
    with store.connect() as conn:
        row = conn.execute('SELECT password_hash FROM users WHERE email=? AND enabled=1', (email,)).fetchone()
    if not row or not isinstance(old, str) or len(old) > 1024 or not check_password_hash(row[0], old):
        raise ValueError('invalid_current_password')
    hashed = generate_password_hash(new)
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT password_hash,enabled FROM users WHERE email=?', (email,)).fetchone()
        if not current or not current['enabled'] or current['password_hash'] != row[0]:
            raise ValueError('invalid_current_password')
        conn.execute('UPDATE users SET password_hash=? WHERE email=?', (hashed, email))
        conn.execute('DELETE FROM sessions WHERE email=?', (email,))
        conn.execute('INSERT INTO audit_log(actor,action,subject,detail,created) VALUES (?,?,?,?,?)',
                     (email, 'account.password_changed', email, '{}', time.time()))


DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32))
