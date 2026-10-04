"""Production HTTP application, served by Gunicorn; named accounts required."""
import datetime
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, g
from werkzeug.exceptions import HTTPException
from . import auth
from .db import Store, database_path
from .server import EVENTS, MAX_BODY, safe_compare
from .worker import minimal_call
from .history import history

ROOT = Path(__file__).resolve().parents[1]


def create_app(store=None, testing=False):
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=MAX_BODY, TESTING=testing)
    store = store or Store(database_path())
    app.store = store
    if os.environ.get('QA_BOOTSTRAP_EMAIL') and os.environ.get('QA_BOOTSTRAP_PASSWORD_HASH'):
        auth.create_user(store, os.environ['QA_BOOTSTRAP_EMAIL'], os.environ['QA_BOOTSTRAP_PASSWORD_HASH'])

    @app.before_request
    def identify():
        g.user = auth.identity(store, request.cookies.get('qa_session'))
        if request.path.startswith('/api/') and request.path not in ('/api/login', '/api/session'):
            if not g.user:
                return jsonify(error='sign_in_required'), 401
            if request.method == 'POST' and not safe_compare(request.headers.get('X-QA-CSRF', ''), g.user['csrf']):
                return jsonify(error='csrf_required'), 403
            if request.method == 'POST' and request.path not in ('/api/logout', '/api/password') and g.user['role'] != 'admin':
                return jsonify(error='admin_required'), 403

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'no-referrer', 'X-Frame-Options':'DENY',
            'Strict-Transport-Security':'max-age=31536000',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.name.lower().replace(' ', '_')), exc.code
        if isinstance(exc, (ValueError, KeyError, TypeError)):
            return jsonify(error='invalid_request'), 400
        # Do not include database URLs, provider responses, or customer content in logs.
        app.logger.error('Request failed: %s', type(exc).__name__)
        return jsonify(error='storage_or_service_unavailable'), 503

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError('object_required')
        return value

    @app.get('/')
    def index():
        return send_from_directory(ROOT / 'web', 'dashboard.html')

    @app.get('/<asset>')
    def asset(asset):
        if asset not in ('app.js', 'style.css'):
            return jsonify(error='not_found'), 404
        return send_from_directory(ROOT / 'web', asset)

    @app.get('/healthz')
    def health():
        with store.connect() as conn:
            conn.execute('SELECT 1').fetchone()
        return jsonify(status='up', mode='shadow')

    @app.get('/readyz')
    def ready():
        beat = float(store.setting('worker_heartbeat') or 0)
        reconciled = float(store.setting('last_reconcile') or 0)
        ready = (os.environ.get('QA_PROCESSING_ENABLED') == 'true' and time.time()-beat < 120
                 and time.time()-reconciled < 7200 and not store.setting('reconcile_error'))
        return jsonify(ready=ready), 200 if ready else 503

    @app.get('/api/session')
    def session():
        return jsonify(authenticated=bool(g.user), role=g.user['role'] if g.user else None,
                       email=g.user['email'] if g.user else None, csrf=g.user['csrf'] if g.user else None,
                       demo=False, auth_mode='named')

    @app.post('/api/login')
    def login():
        data = body()
        # Railway's trusted ingress sets X-Real-IP; do not trust client X-Forwarded-For chains.
        token, error = auth.login(store, data.get('email', ''), data.get('password', ''),
                                 request.headers.get('X-Real-IP') or request.remote_addr)
        if error:
            return jsonify(error=error), 429 if error == 'rate_limited' else 401
        response = jsonify(authenticated=True)
        response.set_cookie('qa_session', token, max_age=8*3600, secure=not testing, httponly=True, samesite='Strict')
        return response

    @app.post('/api/logout')
    def logout():
        auth.logout(store, request.cookies.get('qa_session', ''))
        response = jsonify(authenticated=False)
        response.delete_cookie('qa_session', secure=not testing, httponly=True, samesite='Strict')
        return response

    @app.post('/api/password')
    def password():
        data = body()
        auth.change_password(store, g.user['email'], data.get('current_password'), data.get('new_password'))
        response = jsonify(updated=True, sign_in_required=True)
        response.delete_cookie('qa_session', secure=not testing, httponly=True, samesite='Strict')
        return response

    @app.get('/api/dashboard')
    def dashboard():
        pilot = store.snapshot()
        if g.user['role'] == 'viewer':
            for e in pilot['evaluations']:
                e.pop('turns', None)
                e.pop('result', None)
            pilot['tasks'] = []
        facts, manifest = history()
        return jsonify(audit=facts, legacy=manifest,
                       pilot=pilot, demo=False, audited_on='2026-10-04',
                       processing_enabled=os.environ.get('QA_PROCESSING_ENABLED') == 'true')

    @app.post('/webhooks/aircall')
    def webhook():
        if os.environ.get('QA_PROCESSING_ENABLED') != 'true':
            return jsonify(error='processing_paused'), 503
        data = body()
        if not safe_compare(data.get('token', ''), os.environ.get('AIRCALL_WEBHOOK_TOKEN', '')):
            return jsonify(error='invalid_webhook_token'), 401
        kind = data.get('event')
        if kind not in EVENTS:
            return jsonify(ignored=True)
        source = data.get('data') or {}
        if kind == 'transcription.created':
            source = {'id':source.get('call_id')}
        meta = minimal_call(source)
        key = hashlib.sha256(json.dumps({'kind':kind, 'id':meta['id'], 'timestamp':data.get('timestamp'),
            'asset_id':(data.get('data') or {}).get('id')}, sort_keys=True).encode()).hexdigest()
        added = store.enqueue(meta, key, kind)
        return jsonify(accepted=True, duplicate=not added)

    @app.post('/api/users')
    def create_user():
        data = body()
        user = auth.provision_user(store, data.get('email', ''), data.get('role', ''), g.user['email'])
        if user is None:
            return jsonify(error='account_already_exists'), 409
        return jsonify(user), 201

    @app.post('/api/coaching')
    def coaching():
        data = body()
        owner, due, action = (str(data.get(k, '')).strip() for k in ('owner','due','action'))
        datetime.date.fromisoformat(due)
        if not owner or not action or len(owner)>100 or len(action)>2000:
            raise ValueError('invalid_task')
        tid = store.create_task(int(data['evaluation_id']), owner, due, action, g.user['email'])
        return jsonify(id=tid), 201

    @app.post('/api/coaching/status')
    def status():
        data = body()
        store.task_status(int(data['id']), data['status'], g.user['email'])
        return jsonify(updated=True)

    @app.post('/api/replay')
    def replay():
        if os.environ.get('QA_PROCESSING_ENABLED') != 'true':
            return jsonify(error='processing_paused'), 409
        data = body()
        cid = str(data['call_id'])
        with store.connect() as conn:
            call = conn.execute('SELECT metadata FROM calls WHERE id=?', (cid,)).fetchone()
        if not call:
            raise ValueError('unknown_call')
        store.enqueue(json.loads(call[0]), 'replay:' + secrets.token_hex(16), 'manual.replay')
        with store.connect() as conn:
            conn.execute('INSERT INTO audit_log(actor,action,subject,detail,created) VALUES (?,?,?,?,?)',
                         (g.user['email'],'call.replay',cid,'{}',time.time()))
        return jsonify(queued=True), 202
    return app
