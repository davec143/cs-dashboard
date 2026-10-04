"""Current-result selection and human review must agree with management metrics."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from qa.db import Store
from qa.demo import seed


class ReviewTests(unittest.TestCase):
    def make_store(self):
        return Store(Path(self.tmp.name) / 'reviews.db')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = self.make_store()
        seed(self.store)
        snapshot = self.store.snapshot()
        self.original = next(e for e in snapshot['evaluations'] if e['eligible'])
        self.meta = next(c['metadata'] for c in snapshot['calls'] if c['id'] == self.original['call_id'])
        self.sequence = 0

    def tearDown(self):
        self.tmp.cleanup()

    def replay(self, meta=None):
        self.sequence += 1
        self.store.enqueue(meta or self.meta, 'replay:' + str(self.sequence), 'manual.replay')
        return self.store.claim()

    def record(self, **changes):
        return {**copy.deepcopy(self.original), 'fingerprint': 'new-result', **changes}

    def create_task(self, evaluation_id=None):
        return self.store.create_task(evaluation_id or self.original['id'], 'CS lead', '2026-10-20',
                                      'Practice a verified behavior', 'reviewer@example.com')

    def test_disputed_and_excluded_results_do_not_affect_management_or_new_coaching(self):
        for decision in ('disputed', 'excluded'):
            self.store.review_evaluation(self.original['id'], decision, 'Evidence needs a closer review', 'reviewer@example.com')
            snapshot = self.store.snapshot()
            self.assertEqual(snapshot['validated_count'], 0)
            self.assertIsNone(snapshot['summary']['mean'])
            self.assertEqual(snapshot['summary']['agents'], {})
            self.assertFalse(snapshot['evaluations'][0]['eligible'])
            with self.assertRaisesRegex(ValueError, 'validated evaluation required'):
                self.create_task()

    def test_review_history_is_immutable_audited_and_latest_decision_controls_eligibility(self):
        eid = self.original['id']
        first = self.store.review_evaluation(eid, 'disputed', '  Quotation requires confirmation  ', 'first@example.com')
        second = self.store.review_evaluation(eid, 'approved', 'Confirmed against the recording', 'second@example.com')
        history = self.store.review_history(eid)
        self.assertEqual([row['id'] for row in history], [second, first])
        self.assertEqual(history[1]['note'], 'Quotation requires confirmation')
        self.assertEqual([row['actor'] for row in history], ['second@example.com', 'first@example.com'])
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['validated_count'], 1)
        self.assertEqual(snapshot['summary']['human_reviewed_recent'], 1)
        self.assertEqual(snapshot['evaluations'][0]['result'], self.original['result'])
        with self.store.connect() as conn:
            audit = conn.execute("SELECT * FROM audit_log WHERE action='evaluation.reviewed' ORDER BY id").fetchall()
        self.assertEqual(len(audit), 2)
        self.assertEqual(json.loads(audit[1]['detail']), {'decision': 'approved', 'review_id': second})

    def test_approval_cannot_override_failed_evidence_gate(self):
        job = self.replay()
        self.store.finish(job, 'review', evaluation=self.record(status='review'))
        evaluation = self.store.snapshot()['evaluations'][0]
        with self.assertRaisesRegex(ValueError, '^evidence_checks_required_before_approval$'):
            self.store.review_evaluation(evaluation['id'], 'approved', 'Trying to override attribution', 'reviewer@example.com')
        self.store.review_evaluation(evaluation['id'], 'excluded', 'Insufficient attribution evidence', 'reviewer@example.com')
        self.assertEqual(self.store.snapshot()['validated_count'], 0)

    def test_review_requires_current_result_and_substantive_note(self):
        for decision, note in [('unknown', 'Good enough note'), ('approved', '   '), ('approved', 'x' * 2001)]:
            with self.assertRaisesRegex(ValueError, '^review_decision_and_note_required$'):
                self.store.review_evaluation(self.original['id'], decision, note, 'reviewer@example.com')
        self.replay()
        with self.assertRaisesRegex(ValueError, '^current_evaluation_required$'):
            self.store.review_evaluation(self.original['id'], 'approved', 'Old result is now stale', 'reviewer@example.com')

    def test_corrected_agent_replaces_current_score_without_duplicate_attribution(self):
        old_task = self.create_task()
        meta = {**self.meta, 'agent_id': 'corrected-agent', 'agent': 'Corrected Agent'}
        job = self.replay(meta)
        self.store.finish(job, 'validated', evaluation=self.record(agent_id='corrected-agent'))
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['validated_count'], 1)
        self.assertEqual(snapshot['summary']['agents'], {'Corrected Agent': {'count': 1, 'mean': 75.0}})
        self.assertEqual(len(snapshot['evaluations']), 1)
        self.assertEqual(snapshot['evaluations'][0]['agent_id'], 'corrected-agent')
        self.assertFalse(next(t['eligible'] for t in snapshot['tasks'] if t['id'] == old_task))
        with self.assertRaises(ValueError):
            self.create_task()
        with self.assertRaisesRegex(ValueError, '^current_evaluation_required$'):
            self.store.review_evaluation(self.original['id'], 'approved', 'Cannot approve wrong agent', 'reviewer@example.com')

    def test_cached_older_version_is_selected_exactly_instead_of_newest_id(self):
        job = self.replay()
        self.store.finish(job, 'validated', evaluation=self.record(total=25))
        self.assertEqual(self.store.snapshot()['summary']['mean'], 25.0)
        job = self.replay()
        self.store.finish(job, 'validated', evaluation_id=self.original['id'])
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['validated_count'], 1)
        self.assertEqual(snapshot['summary']['mean'], 75.0)
        self.assertEqual(snapshot['evaluations'][0]['id'], self.original['id'])
        self.create_task()

    def test_cached_return_to_original_agent_uses_original_result(self):
        meta = {**self.meta, 'agent_id': 'corrected-agent', 'agent': 'Corrected Agent'}
        self.store.finish(self.replay(meta), 'validated', evaluation=self.record(agent_id='corrected-agent', total=25))
        self.store.finish(self.replay(), 'validated', evaluation_id=self.original['id'])
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['summary']['agents'], {'Sample Agent': {'count': 1, 'mean': 75.0}})
        self.assertEqual(snapshot['evaluations'][0]['id'], self.original['id'])

    def test_cached_disputed_result_does_not_lose_human_decision(self):
        self.store.review_evaluation(self.original['id'], 'disputed', 'This result requires revision', 'reviewer@example.com')
        self.store.finish(self.replay(), 'validated', evaluation_id=self.original['id'])
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['validated_count'], 0)
        self.assertEqual(snapshot['evaluations'][0]['human_review']['decision'], 'disputed')

    def test_stale_generation_cannot_publish_score_or_reenable_coaching(self):
        job = self.replay()
        self.store.enqueue(self.meta, 'new-transcript', 'transcription.created')
        self.store.finish(job, 'validated', evaluation=self.record())
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['validated_count'], 0)
        self.assertFalse(snapshot['evaluations'][0]['current'])
        self.assertFalse(snapshot['evaluations'][0]['eligible'])
        self.assertEqual(next(c['state'] for c in snapshot['calls'] if c['id'] == self.meta['id']), 'queued')

    def test_review_without_new_result_keeps_prior_score_ineligible_after_restart(self):
        self.store.finish(self.replay(), 'review', error='speaker_mapping_required')
        snapshot = Store(self.store.path).snapshot()
        self.assertEqual(snapshot['validated_count'], 0)
        self.assertFalse(snapshot['evaluations'][0]['current'])
        with self.assertRaises(ValueError):
            self.create_task()

    def test_duplicate_ended_event_cannot_relabel_a_validated_score(self):
        self.store.enqueue({**self.meta, 'agent_id': 'different', 'agent': 'Wrong agent'}, 'later-ended')
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['summary']['agents'], {'Sample Agent': {'count': 1, 'mean': 75.0}})
        self.assertIsNone(self.store.claim())

    def test_cached_pointer_must_belong_to_job_agent_and_status(self):
        job = self.replay({**self.meta, 'agent_id': 'different'})
        with self.assertRaisesRegex(ValueError, '^current_evaluation_mismatch$'):
            self.store.finish(job, 'validated', evaluation_id=self.original['id'])
        job['metadata'] = self.meta
        with self.assertRaisesRegex(ValueError, '^current_evaluation_mismatch$'):
            self.store.finish(job, 'review', evaluation_id=self.original['id'])
        self.assertEqual(self.store.snapshot()['validated_count'], 0)

    def test_existing_coaching_is_held_on_dispute_and_can_only_be_dismissed(self):
        task = self.create_task()
        self.store.review_evaluation(self.original['id'], 'disputed', 'Incorrect behavioral attribution', 'reviewer@example.com')
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['summary']['task_states'], {})
        self.assertEqual(snapshot['summary']['held_tasks'], 1)
        self.assertFalse(snapshot['tasks'][0]['eligible'])
        for status in ('open', 'practicing', 'verified'):
            with self.assertRaisesRegex(ValueError, '^current_validated_evaluation_required$'):
                self.store.task_status(task, status, 'reviewer@example.com')
        self.store.task_status(task, 'dismissed', 'reviewer@example.com')
        self.assertEqual(self.store.snapshot()['summary']['task_states'], {'dismissed': 1})
        self.assertEqual(self.store.snapshot()['summary']['held_tasks'], 0)

    def test_approval_restores_existing_coaching_eligibility(self):
        task = self.create_task()
        self.store.review_evaluation(self.original['id'], 'disputed', 'Please inspect the supporting evidence', 'reviewer@example.com')
        self.store.review_evaluation(self.original['id'], 'approved', 'Supporting evidence has been confirmed', 'reviewer@example.com')
        self.store.task_status(task, 'verified', 'reviewer@example.com')
        self.assertEqual(self.store.snapshot()['summary']['task_states'], {'verified': 1})


class CurrentEvaluationMigrationTests(unittest.TestCase):
    def test_legacy_schema_backfill_preserves_history_and_runs_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            store = Store(path)
            seed(store)
            original = store.snapshot()['evaluations'][0]
            store.review_evaluation(original['id'], 'approved', 'Verified before database migration', 'reviewer@example.com')
            task = store.create_task(original['id'], 'Lead', '2026-10-20', 'Practice ownership', 'reviewer@example.com')
            with store.connect() as conn:
                conn.execute('ALTER TABLE calls DROP COLUMN current_evaluation_id')
            migrated = Store(path)
            snapshot = migrated.snapshot()
            self.assertEqual(snapshot['validated_count'], 1)
            self.assertEqual(snapshot['evaluations'][0]['id'], original['id'])
            self.assertEqual(snapshot['evaluations'][0]['human_review']['decision'], 'approved')
            self.assertEqual(snapshot['tasks'][0]['id'], task)
            with migrated.connect() as conn:
                conn.execute('UPDATE calls SET current_evaluation_id=NULL')
            self.assertEqual(Store(path).snapshot()['validated_count'], 0)

    def test_legacy_migration_does_not_guess_across_agent_or_status_mismatch(self):
        for metadata, state in [({'agent_id': 'someone-else'}, 'validated'), ({'agent_id': 'demo-agent'}, 'review')]:
            with self.subTest(metadata=metadata, state=state), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'legacy.db'
                store = Store(path)
                seed(store)
                with store.connect() as conn:
                    conn.execute('ALTER TABLE calls DROP COLUMN current_evaluation_id')
                    conn.execute('UPDATE calls SET metadata=?,state=?', (json.dumps(metadata), state))
                self.assertEqual(Store(path).snapshot()['validated_count'], 0)


if __name__ == '__main__':
    unittest.main()
