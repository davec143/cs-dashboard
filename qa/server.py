"""Local pilot UI and authenticated durable webhook receiver."""
import argparse
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from .db import Store
from .worker import minimal_call
from .history import history

ROOT = Path(__file__).resolve().parents[1]
MAX_BODY = 1_000_000
EVENTS = ('call.ended', 'transcription.created', 'call.comm_assets_generated')

def safe_compare(got, expected):
    return bool(expected) and hmac.compare_digest(str(got).encode(), str(expected).encode())

def make_handler(store, demo=False, public_bind=False):
    # SameSite cookie + random CSRF token. Local demo is only on loopback.
    sessions = {}
    admin = os.environ.get('QA_ADMIN_TOKEN', '')
    viewer = os.environ.get('QA_VIEW_TOKEN', '')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # No customer payloads, tokens, query strings, or URLs in logs.

        def send(self, code, value, content_type='application/json', headers=None):
            data = json.dumps(value).encode() if content_type == 'application/json' else value.encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0 or size > MAX_BODY:
                raise ValueError('invalid_body_size')
            return json.loads(self.rfile.read(size))

        def identity(self):
            from http.cookies import SimpleCookie
            cookie = SimpleCookie()
            cookie.load(self.headers.get('Cookie', ''))
            sid = cookie.get('qa_session')
            entry = sessions.get(sid.value if sid else '')
            if entry and entry['expires'] > time.time():
                return entry
            if demo and not public_bind:
                return {'role': 'admin', 'actor': 'local-demo', 'csrf': 'local-demo'}
            return None

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/healthz':
                return self.send(200, {'status': 'up', 'mode': 'demo' if demo else 'shadow'})
            if path == '/readyz':
                # Keep probes free of agent/customer data and operational details.
                beat = float(store.setting('worker_heartbeat') or 0)
                reconciled = float(store.setting('last_reconcile') or 0)
                ready = demo or (time.time()-beat < 120 and time.time()-reconciled < 7200
                                 and not store.setting('reconcile_error'))
                return self.send(200 if ready else 503, {'ready': ready})
            static = {'/': ('dashboard.html', 'text/html'), '/app.js': ('app.js', 'application/javascript'),
                      '/style.css': ('style.css', 'text/css')}
            if path in static:
                file, ct = static[path]
                return self.send(200, (ROOT / 'web' / file).read_text(), ct)
            if path == '/api/session':
                identity = self.identity()
                return self.send(200, {'authenticated': bool(identity), 'role': identity['role'] if identity else None,
                                       'csrf': identity['csrf'] if identity else None, 'demo': demo})
            if not self.identity():
                return self.send(401, {'error': 'sign_in_required'})
            if path == '/api/dashboard':
                facts, manifest = history()
                # Viewer gets aggregates and metadata; evidence is for the lead/admin.
                pilot = store.snapshot()
                if self.identity()['role'] == 'viewer':
                    for e in pilot['evaluations']:
                        e.pop('turns', None)
                        e.pop('result', None)
                    pilot['tasks'] = []
                return self.send(200, {'audit': facts, 'legacy': manifest, 'pilot': pilot,
                                       'demo': demo, 'audited_on': '2026-10-04'})
            return self.send(404, {'error': 'not_found'})

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                data = self.body()
                if not isinstance(data, dict):
                    raise ValueError('object_required')
                if path == '/webhooks/aircall':
                    if demo:
                        return self.send(409, {'error': 'demo_cannot_accept_real_calls'})
                    if not safe_compare(data.get('token', ''), os.environ.get('AIRCALL_WEBHOOK_TOKEN', '')):
                        return self.send(401, {'error': 'invalid_webhook_token'})
                    kind = data.get('event')
                    if kind not in EVENTS:
                        return self.send(200, {'ignored': True})
                    source = data.get('data') or {}
                    if kind == 'transcription.created':
                        cid = source.get('call_id')
                        if not str(cid).isdigit():
                            raise ValueError('transcription_call_id_required')
                        source = {'id': cid}  # Worker fetches canonical metadata.
                    meta = minimal_call(source)
                    event_key = hashlib.sha256(json.dumps({
                        'kind': kind, 'id': meta['id'], 'timestamp': data.get('timestamp'),
                        'asset_id': (data.get('data') or {}).get('id')}, sort_keys=True).encode()).hexdigest()
                    # ACK only after the atomic call/event/job transaction commits.
                    added = store.enqueue(meta, event_key, kind)
                    return self.send(200, {'accepted': True, 'duplicate': not added})
                if path == '/api/login':
                    token = data.get('token', '')
                    role = 'admin' if safe_compare(token, admin) else 'viewer' if safe_compare(token, viewer) else None
                    if not role:
                        return self.send(401, {'error': 'invalid_access_token'})
                    sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    if len(sessions) > 1000:
                        sessions.clear()
                    sessions[sid] = {'role': role, 'actor': role + '-token', 'csrf': csrf,
                                     'expires': time.time() + 8 * 3600}
                    suffix = '; Secure' if public_bind else ''
                    return self.send(200, {'authenticated': True}, headers={
                        'Set-Cookie': 'qa_session=' + sid + '; HttpOnly; SameSite=Strict; Path=/' + suffix})
                identity = self.identity()
                if not identity or identity['role'] != 'admin':
                    return self.send(403, {'error': 'admin_required'})
                if not safe_compare(self.headers.get('X-QA-CSRF', ''), identity['csrf']):
                    return self.send(403, {'error': 'csrf_required'})
                if path == '/api/coaching':
                    owner, due, action = (str(data.get(k, '')).strip() for k in ('owner', 'due', 'action'))
                    datetime.date.fromisoformat(due)
                    if not owner or not action or len(owner) > 100 or len(action) > 2000:
                        raise ValueError('invalid_coaching_task')
                    tid = store.create_task(int(data['evaluation_id']), owner, due, action, identity['actor'])
                    return self.send(201, {'id': tid})
                if path == '/api/coaching/status':
                    store.task_status(int(data['id']), data['status'], identity['actor'])
                    return self.send(200, {'updated': True})
                if path == '/api/replay':
                    cid = str(data['call_id'])
                    with store.connect() as conn:
                        call = conn.execute('SELECT metadata FROM calls WHERE id=?', (cid,)).fetchone()
                    if not call:
                        raise ValueError('unknown_call')
                    store.enqueue(json.loads(call[0]), 'replay:' + secrets.token_hex(16), 'manual.replay')
                    with store.connect() as conn:
                        conn.execute('INSERT INTO audit_log(actor,action,subject,detail,created) VALUES (?,?,?,?,?)',
                                     (identity['actor'], 'call.replay', cid, '{}', time.time()))
                    return self.send(202, {'queued': True})
                return self.send(404, {'error': 'not_found'})
            except (ValueError, KeyError, TypeError):
                return self.send(400, {'error': 'invalid_request'})
            except Exception:
                return self.send(503, {'error': 'storage_or_service_unavailable'})
    return Handler

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    host = os.environ.get('QA_HOST', '127.0.0.1')
    public = host not in ('127.0.0.1', 'localhost', '::1')
    if public and (args.demo or len(os.environ.get('QA_ADMIN_TOKEN', '')) < 32 or
                   len(os.environ.get('QA_VIEW_TOKEN', '')) < 32):
        raise SystemExit('External bind requires real mode and distinct strong access tokens behind TLS/SSO.')
    if public and os.environ.get('QA_ADMIN_TOKEN') == os.environ.get('QA_VIEW_TOKEN'):
        raise SystemExit('Admin and view tokens must differ.')
    db = os.environ.get('QA_DB', ROOT / ('runtime/demo.db' if args.demo else 'runtime/pilot.db'))
    store = Store(db)
    if args.demo:
        from .demo import seed
        seed(store)
    server = ThreadingHTTPServer((host, int(os.environ.get('PORT', '8765'))), make_handler(store, args.demo, public))
    print('CS Quality ' + ('demo' if args.demo else 'shadow pilot') + ' listening on http://' + host + ':' + str(server.server_port), flush=True)
    server.serve_forever()

if __name__ == '__main__':
    main()
