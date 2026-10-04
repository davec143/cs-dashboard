import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from qa.core import DIMENSIONS, EVALUATION_SCHEMA, InvalidEvaluation, canonicalize, early_disposition, evaluate_gate, redact
from qa.db import Store
from qa.demo import seed
from qa.providers import OpenAI, ProviderError
from qa.worker import process_one, reconcile

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'test.db')
    def tearDown(self):
        self.tmp.cleanup()

class EvaluationTests(Base):
    def fixture(self):
        seed(self.store)
        e = self.store.snapshot()['evaluations'][0]
        r = copy.deepcopy(e['result'])
        r.pop('gate')
        return r, e['turns']
    def test_total_is_calculated_only_from_applicable_dimensions(self):
        r, turns = self.fixture()
        self.assertEqual(evaluate_gate(r, turns, 'demo-agent')['total'], 75)
    def test_invented_quote_rejected(self):
        r, turns = self.fixture()
        r['dimensions']['rapport']['evidence'][0]['quote'] = 'Wonderful subscription upgrade!'
        with self.assertRaisesRegex(InvalidEvaluation, 'evidence_quote_not_in_source'):
            evaluate_gate(r, turns, 'demo-agent')
    def test_ai_quote_cannot_support_human_score(self):
        r, turns = self.fixture()
        turns[0]['role'] = 'ai'
        with self.assertRaisesRegex(InvalidEvaluation, 'requires_agent_evidence'):
            evaluate_gate(r, turns, 'demo-agent')
    def test_boolean_anchor_rejected(self):
        r, turns = self.fixture()
        r['dimensions']['rapport']['anchor'] = True
        with self.assertRaises(InvalidEvaluation):
            evaluate_gate(r, turns, 'demo-agent')
    def test_unobservable_dimension_routes_to_review(self):
        r, turns = self.fixture()
        r['dimensions']['rapport'].update(applicability='unobservable', anchor=None)
        self.assertEqual(evaluate_gate(r, turns, 'demo-agent')['status'], 'review')
    def test_unknown_speaker_is_not_scored_automatically(self):
        r, turns = self.fixture()
        turns[1]['role'] = 'unknown'
        self.assertEqual(evaluate_gate(r, turns, 'demo-agent')['status'], 'review')
    def test_additional_model_fields_are_rejected(self):
        r, turns = self.fixture()
        r['total'] = 100
        with self.assertRaises(InvalidEvaluation):
            evaluate_gate(r, turns, 'demo-agent')
    def test_official_aircall_roles_do_not_include_phone_numbers(self):
        tr = {'transcription': {'content': {'utterances': [
            {'participant_type':'internal','user_id':123,'text':'Hello'},
            {'participant_type':'external','phone_number':'+15555550123','text':'Hi'},
            {'participant_type':'ai_voice_agent','ai_voice_agent_id':'hannah','text':'Please hold'}]}}}
        turns = canonicalize(tr)
        self.assertEqual([t['role'] for t in turns], ['agent', 'customer', 'ai'])
        self.assertEqual(turns[0]['agent_id'], '123')
        self.assertNotIn('5555550123', json.dumps(turns))
    def test_empty_and_object_only_transcripts_rejected(self):
        for tr in ({'content': {'utterances': []}}, {'filedata':None}, 'plain text'):
            with self.assertRaises(InvalidEvaluation):
                canonicalize(tr)
    def test_short_call_not_automatically_excluded(self):
        self.assertIsNone(early_disposition({'answered_at': 1, 'duration': 7}))
    def test_contacts_redacted(self):
        self.assertEqual(redact('Email john@example.com or +1 (555) 555-0123.'), 'Email [EMAIL] or [NUMBER].')
    def test_excluded_calls_cannot_carry_scores(self):
        r, turns = self.fixture()
        r['call_type'] = 'voicemail'
        with self.assertRaisesRegex(InvalidEvaluation, 'excluded_calls'):
            evaluate_gate(r, turns, 'demo-agent')

class QueueTests(Base):
    def test_parallel_duplicate_webhooks_create_one_call_and_job(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(lambda _: self.store.enqueue({'id':'123'}, 'same'), range(24)))
        self.assertEqual(sum(result), 1)
        self.assertEqual(len(self.store.snapshot()['calls']), 1)
    def test_parallel_workers_claim_only_once(self):
        self.store.enqueue({'id':'123'}, 'a')
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: self.store.claim(), range(8)))
        self.assertEqual(sum(c is not None for c in claims), 1)
    def test_crashed_worker_can_be_reclaimed_and_stale_worker_cannot_finish(self):
        self.store.enqueue({'id':'123'}, 'a', now=0)
        old = self.store.claim(now=0, lease_seconds=10)
        new = self.store.claim(now=11)
        self.assertFalse(self.store.finish(old, 'validated'))
        self.assertTrue(self.store.finish(new, 'review'))
    def test_new_asset_arriving_during_work_is_not_lost(self):
        self.store.enqueue({'id':'123'}, 'a')
        job = self.store.claim()
        self.store.enqueue({'id':'123'}, 'new-transcript', 'transcription.created')
        self.store.finish(job, 'validated')
        self.assertEqual(self.store.snapshot()['calls'][0]['state'], 'queued')
        self.assertIsNotNone(self.store.claim())
    def test_completed_call_is_not_reprocessed_by_another_ended_event(self):
        self.store.enqueue({'id':'123'}, 'a')
        self.store.finish(self.store.claim(), 'validated')
        self.store.enqueue({'id':'123'}, 'b')
        self.assertIsNone(self.store.claim())
    def test_repeated_worker_crashes_exhaust_into_visible_error(self):
        self.store.enqueue({'id':'123'}, 'a', now=0)
        for i in range(6):
            self.assertIsNotNone(self.store.claim(now=i*11, lease_seconds=10))
        self.assertIsNone(self.store.claim(now=70))
        self.assertEqual(self.store.snapshot()['calls'][0]['state'], 'error')
        self.assertEqual(self.store.snapshot()['calls'][0]['last_error'], 'retry_budget_exhausted')
    def test_stale_job_does_not_overwrite_new_asset_metadata(self):
        self.store.enqueue({'id':'123','agent':'old'}, 'a')
        job = self.store.claim()
        self.store.enqueue({'id':'123','agent':'new'}, 'b', 'transcription.created')
        self.store.finish(job, 'validated')
        self.assertEqual(self.store.snapshot()['calls'][0]['metadata']['agent'], 'new')
    def test_queued_reevaluation_cannot_create_coaching_from_old_score(self):
        seed(self.store)
        e = self.store.snapshot()['evaluations'][0]
        self.store.enqueue({'id':e['call_id']}, 'new', 'transcription.created')
        self.assertFalse(self.store.snapshot()['evaluations'][0]['eligible'])
        with self.assertRaises(ValueError):
            self.store.create_task(e['id'], 'Lead', '2026-10-12', 'Practice', 'lead')
    def test_coaching_persists_and_has_an_audit_record(self):
        seed(self.store)
        e = self.store.snapshot()['evaluations'][0]
        tid = self.store.create_task(e['id'], 'CS Lead', '2026-10-12', 'Practice discovery', 'lead')
        self.store.task_status(tid, 'practicing', 'lead')
        self.assertEqual(Store(self.store.path).snapshot()['tasks'][0]['status'], 'practicing')
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0], 2)
    def test_nonvalidated_evaluation_cannot_create_coaching(self):
        with self.assertRaises(ValueError):
            self.store.create_task(999, 'Lead', '2026-10-12', 'Practice', 'lead')

class WorkerTests(Base):
    def test_missing_speaker_identity_does_not_call_evaluator(self):
        class Air:
            def call(self, cid): return {'id':123,'user':{'id':1},'answered_at':2}
            def transcript(self, cid): return {'content':{'utterances':[{'speaker':'a','text':'Hello'}]}}
        class Eval:
            model='test'
            def evaluate(self, *args): raise AssertionError('must not be called')
        self.store.enqueue({'id':'123'}, 'a')
        process_one(self.store, Air(), Eval(), {'agents':{'1':{'enabled':True}}})
        self.assertEqual(self.store.snapshot()['calls'][0]['state'], 'review')
    def test_permanent_provider_error_is_not_retried(self):
        class Air:
            def call(self, cid): raise ProviderError('http_400')
        self.store.enqueue({'id':'123'}, 'a')
        process_one(self.store, Air(), None, {})
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.snapshot()['calls'][0]['state'], 'error')
    def test_transient_provider_error_gets_a_delayed_retry(self):
        class Air:
            def call(self, cid): raise ProviderError('http_429', True, 123)
        self.store.enqueue({'id':'123'}, 'a')
        process_one(self.store, Air(), None, {})
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.snapshot()['calls'][0]['state'], 'queued')
    def test_failed_reconciliation_does_not_advance_cursor(self):
        class Air:
            def calls_page(self, start, end, page): raise ProviderError('http_500', True)
        self.store.setting('reconcile_cursor', 500)
        with self.assertRaises(ProviderError):
            reconcile(self.store, Air(), 1000)
        self.assertEqual(self.store.setting('reconcile_cursor'), '500')
    def test_reconciliation_reaches_all_pages_and_is_idempotent(self):
        class Air:
            def calls_page(self, start, end, page):
                return {'calls':[{'id':page,'status':'done','ended_at':123}],
                        'meta':{'next_page_link':'next' if page==1 else None}}
        with patch('qa.worker.time.sleep'):
            self.assertEqual(reconcile(self.store, Air(), 1000), 2)
            self.assertEqual(reconcile(self.store, Air(), 1100), 0)
        self.assertEqual(len(self.store.snapshot()['calls']), 2)
    def test_openai_refusal_cannot_become_an_evaluation(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY':'fake','EVALUATOR_MODEL':'test'}):
            evaluator = OpenAI()
        with patch('qa.providers.request_json', return_value={'status':'completed','output':[{'content':[{'type':'refusal','refusal':'no'}]}]}):
            with self.assertRaisesRegex(InvalidEvaluation, 'refusal'):
                evaluator.evaluate({}, [])

if __name__ == '__main__':
    unittest.main()
