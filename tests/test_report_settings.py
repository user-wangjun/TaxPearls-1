from __future__ import annotations

import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from src import render
from webapp import app as app_module
from webapp.storage import Store
from tests.enterprise_support import audit as enterprise_audit, material_key


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "样例企业-审计材料.xlsx"


def png(width: int = 160, height: int = 64, color=(20, 96, 170, 255)) -> bytes:
    output = BytesIO()
    Image.new("RGBA", (width, height), color).save(output, format="PNG")
    return output.getvalue()


@material_key
class ReportTemplateSettingsTests(unittest.TestCase):
    def test_stale_actor_cannot_read_or_mutate_branding(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'branding.db')
            actor = store.create_user('branding-admin', 'Branding-test-2026!', '管理员', 'org_admin', 'alpha')
            store.update_org_settings('alpha', '原机构', '原标题', '原页脚')
            store.update_org_logo('alpha', 'image/png', png())
            original_settings, original_logo = store.get_org_settings('alpha'), store.get_org_logo('alpha')
            with patch.object(app_module, 'store', store), patch.object(app_module, '_user', return_value=actor), TestClient(app_module.app) as client:
                for changes in ({'active': 0}, {'org_id': 'beta'}, {'role': 'accountant'}):
                    with store.connect() as db:
                        db.execute("UPDATE users SET active=1,org_id='alpha',role='org_admin' WHERE id=?", (actor['id'],))
                        for key, value in changes.items():
                            db.execute(f'UPDATE users SET {key}=? WHERE id=?', (value, actor['id']))
                    requests = [
                        lambda: client.get('/api/org/settings'), lambda: client.get('/api/org/logo'),
                        lambda: client.put('/api/org/settings', json={'display_name':'被改写','report_title':'被改写','footer_text':''}),
                        lambda: client.post('/api/org/logo', files={'file':('logo.png',png(color=(200,0,0,255)),'image/png')}),
                        lambda: client.delete('/api/org/logo'),
                    ]
                    for request in requests:
                        with self.subTest(changes=changes, request=request):
                            self.assertEqual(request().status_code, 403)
                    self.assertEqual(store.get_org_settings('alpha'), original_settings)
                    self.assertEqual(store.get_org_logo('alpha'), original_logo)

    def test_branding_log_failure_rolls_back_every_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'branding.db')
            actor = store.create_user('branding-admin', 'Branding-test-2026!', '管理员', 'org_admin', 'alpha')
            store.update_org_settings('alpha', '原机构', '原标题', '原页脚')
            store.update_org_logo('alpha', 'image/png', png())
            original_settings, original_logo = store.get_org_settings('alpha'), store.get_org_logo('alpha')
            with store.connect() as db:
                db.execute("CREATE TRIGGER fail_brand_log BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT,'test audit log failure'); END")
            with patch.object(app_module, 'store', store), patch.object(app_module, '_user', return_value=actor), TestClient(app_module.app, raise_server_exceptions=False) as client:
                requests = [
                    lambda: client.put('/api/org/settings', json={'display_name':'被改写','report_title':'被改写','footer_text':''}),
                    lambda: client.post('/api/org/logo', files={'file':('logo.png',png(color=(200,0,0,255)),'image/png')}),
                    lambda: client.delete('/api/org/logo'),
                ]
                for request in requests:
                    self.assertEqual(request().status_code, 500)
                    self.assertEqual(store.get_org_settings('alpha'), original_settings)
                    self.assertEqual(store.get_org_logo('alpha'), original_logo)

    def test_old_database_gets_logo_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            with closing(sqlite3.connect(path)) as db:
                db.execute(
                    """CREATE TABLE org_settings (
                        org_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                        report_title TEXT NOT NULL, footer_text TEXT NOT NULL, updated_at TEXT NOT NULL
                    )"""
                )
            Store(path)
            with closing(sqlite3.connect(path)) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(org_settings)")}
            self.assertTrue({"logo_mime", "logo_bytes", "logo_updated_at"} <= columns)

    def test_template_ui_api_logo_security_pdf_and_org_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "report-settings.db"
            old_store = app_module.store
            app_module.store = Store(db_path)
            try:
                with TestClient(app_module.app) as client:
                    self.assertEqual(client.get("/api/org/settings").status_code, 401)
                    self.assertEqual(client.post("/api/setup", json={
                        "username": "rootadmin", "password": "platform-pass-2026",
                        "display_name": "平台管理员", "org_id": "org-a",
                    }).status_code, 200)
                    self.assertEqual(client.post("/api/login", json={
                        "username": "rootadmin", "password": "platform-pass-2026",
                    }).status_code, 200)
                    for username, role, org_id in (
                        ("admina", "org_admin", "org-a"),
                        ("adminb", "org_admin", "org-b"),
                        ("studentb", "student", "org-b"),
                    ):
                        app_module.store.create_user(username, f"{username}-pass-2026", username, role, org_id)

                    self.assertEqual(client.get('/api/org/settings').status_code, 403)
                    self.assertEqual(client.post('/api/login', json={'username':'admina','password':'admina-pass-2026'}).status_code, 200)
                    xss_settings = {
                        "display_name": "甲机构<script>alert(1)</script>",
                        "report_title": "风险报告<img src=x onerror=alert(2)>",
                        "footer_text": "<script>alert(3)</script> 仅供内部",
                    }
                    saved = client.put("/api/org/settings", json=xss_settings)
                    self.assertEqual(saved.status_code, 200, saved.text)
                    self.assertFalse(saved.json()["has_logo"])
                    self.assertEqual(client.put("/api/org/settings", json={
                        **xss_settings, "display_name": "X" * 81,
                    }).status_code, 422)

                    svg = client.post("/api/org/logo", files={
                        "file": ("active.svg", b'<svg onload="alert(1)"></svg>', "image/svg+xml")
                    })
                    self.assertEqual(svg.status_code, 422)
                    oversized = client.post("/api/org/logo", files={
                        "file": ("huge.png", b"x" * (512 * 1024 + 1), "image/png")
                    })
                    self.assertEqual(oversized.status_code, 422)
                    too_wide = client.post("/api/org/logo", files={
                        "file": ("wide.png", png(1201, 1), "image/png")
                    })
                    self.assertEqual(too_wide.status_code, 422)

                    uploaded = client.post("/api/org/logo", files={
                        "file": ("brand.png", png(), "image/png")
                    })
                    self.assertEqual(uploaded.status_code, 200, uploaded.text)
                    self.assertTrue(uploaded.json()["has_logo"])
                    with app_module.store.connect() as db:
                        for action in ('setup', 'update_org_settings', 'update_org_logo'):
                            self.assertEqual(db.execute('SELECT COUNT(*) FROM audit_log WHERE action=?', (action,)).fetchone()[0], 1)
                    logo = client.get("/api/org/logo")
                    self.assertEqual(logo.status_code, 200)
                    self.assertEqual(logo.headers["content-type"], "image/png")
                    self.assertEqual(logo.headers["x-content-type-options"], "nosniff")
                    self.assertIsNotNone(Store(db_path).get_org_logo("org-a"))

                    with SAMPLE.open("rb") as stream:
                        audit = enterprise_audit(client, files={
                            "file": (SAMPLE.name, stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                        })
                    self.assertEqual(audit.status_code, 200, audit.text)
                    audit_a = audit.json()["audit_id"]
                    html = client.get(f"/api/report/{audit_a}/html")
                    self.assertEqual(html.status_code, 200)
                    self.assertIn("data:image/png;base64,", html.text)
                    self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html.text)
                    self.assertNotIn("<script>alert(1)</script>", html.text)
                    self.assertNotIn("<img src=x onerror=alert(2)>", html.text)
                    self.assertNotIn("<script>alert(3)</script>", render._footer_template("TP-X", xss_settings["footer_text"]))

                    pdf = client.get(f"/api/report/{audit_a}")
                    self.assertEqual(pdf.status_code, 200, pdf.text)
                    self.assertTrue(pdf.content.startswith(b"%PDF"))
                    self.assertRegex(pdf.content, rb"/Subtype\s*/Image")

                    client.post("/api/logout")
                    self.assertEqual(client.post("/api/login", json={
                        "username": "adminb", "password": "adminb-pass-2026",
                    }).status_code, 200)
                    settings_b = client.get("/api/org/settings")
                    self.assertEqual(settings_b.status_code, 200)
                    self.assertEqual(settings_b.json()["display_name"], "税海拾珠")
                    self.assertEqual(client.get("/api/org/logo").status_code, 404)
                    self.assertEqual(client.put("/api/org/settings", json={
                        "display_name": "乙机构", "report_title": "乙机构审计报告", "footer_text": "乙方内部",
                    }).status_code, 200)
                    with SAMPLE.open("rb") as stream:
                        audit = enterprise_audit(client, files={
                            "file": (SAMPLE.name, stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                        })
                    self.assertEqual(audit.status_code, 200, audit.text)
                    audit_b = audit.json()["audit_id"]
                    report_b = client.get(f"/api/report/{audit_b}/html")
                    self.assertEqual(report_b.status_code, 200)
                    self.assertIn("乙机构审计报告", report_b.text)
                    self.assertNotIn("甲机构&lt;script&gt;", report_b.text)
                    client.post("/api/logout")

                    self.assertEqual(client.post("/api/login", json={
                        "username": "studentb", "password": "studentb-pass-2026",
                    }).status_code, 200)
                    self.assertEqual(client.put("/api/org/settings", json={
                        "display_name": "越权", "report_title": "越权", "footer_text": "",
                    }).status_code, 403)
                    self.assertEqual(client.post("/api/org/logo", files={
                        "file": ("brand.png", png(), "image/png")
                    }).status_code, 403)
                    client.post("/api/logout")

                    self.assertEqual(client.post("/api/login", json={
                        "username": "rootadmin", "password": "platform-pass-2026",
                    }).status_code, 200)
                    report_b = client.get(f"/api/report/{audit_b}/html")
                    self.assertEqual(report_b.status_code, 403)
                    self.assertEqual(client.get(f"/api/report/{audit_a}/html").status_code, 403)
                    self.assertEqual(client.get('/api/org/logo').status_code, 403)
                    self.assertEqual(client.post('/api/login', json={'username':'admina','password':'admina-pass-2026'}).status_code, 200)
                    self.assertEqual(client.get(f"/api/report/{audit_a}/html").status_code, 200)
                    self.assertEqual(client.get("/api/org/logo").status_code, 200)
                    deleted = client.delete("/api/org/logo")
                    self.assertEqual(deleted.status_code, 200)
                    self.assertFalse(deleted.json()["has_logo"])
                    self.assertEqual(client.get("/api/org/logo").status_code, 404)
            finally:
                app_module.store = old_store


if __name__ == "__main__":
    unittest.main()
