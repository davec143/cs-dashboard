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
            if request.path.startswith(('/api/users', '/api/reviews', '/api/system')) and g.user['role'] != 'admin':
                return jsonify(error='admin_required'), 403
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
            known = {'invalid_user','invalid_enabled','administrator_required','cannot_change_own_access','last_enabled_admin',
                'current_evaluation_required','review_decision_and_note_required','evidence_checks_required_before_approval',
                'password_must_be_16_to_256_characters','invalid_current_password'}
            return jsonify(error=str(exc) if str(exc) in known else 'invalid_request'), 400
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
        enabled = os.environ.get('QA_PROCESSING_ENABLED') == 'true'
        backup_at = float(store.setting('backup_completed_at') or 0)
        backups_ok = os.environ.get('QA_BACKUPS_REQUIRED') != 'true' or (
            time.time()-backup_at < 36*3600 and not store.setting('backup_error'))
        operations_ok = time.time()-beat < 180 and backups_ok
        ready = (enabled and time.time()-beat < 180
                 and time.time()-reconciled < 7200 and not store.setting('reconcile_error'))
        return jsonify(ready=ready, processing_enabled=enabled, operations_ok=operations_ok), 200 if ready else 503

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
                e.pop('human_review', None)
            pilot['tasks'] = []
        facts, manifest = history()
        if g.user['role'] == 'viewer':
            manifest = []
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
        if not isinstance(source, dict):
            raise ValueError('invalid_event_data')
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

    @app.get('/api/users')
    def users():
        return jsonify(users=auth.list_users(store))

    @app.post('/api/users/access')
    def user_access():
        data = body()
        user = auth.update_user_access(store, data.get('email'), data.get('role'), data.get('enabled'), g.user['email'])
        return (jsonify(user), 200) if user else (jsonify(error='account_not_found'), 404)

    @app.post('/api/users/reset-password')
    def user_reset():
        user = auth.reset_user_password(store, body().get('email'), g.user['email'])
        return (jsonify(user), 200) if user else (jsonify(error='account_not_found'), 404)

    @app.post('/api/reviews')
    def review_evaluation():
        data = body()
        rid = store.review_evaluation(int(data['evaluation_id']), data.get('decision'), data.get('note'), g.user['email'])
        return jsonify(id=rid), 201

    @app.get('/api/reviews/<int:evaluation_id>')
    def review_history(evaluation_id):
        return jsonify(reviews=store.review_history(evaluation_id))

    @app.get('/api/system')
    def system():
        try:
            worker_config = json.loads(store.setting('worker_configuration') or '{}')
        except ValueError:
            worker_config = {}
        now = time.time()
        heartbeat = float(store.setting('worker_heartbeat') or 0)
        reconcile_at = float(store.setting('last_reconcile') or 0)
        enabled = os.environ.get('QA_PROCESSING_ENABLED') == 'true'
        with store.connect() as conn:
            failed = conn.execute("SELECT COUNT(*) FROM jobs WHERE state='error'").fetchone()[0]
            overdue = conn.execute("SELECT COUNT(*) FROM jobs WHERE state='queued' AND due<?", (now-900,)).fetchone()[0]
        alerts = []
        if now-heartbeat >= 180:
            alerts.append('Worker has not reported in the last 3 minutes.')
        if enabled and (now-reconcile_at >= 7200 or store.setting('reconcile_error')):
            alerts.append('Call reconciliation needs attention.')
        if failed:
            alerts.append(f'{failed} calls failed processing. Inspect and retry after resolving the cause.')
        if overdue:
            alerts.append(f'{overdue} queued calls are more than 15 minutes overdue.')
        if store.setting('backup_error'):
            alerts.append('The latest backup or restore drill failed. Inspect the backup service before relying on recovery.')
        backup_at = float(store.setting('backup_completed_at') or 0)
        if os.environ.get('QA_BACKUPS_REQUIRED') == 'true' and now-backup_at >= 36*3600:
            alerts.append('No verified database backup has completed in the last 36 hours.')
        return jsonify(worker_configuration=worker_config, webhook_configured=bool(os.environ.get('AIRCALL_WEBHOOK_TOKEN')),
            processing_enabled=enabled, alerts=alerts, backup_completed_at=store.setting('backup_completed_at'),
            restore_verified_at=store.setting('restore_verified_at'))

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
