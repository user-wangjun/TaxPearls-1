from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import engine, loader, mailer, render
from webapp import app as app_module
from webapp.notifications import deliver_pending, email_content, safe_summary
from webapp.storage import Store
from webapp.access import AccessDenied

ROOT = Path(__file__).resolve().parents[1]


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"TAXPEARLS_NOTIFICATION_EMAIL_ENABLED": "0"})
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_store = app_module.store
        self.store = Store(Path(self.tmp.name) / "notifications.db")
        app_module.store = self.store
        self.password = "Notify-pass-2026!"
        self.admin = self.store.create_user("noticeadmin", self.password, "管理员", "org_admin", "org-a", "admin@example.test")
        self.accountant = self.store.create_user("noticeacct", self.password, "会计", "accountant", "org-a", "acct@example.test")
        self.other = self.store.create_user("noticeother", self.password, "其他机构", "org_admin", "org-b", "other@example.test")
        self.student = self.store.create_user("noticestudent", self.password, "学生", "student", "org-a")
        self.dataset = loader.load(ROOT / "samples" / "样例企业-审计材料.xlsx")
        self.client = self.store.upsert_client(self.admin, self.dataset.company.name, self.dataset.company.taxpayer_id, self.accountant["id"])

    def tearDown(self):
        app_module.store = self.old_store
        self.tmp.cleanup()
        self.env.stop()

    def test_preferences_and_queue_cancellation_roll_back_when_log_fails(self):
        user = self.accountant
        self.store.set_notification_preferences(user, user['id'], True, False, True)
        app_module._save_audit(self.dataset, self.admin, self.client['id'])
        before = self.store.notification_preferences(user['id'])
        with self.store.connect() as db:
            queued = [tuple(r) for r in db.execute('SELECT * FROM notification_deliveries')]
        with patch('webapp.members.log', side_effect=RuntimeError('local log failure')):
            with self.assertRaises(RuntimeError):
                self.store.set_notification_preferences(user, user['id'], False, False, False)
        self.assertEqual(self.store.notification_preferences(user['id']), before)
        with self.store.connect() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM notification_deliveries')], queued)

    def test_retry_and_frozen_delivery_roll_back_when_log_fails(self):
        user = self.accountant
        self.store.set_notification_preferences(user, user['id'], True, False, True)
        app_module._save_audit(self.dataset, self.admin, self.client['id'])
        def unknown(**kwargs):
            raise mailer.MailError('local failure')
        deliver_pending(self.store, sender=unknown)
        notice = self.store.list_notifications(user)[0]
        with self.store.connect() as db:
            before = tuple(db.execute('SELECT * FROM notification_deliveries').fetchone())
        with patch('webapp.members.log', side_effect=RuntimeError('local log failure')):
            with self.assertRaises(RuntimeError):
                self.store.retry_notification_delivery(user, notice['id'])
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM notification_deliveries').fetchone()), before)

    def test_recipient_list_uses_current_actor_and_hides_private_addresses(self):
        self.store.set_notification_preferences(self.accountant, self.accountant['id'], True, False, True)
        rows = self.store.list_notification_recipients(self.admin)
        self.assertEqual({r['id'] for r in rows}, {self.admin['id'], self.accountant['id']})
        self.assertTrue(next(r for r in rows if r['id']==self.accountant['id'])['email_enabled'])
        self.assertNotIn('@', json.dumps(rows))
        with self.assertRaises(AccessDenied):
            self.store.list_notification_recipients(self.accountant)
        with self.store.connect() as db:
            db.execute('UPDATE users SET org_id=? WHERE id=?', ('org-b', self.admin['id']))
        with self.assertRaises(AccessDenied):
            self.store.list_notification_recipients(self.admin)

    def test_teacher_notifications_filter_at_enqueue_read_retry_and_delivery(self):
        teachers=[self.store.create_user('notice-teacher-'+str(i),self.password,'教师'+str(i),'teacher','org-a',
                                       'teacher'+str(i)+'@example.test') for i in range(2)]
        for teacher in teachers:
            self.store.set_notification_preferences(teacher,teacher['id'],True,False,True)
        own=app_module._save_audit(self.dataset,teachers[0])['audit_id']
        other=app_module._save_audit(self.dataset,teachers[1])['audit_id']
        business=app_module._save_audit(self.dataset,self.admin,self.client['id'])['audit_id']
        self.assertEqual([r['audit_id'] for r in self.store.list_notifications(teachers[0])],[own])
        self.assertEqual([r['audit_id'] for r in self.store.list_notifications(teachers[1])],[other])
        notice=self.store.list_notifications(teachers[0])[0]
        # Simulate previously queued same-org notifications. No data is deleted;
        # access and delivery must be re-evaluated under the new policy.
        with self.store.connect() as db:
            for suffix,audit_id in [('other',other),('business',business)]:
                key='old-shared-'+suffix
                db.execute("""INSERT INTO notifications SELECT ?,user_id,org_id,?,event,summary_json,created_at,NULL
                              FROM notifications WHERE id=?""",(key,audit_id,notice['id']))
                db.execute("""INSERT INTO notification_deliveries(notification_id,recipient_email,created_at,updated_at,status)
                              SELECT ?,recipient_email,created_at,updated_at,'failed' FROM notification_deliveries
                              WHERE notification_id=?""",(key,notice['id']))
        with TestClient(app_module.app) as client:
            client.cookies.set(app_module.COOKIE_NAME,self.store.authenticate(teachers[0]['username'],self.password)[1])
            self.assertEqual([r['audit_id'] for r in client.get('/api/notifications').json()],[own])
            for key in ['old-shared-other','old-shared-business']:
                self.assertEqual(client.put('/api/notifications/'+key+'/read').status_code,404)
                self.assertEqual(client.post('/api/notifications/'+key+'/retry').status_code,404)
            self.assertEqual(client.put('/api/notifications/'+notice['id']+'/read').status_code,200)
        with self.store.connect() as db:
            db.execute("UPDATE notification_deliveries SET status='pending' WHERE notification_id LIKE 'old-shared-%'")
        sent=[]
        def sender(**kwargs):
            sent.append(kwargs)
            return 'local-teacher-delivery'
        deliver_pending(self.store,sender=sender)
        self.assertEqual(len(sent),2)
        with self.store.connect() as db:
            for key in ['old-shared-other','old-shared-business']:
                self.assertEqual(db.execute('SELECT status FROM notification_deliveries WHERE notification_id=?',(key,)).fetchone()[0],'suppressed')
                self.assertIsNone(db.execute('SELECT read_at FROM notifications WHERE id=?',(key,)).fetchone()[0])

    def test_stale_teacher_notification_actor_is_rejected_before_mutation(self):
        teacher=self.store.create_user('stale-teacher',self.password,'失效教师','teacher','org-a','stale@example.test')
        self.store.set_notification_preferences(teacher,teacher['id'],True,False,True)
        app_module._save_audit(self.dataset,teacher)
        notice=self.store.list_notifications(teacher)[0]
        with self.store.connect() as db:
            db.execute('UPDATE users SET active=0 WHERE id=?',(teacher['id'],))
        for operation in [lambda:self.store.list_notifications(teacher),
                          lambda:self.store.mark_notification_read(teacher,notice['id']),
                          lambda:self.store.retry_notification_delivery(teacher,notice['id']),
                          lambda:self.store.set_notification_preferences(teacher,teacher['id'],False,False,False)]:
            with self.assertRaises(AccessDenied):operation()
        self.assertIsNone(self.store.claim_notification_delivery())
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT read_at FROM notifications WHERE id=?',(notice['id'],)).fetchone()[0])
            self.assertEqual(db.execute('SELECT email_enabled FROM notification_preferences WHERE user_id=?',(teacher['id'],)).fetchone()[0],1)

    def test_local_http_worker_drains_restart_queue_and_never_resends_accepted(self):
        received = []
        class Sink(BaseHTTPRequestHandler):
            def do_POST(inner):
                received.append((dict(inner.headers), json.loads(inner.rfile.read(int(inner.headers["Content-Length"])))))
                inner.send_response(200)
                inner.send_header("Content-Type", "application/json")
                inner.end_headers()
                inner.wfile.write(b'{"id":"local-accepted"}')
            def log_message(self, *args):
                pass
        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        thread = Thread(target=sink.serve_forever, daemon=True); thread.start()
        try:
            self.store.set_notification_preferences(self.accountant, self.accountant["id"], True, False, True)
            app_module._save_audit(self.dataset, self.admin, self.client["id"])
            app_module.store = Store(self.store.path)
            with patch.dict("os.environ", {"TAXPEARLS_NOTIFICATION_EMAIL_ENABLED":"1", "TAXPEARLS_RESEND_API_KEY":"dummy-local-test"}), patch.object(mailer, "API_ENDPOINT", f"http://127.0.0.1:{sink.server_port}/emails"):
                with TestClient(app_module.app):
                    deadline = time.monotonic() + 5
                    while self.store.list_notifications(self.accountant)[0]["email_status"] != "accepted" and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertEqual(self.store.list_notifications(self.accountant)[0]["email_status"], "accepted")
                with TestClient(app_module.app):
                    self.assertEqual(self.store.list_notifications(self.accountant)[0]["email_status"], "accepted")
            self.assertEqual(len(received), 1)
            headers, payload = received[0]
            self.assertTrue(headers["Idempotency-Key"].startswith("audit-notification/"))
            self.assertNotIn(self.dataset.company.taxpayer_id, json.dumps(payload, ensure_ascii=False))
            self.assertEqual(headers["Authorization"], "Bearer dummy-local-test")
        finally:
            sink.shutdown(); sink.server_close(); thread.join(timeout=2)

    def test_manual_retry_freezes_payload_and_limits_attempts(self):
        self.store.set_notification_preferences(self.accountant, self.accountant["id"], True, False, True)
        app_module._save_audit(self.dataset, self.admin, self.client["id"])
        calls = []
        def unknown(**kwargs):
            calls.append(kwargs)
            raise mailer.MailError("do not log bearer or private body")
        deliver_pending(self.store, sender=unknown)
        notice = self.store.list_notifications(self.accountant)[0]
        with TestClient(app_module.app) as client:
            _, token = self.store.authenticate(self.accountant["username"], self.password)
            client.cookies.set(app_module.COOKIE_NAME, token)
            for _ in range(2):
                self.assertEqual(client.post(f"/api/notifications/{notice['id']}/retry").status_code, 200)
                with patch.dict("os.environ", {"TAXPEARLS_EMAIL_FROM_NAME":"changed-after-enqueue"}):
                    deliver_pending(self.store, sender=unknown)
            self.assertEqual(client.post(f"/api/notifications/{notice['id']}/retry").status_code, 409)
            _, token = self.store.authenticate(self.other["username"], self.password)
            client.cookies.set(app_module.COOKIE_NAME, token)
            self.assertEqual(client.post(f"/api/notifications/{notice['id']}/retry").status_code, 404)
        self.assertEqual(calls, [calls[0]] * 3)
        self.assertEqual(deliver_pending(self.store, sender=unknown), 0)

    def test_expired_retry_and_disabled_worker(self):
        self.store.set_notification_preferences(self.accountant, self.accountant["id"], True, False, True)
        app_module._save_audit(self.dataset, self.admin, self.client["id"])
        with patch.object(mailer, "send_email") as send:
            with TestClient(app_module.app):
                send.assert_not_called()
        claim = self.store.claim_notification_delivery()
        self.store.finish_notification_delivery(claim["notification_id"], claim["claim_token"], "failed", error_code="provider_rejected")
        with self.store.connect() as db:
            db.execute("UPDATE notification_deliveries SET created_at='2020-01-01T00:00:00+00:00'")
        with self.assertRaisesRegex(ValueError, "23 小时"):
            self.store.retry_notification_delivery(self.accountant, claim["notification_id"])

    def test_provider_rejection_then_unsubscribe_cancels_retry(self):
        self.store.set_notification_preferences(self.accountant, self.accountant["id"], True, False, True)
        app_module._save_audit(self.dataset, self.admin, self.client["id"])
        def reject(**kwargs):
            raise mailer.MailError("private rejection body", status=422)
        deliver_pending(self.store, sender=reject)
        notice = self.store.list_notifications(self.accountant)[0]
        self.assertEqual(notice["email_status"], "failed")
        self.assertTrue(self.store.retry_notification_delivery(self.accountant, notice["id"]))
        self.store.set_notification_preferences(self.accountant, self.accountant["id"], True, False, False)
        with patch.object(mailer, "send_email") as send:
            self.assertEqual(deliver_pending(self.store), 0)
            send.assert_not_called()
        self.assertEqual(self.store.list_notifications(self.accountant)[0]["email_status"], "suppressed")

    def test_delivery_payload_column_migrates_existing_database(self):
        with self.store.connect() as db:
            db.execute("ALTER TABLE notification_deliveries DROP COLUMN payload_json")
        migrated = Store(self.store.path)
        with migrated.connect() as db:
            self.assertIn("payload_json", {row["name"] for row in db.execute("PRAGMA table_info(notification_deliveries)")})

    def test_provider_redirect_is_not_followed(self):
        paths = []
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(inner):
                paths.append(inner.path)
                # Drain the POST body before closing: otherwise Windows can reset
                # the socket instead of delivering this intentional 307 response.
                inner.rfile.read(int(inner.headers.get("Content-Length", "0")))
                inner.send_response(307)
                inner.send_header("Location", f"http://127.0.0.1:{inner.server.server_port}/leak")
                inner.send_header("Content-Length", "0")
                inner.end_headers()
            def do_GET(inner):
                paths.append(inner.path)
                inner.send_response(200); inner.end_headers()
            def log_message(self, *args):
                pass
        sink = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        thread = Thread(target=sink.serve_forever, daemon=True); thread.start()
        try:
            with patch.dict("os.environ", {"TAXPEARLS_RESEND_API_KEY":"dummy-local-test"}), patch.object(mailer,"API_ENDPOINT",f"http://127.0.0.1:{sink.server_port}/emails"):
                with self.assertRaises(mailer.MailError) as raised:
                    mailer.send_email(to="recipient@example.test", subject="local", html="local")
                self.assertEqual(raised.exception.status, 307)
            self.assertEqual(paths, ["/emails"])
        finally:
            sink.shutdown(); sink.server_close(); thread.join(timeout=2)

    def test_audit_atomic_notifications_summary_only_and_unsubscribe(self):
        for user in (self.admin, self.accountant, self.other):
            self.store.set_notification_preferences(user, user["id"], True, True, True)
        result = app_module._save_audit(self.dataset, self.admin, self.client["id"])
        messages = self.store.list_notifications(self.accountant)
        self.assertEqual({item["event"] for item in messages}, {"audit_completed", "high_risk"})
        self.assertTrue(all(item["email_status"] == "pending" for item in messages))
        self.assertEqual(self.store.list_notifications(self.other), [])
        payload = json.dumps([item["summary"] for item in messages], ensure_ascii=False)
        for private_value in (self.dataset.company.name, self.dataset.company.taxpayer_id, "1,280,000", "964,000", "科目余额表", "calculation", "measured"):
            self.assertNotIn(private_value, payload)
        subject, html, text = email_content(messages[0]["summary"])
        self.assertIn("税海拾珠", subject)
        self.assertNotIn(self.dataset.company.taxpayer_id, html + text)
        self.store.set_notification_preferences(self.accountant, self.accountant["id"], False, False, False)
        self.assertTrue(all(item["email_status"] == "suppressed" for item in self.store.list_notifications(self.accountant)))
        app_module._save_audit(self.dataset, self.admin, self.client["id"])
        self.assertEqual(len(self.store.list_notifications(self.accountant)), 2)
        self.assertIsNotNone(self.store.get_audit(result["audit_id"]))

    def test_preferences_consent_api_permissions_and_read_ownership(self):
        with TestClient(app_module.app) as client:
            def as_user(user):
                _, token = self.store.authenticate(user["username"], self.password)
                client.cookies.clear()
                client.cookies.set(app_module.COOKIE_NAME, token)

            as_user(self.admin)
            forced = client.put(f"/api/notifications/recipients/{self.accountant['id']}", json={"audit_completed": True, "email_enabled": True})
            self.assertEqual(forced.status_code, 403, forced.text)
            assigned = client.put(f"/api/notifications/recipients/{self.accountant['id']}", json={"audit_completed": True})
            self.assertEqual(assigned.status_code, 200, assigned.text)
            forbidden = client.put(f"/api/notifications/recipients/{self.other['id']}", json={"audit_completed": True})
            self.assertEqual(forbidden.status_code, 403, forbidden.text)
            result = app_module._save_audit(self.dataset, self.admin, self.client["id"])
            as_user(self.accountant)
            self.assertTrue(client.get("/api/notifications/preferences").json()["has_email"])
            self.assertEqual(client.put("/api/notifications/preferences", json={"audit_completed":True,"email_enabled":True}).status_code, 200)
            rows = client.get("/api/notifications").json()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["audit_id"], result["audit_id"])
            self.assertEqual(client.put(f"/api/notifications/{rows[0]['id']}/read").status_code, 200)
            self.assertIsNotNone(client.get("/api/notifications").json()[0]["read_at"])
            as_user(self.other)
            self.assertEqual(client.put(f"/api/notifications/{rows[0]['id']}/read").status_code, 404)
            as_user(self.student)
            self.assertEqual(client.get("/api/notifications").status_code, 403)
            self.assertEqual(client.put("/api/notifications/preferences", json={"audit_completed": True}).status_code, 403)

    def test_reassigned_accountant_cannot_read_old_notification(self):
        self.store.set_notification_preferences(self.accountant, self.accountant["id"], True, False, True)
        app_module._save_audit(self.dataset, self.admin, self.client["id"])
        notice = self.store.list_notifications(self.accountant)[0]
        with self.store.connect() as db:
            db.execute("UPDATE clients SET accountant_id=NULL WHERE id=?", (self.client["id"],))
        self.assertEqual(self.store.list_notifications(self.accountant), [])
        self.assertFalse(self.store.mark_notification_read(self.accountant, notice["id"]))
        self.assertIsNone(self.store.claim_notification_delivery())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM notification_deliveries").fetchone()["status"], "suppressed")

    def test_delivery_claim_receipt_and_interrupted_send_are_not_duplicated(self):
        self.store.set_notification_preferences(self.accountant, self.accountant["id"], True, False, True)
        app_module._save_audit(self.dataset, self.admin, self.client["id"])
        another = Store(self.store.path)
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda target: target.claim_notification_delivery(), [self.store, another]))
        self.assertEqual(sum(item is not None for item in claims), 1)
        claim = next(item for item in claims if item is not None)
        self.assertFalse(self.store.finish_notification_delivery(claim["notification_id"], "wrong-token", "accepted", "test-provider-id"))
        self.assertTrue(self.store.finish_notification_delivery(claim["notification_id"], claim["claim_token"], "accepted", "test-provider-id"))
        self.assertIsNone(self.store.claim_notification_delivery())
        app_module._save_audit(self.dataset, self.admin, self.client["id"])
        interrupted = self.store.claim_notification_delivery()
        with self.store.connect() as db:
            db.execute("UPDATE notification_deliveries SET claimed_at='2020-01-01T00:00:00+00:00' WHERE notification_id=?", (interrupted["notification_id"],))
        self.assertEqual(self.store.recover_notification_claims(), 1)
        self.assertIsNone(self.store.claim_notification_delivery())
        self.assertIn("uncertain", {item["email_status"] for item in self.store.list_notifications(self.accountant)})

    def test_high_risk_only_subscription_ignores_ordinary_hits(self):
        self.store.set_notification_preferences(self.accountant, self.accountant["id"], False, True, True)
        findings = engine.run(engine.load_rules(ROOT / "rules"), self.dataset)
        for item in findings:
            item.rule.severity = "low"
        summary = render.build_view_model(self.dataset, findings)["summary"]
        self.store.save_audit("ordinary-risk", self.admin, self.client["id"], self.dataset, findings, summary, "2026-09-26")
        self.assertEqual(self.store.list_notifications(self.accountant), [])
        self.assertIsNone(self.store.claim_notification_delivery())

    def test_mail_adapter_carries_stable_idempotency_key_without_attachments(self):
        findings = engine.run(engine.load_rules(ROOT / "rules"), self.dataset)
        subject, html, text = email_content(safe_summary("safe-id", findings, "audit_completed", "2026-09-26"))
        with patch.dict("os.environ", {"TAXPEARLS_RESEND_API_KEY": "dummy-local-test"}), patch.object(mailer, "_open_request", return_value=BytesIO(b'{"id":"local-mail-id"}')) as send:
            result = mailer.send_email(to="recipient@example.test", subject=subject, html=html, text=text,
                                       idempotency_key="audit-notice/safe-id")
            self.assertEqual(result, "local-mail-id")
            request = send.call_args.args[0]
            self.assertEqual(request.get_header("Idempotency-key"), "audit-notice/safe-id")
            payload = json.loads(request.data)
            self.assertNotIn("attachments", payload)
            self.assertNotIn(self.dataset.company.taxpayer_id, json.dumps(payload, ensure_ascii=False))

    def test_notification_failure_rolls_back_audit_transaction(self):
        findings = engine.run(engine.load_rules(ROOT / "rules"), self.dataset)
        summary = render.build_view_model(self.dataset, findings)["summary"]
        with patch.object(self.store, "_enqueue_audit_notifications", side_effect=RuntimeError("test rollback")):
            with self.assertRaisesRegex(RuntimeError, "test rollback"):
                self.store.save_audit("rollback-audit", self.admin, self.client["id"], self.dataset, findings, summary, "2026-09-26")
        self.assertIsNone(self.store.get_audit("rollback-audit"))

    def test_email_html_escapes_rule_names_and_ignores_unapproved_fields(self):
        findings = engine.run(engine.load_rules(ROOT / "rules"), self.dataset)
        hit = next(item for item in findings if item.status == "hit")
        hit.rule.name = '<img src=x onerror="alert(1)">'
        summary = safe_summary("safe-id", findings, "audit_completed", "2026-09-26")
        summary["raw_financial_data"] = "PRIVATE-RAW"
        _, html, text = email_content(summary)
        self.assertIn("&lt;img", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("PRIVATE-RAW", html + text)


if __name__ == "__main__":
    unittest.main()
