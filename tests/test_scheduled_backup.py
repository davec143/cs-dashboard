import base64
from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from qa import ops, scheduled_backup as job


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.corrupt = False

    def put_object(self, Bucket, Key, Body, IfNoneMatch, ContentType):
        assert IfNoneMatch == '*'
        if Key in self.objects:
            raise RuntimeError('Already exists')
        self.objects[Key] = Body.read()

    def get_object(self, Bucket, Key):
        content = self.objects[Key]
        if self.corrupt and Key.endswith('.gcm'):
            content += b'corrupt'
        return {'Body': io.BytesIO(content)}


@unittest.skipUnless(importlib.util.find_spec('cryptography'), 'backup-only cryptography dependency not installed')
class ScheduledBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = b'x' * 32
        self.env = patch.dict(os.environ, {
            'DATABASE_URL': 'postgresql://backup:private_password@db.internal/railway',
            'BACKUP_ENCRYPTION_KEY': base64.urlsafe_b64encode(self.key).decode()}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_encrypted_stream_roundtrip_and_authentication_failure_cleanup(self):
        original = self.root / 'source'
        encrypted = self.root / 'encrypted'
        restored = self.root / 'restored'
        original.write_bytes(b'sensitive customer transcript' * 100000)
        job.encrypt_file(original, encrypted, self.key)
        self.assertNotIn(b'sensitive customer transcript', encrypted.read_bytes())
        job.decrypt_file(encrypted, restored, self.key)
        self.assertEqual(restored.read_bytes(), original.read_bytes())
        restored.unlink()
        with encrypted.open('r+b') as output:
            output.seek(-1, os.SEEK_END)
            last = output.read(1)[0]
            output.seek(-1, os.SEEK_END)
            output.write(bytes([last ^ 1]))
        with self.assertRaisesRegex(ops.OpsError, 'authentication_failed'):
            job.decrypt_file(encrypted, restored, self.key)
        self.assertFalse(restored.exists())

    def test_decryption_never_replaces_an_existing_file(self):
        source, encrypted, restored = (self.root / name for name in ('source', 'encrypted', 'restored'))
        source.write_bytes(b'backup')
        restored.write_bytes(b'keep')
        job.encrypt_file(source, encrypted, self.key)
        with self.assertRaises(FileExistsError):
            job.decrypt_file(encrypted, restored, self.key)
        self.assertEqual(restored.read_bytes(), b'keep')

    def test_missing_or_invalid_encryption_key_prevents_any_backup(self):
        for value in ('', 'not-base64', base64.urlsafe_b64encode(b'short').decode()):
            with patch.dict(os.environ, {'BACKUP_ENCRYPTION_KEY': value}), patch('qa.ops.backup') as backup:
                with self.assertRaisesRegex(ops.OpsError, 'encryption_key_invalid'):
                    job.run_backup()
                backup.assert_not_called()

    def fake_backup(self, path):
        path.write_bytes(b'PGDMP-sensitive customer transcript')
        ops._manifest_path(path).write_text(json.dumps({'version': 1, 'sha256': job._digest(path)}))
        return {'bytes': path.stat().st_size, 'sha256': job._digest(path)}

    @contextmanager
    def fake_local(self, root):
        yield 'qa_restore_drill'

    def fake_restore(self, path, name):
        self.assertEqual(name, 'qa_restore_drill')
        self.assertEqual(path.read_bytes(), b'PGDMP-sensitive customer transcript')
        self.assertEqual(json.loads(ops._manifest_path(path).read_text())['sha256'], job._digest(path))
        return {'row_counts': {'calls': 1, 'users': 3}}

    def test_success_only_after_download_decryption_and_restore_and_retains_no_plaintext_object(self):
        storage, connection = MemoryS3(), MagicMock()
        with patch('qa.scheduled_backup._storage', return_value=(storage, 'private-bucket')), \
                patch('psycopg.connect') as connect, patch('qa.ops.backup', side_effect=self.fake_backup), \
                patch('qa.scheduled_backup.local_restore_database', side_effect=self.fake_local), \
                patch('qa.ops.restore_check', side_effect=self.fake_restore) as restore, \
                patch('qa.scheduled_backup._record_status') as status:
            connect.return_value.__enter__.return_value = connection
            connection.execute.return_value.fetchone.return_value = (True,)
            result = job.run_backup()
        restore.assert_called_once()
        self.assertTrue(result['restore_verified'])
        self.assertEqual(len(storage.objects), 3)
        self.assertTrue(any(name.endswith('/verified.json') for name in storage.objects))
        self.assertFalse(any(b'sensitive customer transcript' in body for body in storage.objects.values()))
        success = status.call_args_list[-1].args[1]
        self.assertEqual(success['backup_completed_at'], success['restore_verified_at'])
        self.assertEqual(success['backup_error'], '')
        self.assertNotIn('private_password', json.dumps(result))

    def test_corrupt_download_never_records_success_or_runs_restore(self):
        storage, connection = MemoryS3(), MagicMock()
        storage.corrupt = True
        with patch('qa.scheduled_backup._storage', return_value=(storage, 'private-bucket')), \
                patch('psycopg.connect') as connect, patch('qa.ops.backup', side_effect=self.fake_backup), \
                patch('qa.ops.restore_check') as restore, patch('qa.scheduled_backup._record_status') as status:
            connect.return_value.__enter__.return_value = connection
            connection.execute.return_value.fetchone.return_value = (True,)
            with self.assertRaisesRegex(ops.OpsError, 'uploaded_backup_checksum_mismatch'):
                job.run_backup()
        restore.assert_not_called()
        self.assertFalse(any(name.endswith('/verified.json') for name in storage.objects))
        self.assertFalse(any('backup_completed_at' in call.args[1] for call in status.call_args_list))
        self.assertEqual(status.call_args_list[-1].args[1]['backup_error'], 'uploaded_backup_checksum_mismatch')

    def test_advisory_lock_prevents_overlapping_backup(self):
        with patch('qa.scheduled_backup._storage', return_value=(MemoryS3(), 'private-bucket')), \
                patch('psycopg.connect') as connect, patch('qa.ops.backup') as backup:
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (False,)
            with self.assertRaisesRegex(ops.OpsError, 'already_running'):
                job.run_backup()
            backup.assert_not_called()

    def test_scratch_database_listens_only_on_private_socket_and_is_stopped(self):
        original_target = 'postgresql://existing@test/qa_restore_saved'
        process = MagicMock()
        process.poll.return_value = None
        with patch.dict(os.environ, {'QA_RESTORE_TEST_DATABASE_URL': original_target}), \
                patch('qa.ops._run') as run, patch('qa.scheduled_backup.subprocess.Popen', return_value=process) as start, \
                patch('psycopg.connect') as connect:
            with job.local_restore_database(self.root) as target:
                self.assertEqual(target, 'qa_restore_drill')
                self.assertNotEqual(os.environ['QA_RESTORE_TEST_DATABASE_URL'], original_target)
                self.assertIn('host=', os.environ['QA_RESTORE_TEST_DATABASE_URL'])
            self.assertEqual(os.environ['QA_RESTORE_TEST_DATABASE_URL'], original_target)
        self.assertIn('listen_addresses=', start.call_args.args[0])
        self.assertIn('unix_socket_directories=' + str(self.root / 'socket'), start.call_args.args[0])
        self.assertEqual(os.stat(self.root / 'socket').st_mode & 0o777, 0o700)
        self.assertTrue(any(call.args[0][0] == 'pg_ctl' and 'stop' in call.args[0] for call in run.call_args_list))
        process.wait.assert_called_once()
        self.assertNotIn('DROP', str(connect.return_value.__enter__.return_value.execute.call_args_list))


if __name__ == '__main__':
    unittest.main()
