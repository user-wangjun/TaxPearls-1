from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import engine, loader, render, risk_changes
from src.models import Finding, Metric
from webapp import app as module
from webapp.storage import Store

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples/样例企业-审计材料.xlsx"


def entry(audit_id, period, findings=None):
    data = loader.load(SAMPLE)
    data.company.period = period
    return {"id": audit_id, "org_id": "org-a", "client_id": "client-a",
            "taxpayer_id": data.company.taxpayer_id, "period": period,
            "audited_at": "2026-09-26 12:00:00", "dataset": data,
            "findings": findings if findings is not None else engine.run(engine.load_rules(ROOT / "rules"), data)}


class RiskChangeTests(unittest.TestCase):
    def setUp(self):
        self.base = next(rule for rule in engine.load_rules(ROOT / "rules") if rule.id == "R-001")

    def finding(self, status, value, rule_id="R-001", **changes):
        return Finding(replace(self.base, id=rule_id, **changes), status, value, "threshold", "test")

    def test_every_change_and_missing_checks_are_not_resolution(self):
        old, new = [], []
        cases = [("pass","hit",.01,.2,"new"), ("hit","pass",.2,.01,"resolved"),
                 ("hit","hit",.2,.4,"worsened"), ("hit","hit",.4,.2,"improved"),
                 ("hit","hit",.2,.2,"persistent"), ("pass","pass",.01,.01,"clear"),
                 ("hit","skipped",.2,None,"incomparable"), ("skipped","hit",None,.2,"incomparable")]
        for index, (left,right,lv,rv,expected) in enumerate(cases):
            rule_id = f"R-{index+1:03}"
            old.append(self.finding(left,lv,rule_id)); new.append(self.finding(right,rv,rule_id))
        old.append(self.finding("hit",.2,"R-009"))  # disabled, not resolved
        new.append(self.finding("hit",.2,"R-010"))  # new definition, not new business risk
        result = risk_changes.compare(entry("old","2026-01",old),entry("new","2026-02",new))
        by_id = {row["rule_id"]: row for row in result["items"]}
        for index, case in enumerate(cases):
            self.assertEqual(by_id[f"R-{index+1:03}"]["change"],case[-1])
        self.assertEqual(result["counts"]["incomparable"],4)
        self.assertEqual(sum(result["counts"].values()),10)
        self.assertFalse(result["gap"])

    def test_changed_rule_definition_version_or_reference_is_not_business_change(self):
        old = entry("old","2026-01",[self.finding("hit",.2)])
        for change in ({"version":"2.1"},{"severity":"low"},{"threshold_basis":"different"},
                       {"logic":{**self.base.logic,"threshold":.3}}):
            new = entry("new","2026-02",[self.finding("pass",.01,**change)])
            self.assertEqual(risk_changes.compare(old,new)["items"][0]["change"],"rule_changed")
        new = entry("new","2026-02",[self.finding("pass",.01,effective_from="2026-02-01",source_file="moved.yaml")])
        self.assertEqual(risk_changes.compare(old,new)["items"][0]["change"],"resolved")

    def test_ratio_range_distance_below_above_and_reference_change(self):
        rule = replace(self.base, logic={"type":"ratio_range","numerator":"n","denominator":"d","min":"lo","max":"hi"})
        old = entry("old","2026-01",[Finding(rule,"hit",.005,"","test")])
        new = entry("new","2026-02",[Finding(rule,"hit",.07,"","test")])
        for snapshot in (old,new):
            snapshot["dataset"].metrics.update({"lo":Metric("lo",.01,"reviewed"),"hi":Metric("hi",.045,"reviewed")})
        result = risk_changes.compare(old,new)["items"][0]
        self.assertEqual(result["change"],"worsened")
        self.assertEqual(result["before_distance"],"0.005")
        self.assertEqual(result["after_distance"],"0.025")
        new["dataset"].metrics["lo"].value=.02
        self.assertEqual(risk_changes.compare(old,new)["items"][0]["change"],"rule_changed")

    def test_directed_amount_gap_and_compound_or_graph_not_fake_score(self):
        rule = replace(self.base,logic={"type":"amount_mismatch","left":"x","right":"y","tolerance":1,"direction":"below"},evidence=["x","y"],inputs={"x":"x","y":"y"})
        old,new = entry("old","2026-01"),entry("new","2026-02")
        for snapshot,x in ((old,7),(new,5)):
            snapshot["dataset"].metrics.update({"x":Metric("x",x,"source"),"y":Metric("y",10,"source")})
            snapshot["findings"]=[engine.evaluate(rule,snapshot["dataset"])]
        self.assertEqual(risk_changes.compare(old,new)["items"][0]["change"],"worsened")
        for kind in ("all","graph_traversal"):
            for snapshot in (old,new):
                snapshot["findings"]=[Finding(replace(rule,logic={"type":kind}),"hit",None,"","test")]
            self.assertEqual(risk_changes.compare(old,new)["items"][0]["change"],"persistent")

    def test_identity_and_period_constraints_including_gaps_cross_year(self):
        old,new = entry("old","2025-12"),entry("new","2026-01")
        self.assertFalse(risk_changes.compare(old,new)["gap"])
        new["period"]="2026-03"
        self.assertTrue(risk_changes.compare(old,new)["gap"])
        for change in ({"org_id":"other"},{"client_id":None},{"taxpayer_id":"different"},
                       {"period":"2025-12"},{"period":"2025"},{"period":"bad"},{"period":"0000"}):
            with self.assertRaises(ValueError):
                risk_changes.compare(old,{**new,**change})

    def test_aliases_latest_revision_and_period_order_not_upload_order(self):
        current = entry("current","2026-03")
        rows = [entry("late-backfill","2025-12"),entry("jan-new","2026-01-01 至 2026-01-31"),
                entry("feb","2026-02"),entry("jan-old","2026年1月"),
                entry("quarter","2026-Q1"),entry("future","2026-04"),entry("invalid","bad")]
        candidates = risk_changes.baseline_candidates(rows,current)
        self.assertEqual([row["id"] for row in candidates],["feb","jan-new","late-backfill"])


class RiskChangeApiTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ",{"TAXPEARLS_NOTIFICATION_EMAIL_ENABLED":"0"}); self.env.start()
        self.tmp = TemporaryDirectory()
        self.old_store = module.store
        self.store = Store(Path(self.tmp.name)/"changes.db"); module.store = self.store
        self.password = "Change-review-2026!"
        self.admin = self.store.create_user("changeadmin",self.password,"跨期管理员","org_admin","org-a")
        self.accountant = self.store.create_user("changeacct",self.password,"会计","accountant","org-a")
        self.other_acct = self.store.create_user("otheracct",self.password,"其他会计","accountant","org-a")
        self.outside = self.store.create_user("outside",self.password,"外部管理员","org_admin","org-b")
        self.platform = self.store.create_user("platform",self.password,"平台管理员","platform_admin","platform")
        self.student = self.store.create_user("student",self.password,"学生","student","org-a")
        data = loader.load(SAMPLE)
        self.client = self.store.upsert_client(self.admin,data.company.name,data.company.taxpayer_id,self.accountant["id"])
        rule = next(r for r in engine.load_rules(ROOT/"rules") if r.id=="R-001")
        # Actual engine outputs on two different financial snapshots.
        for audit_id,period,revenue in (("jan-old","2026-01",1280000),("jan-new","2026年1月",1000000),
                                       ("feb","2026-02",1500000),("dec-late","2025-12",1000000)):
            data = deepcopy(data); data.company.period=period
            data.metrics["营业收入"].value=revenue
            data.metrics["增值税.销售额"].value=1000000
            findings=[engine.evaluate(rule,data)]
            self.store.save_audit(audit_id,self.admin,self.client["id"],data,findings,render.build_view_model(data,findings)["summary"],"2026-09-26 12:00:00")

    def tearDown(self):
        module.store=self.old_store; self.tmp.cleanup(); self.env.stop()

    def login(self,client,user):
        _,token=self.store.authenticate(user["username"],self.password)
        client.cookies.clear(); client.cookies.set(module.COOKIE_NAME,token)

    def test_automatic_latest_prior_revision_explicit_baseline_and_restart(self):
        with TestClient(module.app) as client:
            self.login(client,self.accountant)
            result=client.get("/api/audits/feb/changes").json()
            self.assertEqual(result["baseline"]["id"],"jan-new")
            self.assertEqual(result["counts"]["new"],1)
            self.assertIn("jan-old",{row["id"] for row in result["baselines"]})
            explicit=client.get("/api/audits/feb/changes?baseline_id=jan-old").json()
            self.assertEqual(explicit["counts"]["worsened"],1)
            module.store=Store(self.store.path)
            self.assertEqual(client.get("/api/audits/feb/changes?baseline_id=jan-old").json(),explicit)
            self.assertEqual(client.get("/api/audits/jan-new/changes?baseline_id=jan-old").status_code,422)
            first=client.get("/api/audits/dec-late/changes").json()
            self.assertEqual(first["status"],"no_baseline")
            self.assertEqual(first["items"],[])
            self.assertEqual(self.store.get_audit("jan-old")["findings"][0].measured,.28)

    def test_api_permissions_and_client_reassignment(self):
        with TestClient(module.app) as client:
            self.assertEqual(client.get("/api/audits/feb/changes").status_code,401)
            for user,status in ((self.student,403),(self.outside,404),(self.other_acct,404),(self.platform,403)):
                self.login(client,user)
                self.assertEqual(client.get("/api/audits/feb/changes").status_code,status)
            self.login(client,self.accountant)
            self.assertEqual(client.get("/api/audits/feb/changes?baseline_id=missing").status_code,404)
            with self.store.connect() as db:
                db.execute("UPDATE clients SET accountant_id=? WHERE id=?",(self.other_acct["id"],self.client["id"]))
            self.assertEqual(client.get("/api/audits/feb/changes").status_code,404)

    def test_cross_org_or_customer_explicit_baseline_does_not_leak(self):
        snapshot=entry("foreign","2026-01")
        data=snapshot["dataset"]; data.company.taxpayer_id="OTHER-TAX-ID"
        self.store.save_audit("foreign",self.outside,None,data,snapshot["findings"],{},"2026-09-26 12:00:00")
        self.store.save_audit("other-client",self.admin,None,data,snapshot["findings"],{},"2026-09-26 12:00:00")
        with TestClient(module.app) as client:
            self.login(client,self.admin)
            self.assertEqual(client.get("/api/audits/feb/changes?baseline_id=foreign").status_code,404)
            self.assertEqual(client.get("/api/audits/feb/changes?baseline_id=other-client").status_code,422)
            self.login(client,self.platform)
            self.assertEqual(client.get("/api/audits/feb/changes?baseline_id=foreign").status_code,403)


if __name__ == "__main__":
    unittest.main()
