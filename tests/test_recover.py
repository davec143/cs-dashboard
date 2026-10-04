import tempfile
import unittest
from pathlib import Path
from qa.db import Store
from qa.providers import ProviderError
from qa.recover import apply_recovery, existing_ids, plan_recovery, timestamp

CONFIG = {'agents': {'7': {'enabled': True}}}
def call(cid, agent=7):
    return {'id': cid, 'user': {'id': agent}, 'started_at': 100,
            'ended_at': 150, 'status': 'done'}

class Client:
    def __init__(self, pages): self.pages = pages
    def calls_page(self, start, end, page):
        result = self.pages[page - 1]
        if isinstance(result, Exception): raise result
        return result

def page(calls, more=False):
    return {'calls': calls, 'meta': {'next_page_link': 'next' if more else None}}

class RecoveryTests(unittest.TestCase):
    def test_pages_deduplicate_filter_and_preserve_existing(self):
        p = plan_recovery(Client([page([call(1), call(2)], True), page([call(2), call(3, 8)])]),
                          CONFIG, {'1'}, 0, 200, pause=lambda _: None)
        self.assertEqual([c['id'] for c in p['missing']], ['2'])
        self.assertEqual((p['existing'], p['out_of_scope'], p['seen']), (1, 1, 3))

    def test_dry_run_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'absent.db'
            plan_recovery(Client([page([call(1)])]), CONFIG, existing_ids(path), 0, 200)
            self.assertFalse(path.exists())

    def test_failure_on_later_page_cannot_return_partial_plan(self):
        with self.assertRaises(ProviderError):
            plan_recovery(Client([page([call(1)], True), ProviderError('http_429')]),
                          CONFIG, set(), 0, 200, pause=lambda _: None)

    def test_enqueue_is_idempotent_and_respects_arriving_webhook(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'qa.db')
            p = plan_recovery(Client([page([call(1), call(2)])]), CONFIG, set(), 0, 200)
            store.enqueue({'id': '1', 'agent': 'newer metadata'}, 'webhook')
            store.finish(store.claim(), 'review')
            self.assertEqual(apply_recovery(store, p), 1)
            self.assertEqual(apply_recovery(store, p), 0)
            first = next(c for c in store.snapshot()['calls'] if c['id'] == '1')
            self.assertEqual(first['state'], 'review')
            self.assertEqual(first['metadata']['agent'], 'newer metadata')

    def test_missing_pagination_or_out_of_window_data_rejected(self):
        for result in ({'calls': [call(1)], 'meta': {}}, page([{**call(1), 'started_at': 300}])):
            with self.assertRaises(ProviderError):
                plan_recovery(Client([result]), CONFIG, set(), 0, 200)

    def test_explicit_timezone_and_bounded_range_required(self):
        with self.assertRaises(ValueError): timestamp('2026-09-30T00:00:00')
        self.assertEqual(timestamp('1970-01-01T00:00:00Z'), 0)
        with self.assertRaises(ValueError):
            plan_recovery(Client([]), CONFIG, set(), 0, 32 * 86400)
