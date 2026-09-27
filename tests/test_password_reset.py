"""FR-G11 部分验收：忘记密码（邮件自助重置）链路。

覆盖密码重置及安全边界的关键用例：
- 防枚举（未知邮箱与已注册邮箱响应一致）
- 完整重置流程（含旧会话失效）
- 令牌一次性、可过期
- 弱口令拒绝
- 邮箱归一化与唯一性
"""
from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from webapp import app as app_module
from webapp.login_guard import RateLimiter
from webapp.storage import Store


class PasswordResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.browser=app_module.email_auth.browser_secret()
        env=patch.dict(os.environ,{'TAXPEARLS_PUBLIC_BASE_URL':'http://localhost'})
        env.start();self.addCleanup(env.stop)
        verify=patch.object(app_module,'email_verify_limiter',RateLimiter({'token':(10,900),'ip':(60,900)}))
        verify.start();self.addCleanup(verify.stop)
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        old_store, old_limiter, old_sender = (app_module.store, app_module.reset_limiter,
                                              app_module.send_password_reset_email)
        self._restore = (old_store, old_limiter, old_sender, app_module.reset_confirm_limiter)
        app_module.store = Store(Path(self._directory.name) / "reset.db")
        # 测试放宽限流，便于重复请求；限流本身由 test_rate_limit 覆盖。
        app_module.reset_limiter = RateLimiter({"email": (50, 15 * 60), "ip": (500, 60 * 60)})
        app_module.reset_confirm_limiter = RateLimiter({"token": (5, 15 * 60), "ip": (40, 15 * 60)})
        self.sent: list[dict[str, str]] = []

        def capture(*, to: str, reset_url: str, expires_minutes: int = 10) -> str:
            self.sent.append({"to": to, "reset_url": reset_url})
            return "test-mail-id"

        app_module.send_password_reset_email = capture

    def tearDown(self) -> None:
        old_store, old_limiter, old_sender, old_confirm_limiter = self._restore
        app_module.store, app_module.reset_limiter = old_store, old_limiter
        app_module.send_password_reset_email = old_sender
        app_module.reset_confirm_limiter = old_confirm_limiter

    def _client(self) -> TestClient:
        client=TestClient(app_module.app,base_url='http://localhost')
        client.cookies.set(app_module.EMAIL_COOKIE_NAME,self.browser)
        return client

    def _setup_admin(self, email: str = "root@example.com") -> None:
        app_module.store.create_initial_admin("rootadmin", "strong-pass-2026", "管理", "default", email)

    def _token_from_last_mail(self) -> str:
        self.assertTrue(self.sent, "应当已发送重置邮件")
        token=parse_qs(urlsplit(self.sent[-1]['reset_url']).fragment)['email'][0]
        verified=self._client().post('/api/auth/email/verify',json={'token':token})
        self.assertEqual(verified.status_code,200,verified.text)
        return verified.json()['proof']

    def test_unknown_email_gets_identical_response_without_sending(self):
        self._setup_admin()
        known = self._client().post("/api/auth/password/reset", json={"email": "root@example.com"})
        unknown = self._client().post("/api/auth/password/reset", json={"email": "ghost@example.com"})
        self.assertEqual(known.status_code, 200)
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(known.json(), unknown.json())
        self.assertEqual(len(self.sent), 1)  # 只有已注册邮箱真正发信

    def test_email_normalization_and_hashed_identity_in_audit_log(self):
        self._setup_admin("Root@Example.com ")
        result = self._client().post("/api/auth/password/reset", json={"email": "  ROOT@Example.COM "})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.sent[0]["to"], "root@example.com")
        with app_module.store.connect() as db:
            rows = db.execute("SELECT detail FROM audit_log WHERE action='password_reset_sent'").fetchall()
        self.assertTrue(rows)
        self.assertNotIn("root@example.com", str([tuple(r) for r in rows]))

    def test_full_reset_flow_invalidates_old_sessions(self):
        self._setup_admin()
        client = self._client()
        old_login = client.post("/api/login", json={"username": "rootadmin", "password": "strong-pass-2026"})
        self.assertEqual(old_login.status_code, 200)
        old_cookie = old_login.cookies.get(app_module.COOKIE_NAME)

        self._client().post("/api/auth/password/reset", json={"email": "root@example.com"})
        token = self._token_from_last_mail()
        confirm = self._client().post("/api/auth/password/reset/confirm",
                                      json={"token": token, "password": "fresh-pass-2026"})
        self.assertEqual(confirm.status_code, 200, confirm.text)

        me = self._client().get("/api/me", cookies={app_module.COOKIE_NAME: old_cookie})
        self.assertEqual(me.status_code, 401)  # 旧会话已失效
        new_login = client.post("/api/login", json={"username": "rootadmin", "password": "fresh-pass-2026"})
        self.assertEqual(new_login.status_code, 200)
        old_login_retry = client.post("/api/login", json={"username": "rootadmin", "password": "strong-pass-2026"})
        self.assertEqual(old_login_retry.status_code, 401)

    def test_token_is_single_use(self):
        self._setup_admin()
        self._client().post("/api/auth/password/reset", json={"email": "root@example.com"})
        token = self._token_from_last_mail()
        first = self._client().post("/api/auth/password/reset/confirm",
                                    json={"token": token, "password": "fresh-pass-2026"})
        self.assertEqual(first.status_code, 200)
        again = self._client().post("/api/auth/password/reset/confirm",
                                    json={"token": token, "password": "another-pass-2026"})
        self.assertEqual(again.status_code, 422)

    def test_expired_token_rejected(self):
        self._setup_admin()
        self._client().post("/api/auth/password/reset", json={"email": "root@example.com"})
        token = self._token_from_last_mail()
        with app_module.store.connect() as db:
            db.execute("UPDATE email_tokens SET expires_at='2000-01-01T00:00:00+00:00'")
        late = self._client().post("/api/auth/password/reset/confirm",
                                   json={"token": token, "password": "fresh-pass-2026"})
        self.assertEqual(late.status_code, 422)

    def test_weak_password_rejected_without_consuming_token(self):
        self._setup_admin()
        self._client().post("/api/auth/password/reset", json={"email": "root@example.com"})
        token = self._token_from_last_mail()
        weak = self._client().post("/api/auth/password/reset/confirm", json={"token": token, "password": "short"})
        self.assertEqual(weak.status_code, 422)
        ok = self._client().post("/api/auth/password/reset/confirm",
                                 json={"token": token, "password": "fresh-pass-2026"})
        self.assertEqual(ok.status_code, 200)  # 弱口令失败不消耗令牌

    def test_unknown_or_malformed_token_rejected(self):
        self._setup_admin()
        for token in ("", "not-a-real-token"):
            result = self._client().post("/api/auth/password/reset/confirm",
                                         json={"token": token, "password": "fresh-pass-2026"})
            self.assertEqual(result.status_code, 422)

    def test_duplicate_email_rejected_on_user_creation(self):
        self._setup_admin("taken@example.com")
        app_module.store.create_user('duplicate-org-admin','strong-pass-2026','机构管理员','org_admin','default')
        app_module.members.set_quota(app_module.store,app_module.store.get_user_by_email('taken@example.com'),'default',5,0)
        client = self._client()
        client.post("/api/login", json={"username": "duplicate-org-admin", "password": "strong-pass-2026"})
        duplicate = client.post("/api/users", json={
            "username": "seconduser", "password": "strong-pass-2026",
            "display_name": "第二人", "role": "accountant", "email": "Taken@Example.com",
        })
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn("邮箱", duplicate.json()["detail"])

    def test_optional_email_kept_blank(self):
        created = app_module.store.create_user("plainuser", "strong-pass-2026", "普通用户", "teacher", "default")
        self.assertEqual(created["email"], "")
        result = self._client().post("/api/auth/password/reset", json={"email": "plainuser@example.com"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.sent, [])  # 无邮箱的账号不产生发信


if __name__ == "__main__":
    unittest.main()
