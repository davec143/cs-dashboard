import http.client
import json
import os
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from test_quality import Base
from qa.demo import seed
from qa.server import make_handler

class HTTPTests(Base):
    def setUp(self):
        super().setUp()
        seed(self.store)
        self.env = patch.dict(os.environ, {'QA_ADMIN_TOKEN':'admin-test-token',
            'QA_VIEW_TOKEN':'viewer-test-token','AIRCALL_WEBHOOK_TOKEN':'webhook-test-token'})
        self.env.start()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.store))
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.env.stop()
        super().tearDown()
    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
        data = json.dumps(body) if body is not None else None
        conn.request(method, path, data, headers or {})
        response = conn.getresponse()
        result = (response.status, dict(response.getheaders()), json.loads(response.read()))
        conn.close()
        return result
    def login(self, token):
        status, headers, _ = self.request('POST', '/api/login', {'token':token})
        self.assertEqual(status, 200)
        cookie = {'Cookie':headers['Set-Cookie'].split(';')[0]}
        csrf = self.request('GET', '/api/session', headers=cookie)[2]['csrf']
        return {**cookie, 'X-QA-CSRF':csrf}
    def test_readiness_does_not_claim_an_unstarted_worker_is_ready(self):
        self.assertEqual(self.request('GET','/healthz')[0],200)
        self.assertEqual(self.request('GET','/readyz')[0],503)
    def test_anonymous_cannot_read_dashboard(self):
        self.assertEqual(self.request('GET','/api/dashboard')[0],401)
    def test_viewer_has_no_transcripts_or_coaching_write_access(self):
        headers = self.login('viewer-test-token')
        status, _, data = self.request('GET','/api/dashboard',headers=headers)
        self.assertEqual(status,200)
        self.assertNotIn('turns',data['pilot']['evaluations'][0])
        self.assertNotIn('result',data['pilot']['evaluations'][0])
        self.assertEqual(data['pilot']['tasks'],[])
        self.assertEqual(self.request('POST','/api/coaching',{},headers)[0],403)
    def test_lead_requires_csrf_and_can_save_persistent_task(self):
        headers = self.login('admin-test-token')
        e = self.store.snapshot()['evaluations'][0]
        payload = {'evaluation_id':e['id'],'owner':'Test Lead','due':'2026-10-12','action':'Practice discovery'}
        no_csrf = {'Cookie':headers['Cookie']}
        self.assertEqual(self.request('POST','/api/coaching',payload,no_csrf)[0],403)
        self.assertEqual(self.request('POST','/api/coaching',payload,headers)[0],201)
        self.assertEqual(self.store.snapshot()['tasks'][0]['owner'],'Test Lead')
    def test_webhook_token_and_duplicate_delivery(self):
        body = {'token':'wrong','event':'transcription.created','timestamp':123,'data':{'call_id':1234}}
        self.assertEqual(self.request('POST','/webhooks/aircall',body)[0],401)
        body['token']='webhook-test-token'
        self.assertFalse(self.request('POST','/webhooks/aircall',body)[2]['duplicate'])
        self.assertTrue(self.request('POST','/webhooks/aircall',body)[2]['duplicate'])
        self.assertEqual(len(self.store.snapshot()['calls']),4)
    def test_unknown_event_ignored_and_bad_call_id_rejected(self):
        body = {'token':'webhook-test-token','event':'unknown','data':{}}
        self.assertTrue(self.request('POST','/webhooks/aircall',body)[2]['ignored'])
        body['event']='transcription.created'
        self.assertEqual(self.request('POST','/webhooks/aircall',body)[0],400)
