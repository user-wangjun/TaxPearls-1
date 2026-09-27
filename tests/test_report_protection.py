from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import engine, loader, render
from src.report_protection import protect_html
from webapp import app as module
from webapp.report_archive import build_snapshot, digest
from webapp.storage import Store

ROOT=Path(__file__).resolve().parents[1]


class ReportProtectionTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{"TAXPEARLS_NOTIFICATION_EMAIL_ENABLED":"0"});self.env.start()
        self.tmp=TemporaryDirectory();self.old_store=module.store
        self.store=Store(Path(self.tmp.name)/"protection.db");module.store=self.store
        self.password="Protection-test-2026!"
        self.admin=self.store.create_user("protectionadmin",self.password,"核验管理员","org_admin","org-a")
        self.accountant=self.store.create_user("protectionaccountant",self.password,"核验会计","accountant","org-a")
        self.data=loader.load(ROOT/"samples/样例企业-审计材料.xlsx")
        self.data.company.name="D08客户（仿真样例）";self.data.company.taxpayer_id="PROTECTION-A"
        self.customer=self.store.upsert_client(self.admin,self.data.company.name,self.data.company.taxpayer_id,self.accountant["id"])
        self.findings=engine.run(engine.load_rules(ROOT/"rules"),self.data)
        self.entry={"id":"protected","org_id":"org-a","audited_at":"2026-09-26 12:00:00","dataset":self.data,"findings":self.findings}
        self.snapshot=build_snapshot(self.entry,module._org_branding("org-a"))
        summary=render.build_view_model(self.data,self.findings)["summary"]
        self.store.save_audit("protected",self.admin,self.customer["id"],self.data,self.findings,summary,self.entry["audited_at"],self.snapshot)
        self.identifier=self.snapshot["manifest"]["protection"]["id"]
        self.client=TestClient(module.app);self.client.__enter__();self.login(self.admin)

    def tearDown(self):
        self.client.__exit__(None,None,None);module.store=self.old_store;self.tmp.cleanup();self.env.stop()

    def login(self,user):
        _,token=self.store.authenticate(user["username"],self.password)
        self.client.cookies.clear();self.client.cookies.set(module.COOKIE_NAME,token)

    def check(self,identifier,content,kind="html"):
        return self.client.post("/api/report-verification/"+identifier,json={"format":kind,"sha256":digest(content)})

    def test_deterministic_escaped_marks_and_local_cli_boundary(self):
        html,local=protect_html('<html><body>original</body></html>','客户<script>alert(1)</script>',"2026-09-26","same")
        self.assertIn("&lt;script&gt;",html);self.assertNotIn("<script>",html)
        self.assertTrue(local["id"].startswith("TPL-"));self.assertIn("未登记服务器原件",html)
        self.assertIn("不能阻止复制",html);self.assertEqual(html.count('<span>'),4)
        generated,_=render.render_html(self.data,self.findings,when=datetime(2026,9,26),write=False)
        self.assertIn("tp-watermark",generated);self.assertIn("TPL-",generated)
        duplicate=build_snapshot(self.entry,module._org_branding("org-a"))
        self.assertEqual(duplicate,self.snapshot)
        with self.assertRaises(ValueError):protect_html("missing body","客户","2026-09-26","same")

    def test_same_human_report_number_not_same_identity_and_new_versions(self):
        other=build_snapshot({**self.entry,"id":"different-audit"},module._org_branding("org-a"))
        self.assertEqual(other["manifest"]["report_no"],self.snapshot["manifest"]["report_no"])
        self.assertNotEqual(other["manifest"]["protection"]["id"],self.identifier)
        old=self.client.get("/api/report/protected/html?version=1").content
        self.store.update_org_settings("org-a","新机构","新报告","新页脚")
        self.client.post("/api/audits/protected/report-versions")
        versions=self.store.report_versions("protected")
        self.assertEqual(len(versions),2)
        self.assertNotEqual(versions[0]["manifest"]["protection"]["id"],self.identifier)
        self.assertTrue(self.check(self.identifier,old).json()["sha256_matches"])
        new=self.client.get("/api/report/protected/html?version=2").content
        self.assertFalse(self.check(self.identifier,new).json()["sha256_matches"])

    def test_registration_is_not_file_verification_copied_id_and_byte_changes_fail(self):
        meta=self.client.get("/api/report-verification/"+self.identifier)
        self.assertEqual(meta.status_code,200);self.assertNotIn("sha256_matches",meta.json())
        self.assertEqual(meta.headers["Cache-Control"],"private, no-store")
        original=self.client.get("/api/report/protected/html?version=1").content
        match=self.check(self.identifier,original)
        self.assertTrue(match.json()["sha256_matches"])
        for changed in (original+b" ",original.replace("核对".encode(),"改动".encode(),1),self.identifier.encode()):
            self.assertFalse(self.check(self.identifier,changed).json()["sha256_matches"])
        self.assertNotIn("dataset",match.json());self.assertNotIn("findings",match.json())

    def test_validation_unexported_pdf_and_no_renderer_call(self):
        with patch.object(render,"export_pdf",side_effect=AssertionError("verification must not render")):
            self.assertEqual(self.check(self.identifier,b"%PDF-unknown","pdf").status_code,409)
        for body in ({"format":"pdf","sha256":"bad"},{"format":"other","sha256":"0"*64},{}):
            self.assertEqual(self.client.post("/api/report-verification/"+self.identifier,json=body).status_code,422)
        for identifier in ("TPV-"+"0"*40,"TPL-"+"0"*40,"bad"):
            self.assertEqual(self.client.get("/api/report-verification/"+identifier).status_code,404)

    def test_current_permissions_cross_org_student_and_reassignment(self):
        self.login(self.accountant)
        self.assertEqual(self.client.get("/api/report-verification/"+self.identifier).status_code,200)
        replacement=self.store.create_user("replacementaccountant",self.password,"新会计","accountant","org-a")
        self.store.upsert_client(self.admin,self.customer["name"],self.customer["taxpayer_id"],replacement["id"])
        self.assertEqual(self.client.get("/api/report-verification/"+self.identifier).status_code,404)
        self.assertEqual(self.check(self.identifier,b"anything").status_code,404)
        for role,org,expected in (("org_admin","org-b",404),("platform_admin","org-b",403),("student","org-a",403)):
            user=self.store.create_user(role+org,self.password,role,role,org);self.login(user)
            self.assertEqual(self.client.get("/api/report-verification/"+self.identifier).status_code,expected)
            self.assertEqual(self.check(self.identifier,b"anything").status_code,expected)
        self.client.cookies.clear();self.assertEqual(self.client.get("/api/report-verification/"+self.identifier).status_code,401)

    def test_org_original_and_admin_only(self):
        report=self.client.post("/api/org/reports",json={}).json()
        identifier=report["snapshot"]["protection"]["id"]
        html=self.client.get(f"/api/org/reports/{report['id']}/html").content
        self.assertTrue(self.check(identifier,html).json()["sha256_matches"])
        self.assertEqual(self.client.get("/api/report-verification/"+identifier).json()["kind"],"org")
        for role in ("accountant","teacher"):
            user=self.store.create_user("orgcheck"+role,self.password,role,role,"org-a");self.login(user)
            self.assertEqual(self.client.get("/api/report-verification/"+identifier).status_code,404)

    def test_old_archives_not_rewritten_and_migration_does_not_invent_marks(self):
        html,_=render.render_html(self.data,self.findings,when=datetime(2026,9,26),write=False,protect=False)
        old=deepcopy(self.snapshot);old["manifest"].pop("protection");old["manifest"]["audit_id"]="old"
        old["html"]=html
        self.store.save_audit("old",self.admin,self.customer["id"],self.data,self.findings,{},self.entry["audited_at"],old)
        self.store.attach_report_pdf("old",1,b"%PDF-legacy-frozen")
        with self.store.connect() as db:db.execute("DROP TABLE report_protections")
        module.store=Store(self.store.path)
        self.assertNotIn("protection",self.store.get_report_version("old",1)["manifest"])
        self.assertEqual(self.client.get("/api/report/old/html?version=1").text,html)
        self.client.post("/api/audits/old/report-versions")
        self.assertEqual(len(self.store.report_versions("old")),2)
        self.assertEqual(self.client.get("/api/report/old?version=1").content,b"%PDF-legacy-frozen")
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM report_protections WHERE audit_id='old'").fetchone()[0],1)

    def test_corrupt_original_and_registry_mismatch_refused_no_rebuild(self):
        with self.store.connect() as db:db.execute("UPDATE audit_report_versions SET html='tampered' WHERE audit_id='protected'")
        with patch.object(render,"export_pdf",side_effect=AssertionError("no rebuild")):
            self.assertEqual(self.client.get("/api/report-verification/"+self.identifier).status_code,409)
            self.assertEqual(self.check(self.identifier,b"anything").status_code,409)
        with self.store.connect() as db:db.execute("UPDATE audit_report_versions SET html=? WHERE audit_id='protected'",(self.snapshot["html"],))
        self.store.update_org_settings("org-a","新机构","不同版本","页脚")
        self.client.post("/api/audits/protected/report-versions")
        with self.store.connect() as db:db.execute("UPDATE report_protections SET version=2 WHERE id=?",(self.identifier,))
        self.assertEqual(self.client.get("/api/report-verification/"+self.identifier).status_code,409)
        self.assertEqual(self.check(self.identifier,b"anything").status_code,409)

    def test_registration_and_audit_are_atomic(self):
        bad=deepcopy(self.snapshot);bad["manifest"]["audit_id"]="rollback"
        bad["manifest"]["protection"]["method"]="local-content"
        with self.assertRaises(ValueError):
            self.store.save_audit("rollback",self.admin,self.customer["id"],self.data,self.findings,{},self.entry["audited_at"],bad)
        self.assertIsNone(self.store.get_audit("rollback"))
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM report_protections WHERE audit_id='rollback'").fetchone()[0],0)

    def test_real_pdf_every_page_marked_and_original_survives_restart_backup(self):
        import io
        import logging
        import pdfplumber
        import pypdfium2
        from scripts.ops_db import create_backup,restore_backup
        org=self.client.post("/api/org/reports",json={}).json()
        originals=[]
        for path,identifier,report_date in (("/api/report/protected?version=1",self.identifier,
                                             self.snapshot["manifest"]["protection"]["report_date"]),
                                (f"/api/org/reports/{org['id']}/pdf",org["snapshot"]["protection"]["id"],
                                 org["snapshot"]["protection"]["report_date"])):
            response=self.client.get(path);self.assertEqual(response.status_code,200)
            with patch.object(logging.getLogger("pdfminer.pdffont"),"level",logging.ERROR),pdfplumber.open(io.BytesIO(response.content)) as document:
                for page in document.pages:
                    marks=page.search(identifier)
                    self.assertTrue(marks)
                    for mark in marks:
                        self.assertGreaterEqual(mark["x0"],0);self.assertGreaterEqual(mark["top"],0)
                        self.assertLessEqual(mark["x1"],page.width);self.assertLessEqual(mark["bottom"],page.height)
            with pypdfium2.PdfDocument(response.content) as pdf:
                self.assertGreaterEqual(len(pdf),2)
                for index in range(len(pdf)):
                    page=pdf[index];text=page.get_textpage()
                    try:
                        value=text.get_text_range()
                        self.assertIn(identifier,value)
                        self.assertIn("报告日期 " + report_date,value)
                        self.assertIn(self.data.company.name if identifier==self.identifier else "多客户 1 户",value)
                    finally:text.close();page.close()
            self.assertTrue(self.check(identifier,response.content,"pdf").json()["sha256_matches"])
            self.assertFalse(self.check(identifier,response.content+b"changed","pdf").json()["sha256_matches"])
            originals.append((path,identifier,response.content))
        backup=Path(self.tmp.name)/"backup.db";restored=Path(self.tmp.name)/"restored.db"
        create_backup(self.store.path,backup);restore_backup(backup,restored);module.store=Store(restored)
        with patch.object(render,"export_pdf",side_effect=AssertionError("must read frozen originals")):
            for path,identifier,content in originals:
                self.assertEqual(self.client.get(path).content,content)
                self.assertTrue(self.check(identifier,content,"pdf").json()["sha256_matches"])


if __name__=="__main__":unittest.main()
