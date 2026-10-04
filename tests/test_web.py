import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from werkzeug.security import generate_password_hash
from qa.auth import create_user
from qa.db import Store
from qa.demo import seed
from qa.web import create_app

class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=self.make_store()
        seed(self.store)
        self.env=patch.dict(os.environ,{'QA_PROCESSING_ENABLED':'false','QA_AUDIT_DIR':self.tmp.name})
        self.env.start()
        create_user(self.store,'lead@example.com',generate_password_hash('LeadTestPassword123!'))
        create_user(self.store,'viewer@example.com',generate_password_hash('ViewTestPassword123!'),'viewer')
        self.app=create_app(self.store,testing=True)
        self.client=self.app.test_client()
    def make_store(self):
        return Store(Path(self.tmp.name)/'test.db')
    def tearDown(self):
        self.env.stop();self.tmp.cleanup()
    def login(self,email='lead@example.com',password='LeadTestPassword123!'):
        r=self.client.post('/api/login',json={'email':email,'password':password})
        self.assertEqual(r.status_code,200)
        return {'X-QA-CSRF':self.client.get('/api/session').json['csrf']}
    def test_auth_required_and_paused_worker_cannot_accept_calls(self):
        self.assertEqual(self.client.get('/api/dashboard').status_code,401)
        self.assertEqual(self.client.post('/webhooks/aircall',json={}).status_code,503)
        self.assertEqual(self.client.get('/readyz').status_code,503)
        self.assertEqual(self.client.get('/healthz').status_code,200)
    def test_session_survives_application_restart_and_logout_revokes_it(self):
        h=self.login();cookie=self.client.get_cookie('qa_session').value
        other=create_app(self.store,testing=True).test_client();other.set_cookie('qa_session',cookie)
        self.assertTrue(other.get('/api/session').json['authenticated'])
        self.assertEqual(self.client.post('/api/logout',json={},headers=h).status_code,200)
        self.assertFalse(other.get('/api/session').json['authenticated'])
    def test_viewer_cannot_read_evidence_or_write(self):
        h=self.login('viewer@example.com','ViewTestPassword123!')
        data=self.client.get('/api/dashboard').json
        self.assertNotIn('turns',data['pilot']['evaluations'][0])
        self.assertNotIn('result',data['pilot']['evaluations'][0])
        self.assertEqual(data['pilot']['tasks'],[])
        self.assertEqual(data['legacy'],[])
        self.assertEqual(self.client.post('/api/coaching',json={},headers=h).status_code,403)
    def test_csrf_and_named_actor(self):
        h=self.login();e=self.store.snapshot()['evaluations'][0]
        body={'evaluation_id':e['id'],'owner':'Lead','due':'2026-12-10','action':'Practice summarizing'}
        self.assertEqual(self.client.post('/api/coaching',json=body).status_code,403)
        self.assertEqual(self.client.post('/api/coaching',json=body,headers=h).status_code,201)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT actor FROM audit_log').fetchone()[0],'lead@example.com')
    def test_password_change_revokes_all_sessions(self):
        h=self.login()
        self.assertEqual(self.client.post('/api/password',headers=h,json={'current_password':'wrong','new_password':'NewTestPassword123!'}).status_code,400)
        self.assertEqual(self.client.post('/api/password',headers=h,json={'current_password':'LeadTestPassword123!','new_password':'NewTestPassword123!'}).status_code,200)
        self.assertFalse(self.client.get('/api/session').json['authenticated'])
        self.login(password='NewTestPassword123!')
    def test_login_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.client.post('/api/login',json={'email':'lead@example.com','password':'wrong'}).status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'email':'lead@example.com','password':'wrong'}).status_code,429)
    def test_valid_webhook_is_deduplicated(self):
        with patch.dict(os.environ,{'QA_PROCESSING_ENABLED':'true','AIRCALL_WEBHOOK_TOKEN':'test-hook'}):
            payload={'event':'call.ended','token':'test-hook','timestamp':123,'data':{'id':567}}
            self.assertFalse(self.client.post('/webhooks/aircall',json=payload).json['duplicate'])
            self.assertTrue(self.client.post('/webhooks/aircall',json=payload).json['duplicate'])
            payload['token']='wrong'
            self.assertEqual(self.client.post('/webhooks/aircall',json=payload).status_code,401)
    def test_cookie_and_content_security(self):
        prod=create_app(self.store).test_client()
        r=prod.post('/api/login',json={'email':'lead@example.com','password':'LeadTestPassword123!'})
        self.assertIn('Secure',r.headers['Set-Cookie']);self.assertIn('HttpOnly',r.headers['Set-Cookie'])
        self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
        self.assertEqual(self.client.post('/api/login',data='x'*1_000_001,content_type='application/json').status_code,413)

    def test_admin_can_provision_unique_account_and_it_can_sign_in(self):
        h=self.login()
        r=self.client.post('/api/users',headers=h,json={'email':'new-admin@example.com','role':'admin'})
        self.assertEqual(r.status_code,201)
        self.assertGreaterEqual(len(r.json['initial_password']),24)
        password=r.json['initial_password']
        duplicate=self.client.post('/api/users',headers=h,json={'email':'new-admin@example.com','role':'viewer'})
        self.assertEqual(duplicate.status_code,409)
        self.client.post('/api/logout',json={},headers=h)
        self.login('new-admin@example.com',password)
        self.assertEqual(self.client.get('/api/session').json['role'],'admin')
        with self.store.connect() as conn:
            row=conn.execute("SELECT actor,subject,detail FROM audit_log WHERE action='account.created'").fetchone()
            self.assertEqual(row['actor'],'lead@example.com')
            self.assertNotIn(password,row['detail'])
    def test_user_provisioning_requires_admin_and_csrf(self):
        payload={'email':'new@example.com','role':'admin'}
        self.assertEqual(self.client.post('/api/users',json=payload).status_code,401)
        h=self.login('viewer@example.com','ViewTestPassword123!')
        self.assertEqual(self.client.post('/api/users',headers=h,json=payload).status_code,403)
        self.client.post('/api/logout',headers=h,json={})
        self.login()
        self.assertEqual(self.client.post('/api/users',json=payload).status_code,403)
    def test_user_provisioning_rejects_invalid_role_and_address(self):
        h=self.login()
        for payload in ({'email':'invalid','role':'admin'},{'email':'new@example.com','role':'superuser'}):
            self.assertEqual(self.client.post('/api/users',headers=h,json=payload).status_code,400)

    def test_user_management_is_admin_only_and_revokes_access(self):
        h=self.login()
        users=self.client.get('/api/users')
        self.assertEqual(users.status_code,200)
        self.assertNotIn('password_hash',str(users.json))
        changed=self.client.post('/api/users/access',headers=h,json={'email':'viewer@example.com','role':'viewer','enabled':False})
        self.assertEqual(changed.status_code,200)
        self.assertFalse(changed.json['enabled'])
        reset=self.client.post('/api/users/reset-password',headers=h,json={'email':'viewer@example.com'})
        self.assertEqual(reset.status_code,200)
        self.assertGreaterEqual(len(reset.json['initial_password']),24)
        self.assertEqual(self.client.post('/api/users/access',headers=h,json={'email':'lead@example.com','role':'viewer','enabled':True}).status_code,400)

    def test_sensitive_management_reads_require_admin(self):
        self.login('viewer@example.com','ViewTestPassword123!')
        for path in ['/api/users','/api/system','/api/reviews/1']:
            self.assertEqual(self.client.get(path).status_code,403)

    def test_review_removes_disputed_score_and_preserves_history(self):
        h=self.login();e=self.store.snapshot()['evaluations'][0]
        body={'evaluation_id':e['id'],'decision':'disputed','note':'The interpretation of the quoted turn needs correction.'}
        self.assertEqual(self.client.post('/api/reviews',json=body).status_code,403)
        self.assertEqual(self.client.post('/api/reviews',json=body,headers=h).status_code,201)
        data=self.client.get('/api/dashboard').json
        self.assertEqual(data['pilot']['validated_count'],0)
        self.assertIsNone(data['pilot']['summary']['mean'])
        self.assertEqual(self.client.get(f"/api/reviews/{e['id']}").json['reviews'][0]['decision'],'disputed')
        body['decision']='approved';body['note']='Verified interpretation against the complete conversation.'
        self.assertEqual(self.client.post('/api/reviews',json=body,headers=h).status_code,201)
        self.assertEqual(self.client.get('/api/dashboard').json['pilot']['validated_count'],1)
        self.assertEqual(len(self.client.get(f"/api/reviews/{e['id']}").json['reviews']),2)
        self.client.post('/api/logout',json={},headers=h)
        self.login('viewer@example.com','ViewTestPassword123!')
        self.assertNotIn('human_review',self.client.get('/api/dashboard').json['pilot']['evaluations'][0])

    def test_readiness_distinguishes_intentional_pause_from_failure(self):
        self.assertIs(self.client.get('/readyz').json['processing_enabled'],False)
        with patch.dict(os.environ,{'QA_PROCESSING_ENABLED':'true'}):
            r=self.client.get('/readyz');self.assertEqual(r.status_code,503)
            self.assertIs(r.json['processing_enabled'],True)

    def test_webhook_rejects_invalid_data_shape(self):
        with patch.dict(os.environ,{'QA_PROCESSING_ENABLED':'true','AIRCALL_WEBHOOK_TOKEN':'test-hook'}):
            r=self.client.post('/webhooks/aircall',json={'event':'call.ended','token':'test-hook','data':['invalid']})
            self.assertEqual(r.status_code,400)
