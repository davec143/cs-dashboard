import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from qa import ops


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / 'database.dump'
        self.environ = patch.dict(os.environ, {
            'DATABASE_URL': 'postgresql://backup:private_password@db.internal/railway',
            'QA_RESTORE_TEST_DATABASE_URL': 'postgresql://test:other_secret@localhost/qa_restore_drill',
            'PGHOST': 'wrong-inherited-host', 'PGSERVICE': 'unexpected-service'}, clear=False)
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def tool(self, command, **kwargs):
        if command[0] == 'pg_dump':
            kwargs['stdout'].write(b'PGDMP-valid-test-archive')
            return subprocess.CompletedProcess(command, 0, None, b'')
        toc = '\n'.join('1; 2 3 TABLE public ' + name + ' owner' for name in ops.CORE_TABLES)
        return subprocess.CompletedProcess(command, 0, toc.encode(), b'')

    def make_backup(self):
        with patch('qa.ops.subprocess.run', side_effect=self.tool):
            return ops.backup(self.archive)

    def test_backup_is_private_and_credentials_are_not_process_arguments(self):
        with patch('qa.ops.subprocess.run', side_effect=self.tool) as run:
            result = ops.backup(self.archive)
        self.assertTrue(result['archive_verified'])
        self.assertFalse(result['restore_verified'])
        for path in (self.archive, ops._manifest_path(self.archive)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        command, = run.call_args_list[0].args
        self.assertNotIn('private_password', ' '.join(command))
        self.assertEqual(run.call_args_list[0].kwargs['env']['PGHOST'], 'db.internal')
        self.assertNotIn('PGSERVICE', run.call_args_list[0].kwargs['env'])
        self.assertNotIn('DATABASE_URL', run.call_args_list[0].kwargs['env'])
        self.assertNotIn('private_password', json.dumps(result))

    def test_existing_artifact_or_manifest_is_never_overwritten(self):
        for path in (self.archive, ops._manifest_path(self.archive)):
            path.write_text('keep')
            with self.assertRaisesRegex(ops.OpsError, 'destination_exists'):
                self.make_backup()
            self.assertEqual(path.read_text(), 'keep')
            path.unlink()

    def test_failed_dump_removes_partial_files_and_redacts_tool_stderr(self):
        def fail(command, **kwargs):
            kwargs['stdout'].write(b'PGDMP-incomplete')
            return subprocess.CompletedProcess(command, 1, None, b'password private_password, customer data')
        with patch('qa.ops.subprocess.run', side_effect=fail), contextlib.redirect_stdout(io.StringIO()) as out:
            code = ops.main(['backup', '--destination', str(self.archive)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())['error'], 'postgres_client_failed')
        self.assertFalse(self.archive.exists())
        self.assertFalse(ops._manifest_path(self.archive).exists())
        self.assertNotIn('private_password', out.getvalue())

    def test_archive_tampering_fails_verification(self):
        self.make_backup()
        with self.archive.open('ab') as output:
            output.write(b'changed')
        with patch('qa.ops.subprocess.run', side_effect=self.tool):
            with self.assertRaisesRegex(ops.OpsError, 'checksum_or_manifest_mismatch'):
                ops.verify_archive(self.archive)

    def test_missing_application_tables_rejects_backup_and_removes_it(self):
        def incomplete(command, **kwargs):
            if command[0] == 'pg_dump':
                return self.tool(command, **kwargs)
            return subprocess.CompletedProcess(command, 0, b'1; 2 3 TABLE public calls owner', b'')
        with patch('qa.ops.subprocess.run', side_effect=incomplete):
            with self.assertRaisesRegex(ops.OpsError, 'missing_application_tables'):
                ops.backup(self.archive)
        self.assertFalse(self.archive.exists())
        self.assertFalse(ops._manifest_path(self.archive).exists())

    def test_timeout_does_not_echo_subprocess_diagnostics(self):
        error = subprocess.TimeoutExpired(['pg_dump'], 600, stderr=b'private_password')
        with patch('qa.ops.subprocess.run', side_effect=error), contextlib.redirect_stdout(io.StringIO()) as out:
            code = ops.main(['backup', '--destination', str(self.archive)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())['error'], 'postgres_client_timeout')
        self.assertNotIn('private_password', out.getvalue())
        self.assertFalse(self.archive.exists())

    def test_restore_requires_explicit_test_database_and_disallows_production(self):
        with patch('psycopg.connect') as connect:
            with self.assertRaisesRegex(ops.OpsError, 'production_target_forbidden'):
                ops.restore_check(self.archive, 'railway', 'DATABASE_URL')
            with self.assertRaisesRegex(ops.OpsError, 'confirmation_required'):
                ops.restore_check(self.archive, 'wrong_name')
            with patch.dict(os.environ, {'QA_RESTORE_TEST_DATABASE_URL': os.environ['DATABASE_URL']}):
                with self.assertRaisesRegex(ops.OpsError, 'confirmation_required'):
                    ops.restore_check(self.archive, 'railway')
            connect.assert_not_called()

    def test_even_test_named_production_database_is_forbidden(self):
        with patch.dict(os.environ, {'DATABASE_URL': os.environ['QA_RESTORE_TEST_DATABASE_URL']}):
            with self.assertRaisesRegex(ops.OpsError, 'production_target_forbidden'):
                ops.restore_check(self.archive, 'qa_restore_drill')

    def mock_connection(self, objects=0, routines=0):
        conn = MagicMock()
        def execute(statement, *args):
            statement = str(statement)
            result = MagicMock()
            if 'current_database()' in statement:
                result.fetchone.return_value = ('qa_restore_drill',)
            elif 'pg_class' in statement:
                result.fetchone.return_value = (objects,)
            elif 'pg_proc' in statement:
                result.fetchone.return_value = (routines,)
            elif 'FROM pg_tables' in statement:
                result.fetchall.return_value = [(name,) for name in ops.CORE_TABLES]
            else:
                result.fetchone.return_value = (0,)
            return result
        conn.execute.side_effect = execute
        return conn

    def test_restore_refuses_nonempty_target_before_running_restore(self):
        self.make_backup()
        conn = self.mock_connection(objects=1)
        with patch('psycopg.connect') as connect, patch('qa.ops.subprocess.run', side_effect=self.tool) as run:
            connect.return_value.__enter__.return_value = conn
            with self.assertRaisesRegex(ops.OpsError, 'target_not_empty'):
                ops.restore_check(self.archive, 'qa_restore_drill')
        self.assertFalse(any('--single-transaction' in c.args[0] for c in run.call_args_list))

    def test_restore_atomic_checks_all_tables_and_does_not_drop_target(self):
        self.make_backup()
        conn = self.mock_connection()
        with patch('psycopg.connect') as connect, patch('qa.ops.subprocess.run', side_effect=self.tool) as run:
            connect.return_value.__enter__.return_value = conn
            result = ops.restore_check(self.archive, 'qa_restore_drill')
        commands = [c.args[0] for c in run.call_args_list]
        restore_command = next(c for c in commands if '--single-transaction' in c)
        self.assertIn('--exit-on-error', restore_command)
        self.assertNotIn('--clean', restore_command)
        self.assertNotIn('other_secret', ' '.join(restore_command))
        self.assertTrue(result['target_retained'])
        self.assertTrue(result['restore_verified'])
        self.assertEqual(set(result['row_counts']), ops.CORE_TABLES)
        self.assertFalse(any('DROP ' in str(c) for c in conn.execute.call_args_list))

    def test_restore_connection_failure_is_sanitized(self):
        self.make_backup()
        with patch('qa.ops.subprocess.run', side_effect=self.tool), patch('psycopg.connect',
                side_effect=RuntimeError('postgresql://test:other_secret@localhost/customer_data')):
            with self.assertRaisesRegex(ops.OpsError, '^restore_verification_failed$'):
                ops.restore_check(self.archive, 'qa_restore_drill')

    def test_health_does_not_report_a_paused_worker_as_processing_ready(self):
        with patch('qa.ops._probe', side_effect=[(200, {'status': 'up'}), (503, {'ready': False, 'processing_enabled': False})]):
            result = ops.check('https://example.test')
        self.assertFalse(result['ok'])
        self.assertTrue(result['live'])
        self.assertFalse(result['processing_ready'])
        with patch('qa.ops._probe', side_effect=[(200, {'status': 'up'}), (503, {'ready': False, 'processing_enabled': False})]):
            result = ops.check('https://example.test', allow_paused=True)
        self.assertTrue(result['ok'])
        self.assertFalse(result['processing_ready'])

    def test_allow_paused_cannot_hide_failure_of_enabled_processing(self):
        for readiness in ({'ready': False, 'processing_enabled': True}, {'ready': False}):
            with patch('qa.ops._probe', side_effect=[(200, {'status': 'up'}), (503, readiness)]):
                self.assertFalse(ops.check('https://example.test', allow_paused=True)['ok'])

    def test_operations_failure_is_not_hidden_by_expected_pause_or_readiness(self):
        for readiness, code in (({'ready': False, 'processing_enabled': False, 'operations_ok': False}, 503),
                                ({'ready': True, 'processing_enabled': True, 'operations_ok': False}, 200)):
            with patch('qa.ops._probe', side_effect=[(200, {'status': 'up'}), (code, readiness)]):
                result = ops.check('https://example.test', allow_paused=True)
            self.assertFalse(result['ok'])
            self.assertFalse(result['operations_ok'])

    def test_monitor_does_not_echo_remote_body_and_requires_valid_status(self):
        with patch('qa.ops._probe', side_effect=[(200, {'status': 'up'}), (500, {'secret': 'customer'})]):
            result = ops.check('https://example.test', allow_paused=True)
        self.assertFalse(result['ok'])
        self.assertNotIn('customer', json.dumps(result))
        with self.assertRaisesRegex(ops.OpsError, 'https_origin_required'):
            ops.check('https://user:password@example.test')


if __name__ == '__main__':
    unittest.main()
