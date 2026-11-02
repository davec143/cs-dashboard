import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from qa import sheet
from qa.auth import create_user
from qa.db import Store
from qa.web import create_app

TOKEN = 't' * 40


def row(cid, agent, day, total, kind='Evaluated', dims=None):
    dims = dims if dims is not None else [total] * 6
    return [cid, agent, day, '+15555550100', total, *dims, 'Yes' if kind == 'Evaluated' else 'No', kind,
            'Warm opening | Clear recap', 'Ask one more question', 'Customer asked about strip lights', 'Next call: confirm']


def payload(rows):
    return {'range': 'Data!A1:Q', 'majorDimension': 'ROWS', 'values': [list(sheet.HEADER)] + rows}


class SheetParseTests(unittest.TestCase):
    def test_normalizes_ids_dates_scores_and_skips_blank_rows(self):
        rows = sheet.parse(payload([
            [4296610562.0, 'Ed', '10/31/2026', '+1', '63', '70%', '', 60, 48, 58, 68, 'Yes', 'Evaluated'],
            ['', '', '', ''],
            ['3381486114.0', 'Mel', 46014, '+1', None, None, None, None, None, None, None, 'No', 'Voicemail'],
        ]))
        self.assertEqual([r['call_id'] for r in rows], ['4296610562', '3381486114'])
        self.assertEqual(rows[0]['call_date'], '2026-10-31')
        self.assertEqual(rows[1]['call_date'], '2025-12-23')
        self.assertEqual((rows[0]['total'], rows[0]['rapport'], rows[0]['needs_discovery']), (63.0, 70.0, None))
        self.assertEqual(rows[0]['row_number'], 2)
        self.assertEqual(rows[1]['row_number'], 4)
        self.assertEqual(rows[0]['strengths'], '')

    def test_rejects_wrong_header_or_shape(self):
        for bad in (None, {}, {'values': []}, {'values': [['Call ID', 'Agent']]}, {'values': 'x'}):
            with self.assertRaises(sheet.SheetError):
                sheet.parse(bad)


class SheetStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'test.db')

    def test_summary_matches_sheet_dashboard_math(self):
        rows = sheet.parse(payload([
            row('1', 'Ann', '2026-05-09', 50), row('2', 'Ann', '2026-05-09', 55),  # 52.5 -> 53
            row('3', 'Bob', '2026-05-10', 71), row('3', 'Bob', '2026-05-10', 74),  # duplicate rows count
            row('4', 'Bob', '2026-05-10', None, dims=[None] * 6),  # evaluated, no score
            row('5', 'Cy', '2026-06-01', None, 'Voicemail', [None] * 6),
        ]))
        sheet.replace(self.store, rows)
        s = sheet.summary(self.store)
        self.assertEqual((s['total_calls'], s['unique_calls'], s['evaluated'], s['voicemails']), (6, 5, 5, 1))
        self.assertEqual(s['dimensions'][0]['mean'], 63)  # (50+55+71+74)/4 = 62.5 -> 63
        self.assertEqual({d['date']: d['mean'] for d in s['daily']}, {'2026-05-09': 53, '2026-05-10': 73})
        card = {a['agent']: a for a in s['scorecard']}
        self.assertEqual((card['Bob']['calls'], card['Bob']['total']), (3, 73))
        self.assertEqual([a['agent'] for a in s['scorecard']], ['Bob', 'Ann'])
        self.assertEqual(s['health']['duplicate_call_ids'], ['3'])
        self.assertEqual(s['health']['evaluated_missing_scores'], ['4'])
        self.assertEqual(s['monthly'], {'2026-05': 5, '2026-06': 1})

    def test_each_sync_replaces_everything_and_refuses_a_collapse(self):
        sheet.replace(self.store, sheet.parse(payload([row(str(i), 'A', '2026-05-01', 70) for i in range(10)])))
        sheet.replace(self.store, sheet.parse(payload([row(str(i), 'A', '2026-05-01', 80) for i in range(9)])))
        s = sheet.summary(self.store)
        self.assertEqual((s['total_calls'], s['dimensions'][0]['mean']), (9, 80))
        with self.assertRaisesRegex(sheet.SheetError, 'shrank'):
            sheet.replace(self.store, sheet.parse(payload([row('1', 'A', '2026-05-01', 1)])))
        self.assertEqual(sheet.summary(self.store)['total_calls'], 9)
        sheet.replace(self.store, sheet.parse(payload([row('1', 'A', '2026-05-01', 1)])), force=True)
        self.assertEqual(sheet.summary(self.store)['total_calls'], 1)


class SheetEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'test.db')
        env = patch.dict(os.environ, {'QA_PROCESSING_ENABLED': 'false', 'QA_AUDIT_DIR': self.tmp.name,
                                      'SHEET_INGEST_TOKEN': TOKEN})
        env.start()
        self.addCleanup(env.stop)
        create_user(self.store, 'lead@example.com', generate_password_hash('LeadTestPassword123!'))
        create_user(self.store, 'viewer@example.com', generate_password_hash('ViewTestPassword123!'), 'viewer')
        self.client = create_app(self.store, testing=True).test_client()

    def post(self, body, token=TOKEN):
        return self.client.post('/ingest/sheet', json=body, headers={'Authorization': 'Bearer ' + token})

    def test_ingest_requires_token_and_valid_sheet(self):
        self.assertEqual(self.post(payload([]), token='wrong' * 10).status_code, 401)
        self.assertEqual(self.client.post('/ingest/sheet', json=payload([])).status_code, 401)
        r = self.post({'values': [['nope']]})
        self.assertEqual((r.status_code, r.json['error']), (422, 'sheet_header_mismatch'))
        self.assertEqual(self.store.setting('sheet_sync_error'), 'sheet_header_mismatch')

    def test_large_snapshot_accepted_and_reaches_dashboard(self):
        rows = [row(str(4000000000 + i), 'Agent %d' % (i % 6), '2026-10-%02d' % (i % 28 + 1), 60 + i % 30)
                for i in range(4000)]
        for r in rows:
            r[13] = 'x' * 900  # Bigger than the 1 MB default body limit in total.
        r = self.post(payload(rows))
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json['rows'], 4000)
        self.assertEqual(self.store.setting('sheet_sync_error'), '')
        self.client.post('/api/login', json={'email': 'viewer@example.com', 'password': 'ViewTestPassword123!'})
        data = self.client.get('/api/dashboard').json['sheet']
        self.assertEqual(data['total_calls'], 4000)
        self.assertNotIn('customer_number', data['rows'][0])
        self.assertNotIn('strengths', data['rows'][0])
        detail = self.client.get('/api/sheet/rows/2').json
        self.assertNotIn('customer_number', detail)
        self.assertEqual(len(detail['strengths']), 900)

    def test_freshness_feeds_readiness_and_alerts(self):
        self.assertFalse(self.client.get('/readyz').json['sheet_ok'])
        self.post(payload([row('1', 'A', '2026-05-01', 70)]))
        self.assertTrue(self.client.get('/readyz').json['sheet_ok'])
        self.store.setting('sheet_last_sync', time.time() - sheet.STALE_AFTER - 1)
        self.assertFalse(self.client.get('/readyz').json['operations_ok'])
        h = self.client.post('/api/login', json={'email': 'lead@example.com', 'password': 'LeadTestPassword123!'})
        self.assertEqual(h.status_code, 200)
        alerts = self.client.get('/api/system').json['alerts']
        self.assertTrue(any('Google Sheet feed' in a for a in alerts))


if __name__ == '__main__':
    unittest.main()
