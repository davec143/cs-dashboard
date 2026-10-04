"""Account lifecycle and interleaved credential-change regressions."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash
from qa import auth
from qa.db import Store


PASSWORD = 'AccountTestPassword123!'
PASSWORD_HASH = generate_password_hash(PASSWORD)


class AuthManagementTests(unittest.TestCase):
    def make_store(self):
        return Store(Path(self.tmp.name) / 'accounts.db')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = self.make_store()
        auth.create_user(self.store, 'first@example.com', PASSWORD_HASH)
        auth.create_user(self.store, 'second@example.com', PASSWORD_HASH)
        auth.create_user(self.store, 'viewer@example.com', PASSWORD_HASH, 'viewer')

    def tearDown(self):
        self.tmp.cleanup()

    def sign_in(self, email='second@example.com', password=PASSWORD):
        token, error = auth.login(self.store, email, password, 'test-ip')
        self.assertIsNone(error)
        self.assertIsNotNone(token)
        return token

    def test_account_list_contains_only_safe_fields(self):
        users = auth.list_users(self.store)
        self.assertEqual([u['email'] for u in users],
                         ['first@example.com', 'second@example.com', 'viewer@example.com'])
        for user in users:
            self.assertEqual(set(user), {'email', 'role', 'enabled', 'created'})
            self.assertIs(user['enabled'], True)

    def test_disable_revokes_all_sessions_and_blocks_login(self):
        tokens = [self.sign_in(), self.sign_in()]
        changed = auth.update_user_access(self.store, ' SECOND@example.com ', 'admin', False, 'first@example.com')
        self.assertFalse(changed['enabled'])
        for token in tokens:
            self.assertIsNone(auth.identity(self.store, token))
        self.assertEqual(auth.login(self.store, 'second@example.com', PASSWORD, 'test-ip'),
                         (None, 'invalid_credentials'))
        with self.store.connect() as conn:
            audit = conn.execute("SELECT * FROM audit_log WHERE action='account.access_changed'").fetchone()
        self.assertEqual(audit['actor'], 'first@example.com')
        self.assertEqual(audit['subject'], 'second@example.com')
        self.assertEqual(json.loads(audit['detail']), {
            'before': {'role': 'admin', 'enabled': True},
            'after': {'role': 'admin', 'enabled': False}})

    def test_role_change_revokes_old_sessions_and_new_login_has_new_role(self):
        token = self.sign_in()
        auth.update_user_access(self.store, 'second@example.com', 'viewer', True, 'first@example.com')
        self.assertIsNone(auth.identity(self.store, token))
        self.assertEqual(auth.identity(self.store, self.sign_in())['role'], 'viewer')

    def test_reenable_does_not_restore_old_sessions(self):
        token = self.sign_in()
        auth.update_user_access(self.store, 'second@example.com', 'admin', False, 'first@example.com')
        auth.update_user_access(self.store, 'second@example.com', 'admin', True, 'first@example.com')
        self.assertIsNone(auth.identity(self.store, token))
        self.assertIsNotNone(auth.identity(self.store, self.sign_in()))

    def test_noop_preserves_session_and_does_not_create_audit_event(self):
        token = self.sign_in()
        auth.update_user_access(self.store, 'second@example.com', 'admin', True, 'first@example.com')
        self.assertIsNotNone(auth.identity(self.store, token))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0], 0)

    def test_admin_cannot_remove_their_own_access(self):
        for role, enabled in [('viewer', True), ('admin', False), ('viewer', False)]:
            with self.assertRaisesRegex(ValueError, '^cannot_change_own_access$'):
                auth.update_user_access(self.store, 'first@example.com', role, enabled, 'first@example.com')
        self.assertEqual(sum(u['role'] == 'admin' and u['enabled'] for u in auth.list_users(self.store)), 2)

    def test_concurrent_admin_demotion_keeps_one_enabled_admin(self):
        barrier = threading.Barrier(2)

        def demote(target, actor):
            barrier.wait(timeout=10)
            try:
                auth.update_user_access(self.store, target, 'viewer', True, actor)
                return 'changed'
            except ValueError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(demote, 'first@example.com', 'second@example.com'),
                       pool.submit(demote, 'second@example.com', 'first@example.com')]
            results = [future.result(timeout=20) for future in results]
        self.assertCountEqual(results, ['changed', 'administrator_required'])
        self.assertEqual(sum(u['role'] == 'admin' and u['enabled'] for u in auth.list_users(self.store)), 1)

    def test_mutations_recheck_actor_in_database(self):
        for actor in ('viewer@example.com', 'unknown@example.com'):
            for operation in (
                lambda: auth.provision_user(self.store, 'new@example.com', 'admin', actor),
                lambda: auth.reset_user_password(self.store, 'second@example.com', actor),
                lambda: auth.update_user_access(self.store, 'second@example.com', 'viewer', True, actor),
            ):
                with self.assertRaisesRegex(ValueError, '^administrator_required$'):
                    operation()
        auth.update_user_access(self.store, 'second@example.com', 'admin', False, 'first@example.com')
        with self.assertRaisesRegex(ValueError, '^administrator_required$'):
            auth.reset_user_password(self.store, 'viewer@example.com', 'second@example.com')

    def test_update_validates_input_and_missing_user(self):
        for email, role, enabled, error in [
            ('invalid', 'admin', True, 'invalid_user'),
            ('second@example.com', 'owner', True, 'invalid_user'),
            ('second@example.com', 'admin', 'false', 'invalid_enabled'),
            ('second@example.com', 'admin', 0, 'invalid_enabled'),
        ]:
            with self.assertRaisesRegex(ValueError, '^' + error + '$'):
                auth.update_user_access(self.store, email, role, enabled, 'first@example.com')
        self.assertIsNone(auth.update_user_access(self.store, 'missing@example.com', 'viewer', True, 'first@example.com'))
        self.assertIsNone(auth.reset_user_password(self.store, 'missing@example.com', 'first@example.com'))

    def test_password_reset_is_unique_revokes_sessions_and_does_not_log_secret(self):
        token = self.sign_in()
        reset = auth.reset_user_password(self.store, 'second@example.com', 'first@example.com')
        other = auth.reset_user_password(self.store, 'viewer@example.com', 'first@example.com')
        self.assertGreaterEqual(len(reset['initial_password']), 24)
        self.assertNotEqual(reset['initial_password'], other['initial_password'])
        self.assertIsNone(auth.identity(self.store, token))
        self.assertEqual(auth.login(self.store, 'second@example.com', PASSWORD, 'test-ip'),
                         (None, 'invalid_credentials'))
        self.sign_in(password=reset['initial_password'])
        with self.store.connect() as conn:
            audit = conn.execute("SELECT * FROM audit_log WHERE subject=?", ('second@example.com',)).fetchone()
            user = conn.execute('SELECT password_hash FROM users WHERE email=?', ('second@example.com',)).fetchone()
        self.assertEqual(audit['actor'], 'first@example.com')
        self.assertEqual(audit['action'], 'account.password_reset')
        self.assertNotIn(reset['initial_password'], audit['detail'])
        self.assertNotEqual(user[0], reset['initial_password'])
        self.assertTrue(check_password_hash(user[0], reset['initial_password']))

    def test_reset_preserves_disabled_account_state(self):
        auth.update_user_access(self.store, 'second@example.com', 'admin', False, 'first@example.com')
        reset = auth.reset_user_password(self.store, 'second@example.com', 'first@example.com')
        self.assertFalse(reset['enabled'])
        self.assertEqual(auth.login(self.store, 'second@example.com', reset['initial_password'], 'test-ip'),
                         (None, 'invalid_credentials'))

    def test_login_cannot_create_session_after_password_reset(self):
        def reset_during_verification(hashed, password):
            result = check_password_hash(hashed, password)
            auth.reset_user_password(self.store, 'second@example.com', 'first@example.com')
            return result

        with patch('qa.auth.check_password_hash', side_effect=reset_during_verification):
            self.assertEqual(auth.login(self.store, 'second@example.com', PASSWORD, 'test-ip'),
                             (None, 'invalid_credentials'))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)

    def test_login_cannot_create_session_after_account_disable(self):
        def disable_during_verification(hashed, password):
            result = check_password_hash(hashed, password)
            auth.update_user_access(self.store, 'second@example.com', 'admin', False, 'first@example.com')
            return result

        with patch('qa.auth.check_password_hash', side_effect=disable_during_verification):
            self.assertEqual(auth.login(self.store, 'second@example.com', PASSWORD, 'test-ip'),
                             (None, 'invalid_credentials'))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)

    def test_password_change_cannot_overwrite_concurrent_admin_reset(self):
        issued = {}

        def reset_during_verification(hashed, password):
            result = check_password_hash(hashed, password)
            issued.update(auth.reset_user_password(self.store, 'second@example.com', 'first@example.com'))
            return result

        with patch('qa.auth.check_password_hash', side_effect=reset_during_verification):
            with self.assertRaisesRegex(ValueError, '^invalid_current_password$'):
                auth.change_password(self.store, 'second@example.com', PASSWORD, 'DifferentPassword123!')
        self.sign_in(password=issued['initial_password'])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='account.password_changed'").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
