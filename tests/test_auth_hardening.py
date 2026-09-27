from __future__ import annotations
import auth_support

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as app_module
from webapp import captcha
from webapp.login_guard import LoginGuard, RateLimiter
from webapp.storage import ROLES, SetupAlreadyInitialized, Store, _hash_token, _validate_password


class AuthHardeningTests(unittest.TestCase):
    def test_initialized_setup_rejected_before_password_hash_and_log_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'setup.db')
            with patch.object(store, '_log', side_effect=RuntimeError('test log failure')):
                with self.assertRaises(RuntimeError):
                    store.create_initial_admin('initial-admin', 'Strong-test-2026!', '管理', 'default')
            self.assertFalse(store.has_users())
            store.create_initial_admin('initial-admin', 'Strong-test-2026!', '管理', 'default')
            with patch('webapp.storage._passwords') as passwords:
                with self.assertRaises(SetupAlreadyInitialized):
                    store.create_initial_admin('second-admin', 'Strong-test-2026!', '管理', 'default')
                passwords.hash.assert_not_called()
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='setup'").fetchone()[0], 1)

    def test_setup_is_atomic_across_connections_and_unique_in_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.db"
            first, second = Store(path), Store(path)

            def attempt(args):
                store, name = args
                try:
                    return store.create_initial_admin(name, "strong-pass-2026", name, "default")["username"]
                except SetupAlreadyInitialized:
                    return "already initialized"

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, [(first, "owner-one"), (second, "owner-two")]))
            self.assertEqual(results.count("already initialized"), 1)
            self.assertEqual(len(first.list_users()), 1)
            with self.assertRaises(sqlite3.IntegrityError):
                first.create_user("another-admin", "strong-pass-2026", "其他管理员", "platform_admin", "default")
            self.assertEqual(len(Store(path).list_users()), 1)

    def test_legacy_multiple_admins_require_manual_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, "
                           "password_hash TEXT NOT NULL, display_name TEXT NOT NULL, role TEXT NOT NULL, "
                           "org_id TEXT NOT NULL, active INTEGER NOT NULL, created_at TEXT NOT NULL)")
                db.executemany("INSERT INTO users VALUES (?,?,?,?,?,?,?,?)", [
                    (str(index), f"old{index}", "legacy", "旧管理员", "platform_admin", "default", 1, "2026-01-01")
                    for index in (1, 2)
                ])
                db.commit()
            with self.assertRaisesRegex(RuntimeError, "未自动删除账号"):
                Store(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)

    def test_password_policy_applies_to_setup_and_all_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            old = app_module.store
            app_module.store = Store(Path(directory) / "auth.db")
            try:
                with TestClient(app_module.app) as client:
                    for password in ("a", "password123!", "alllowercase2026"):
                        response = client.post("/api/setup", json={"username": "admin", "password": password})
                        self.assertEqual(response.status_code, 422, response.text)
                    response = client.post("/api/setup", json={"username": "admin", "password": "strong-pass-2026"})
                    self.assertEqual(response.status_code, 200, response.text)
                    client.post("/api/login", json={"username": "admin", "password": "strong-pass-2026"})
                    for role in ("student", "accountant"):
                        with self.assertRaises(ValueError):
                            app_module.store.create_user(role, 'a', role, role, 'default')
                        response = client.post("/api/users", json={
                            "username": role, "password": "a", "display_name": role,
                            "role": role, "org_id": "default",
                        })
                        self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(len(app_module.store.list_users()), 1)
            finally:
                app_module.store = old

    def test_login_guard_delays_and_locks_both_dimensions(self):
        now = [1000.0]
        guard = LoginGuard(clock=lambda: now[0])
        for attempt in range(1, 11):
            self.assertEqual(guard.retry_after("target", "192.0.2.1"), 0)
            self.assertEqual(guard.record_failure("target", "192.0.2.1"), attempt)
            wait = guard.retry_after("target", "192.0.2.1")
            if attempt >= 5:
                self.assertGreater(wait, 0)
            if attempt < 10:
                now[0] += wait
        self.assertEqual(guard.retry_after("other", "192.0.2.1"), 900)
        self.assertEqual(guard.retry_after("target", "192.0.2.2"), 900)
        guard.record_success("target")
        self.assertEqual(guard.retry_after("target", "192.0.2.2"), 0)
        now[0] += 901
        self.assertEqual(guard.retry_after("other", "192.0.2.1"), 0)

    def test_login_guard_prevents_parallel_attempts_per_account_or_ip(self):
        guard = LoginGuard()
        with guard.reserve("target", "192.0.2.1") as first:
            self.assertEqual(first, 0)
            with guard.reserve("target", "192.0.2.2") as account_overlap:
                self.assertEqual(account_overlap, 1)
            with guard.reserve("other", "192.0.2.1") as ip_overlap:
                self.assertEqual(ip_overlap, 1)
            with guard.reserve("other", "192.0.2.2") as independent:
                self.assertEqual(independent, 0)
        with guard.reserve("target", "192.0.2.1") as free_again:
            self.assertEqual(free_again, 0)

    def test_login_route_limits_and_audits_failures_without_plain_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            old_store, old_guard = app_module.store, app_module.login_guard
            app_module.store = Store(Path(directory) / "auth.db")
            app_module.login_guard = LoginGuard()
            try:
                app_module.store.create_initial_admin("rootadmin", "strong-pass-2026", "管理", "default")
                with TestClient(app_module.app) as client:
                    for _ in range(5):
                        result = client.post("/api/login", json={"username": "rootadmin", "password": "wrong-password"})
                        self.assertEqual(result.status_code, 401, result.text)
                    blocked = client.post("/api/login", json={"username": "rootadmin", "password": "strong-pass-2026"})
                    self.assertEqual(blocked.status_code, 429, blocked.text)
                    self.assertGreaterEqual(int(blocked.headers["Retry-After"]), 1)
                with app_module.store.connect() as db:
                    rows = db.execute("SELECT action,target_id,detail FROM audit_log ORDER BY id").fetchall()
                self.assertEqual(rows[0]['action'], 'setup')
                rows = rows[1:]
                self.assertEqual(len(rows), 5)
                self.assertTrue(all(row["action"] == "login_failed" for row in rows))
                self.assertNotIn("rootadmin", str([tuple(row) for row in rows]))
                self.assertNotIn("wrong-password", str([tuple(row) for row in rows]))
            finally:
                app_module.store, app_module.login_guard = old_store, old_guard


class PasswordPolicyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "policy.db")

    def _platform(self):
        return self.store.create_initial_admin("rootadmin", "Zebra!Forest92", "管理", "default",
                                               "root@example.com")

    def test_length_and_three_character_classes_at_boundaries(self):
        for password in ("abcdefgh9!", "Abcdefghij", "abcdefg123", "ab12345678 ",
                         "ab12345678\t", "a" * 129, "aB8!" * 32 + "x"):
            with self.subTest(password_length=len(password)):
                if password == "abcdefgh9!":
                    _validate_password(password)  # Exactly ten, lower/digit/symbol.
                else:
                    with self.assertRaises(ValueError):
                        _validate_password(password)
        for password in ("Abcdefgh12", "Abcdefghi!", "abcd12345_", "aB8!" * 32):
            _validate_password(password)
        for password in ("short123!", None, 123):
            with self.assertRaises(ValueError):
                _validate_password(password)

    def test_common_password_case_padding_and_width_cannot_bypass(self):
        for password in ("Password123!", "WELCOME123!", "Changeme123!", "Password2026!",
                         " Password123! ", "Ｐａｓｓｗｏｒｄ１２３！"):
            with self.subTest(password=password), self.assertRaisesRegex(ValueError, "常见弱口令"):
                _validate_password(password)
        _validate_password("Zebra!Forest92")

    def test_email_local_part_case_width_and_short_names(self):
        for email, password in ((" Owner@Example.COM ", "xOWNER!8265"),
                                ("owner@example.com", "ｘＯＷＮＥＲ！８２６５"),
                                ("a@example.com", "ZebrA!Forest92"),
                                ("first.last@example.com", "First.Last!29")):
            with self.subTest(email=email), self.assertRaisesRegex(ValueError, "@ 前部分"):
                _validate_password(password, email)
        _validate_password("Zebra!Forest92", "owner@example.com")
        _validate_password("Zebra!Forest92", "")  # Optional email stays optional.

    def test_create_user_policy_covers_every_role_without_partial_users(self):
        for index, role in enumerate(sorted(ROLES)):
            with self.subTest(role=role):
                with self.assertRaisesRegex(ValueError, "@ 前部分"):
                    self.store.create_user(f"user-{index}", "xOWNER!8265", "姓名", role,
                                           "default", " OWNER@example.com ")
                self.assertIsNone(self.store.get_user_by_email("owner@example.com"))
                self.store.create_user(f"user-{index}", "Zebra!Forest92", "姓名", role,
                                       "default", f"member{index}@example.com")
        self.assertEqual(len(self.store.list_users()), len(ROLES))

    def test_setup_and_admin_api_enforce_policy_and_display_current_hint(self):
        old_store, old_guard = app_module.store, app_module.login_guard
        app_module.store, app_module.login_guard = self.store, LoginGuard()
        self.addCleanup(setattr, app_module, "store", old_store)
        self.addCleanup(setattr, app_module, "login_guard", old_guard)
        with TestClient(app_module.app) as client:
            payload = {"username": "rootadmin", "password": "xROOT!82654", "email": "root@example.com"}
            denied = client.post("/api/setup", json=payload)
            self.assertEqual(denied.status_code, 422, denied.text)
            self.assertNotIn(payload["password"], denied.text)
            self.assertEqual(self.store.list_users(), [])
            payload["password"] = "Zebra!Forest92"
            self.assertEqual(client.post("/api/setup", json=payload).status_code, 200)
            self.assertEqual(client.post("/api/login", json=payload).status_code, 200)
            self.store.create_user('org-policy-admin','Zebra!Forest92','机构管理员','org_admin','default')
            self.assertEqual(client.post('/api/login',json={'username':'org-policy-admin','password':'Zebra!Forest92'}).status_code,200)
            denied = client.post("/api/users", json={"username": "accountant", "password": "xOWNER!8265",
                "display_name": "会计", "role": "accountant", "email": "owner@example.com"})
            self.assertEqual(denied.status_code, 422, denied.text)
            self.assertEqual(len(self.store.list_users()), 2)
            page = client.get("/").text
            self.assertIn("空白不算符号", page)
            self.assertIn("邮箱的 @ 前部分", page)

    def test_registration_rejection_keeps_code_invite_quota_and_sessions_unchanged(self):
        platform = self._platform()
        invite = self.store.create_invite_code(platform, "机构", 2)
        code = auth_support.register_code(self.store,"owner@example.com")
        with self.store.connect() as db:
            before = [tuple(row) for row in db.execute("SELECT * FROM email_tokens")]
        with self.assertRaisesRegex(ValueError, "@ 前部分"):
            self.store.register_with_code(" OWNER@example.com ", code, invite["code"], "xOWNER!8265", browser_session=auth_support.BROWSER)
        with self.store.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM email_tokens")], before)
            self.assertIsNone(db.execute("SELECT redeemed_by FROM invite_codes").fetchone()[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM org_quota").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        user, token = self.store.register_with_code("owner@example.com", code, invite["code"], "Zebra!Forest92", browser_session=auth_support.BROWSER)
        self.assertEqual(self.store.user_for_token(token)["id"], user["id"])

    def test_reset_rejection_is_atomic_for_every_role_then_valid_reset_revokes_sessions(self):
        for index, role in enumerate(sorted(ROLES)):
            with self.subTest(role=role):
                email = f"member{index}@example.com"
                user = self.store.create_user(f"user-{index}", "Zebra!Forest92", "姓名", role, "default", email)
                _, session = self.store.authenticate(user["username"], "Zebra!Forest92")
                token = auth_support.reset_proof(self.store,email)
                with self.store.connect() as db:
                    before = db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()[0]
                weak = f"xMEMBER{index}!8265"
                with self.assertRaisesRegex(ValueError, "@ 前部分"):
                    auth_support.reset_password(self.store,token,weak)
                self.assertIsNotNone(self.store.user_for_token(session))
                with self.store.connect() as db:
                    self.assertEqual(db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()[0], before)
                    self.assertIsNone(db.execute("SELECT used_at FROM email_tokens WHERE proof_hash=?", (_hash_token(token),)).fetchone()[0])
                auth_support.reset_password(self.store,token,"Birch!Ocean83")
                self.assertIsNone(self.store.user_for_token(session))
                self.assertIsNotNone(self.store.authenticate(user["username"], "Birch!Ocean83"))
                self.assertIsNone(self.store.authenticate(user["username"], "Zebra!Forest92"))
                with self.assertRaises(ValueError):
                    auth_support.reset_password(self.store,token,"Birch!Ocean83")

    def test_reset_api_uses_bound_email_not_caller_identity_without_secret_logging(self):
        user = self._platform()
        browser=app_module.email_auth.browser_secret()
        delivery=app_module.email_auth.issue(self.store,user['email'],'reset',browser)
        token=app_module.email_auth.redeem_magic(self.store,delivery['token'],browser)['proof']
        old = app_module.store
        app_module.store = self.store
        self.addCleanup(setattr, app_module, "store", old)
        with TestClient(app_module.app) as client:
            client.cookies.set(app_module.EMAIL_COOKIE_NAME,browser)
            weak = "xROOT!82654"
            denied = client.post("/api/auth/password/reset/confirm", json={"token": token,
                "password": weak, "email": "other@example.com"})
            self.assertEqual(denied.status_code, 422, denied.text)
            self.assertNotIn(weak, denied.text)
            self.assertNotIn(token, denied.text)
            accepted = client.post("/api/auth/password/reset/confirm", json={"token": token, "password": "Birch!Ocean83"})
            self.assertEqual(accepted.status_code, 200, accepted.text)
        with self.store.connect() as db:
            logs = str([tuple(row) for row in db.execute("SELECT * FROM audit_log")])
        self.assertNotIn(weak, logs)
        self.assertNotIn("Birch!Ocean83", logs)
        self.assertNotIn(token, logs)

    def test_legacy_hashes_are_not_rewritten_or_login_blocked_by_new_setting_policy(self):
        user = self._platform()
        from webapp.storage import _passwords
        legacy_password = "Root!Legacy26"
        with self.store.connect() as db:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (_passwords.hash(legacy_password), user["id"]))
        self.assertIsNotNone(Store(self.store.path).authenticate(user["username"], legacy_password))
        token = auth_support.reset_proof(self.store,user["email"])
        with self.assertRaisesRegex(ValueError, "@ 前部分"):
            auth_support.reset_password(self.store,token,legacy_password)
        auth_support.reset_password(self.store,token,"Birch!Ocean83")


class AuthAbuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        replacement = patch.object(app_module, 'store', Store(Path(temporary.name) / 'abuse.db'))
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_limiter_hard_capacity_preserves_active_limits_and_recovers_after_expiry(self):
        now = [0.0]
        limiter = RateLimiter({"email": (1, 10), "ip": (10, 100)}, max_keys=3, clock=lambda: now[0])
        self.assertTrue(limiter.allow(email=" A@example.com ", ip="192.0.2.1"))
        self.assertTrue(limiter.allow(email="b@example.com", ip="192.0.2.1"))
        for index in range(20):
            self.assertFalse(limiter.allow(email=f"flood{index}@example.com", ip="192.0.2.2"))
        self.assertEqual(len(limiter._hits), 3)
        self.assertFalse(limiter.allow(email="a@EXAMPLE.COM", ip="192.0.2.1"))
        self.assertNotIn("a@example.com", str(limiter._hits))
        now[0] = 10.0  # Email entries expire on their own shorter window.
        self.assertTrue(limiter.allow(email="c@example.com", ip="192.0.2.1"))
        self.assertEqual(len(limiter._hits), 2)
        self.assertEqual(len(limiter._hits[("ip", limiter._key("192.0.2.1"))]), 3)

    def test_limiter_checks_all_dimensions_without_charging_partial_or_denied_requests(self):
        limiter = RateLimiter({"email": (1, 10), "ip": (2, 10)}, max_keys=8)
        with self.assertRaises(ValueError):
            limiter.allow(email="a@example.com")
        self.assertEqual(limiter._hits, {})
        self.assertTrue(limiter.allow(email="a@example.com", ip="192.0.2.1"))
        self.assertFalse(limiter.allow(email="a@example.com", ip="192.0.2.2"))
        self.assertTrue(limiter.allow(email="b@example.com", ip="192.0.2.2"))
        self.assertTrue(limiter.allow(email="c@example.com", ip="192.0.2.2"))
        self.assertFalse(limiter.allow(email="d@example.com", ip="192.0.2.2"))
        self.assertNotIn(("email", limiter._key("d@example.com")), limiter._hits)

    def test_limiter_parallel_reservations_cannot_exceed_limit_or_capacity(self):
        limiter = RateLimiter({"ip": (5, 100)}, max_keys=1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: limiter.allow(ip="192.0.2.1"), range(30)))
        self.assertEqual(sum(results), 5)
        self.assertEqual(len(limiter._hits), 1)
        self.assertFalse(limiter.allow(ip="192.0.2.2"))

    def test_captcha_capacity_does_not_evict_valid_challenges_and_recovers_at_expiry(self):
        now = [0.0]
        with patch.object(captcha, '_store', {}), patch.object(captcha, 'MAX_KEYS', 2), \
             patch.object(captcha.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(captcha, '_render', return_value='data:image/png;base64,test'):
            first, second = captcha.issue('AB2D'), captcha.issue('CD3E')
            with self.assertRaises(captcha.CaptchaCapacityError):
                captcha.issue('EF4G')
            self.assertEqual(len(captcha._store), 2)
            self.assertTrue(captcha.verify(first['captcha_id'], 'ab2d'))
            self.assertFalse(captcha.verify(first['captcha_id'], 'AB2D'))
            captcha.issue('EF4G')
            now[0] = captcha.LIFETIME_SECONDS
            self.assertFalse(captcha.verify(second['captcha_id'], 'CD3E'))
            captcha.issue('GH5J')
            self.assertEqual(len(captcha._store), 1)

    def test_captcha_empty_wrong_answers_consume_challenge_and_render_failure_cleans_slot(self):
        with patch.object(captcha, '_store', {}), patch.object(captcha, '_render', return_value='image'):
            for answer in ('', 'wrong'):
                puzzle = captcha.issue('AB2D')
                self.assertFalse(captcha.verify(puzzle['captcha_id'], answer))
                self.assertFalse(captcha.verify(puzzle['captcha_id'], 'AB2D'))
            with patch.object(captcha, '_render', side_effect=RuntimeError('render failed')):
                with self.assertRaises(RuntimeError):
                    captcha.issue('AB2D')
            self.assertEqual(captcha._store, {})

    def test_captcha_route_limits_before_render_and_reports_capacity_without_evicting(self):
        with patch.object(app_module, 'captcha_limiter', RateLimiter({'ip': (1, 60)})), \
             patch.object(captcha, 'issue', side_effect=captcha.CaptchaCapacityError()) as issue:
            with TestClient(app_module.app) as client:
                first = client.get('/api/auth/captcha')
                self.assertEqual(first.status_code, 503)
                self.assertEqual(first.headers['Retry-After'], '60')
                second = client.get('/api/auth/captcha')
                self.assertEqual(second.status_code, 429)
                self.assertEqual(second.headers['Cache-Control'], 'private, no-store')
                issue.assert_called_once()

    def test_register_completion_limits_normalized_email_and_ip_before_storage(self):
        limiter = RateLimiter({'email': (1, 60), 'ip': (2, 60)})
        with patch.object(app_module, 'register_complete_limiter', limiter), \
             patch.object(app_module.store, 'register_with_code', side_effect=ValueError('凭证无效')) as register:
            with TestClient(app_module.app) as client:
                payload = {'email': ' Owner@example.com ', 'code': '123456', 'invite_code': 'AB2D', 'password': 'Zebra!Forest92'}
                self.assertEqual(client.post('/api/register/complete', json=payload).status_code, 422)
                payload['email'] = 'OWNER@EXAMPLE.COM'
                self.assertEqual(client.post('/api/register/complete', json=payload).status_code, 429)
                payload['email'] = 'other@example.com'
                self.assertEqual(client.post('/api/register/complete', json=payload).status_code, 422)
                payload['email'] = 'third@example.com'
                self.assertEqual(client.post('/api/register/complete', json=payload).status_code, 429)
            self.assertEqual(register.call_count, 2)

    def test_reset_confirmation_limits_token_and_ip_without_identity_or_secret_response(self):
        limiter = RateLimiter({'token': (1, 60), 'ip': (2, 60)})
        with patch.object(app_module, 'reset_confirm_limiter', limiter), \
             patch.object(app_module.email_auth, 'reset_password', side_effect=ValueError('链接无效')) as reset:
            with TestClient(app_module.app) as client:
                payload = {'token': 'private-token', 'password': 'Zebra!Forest92'}
                self.assertEqual(client.post('/api/auth/password/reset/confirm', json=payload).status_code, 422)
                blocked = client.post('/api/auth/password/reset/confirm', json=payload)
                self.assertEqual(blocked.status_code, 429)
                self.assertNotIn(payload['token'], blocked.text)
                self.assertNotIn(payload['password'], blocked.text)
                payload['token'] = 'different-token'
                self.assertEqual(client.post('/api/auth/password/reset/confirm', json=payload).status_code, 422)
                payload['token'] = 'third-token'
                self.assertEqual(client.post('/api/auth/password/reset/confirm', json=payload).status_code, 429)
            self.assertEqual(reset.call_count, 2)

    def test_oversized_auth_fields_rejected_before_store_without_echoing_credentials(self):
        with patch.object(app_module.store, 'register_with_code') as register, \
             patch.object(app_module.email_auth, 'reset_password') as reset:
            with TestClient(app_module.app) as client:
                secret = 'credential-secret-' * 40
                response = client.post('/api/register/complete', json={'email': 'owner@example.com',
                    'code': '123456', 'invite_code': secret, 'password': 'Zebra!Forest92'})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(secret, response.text)
                response = client.post('/api/auth/password/reset/confirm', json={'token': secret, 'password': 'Zebra!Forest92'})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(secret, response.text)
            register.assert_not_called()
            reset.assert_not_called()


if __name__ == "__main__":
    unittest.main()
