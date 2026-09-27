from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import engine, loader, render
from webapp import app as module
from webapp.org_reports import overview
from webapp.report_archive import digest
from webapp.storage import Store

ROOT=Path(__file__).resolve().parents[1]


class OrgReportTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{"TAXPEARLS_NOTIFICATION_EMAIL_ENABLED":"0"});self.env.start()
        self.tmp=TemporaryDirectory();self.old_store=module.store
        self.store=Store(Path(self.tmp.name)/"institution.db");module.store=self.store
        self.password="Institution-test-2026!"
        self.admin=self.store.create_user("institutionadmin",self.password,"机构管理员","org_admin","org-a")
        self.clients=[self.store.upsert_client(self.admin,name,tax) for name,tax in
                      (("客户甲（仿真样例）","TAX-A"),("客户乙（仿真样例）","TAX-B"),("未审计客户（仿真样例）","TAX-C"))]
        self.data=loader.load(ROOT/"samples/样例企业-审计材料.xlsx")
        self.rules={rule.id:rule for rule in engine.load_rules(ROOT/"rules")}
        self.save("a-jan-old",0,"2026-01",.28,"2026-09-26 12:00:00")
        self.save("a-jan-new",0,"2026年1月",0,"2026-09-27 12:00:00",missing=True)
        self.save("a-feb",0,"2026-02",.5,"2026-09-02 12:00:00",missing=True,version="2.1")
        self.save("a-dec-backfill",0,"2025-12",.9,"2026-09-30 12:00:00")
        self.save("b-jan",1,"2026-01",0,"2026-09-03 12:00:00")
        self.client=TestClient(module.app);self.client.__enter__();self.login(self.admin)

    def tearDown(self):
        self.client.__exit__(None,None,None);module.store=self.old_store;self.tmp.cleanup();self.env.stop()

    def login(self,user):
        _,token=self.store.authenticate(user["username"],self.password)
        self.client.cookies.clear();self.client.cookies.set(module.COOKIE_NAME,token)

    def save(self,audit_id,index,period,deviation,when,missing=False,version="2.0"):
        data=deepcopy(self.data); customer=self.clients[index]
        data.company.name=customer["name"];data.company.taxpayer_id=customer["taxpayer_id"];data.company.period=period
        data.metrics["营业收入"].value=1000000*(1+deviation);data.metrics["增值税.销售额"].value=1000000
        findings=[engine.evaluate(replace(self.rules["R-001"],version=version),data)]
        if missing:findings.append(engine.evaluate(self.rules["R-003"],data))
        # Cached summary is deliberately wrong: report must use frozen findings.
        self.store.save_audit(audit_id,self.admin,customer["id"],data,findings,{"hit":999},when)

    def create(self,**body):
        result=self.client.post("/api/org/reports",json=body)
        self.assertEqual(result.status_code,200,result.text)
        return result.json()

    def test_creation_log_failure_rolls_back_report_and_protection(self):
        with patch.object(self.store, '_log', side_effect=RuntimeError('audit log unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'audit log unavailable'):
                self.client.post('/api/org/reports', json={})
        self.assertEqual(self.store.list_org_reports(self.admin), [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM report_protections').fetchone()[0], 0)
        saved = self.create()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='create_org_report' AND target_id=?",
                                        (saved['id'],)).fetchone()[0], 1)

    def test_latest_business_period_not_upload_date_and_no_duplicate_client(self):
        data=self.client.get("/api/org/overview").json()
        by_id={row["client_id"]:row for row in data["rows"]}
        self.assertEqual(len(by_id),3)
        self.assertEqual(by_id[self.clients[0]["id"]]["audit_id"],"a-feb")
        self.assertEqual(by_id[self.clients[1]["id"]]["state"],"clear")
        self.assertEqual(by_id[self.clients[2]["id"]]["state"],"no_audit")
        self.assertEqual({key:data["totals"][key] for key in ("clients","audited","hit","pass","skipped","high","clear","no_audit")},
                         {"clients":3,"audited":2,"hit":1,"pass":1,"skipped":1,"high":1,"clear":1,"no_audit":1})
        self.assertTrue(data["mixed_periods"]);self.assertTrue(data["mixed_rule_sets"])
        self.assertEqual(sum(row["hit"] for row in data["rule_distribution"]),1)
        self.assertEqual(len([row for row in data["rule_distribution"] if row["id"]=="R-001"]),2)

    def test_period_alias_latest_revision_missing_is_not_clear_and_subset(self):
        data=self.client.get("/api/org/overview",params={"period":"2026-01-01 至 2026-01-31"}).json()
        self.assertFalse(data["mixed_periods"])
        row=next(row for row in data["rows"] if row["client_id"]==self.clients[0]["id"])
        self.assertEqual((row["audit_id"],row["state"]),("a-jan-new","incomplete"))
        self.assertEqual(data["totals"]["hit"],0)
        self.assertEqual(data["totals"]["incomplete"],1)
        self.assertEqual(data["totals"]["clear"],1)
        result=self.create(period="2026年1月",client_ids=[self.clients[0]["id"]])
        self.assertEqual(result["snapshot"]["totals"]["clients"],1)
        self.assertNotIn(self.clients[1]["id"],{row["client_id"] for row in result["snapshot"]["rows"]})
        self.assertEqual(len(data["available_periods"]),3)  # aliases collapse
        self.assertEqual(self.client.get("/api/org/overview?period=0000").status_code,422)

    def test_equal_period_end_prefers_shorter_interval_and_definition_hash_separates(self):
        self.save("a-quarter",0,"2026-Q1",.5,"2026-10-02 12:00:00")
        self.save("a-month",0,"2026-03",.5,"2026-09-01 12:00:00")
        data=self.client.get("/api/org/overview").json()
        self.assertEqual(next(row for row in data["rows"] if row["client_id"]==self.clients[0]["id"])["audit_id"],"a-month")
        quarter=self.client.get("/api/org/overview?period=2026-Q1").json()
        self.assertEqual(quarter["totals"]["audited"],1)
        # Identical version labels with different definitions must not collapse.
        with self.store.connect() as db:
            encoded=db.execute("SELECT findings_json FROM audits WHERE id='a-month'").fetchone()[0]
            findings=json.loads(encoded);findings[0]["rule"]["threshold_basis"]="Different reviewed threshold basis"
            db.execute("UPDATE audits SET findings_json=? WHERE id='a-month'",(json.dumps(findings),))
        result=self.client.get("/api/org/overview").json()
        rows=[row for row in result["rule_distribution"] if row["id"]=="R-001"]
        self.assertEqual(len(rows),2);self.assertEqual({row["version"] for row in rows},{"2.0"})
        self.assertEqual(len({row["definition_sha256"] for row in rows}),2)

    def test_exclusion_boundaries_and_same_name_not_identity(self):
        data=deepcopy(self.data);data.company.taxpayer_id="OTHER";data.company.period="2026-03"
        findings=[engine.evaluate(self.rules["R-001"],data)]
        self.store.save_audit("wrong-tax",self.admin,self.clients[0]["id"],data,findings,{},"2026-10-01 12:00:00")
        self.store.save_audit("unlinked",self.admin,None,data,findings,{},"2026-10-01 12:00:00")
        self.save("bad-period",1,"not-a-period",0,"2026-10-01 12:00:00")
        with self.store.connect() as db:db.execute("UPDATE clients SET name=? WHERE id=?",(self.clients[0]["name"],self.clients[1]["id"]))
        data=self.client.get("/api/org/overview").json()
        self.assertEqual(data["excluded_audits"],{"unlinked":1,"identity_mismatch":1,"invalid_period":1})
        self.assertEqual({row["audit_id"] for row in data["rows"]},{"a-feb","b-jan",None})
        self.assertEqual(data["totals"]["clients"],3)

    def test_twenty_clients_full_rule_set_in_one_report(self):
        selected=[]
        for index in range(20):
            customer=self.store.upsert_client(self.admin,f"批量客户{index:02d}（仿真样例）",f"BATCH-{index:02d}")
            selected.append(customer)
            data=deepcopy(self.data)
            data.company.name=customer["name"];data.company.taxpayer_id=customer["taxpayer_id"];data.company.period="2026-03"
            findings=[engine.evaluate(rule,data) for rule in self.rules.values()]
            self.store.save_audit(f"batch-{index:02d}",self.admin,customer["id"],data,findings,{},"2026-10-01 12:00:00")
        result=self.create(period="2026-03",client_ids=[customer["id"] for customer in selected])
        snapshot=result["snapshot"]
        self.assertEqual(snapshot["totals"]["clients"],20)
        self.assertEqual(snapshot["totals"]["audited"],20)
        self.assertEqual(snapshot["totals"]["no_audit"],0)
        self.assertEqual(sum(snapshot["totals"][key] for key in ("hit","pass","skipped")),20*len(self.rules))
        self.assertEqual(len(snapshot["rule_distribution"]),len(self.rules))
        self.assertFalse(snapshot["mixed_periods"]);self.assertFalse(snapshot["mixed_rule_sets"])
        html=self.client.get(f"/api/org/reports/{result['id']}/html").text
        for customer in selected:self.assertIn(customer["name"],html)

    def test_frozen_html_brand_clients_rules_and_restart(self):
        result=self.create();report_id=result["id"]
        original=self.client.get(f"/api/org/reports/{report_id}/html")
        self.assertEqual(original.headers["X-TaxPearls-SHA256"],digest(original.content))
        self.assertIn("本次检查全部通过的客户 1 家",original.text)
        self.assertNotIn("built-in method",original.text)
        self.store.update_org_settings("org-a","新机构名","新报告标题","新页脚")
        with self.store.connect() as db:
            db.execute("UPDATE clients SET name='changed-name' WHERE id=?",(self.clients[0]["id"],))
        self.save("a-mar",0,"2026-03",0,"2026-10-01 12:00:00")
        module.store=Store(self.store.path)
        with patch.object(engine,"load_rules",side_effect=AssertionError("must not reload YAML")), \
             patch.object(Path,"read_text",side_effect=AssertionError("must not reload template")):
            reopened=self.client.get(f"/api/org/reports/{report_id}").json()
            self.assertEqual(reopened["snapshot"],result["snapshot"])
            self.assertEqual(self.client.get(f"/api/org/reports/{report_id}/html").content,original.content)
        self.assertEqual(self.client.get("/api/org/reports").json()[0]["id"],report_id)

    def test_permissions_foreign_client_ids_and_report_ids(self):
        report=self.create();report_id=report["id"]
        for role in ("accountant","teacher","student"):
            user=self.store.create_user("role"+role,self.password,role,role,"org-a");self.login(user)
            for method,path,kwargs in (("get","/api/org/overview",{}),("get","/api/org/reports",{}),
                ("post","/api/org/reports",{"json":{}}),("get",f"/api/org/reports/{report_id}",{}),
                ("get",f"/api/org/reports/{report_id}/html",{}),("get",f"/api/org/reports/{report_id}/pdf",{})):
                self.assertEqual(getattr(self.client,method)(path,**kwargs).status_code,403)
        foreign=self.store.create_user("foreignadmin",self.password,"外部管理员","org_admin","org-b");self.login(foreign)
        self.assertEqual(self.client.get("/api/org/overview").json()["totals"]["clients"],0)
        self.assertEqual(self.client.get("/api/org/reports").json(),[])
        for suffix in ("","/html","/pdf"):
            self.assertEqual(self.client.get(f"/api/org/reports/{report_id}"+suffix).status_code,404)
        self.assertEqual(self.client.post("/api/org/reports",json={"client_ids":[self.clients[0]["id"]]}).status_code,404)
        self.assertEqual(self.client.post("/api/org/reports",json={}).status_code,422)
        self.client.cookies.clear();self.assertEqual(self.client.get("/api/org/overview").status_code,401)

    def test_validation_unknown_definition_and_scope(self):
        for body in ({"client_ids":[]},{"client_ids":[self.clients[0]["id"]]*2},{"period":"bad"},{"period":"x"*81}):
            self.assertEqual(self.client.post("/api/org/reports",json=body).status_code,422)
        self.assertEqual(self.client.post("/api/org/reports",json={"client_ids":["absent"]}).status_code,404)
        sources=self.store.org_report_sources(self.admin)
        candidate=next(row for row in sources["audits"] if row["id"]=="a-feb")
        frozen=json.loads(candidate["findings_json"])
        frozen[0]["status"]="unknown";candidate["findings_json"]=json.dumps(frozen)
        with self.assertRaises(ValueError):overview(sources)

    def test_xss_escaped_in_brand_and_client_name(self):
        evil='<script>alert("escaped")</script>'
        with self.store.connect() as db:db.execute("UPDATE clients SET name=? WHERE id=?",(evil,self.clients[0]["id"]))
        self.store.update_org_settings("org-a",evil,evil,evil)
        result=self.create();html=self.client.get(f"/api/org/reports/{result['id']}/html").text
        self.assertNotIn(evil,html);self.assertIn("&lt;script&gt;",html)

    def test_real_pdf_all_clients_a4_frozen_restart_and_backup(self):
        import pypdfium2
        from scripts.ops_db import create_backup,restore_backup
        result=self.create();report_id=result["id"]
        first=self.client.get(f"/api/org/reports/{report_id}/pdf")
        self.assertEqual(first.status_code,200,first.text[:100] if first.status_code!=200 else "")
        self.assertEqual(first.headers["X-TaxPearls-SHA256"],digest(first.content))
        texts=[]
        with pypdfium2.PdfDocument(first.content) as pdf:
            for index in range(len(pdf)):
                page=pdf[index]
                try:
                    width,height=page.get_size();self.assertAlmostEqual(width,595.3,delta=2);self.assertAlmostEqual(height,841.9,delta=2)
                    text=page.get_textpage()
                    try:texts.append(text.get_text_range())
                    finally:text.close()
                finally:page.close()
        for customer in self.clients:self.assertIn(customer["name"],"".join(texts))
        self.assertIn(result["snapshot"]["report_no"],"".join(texts))
        self.assertNotIn("built-in method","".join(texts))
        self.assertIn("本次检查全部通过的客户 1 家","".join(texts))
        module.store=Store(self.store.path)
        with patch.object(render,"export_pdf",side_effect=AssertionError("must use frozen PDF")):
            self.assertEqual(self.client.get(f"/api/org/reports/{report_id}/pdf").content,first.content)
        backup=Path(self.tmp.name)/"backup.sqlite3";create_backup(self.store.path,backup)
        restored=Path(self.tmp.name)/"restored.sqlite3";restore_backup(backup,restored)
        self.assertEqual(Store(restored).get_org_report(report_id,self.admin)["pdf_bytes"],first.content)

    def test_pdf_failure_temp_cleanup_and_corruption_refusal(self):
        result=self.create();report_id=result["id"]
        temp=Path(self.tmp.name)/"failed.pdf";fd=os.open(temp,os.O_CREAT|os.O_RDWR)
        with patch("webapp.org_reports.tempfile.mkstemp",return_value=(fd,str(temp))), \
             patch.object(render,"export_pdf",side_effect=RuntimeError("private-detail")):
            response=self.client.get(f"/api/org/reports/{report_id}/pdf")
        self.assertEqual(response.status_code,500);self.assertNotIn("private-detail",response.text);self.assertFalse(temp.exists())
        self.assertIsNone(self.store.get_org_report(report_id,self.admin)["pdf_bytes"])
        with self.store.connect() as db:db.execute("UPDATE org_reports SET html='tampered' WHERE id=?",(report_id,))
        with patch.object(render,"export_pdf",side_effect=AssertionError("must not regenerate corrupted archive")):
            for suffix in ("","/html","/pdf"):self.assertEqual(self.client.get(f"/api/org/reports/{report_id}"+suffix).status_code,409)

    def test_concurrent_pdf_first_writer_and_old_schema_migration(self):
        with self.store.connect() as db:db.execute("DROP TABLE org_reports")
        module.store=Store(self.store.path)
        self.assertEqual(self.client.get("/api/org/reports").json(),[])
        result=self.create();other=Store(self.store.path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs=list(pool.map(lambda pair:pair[0].attach_org_report_pdf(result["id"],self.admin,pair[1])["pdf_bytes"],
                                  [(self.store,b"%PDF-first"),(other,b"%PDF-second")]))
        self.assertEqual(outputs[0],outputs[1])
        with self.assertRaises(ValueError):self.store.attach_org_report_pdf(result["id"],self.admin,b"invalid")


if __name__=="__main__":unittest.main()
