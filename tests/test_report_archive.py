from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import engine, loader, render
from webapp import app as module
from webapp.report_archive import build_snapshot, digest
from webapp.storage import Store

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples/样例企业-审计材料.xlsx"


class ReportArchiveTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TAXPEARLS_NOTIFICATION_EMAIL_ENABLED": "0"}); self.env.start()
        self.tmp = TemporaryDirectory()
        self.old_store = module.store
        self.store = Store(Path(self.tmp.name) / "archive.db"); module.store = self.store
        self.password = "Archive-test-2026!"
        self.admin = self.store.create_user("archiveadmin", self.password, "归档管理员", "org_admin", "org-a")
        self.data = loader.load(SAMPLE)
        self.findings = engine.run(engine.load_rules(ROOT / "rules"), self.data)
        self.summary = render.build_view_model(self.data, self.findings)["summary"]
        self.store.save_audit("legacy", self.admin, None, self.data, self.findings, self.summary, "2020-01-01 12:00:00")
        self.client = TestClient(module.app); self.client.__enter__()
        self.login(self.admin)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        module.store = self.old_store; self.tmp.cleanup(); self.env.stop()

    def login(self, user):
        _, token = self.store.authenticate(user["username"], self.password)
        self.client.cookies.clear(); self.client.cookies.set(module.COOKIE_NAME, token)

    def snapshot(self, audit_id="legacy"):
        return build_snapshot(self.store.get_audit(audit_id), module._org_branding("org-a"))

    def test_new_audit_auto_archive_and_atomic_failure(self):
        result = module._save_audit(self.data, self.admin)
        versions = self.store.report_versions(result["audit_id"])
        self.assertEqual([v["version"] for v in versions], [1])
        frozen_rules = self.store.get_audit(result["audit_id"])["findings"]
        self.assertEqual(len(versions[0]["manifest"]["rules"]), len(frozen_rules))
        self.assertIn("R-001", {r["id"] for r in versions[0]["manifest"]["rules"]})
        response = self.client.post(f"/api/audits/{result['audit_id']}/report-versions")
        self.assertEqual(response.json()["version"], 1)
        bad = self.snapshot(); bad["manifest"]["audit_id"] = "wrong"
        with self.assertRaises(ValueError):
            self.store.save_audit("rollback", self.admin, None, self.data, self.findings, self.summary,
                                  "2026-09-26 12:00:00", report_snapshot=bad)
        self.assertIsNone(self.store.get_audit("rollback"))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notifications WHERE audit_id='rollback'").fetchone()[0], 0)

    def test_revoked_exporter_cannot_persist_pdf_after_render(self):
        self.store.archive_report('legacy', self.admin, self.snapshot())
        def revoke_while_rendering(_html, output, **_kwargs):
            with self.store.connect() as db:
                db.execute('UPDATE users SET active=0 WHERE id=?', (self.admin['id'],))
            Path(output).write_bytes(b'%PDF-synthetic-render')
        with patch.object(render, 'export_pdf', side_effect=revoke_while_rendering):
            response = self.client.get('/api/report/legacy?version=1')
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIsNone(self.store.get_report_version('legacy', 1)['pdf_bytes'])

    def test_archive_log_failure_rolls_back_version_and_protection(self):
        url = '/api/audits/legacy/report-versions'
        with patch.object(self.store, '_log', side_effect=RuntimeError('audit log unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'audit log unavailable'):
                self.client.post(url)
        self.assertEqual(self.store.report_versions('legacy'), [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM report_protections').fetchone()[0], 0)
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(self.client.post(url).status_code, 200)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='archive_report'").fetchone()[0], 1)

    def test_legacy_first_archive_is_not_backdated_and_brand_changes_are_versions(self):
        self.assertEqual(self.client.get("/api/audits/legacy/report-versions").json(), [])
        first = self.client.post("/api/audits/legacy/report-versions").json()
        self.assertFalse(first["created_at"].startswith("2020"))
        original = self.client.get("/api/report/legacy/html?version=1")
        self.store.update_org_settings("org-a", "新版机构", "新版报告标题", "新版页脚")
        self.store.update_org_logo("org-a", "image/png", b"synthetic-logo-bytes")
        second = self.client.get("/api/report/legacy/html")
        self.assertEqual(second.headers["X-TaxPearls-Report-Version"], "2")
        self.assertIn("新版报告标题", second.text)
        self.assertNotEqual(second.content, original.content)
        self.assertEqual(self.client.get("/api/report/legacy/html?version=1").content, original.content)
        self.assertEqual(self.client.post("/api/audits/legacy/report-versions").json()["version"], 2)
        module.store = Store(self.store.path)
        with patch.object(engine, "load_rules", side_effect=AssertionError("no current YAML")), \
             patch.object(render, "render_html", side_effect=AssertionError("no current renderer")):
            self.assertEqual(self.client.get("/api/report/legacy/html?version=1").content, original.content)
        self.assertEqual(self.client.get("/api/report/legacy/html?version=999").status_code, 404)

    def test_template_and_narrative_changes_leave_old_html_untouched(self):
        original = self.client.get("/api/report/legacy/html").content
        template = (render.TEMPLATE_DIR / "report.html").read_text(encoding="utf-8")
        real_read = Path.read_text
        def changed_read(path, *args, **kwargs):
            return template + "<!-- template-revision -->" if path == render.TEMPLATE_DIR / "report.html" else real_read(path, *args, **kwargs)
        with patch.object(Path, "read_text", changed_read):
            changed = self.client.get("/api/report/legacy/html")
            self.assertEqual(changed.headers["X-TaxPearls-Report-Version"], "2")
            self.assertIn("template-revision", changed.text)
            self.assertEqual(self.client.get("/api/report/legacy/html?version=1").content, original)
        narrative = {"overall_assessment": [{"text": "已核对的测试叙述", "rule_ids": ["R-001"]}],
                     "recommendations": [], "summary": self.summary, "model": "test-model",
                     "evidence_hash": module.audit_narrative_hash(self.findings)}
        self.store.save_audit_narrative("legacy", narrative["evidence_hash"], narrative, self.admin)
        changed = self.client.get("/api/report/legacy/html")
        self.assertEqual(changed.headers["X-TaxPearls-Report-Version"], "3")
        self.assertIn("已核对的测试叙述", changed.text)
        self.assertEqual(self.client.get("/api/report/legacy/html?version=1").content, original)
        self.assertEqual(self.store.report_versions("legacy")[0]["manifest"]["narrative_model"], "test-model")

    def test_concurrent_dedup_version_allocation_and_pdf_first_writer_wins(self):
        other = Store(self.store.path); snapshot = self.snapshot()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda s: s.archive_report("legacy", self.admin, snapshot), [self.store, other]))
        self.assertEqual(sorted(results), [(1, False), (1, True)])
        snapshots = []
        for index in range(2):
            branding=module._org_branding("org-a");branding["report_title"]+=f" · variant-{index}"
            snapshots.append(build_snapshot(self.store.get_audit("legacy"),branding))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: pair[0].archive_report("legacy", self.admin, pair[1]), zip([self.store, other], snapshots)))
        self.assertEqual(sorted(results), [(2, True), (3, True)])
        with ThreadPoolExecutor(max_workers=2) as pool:
            pdfs = list(pool.map(lambda pair: pair[0].attach_report_pdf("legacy", 1, pair[1])["pdf_bytes"],
                                 [(self.store, b"%PDF-first"), (other, b"%PDF-second")]))
        self.assertEqual(pdfs[0], pdfs[1])
        self.assertIn(pdfs[0], [b"%PDF-first", b"%PDF-second"])
        with self.assertRaises(ValueError): self.store.attach_report_pdf("legacy", 1, b"invalid")

    def test_pdf_frozen_bytes_restart_and_failed_export_temp_cleanup(self):
        exported = []
        def fake_export(html, output, **kwargs):
            exported.append(html); Path(output).write_bytes(b"%PDF-immutable-test")
        with patch.object(render, "export_pdf", side_effect=fake_export):
            first = self.client.get("/api/report/legacy?version=1")
            self.assertEqual(first.status_code, 404)  # never invented explicit version
            first = self.client.get("/api/report/legacy")
            self.assertEqual(first.status_code, 200)
        self.assertEqual(len(exported), 1)
        self.assertEqual(first.headers["X-TaxPearls-SHA256"], digest(first.content))
        self.assertIn("-v1.pdf", first.headers["Content-Disposition"])
        module.store = Store(self.store.path)
        self.store.update_org_settings("org-a", "其他机构名", "新版报告", "其他页脚")
        with patch.object(render, "export_pdf", side_effect=AssertionError("old PDF must not be rerendered")):
            self.assertEqual(self.client.get("/api/report/legacy?version=1").content, first.content)
        output = Path(self.tmp.name) / "failed.pdf"
        fd = os.open(output, os.O_CREAT | os.O_RDWR)
        with patch.object(module.tempfile, "mkstemp", return_value=(fd, str(output))), \
             patch.object(render, "export_pdf", side_effect=RuntimeError("synthetic failure")):
            self.assertEqual(self.client.get("/api/report/legacy").status_code, 500)
        self.assertFalse(output.exists())
        self.assertIsNone(self.store.get_report_version("legacy", 2)["pdf_bytes"])

    def test_corruption_refuses_read_without_regeneration(self):
        self.store.archive_report("legacy", self.admin, self.snapshot())
        for column, bad in (("html", "tampered"), ("manifest_json", "{}"), ("pdf_bytes", b"%PDF-tampered")):
            if column == "pdf_bytes": self.store.attach_report_pdf("legacy", 1, b"%PDF-original")
            with self.store.connect() as db:
                original = db.execute(f"SELECT {column} FROM audit_report_versions WHERE audit_id='legacy'").fetchone()[0]
                db.execute(f"UPDATE audit_report_versions SET {column}=? WHERE audit_id='legacy'", (bad,))
            with patch.object(render, "export_pdf", side_effect=AssertionError("no regeneration")):
                self.assertEqual(self.client.get("/api/report/legacy/html?version=1").status_code, 409)
                self.assertEqual(self.client.get("/api/report/legacy?version=1").status_code, 409)
            with self.store.connect() as db:
                self.assertEqual(db.execute(f"SELECT {column} FROM audit_report_versions WHERE audit_id='legacy'").fetchone()[0], bad)
                db.execute(f"UPDATE audit_report_versions SET {column}=? WHERE audit_id='legacy'", (original,))

    def test_real_pdf_first_export_and_byte_identical_restart(self):
        import pypdfium2
        first = self.client.get("/api/report/legacy")
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.content.startswith(b"%PDF"))
        with pypdfium2.PdfDocument(first.content) as pdf:
            self.assertGreater(len(pdf), 0)
            page = pdf[0]
            try:
                width, height = page.get_size()
                self.assertAlmostEqual(width, 595.3, delta=2)
                self.assertAlmostEqual(height, 841.9, delta=2)
                textpage = page.get_textpage()
                try:
                    self.assertIn(self.data.company.name, textpage.get_text_range())
                finally:
                    textpage.close()
            finally:
                page.close()
        module.store = Store(self.store.path)
        with patch.object(render, "export_pdf", side_effect=AssertionError("must read archived PDF")):
            second = self.client.get("/api/report/legacy?version=1")
        self.assertEqual(first.content, second.content)
        self.assertEqual(first.headers["X-TaxPearls-SHA256"], digest(second.content))

    def test_search_literal_filters_pagination_and_validation(self):
        for index in range(5):
            data = deepcopy(self.data); data.company.name = f"归档企业_{index}%"; data.company.period = f"2026-0{index+1}"
            self.store.save_audit(f"search-{index}", self.admin, None, data, self.findings,
                                  {**self.summary, "hit": index, "high": index % 2}, f"2026-09-{index+1:02} 12:00:00")
        def query(**params): return self.client.get("/api/archive", params=params)
        self.assertEqual(query(q="%").json()["total"], 5)
        self.assertEqual(query(q="_2%").json()["total"], 1)
        self.assertEqual(query(q="' OR 1=1 --").json()["total"], 0)
        self.assertEqual(query(q=self.data.company.taxpayer_id).json()["total"], 6)
        self.assertEqual(query(q="search-2").json()["items"][0]["id"], "search-2")
        self.assertEqual(query(period="2026-03").json()["total"], 1)
        self.assertEqual(query(q="归档企业", risk="hit").json()["total"], 4)
        self.assertEqual(query(q="归档企业", risk="high").json()["total"], 2)
        self.assertEqual(query(date_from="2026-09-02", date_to="2026-09-04").json()["total"], 3)
        first, second = query(page_size=2).json(), query(page=2, page_size=2).json()
        self.assertEqual(first["total"], 6)
        self.assertFalse({r["id"] for r in first["items"]} & {r["id"] for r in second["items"]})
        for params in ({"date_from":"bad"}, {"date_from":"2026-10-01","date_to":"2026-01-01"},
                       {"page":0}, {"page_size":101}, {"risk":"unknown"}, {"q":"x"*121}):
            self.assertEqual(query(**params).status_code, 422)

    def test_permissions_and_reassignment_guard_all_archive_paths(self):
        accountant = self.store.create_user("archiveacct", self.password, "会计", "accountant", "org-a")
        student = self.store.create_user("archivestudent", self.password, "学生", "student", "org-a")
        outside = self.store.create_user("archiveoutside", self.password, "外部", "org_admin", "org-b")
        customer = self.store.upsert_client(self.admin, self.data.company.name, self.data.company.taxpayer_id, accountant["id"])
        with self.store.connect() as db: db.execute("UPDATE audits SET client_id=? WHERE id='legacy'", (customer["id"],))
        self.store.archive_report("legacy", self.admin, self.snapshot())
        self.login(accountant)
        self.assertEqual(self.client.get("/api/archive").json()["total"], 1)
        self.assertEqual(self.client.get("/api/report/legacy?version=1").status_code, 409)
        with patch.object(render, "export_pdf", side_effect=lambda html, path, **kw: Path(path).write_bytes(b"%PDF-ok")):
            self.assertEqual(self.client.get("/api/report/legacy?version=1&confirm=true").status_code, 200)
        with self.store.connect() as db: db.execute("UPDATE clients SET accountant_id=NULL WHERE id=?", (customer["id"],))
        for user, status in ((accountant, 404), (outside, 404), (student, 403)):
            self.login(user)
            if user != student: self.assertEqual(self.client.get("/api/archive").json()["total"], 0)
            else: self.assertEqual(self.client.get("/api/archive").status_code, 403)
            for method, path in (("get", "/api/audits/legacy/report-versions"), ("post", "/api/audits/legacy/report-versions"),
                                 ("get", "/api/report/legacy/html?version=1"), ("get", "/api/report/legacy?version=1&confirm=true")):
                self.assertEqual(getattr(self.client, method)(path).status_code, status)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/archive").status_code, 401)

    def test_old_database_migration_and_backup_restore_include_frozen_pdf(self):
        from scripts.ops_db import create_backup, restore_backup
        # Simulate a pre-D06 schema, without touching a user database.
        with self.store.connect() as db: db.execute("DROP TABLE audit_report_versions")
        migrated = Store(self.store.path)
        self.assertIsNotNone(migrated.get_audit("legacy"))
        self.assertEqual(migrated.report_versions("legacy"), [])
        migrated.archive_report("legacy", self.admin, self.snapshot())
        original = migrated.attach_report_pdf("legacy", 1, b"%PDF-backup-preserved")
        backup = Path(self.tmp.name) / "archive-backup.sqlite3"
        create_backup(migrated.path, backup)
        restored_path = Path(self.tmp.name) / "restored.sqlite3"
        restore_backup(backup, restored_path)
        reopened = Store(restored_path).get_report_version("legacy", 1)
        for key in ("html", "html_sha256", "manifest", "pdf_bytes", "pdf_sha256", "created_at"):
            self.assertEqual(reopened[key], original[key])


if __name__ == "__main__":
    unittest.main()
