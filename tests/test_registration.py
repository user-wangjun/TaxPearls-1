"""FR-G10/G11 第一轮验收：图片人机验证 + 邮箱验证码 + 创始码注册。

覆盖单事务注册、邀请码一次性核销、验证码尝试上限和签发权限。
"""
from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as app_module
from webapp.captcha import issue as issue_captcha
from webapp.login_guard import LoginGuard, RateLimiter
from webapp.storage import Store


class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.browser=app_module.email_auth.browser_secret()
        env=patch.dict(os.environ,{'TAXPEARLS_PUBLIC_BASE_URL':'http://localhost'})
        env.start();self.addCleanup(env.stop)
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self._restore = (app_module.store, app_module.login_guard, app_module.reset_limiter,
                         app_module.register_code_limiter, app_module.send_registration_code_email,
                         app_module.register_complete_limiter)
        app_module.store = Store(Path(self._directory.name) / "register.db")
        app_module.login_guard = LoginGuard()
        app_module.reset_limiter = RateLimiter({"email": (50, 15 * 60), "ip": (500, 60 * 60)})
        app_module.register_code_limiter = RateLimiter({"email": (50, 15 * 60), "ip": (500, 60 * 60)})
        # Each test is a fresh deployment; keep real completion limits intact.
        app_module.register_complete_limiter = RateLimiter({"email": (10, 15 * 60), "ip": (60, 60 * 60)})
        self.sent: list[dict[str, str]] = []

        def capture(*, to: str, code: str, signup_url: str, expires_minutes: int = 10) -> str:
            self.sent.append({"to": to, "code": code, "signup_url": signup_url})
            return "test-mail-id"

        app_module.send_registration_code_email = capture
        self.addCleanup(self._restore_all)

    def tearDown(self) -> None:
        self._restore_all()

    def _restore_all(self) -> None:
        (app_module.store, app_module.login_guard, app_module.reset_limiter,
         app_module.register_code_limiter, app_module.send_registration_code_email,
         app_module.register_complete_limiter) = self._restore

    def _client(self) -> TestClient:
        # Separate HTTP clients represent tabs sharing the initiating browser,
        # not shared logged-in identities. Cross-browser rejection has its own tests.
        client=TestClient(app_module.app,base_url='http://localhost')
        client.cookies.set(app_module.EMAIL_COOKIE_NAME,self.browser)
        return client

    def _setup_platform_admin(self) -> None:
        app_module.store.create_initial_admin("rootadmin", "strong-pass-2026", "平台管理",
                                              "default", "root@example.com")

    def _admin_client(self) -> TestClient:
        client = self._client()
        result = client.post("/api/login", json={"username": "rootadmin", "password": "strong-pass-2026"})
        assert result.status_code == 200, result.text
        return client

    def _create_invite(self, client: TestClient, bound_email: str,
                       org_name: str = "测试税务师事务所", seats: int = 20) -> dict:
        result = client.post("/api/invites", json={
            "org_name": org_name, "seats": seats, "bound_email": bound_email})
        assert result.status_code == 200, result.text
        return result.json()

    def _start(self, client: TestClient, email: str) -> object:
        captcha = issue_captcha("AB2D")  # 测试钩子：注入固定明文；答案故意用小写，覆盖大小写不敏感
        return client.post("/api/auth/email/start", json={
            "email": email, "captcha_id": captcha["captcha_id"],
            "captcha_answer": "ab2d"})

    def _code_from_last_mail(self) -> str:
        self.assertTrue(self.sent, "应当已发送验证邮件")
        return self.sent[-1]['code']

    def test_invite_creation_requires_platform_admin(self):
        self._setup_platform_admin()
        client = self._client()
        client.post("/api/login", json={"username": "rootadmin", "password": "strong-pass-2026"})
        app_module.store.create_user("orgboss", "strong-pass-2026", "机构管理员", "org_admin", "org-a")
        client.post("/api/logout")
        client.post("/api/login", json={"username": "orgboss", "password": "strong-pass-2026"})
        forbidden = client.post("/api/invites", json={
            "org_name": "越权机构", "seats": 5, "bound_email": "x@example.com"})
        self.assertEqual(forbidden.status_code, 403)
        anonymous = self._client().post("/api/invites", json={
            "org_name": "匿名机构", "seats": 5, "bound_email": "y@example.com"})
        self.assertIn(anonymous.status_code, (401, 403))

    def test_full_founder_registration_flow(self):
        self._setup_platform_admin()
        admin = self._admin_client()
        invite = self._create_invite(admin, "owner@example.com")
        self.assertRegex(invite["code"], r"^[0-9A-HJ-KM-NP-TV-Z]{12}$")  # Crockford：无 I/L/O/U

        blocked = self._client().post("/api/auth/email/start", json={
            "email": "owner@example.com", "captcha_id": "bogus", "captcha_answer": "0"})
        self.assertEqual(blocked.status_code, 422)  # 人机验证不过不发信

        client = self._client()
        started = self._start(client, "Owner@Example.com ")
        self.assertEqual(started.status_code, 200, started.text)
        self.assertFalse(started.json()["exists"])
        self.assertEqual(self.sent[-1]["to"], "owner@example.com")

        code = self._code_from_last_mail()
        done = client.post("/api/register/complete", json={
            "email": "owner@example.com", "code": code,
            "invite_code": invite["code"], "password": "founder-pass-2026"})
        self.assertEqual(done.status_code, 200, done.text)
        user = done.json()["user"]
        self.assertEqual(user["role"], "org_admin")
        self.assertEqual(user["email"], "owner@example.com")
        self.assertTrue(user["org_id"].startswith("org-"))

        me = client.get("/api/me")  # complete 已种会话，同一客户端应携带 cookie
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], user["username"])

        with app_module.store.connect() as db:
            quota = db.execute("SELECT seats FROM org_quota WHERE org_id=?", (user["org_id"],)).fetchone()
        self.assertEqual(quota["seats"], 20)

        login = self._client().post("/api/login", json={
            "username": user["username"], "password": "founder-pass-2026"})
        self.assertEqual(login.status_code, 200)

    def test_registered_email_is_told_to_login(self):
        self._setup_platform_admin()
        invite = self._create_invite(self._admin_client(), "owner@example.com")
        self._start(self._client(), "owner@example.com")
        done = self._client().post("/api/register/complete", json={
            "email": "owner@example.com", "code": self._code_from_last_mail(),
            "invite_code": invite["code"], "password": "founder-pass-2026"})
        self.assertEqual(done.status_code, 200, done.text)
        before = len(self.sent)
        again = self._start(self._client(), "owner@example.com")
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()["exists"])  # 已注册：提示直接登录，不发信
        self.assertEqual(len(self.sent), before)

    def test_code_attempts_exhausted_after_five_failures(self):
        self._setup_platform_admin()
        self._create_invite(self._admin_client(), "owner@example.com")
        self._start(self._client(), "owner@example.com")
        for attempt in range(4):
            wrong = self._client().post("/api/register/complete", json={
                "email": "owner@example.com", "code": "000000" if attempt < 4 else "000000",
                "invite_code": "AAAA2222BBBB", "password": "founder-pass-2026"})
            self.assertEqual(wrong.status_code, 422)
        fifth = self._client().post("/api/register/complete", json={
            "email": "owner@example.com", "code": "000000",
            "invite_code": "AAAA2222BBBB", "password": "founder-pass-2026"})
        self.assertIn("重新获取", fifth.json()["detail"])
        # 即便第 5 次输对，令牌也已作废
        real_code = self._code_from_last_mail()
        invalidated = self._client().post("/api/register/complete", json={
            "email": "owner@example.com", "code": real_code,
            "invite_code": "AAAA2222BBBB", "password": "founder-pass-2026"})
        self.assertEqual(invalidated.status_code, 422)

    def test_invite_code_reuse_and_mismatch_rejected(self):
        self._setup_platform_admin()
        invite = self._create_invite(self._admin_client(), "owner@example.com")
        self._start(self._client(), "owner@example.com")
        first = self._client().post("/api/register/complete", json={
            "email": "owner@example.com", "code": self._code_from_last_mail(),
            "invite_code": invite["code"], "password": "founder-pass-2026"})
        self.assertEqual(first.status_code, 200)

        # 同一码再来一次（即便换邮箱）→ 已被使用
        self._start(self._client(), "second@example.com")
        reuse = self._client().post("/api/register/complete", json={
            "email": "second@example.com", "code": self._code_from_last_mail(),
            "invite_code": invite["code"], "password": "founder-pass-2026"})
        self.assertEqual(reuse.status_code, 422)
        self.assertIn("已被使用", reuse.json()["detail"])

        # 绑定邮箱不匹配：给真实负责人签一张新码，但用他人邮箱验证 → 拒绝
        mismatch_invite = self._create_invite(self._admin_client(), "realowner@example.com", org_name="另一家")
        self._start(self._client(), "someone-else@example.com")
        mismatch = self._client().post("/api/register/complete", json={
            "email": "someone-else@example.com", "code": self._code_from_last_mail(),
            "invite_code": mismatch_invite["code"], "password": "founder-pass-2026"})
        self.assertEqual(mismatch.status_code, 422)
        self.assertIn("绑定", mismatch.json()["detail"])

    def _invite_code_for(self, bound_email: str) -> str:
        """测试辅助：为新场景补签一张创始码并返回明文（库里不存明文，无法反查）。"""
        return self._create_invite(self._admin_client(), bound_email, org_name="补签机构")["code"]

    def test_invalid_invite_code_rejected(self):
        self._setup_platform_admin()
        self._create_invite(self._admin_client(), "owner@example.com")
        self._start(self._client(), "owner@example.com")
        bad = self._client().post("/api/register/complete", json={
            "email": "owner@example.com", "code": self._code_from_last_mail(),
            "invite_code": "ZZZZ9999XXXX", "password": "founder-pass-2026"})
        self.assertEqual(bad.status_code, 422)

    def test_weak_password_rejected(self):
        self._setup_platform_admin()
        self._create_invite(self._admin_client(), "owner@example.com")
        self._start(self._client(), "owner@example.com")
        weak = self._client().post("/api/register/complete", json={
            "email": "owner@example.com", "code": self._code_from_last_mail(),
            "invite_code": self._invite_code_for("owner@example.com"), "password": "short"})
        self.assertEqual(weak.status_code, 422)

    def test_unbound_invite_any_email_can_register(self):
        """创始码只捆机构名（不绑邮箱）：任意邮箱凭码 + 邮箱验证码即可注册。

        安全性由「码一次性 + 注册侧邮箱验证码」双因子兜底；台账须能看见注册人。
        """
        self._setup_platform_admin()
        admin = self._admin_client()
        result = admin.post("/api/invites", json={"org_name": "松山湖大学"})
        self.assertEqual(result.status_code, 200, result.text)
        invite = result.json()
        self.assertEqual(invite["bound_email"], "")  # 不绑邮箱
        self.assertEqual(invite["seats"], 5)  # 默认小型 5 席

        client = self._client()
        self._start(client, "dean@example.edu")
        done = client.post("/api/register/complete", json={
            "email": "dean@example.edu", "code": self._code_from_last_mail(),
            "invite_code": invite["code"], "password": "founder-pass-2026"})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["user"]["role"], "org_admin")
        with app_module.store.connect() as db:
            quota = db.execute("SELECT seats FROM org_quota WHERE org_id=?",
                               (done.json()["org_id"],)).fetchone()
        self.assertEqual(quota["seats"], 5)

        # 一码一位：第二个邮箱再来 → 已被使用
        self._start(self._client(), "late@example.edu")
        reuse = self._client().post("/api/register/complete", json={
            "email": "late@example.edu", "code": self._code_from_last_mail(),
            "invite_code": invite["code"], "password": "founder-pass-2026"})
        self.assertEqual(reuse.status_code, 422)
        self.assertIn("已被使用", reuse.json()["detail"])

        # 台账：平台管理员能看见谁注册了
        ledger = admin.get("/api/invites")
        self.assertEqual(ledger.status_code, 200)
        row = next(r for r in ledger.json() if r["org_name"] == "松山湖大学")
        self.assertEqual(row["seats"], 5)
        self.assertEqual(row["redeemed_email"], "dean@example.edu")
        self.assertTrue(row["redeemed_by"])
        self.assertTrue(row["redeemed_at"])

    def test_register_code_rate_limited(self):
        self._setup_platform_admin()
        app_module.register_code_limiter = RateLimiter({"email": (1, 15 * 60), "ip": (500, 60 * 60)})
        self._start(self._client(), "owner@example.com")
        second = self._start(self._client(), "owner@example.com")
        self.assertEqual(second.status_code, 429)


if __name__ == "__main__":
    unittest.main()
