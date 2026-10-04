"""Adversarial provider responses must not stop durable queue processing."""
import http.client
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from qa.core import InvalidEvaluation
from qa.db import Store
from qa.demo import seed
from qa.providers import Aircall, OpenAI, ProviderError, request_json, retry_delay
from qa.recover import plan_recovery
from qa.worker import configuration_status, load_config, main, minimal_call, process_one, reconcile


CONFIG = {'agents': {'7': {'enabled': True}}}


class ProviderBoundaryTests(unittest.TestCase):
    def test_non_object_json_is_a_controlled_provider_error(self):
        for payload in ('[]', 'null', '42', '"private transcript"'):
            with self.subTest(payload=payload), patch('urllib.request.urlopen', return_value=io.BytesIO(payload.encode())):
                with self.assertRaisesRegex(ProviderError, '^provider_invalid_shape$'):
                    request_json('https://example.test', {})

    def test_interrupted_response_can_be_retried(self):
        with patch('urllib.request.urlopen', side_effect=http.client.IncompleteRead(b'private body')):
            with self.assertRaises(ProviderError) as caught:
                request_json('https://example.test', {})
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(str(caught.exception), 'network_timeout')

    def test_retry_after_accepts_seconds_and_http_dates_and_caps_wait(self):
        self.assertEqual(retry_delay('120'), 120)
        self.assertEqual(retry_delay('0'), 1)
        self.assertEqual(retry_delay('999999'), 3600)
        self.assertEqual(retry_delay('Thu, 01 Jan 1970 00:02:00 GMT', now=0), 120)
        self.assertEqual(retry_delay('Thu, 01 Jan 1970 00:00:00 GMT', now=10), 1)
        self.assertIsNone(retry_delay('invalid'))

    def test_429_does_not_expose_response_body(self):
        error = urllib.error.HTTPError('https://example.test', 429, 'private provider message',
                                       {'Retry-After': '60'}, io.BytesIO(b'private transcript'))
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(ProviderError) as caught:
                request_json('https://example.test', {})
        self.assertEqual(str(caught.exception), 'http_429')
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.retry_after, 60)

    def test_aircall_requires_wrapped_call_object(self):
        with patch.dict('os.environ', {'AIRCALL_ACCESS_TOKEN': 'test-only'}):
            client = Aircall()
        for response in ({}, {'call': []}, []):
            with self.subTest(response=response), patch.object(client, 'get', return_value=response):
                with self.assertRaisesRegex(ProviderError, 'aircall_invalid_call'):
                    client.call('123')

    def test_malformed_model_shapes_route_to_review(self):
        responses = [[], {'status': 'completed'}, {'status': 'completed', 'output': [None]},
                     {'status': 'completed', 'output': [{'content': None}]},
                     {'status': 'completed', 'output': [{'content': [None]}]},
                     {'status': 'completed', 'output': [{'content': [{'type': 'output_text', 'text': None}]}]}]
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-only', 'EVALUATOR_MODEL': 'test'}):
            evaluator = OpenAI()
        for response in responses:
            with self.subTest(response=response), patch('qa.providers.request_json', return_value=response):
                with self.assertRaises(InvalidEvaluation):
                    evaluator.evaluate({}, [])


class PipelineHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'test.db')

    def test_worker_is_paused_without_explicit_enable(self):
        for value in (None, 'false', 'TRUE', '1'):
            env = {} if value is None else {'QA_PROCESSING_ENABLED': value}
            with self.subTest(value=value), patch.dict('os.environ', env, clear=True), \
                 patch('sys.argv', ['worker', '--once']), patch('qa.worker.Aircall') as aircall, \
                 patch('qa.worker.Store', return_value=self.store), patch('qa.worker.OpenAI') as evaluator:
                with self.assertRaisesRegex(SystemExit, 'paused'):
                    main()
                aircall.assert_not_called()
                evaluator.assert_not_called()
        self.assertIsNotNone(self.store.setting('worker_heartbeat'))
        status = json.loads(self.store.setting('worker_configuration'))
        self.assertEqual(status, {'aircall_configured': False, 'openai_configured': False,
                                 'model_configured': False, 'agents_configured': False,
                                 'config_valid': True})

    def test_configuration_status_contains_only_boolean_flags(self):
        with patch.dict('os.environ', {'AIRCALL_API_ID': 'private-id', 'AIRCALL_API_TOKEN': 'private-token',
                                      'OPENAI_API_KEY': 'private-key', 'EVALUATOR_MODEL': 'model-name'}, clear=True):
            status = configuration_status(CONFIG)
            self.assertTrue(all(value is True for value in status.values()))
            self.assertNotIn('private', json.dumps(status))
            self.assertFalse(configuration_status(None)['config_valid'])

    def test_bad_configuration_cannot_enable_unknown_agents(self):
        config_file = Path(self.tmp.name) / 'config.json'
        for config in ([], {'agents': []}, {'agents': {'7': {'enabled': 'false'}}},
                       {'agents': {'7': None}}, {'speaker_maps': {'123': {'speaker': []}}}):
            config_file.write_text(json.dumps(config))
            with self.subTest(config=config), patch.dict('os.environ', {'QA_CONFIG': str(config_file)}, clear=True):
                with self.assertRaises(ValueError):
                    load_config()

    def test_malformed_metadata_is_rejected_before_persistence(self):
        invalid = [[], {'id': True}, {'id': '１２３'}, {'id': 123, 'user': []},
                   {'id': 123, 'user': {'name': []}}, {'id': 123, 'started_at': 'tomorrow'},
                   {'id': 123, 'started_at': float('nan')}, {'id': 123, 'ended_at': True},
                   {'id': 123, 'duration': -1}, {'id': 123, 'started_at': 253402300800},
                   {'id': 123, 'started_at': 10 ** 500}]
        for call in invalid:
            with self.subTest(call=call), self.assertRaises(ValueError):
                minimal_call(call)

    def test_mismatched_call_cannot_be_attributed_or_evaluated(self):
        self.store.enqueue({'id': '123'}, 'event')
        aircall = Mock()
        aircall.call.return_value = {'id': 456, 'user': {'id': 7}, 'answered_at': 1}
        evaluator = Mock()
        self.assertTrue(process_one(self.store, aircall, evaluator, CONFIG))
        call = self.store.snapshot()['calls'][0]
        self.assertEqual(call['last_error'], 'provider_call_id_mismatch')
        self.assertEqual(call['state'], 'error')
        self.assertEqual(call['metadata']['id'], '123')
        aircall.transcript.assert_not_called()
        evaluator.evaluate.assert_not_called()

    def test_identical_transcript_replay_restores_cached_evaluation_without_model_call(self):
        fixtures = Store(Path(self.tmp.name) / 'fixtures.db')
        seed(fixtures)
        fixture = fixtures.snapshot()['evaluations'][0]
        result = fixture['result']
        result.pop('gate')
        transcript = {'content': {'utterances': [
            {'participant_type': 'internal' if turn['role'] == 'agent' else 'external',
             'user_id': 7 if turn['role'] == 'agent' else None, 'text': turn['text']}
            for turn in fixture['turns']]}}
        aircall = Mock()
        aircall.call.return_value = {'id': 123, 'user': {'id': 7}, 'answered_at': 1}
        aircall.transcript.return_value = transcript
        evaluator = Mock(model='test')
        evaluator.evaluate.return_value = result
        self.store.enqueue({'id': '123'}, 'original')
        process_one(self.store, aircall, evaluator, CONFIG)
        original = self.store.snapshot()['evaluations'][0]
        self.assertTrue(original['eligible'])
        self.store.enqueue({'id': '123'}, 'new-asset', 'transcription.created')
        self.assertFalse(self.store.snapshot()['evaluations'][0]['eligible'])
        evaluator.evaluate.reset_mock()
        process_one(self.store, aircall, evaluator, CONFIG)
        replay = self.store.snapshot()
        evaluator.evaluate.assert_not_called()
        self.assertEqual(len(replay['evaluations']), 1)
        self.assertEqual(replay['evaluations'][0]['id'], original['id'])
        self.assertTrue(replay['evaluations'][0]['eligible'])
        self.assertEqual(replay['validated_count'], 1)

    def test_malformed_reconciliation_keeps_last_successful_cursor(self):
        responses = [[], {'calls': [None], 'meta': {'next_page_link': None}},
                     {'calls': [{'id': 1, 'status': 'done', 'ended_at': 20, 'user': []}],
                      'meta': {'next_page_link': None}}]
        self.store.setting('reconcile_cursor', 500)
        for response in responses:
            with self.subTest(response=response), self.assertRaises(ProviderError):
                reconcile(self.store, Mock(calls_page=Mock(return_value=response)), end=1000)
            self.assertEqual(self.store.setting('reconcile_cursor'), '500')
            self.assertIsNone(self.store.setting('last_reconcile'))

    def test_invalid_reconciliation_cursor_does_not_request_arbitrary_window(self):
        for value in ('nan', 'garbage', '-1', '1001'):
            self.store.setting('reconcile_cursor', value)
            aircall = Mock()
            with self.subTest(value=value), self.assertRaisesRegex(ProviderError, 'invalid_cursor'):
                reconcile(self.store, aircall, end=1000)
            aircall.calls_page.assert_not_called()

    def test_reconciliation_updates_heartbeat_between_pages(self):
        aircall = Mock()
        aircall.calls_page.side_effect = [
            {'calls': [], 'meta': {'next_page_link': 'next'}},
            {'calls': [], 'meta': {'next_page_link': None}}]
        with patch('qa.worker.time.time', side_effect=[100, 200]), patch('qa.worker.time.sleep'):
            self.assertEqual(reconcile(self.store, aircall, end=1000), 0)
        self.assertEqual(self.store.setting('worker_heartbeat'), '200')

    def test_reconcile_only_reports_failed_run_as_failure(self):
        with patch.dict('os.environ', {'QA_PROCESSING_ENABLED': 'true'}), \
             patch('sys.argv', ['worker', '--reconcile-only']), \
             patch('qa.worker.load_config', return_value=CONFIG), \
             patch('qa.worker.Store', return_value=self.store), patch('qa.worker.Aircall'), \
             patch('qa.worker.reconcile', side_effect=ProviderError('http_429', True)):
            with self.assertRaisesRegex(SystemExit, 'Reconciliation stopped: http_429'):
                main()
        self.assertEqual(self.store.setting('reconcile_error'), 'http_429')

    def test_malformed_recovery_never_returns_partial_plan(self):
        for response in ([], {'calls': [None], 'meta': {'next_page_link': None}}):
            with self.subTest(response=response), self.assertRaises(ProviderError):
                plan_recovery(Mock(calls_page=Mock(return_value=response)), CONFIG, set(), 0, 100)

    def test_recovery_rejects_nonfinite_or_boolean_time_windows(self):
        for start, end in ((0, float('inf')), (0, float('nan')), (False, 100), (0, 10 ** 500)):
            aircall = Mock()
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                plan_recovery(aircall, CONFIG, set(), start, end)
            aircall.calls_page.assert_not_called()


if __name__ == '__main__':
    unittest.main()
