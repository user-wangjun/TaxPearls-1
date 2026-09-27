from __future__ import annotations

import tempfile
import unittest
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import engine, loader
from webapp import app as app_module
from webapp.storage import Store
from webapp.access import AccessDenied


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "样例企业-审计材料.xlsx"


class RuleManagementTests(unittest.TestCase):
    def test_publish_log_failure_rolls_back_new_version_and_previous_end(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'atomic-rules.db')
            actor = store.create_user('atomicadmin', 'Atomic-pass-2026!', '管理员', 'platform_admin', 'org-a')
            base = next(rule for rule in engine.load_rules(ROOT / 'rules') if rule.id=='R-001')
            def publish(version, expected, start):
                candidate = replace(base, version=version, effective_from=start)
                return store.set_rule_override(base.id,version,candidate.logic,candidate.threshold_basis,
                                               actor,expected,start,rule_snapshot=asdict(candidate))
            publish('2.1','2.0','2026-01-01')
            before = store.rule_versions()
            overrides = store.rule_overrides()
            with patch('webapp.members.log', side_effect=RuntimeError('local log failure')):
                with self.assertRaises(RuntimeError):
                    publish('2.2','2.1','2026-07-01')
            self.assertEqual(store.rule_versions(),before)
            self.assertEqual(store.rule_overrides(),overrides)
            publish('2.2','2.1','2026-07-01')
            self.assertEqual(store.rule_versions('R-001')['R-001'][0]['effective_to'],'2026-06-30')
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='update_rule_parameters'").fetchone()[0],2)

    def test_publish_rechecks_actor_in_write_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'revoked-rules.db')
            actor = store.create_user('revokedadmin', 'Revoked-pass-2026!', '管理员', 'platform_admin', 'org-a')
            base = next(rule for rule in engine.load_rules(ROOT / 'rules') if rule.id=='R-001')
            candidate = replace(base,version='2.1')
            for change in ["active=0", "role='org_admin'", "org_id='org-b'"]:
                with store.connect() as db:
                    db.execute("UPDATE users SET active=1,role='platform_admin',org_id='org-a' WHERE id=?",(actor['id'],))
                    db.execute('UPDATE users SET '+change+' WHERE id=?',(actor['id'],))
                with self.assertRaises(AccessDenied):
                    store.set_rule_override(base.id,'2.1',candidate.logic,candidate.threshold_basis,
                                            actor,'2.0',rule_snapshot=asdict(candidate))
                self.assertEqual(store.rule_versions(),{})
                self.assertEqual(store.rule_overrides(),{})

    def test_legacy_override_migration_preserves_undated_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-rules.db"
            store = Store(path)
            actor = store.create_user("legacyadmin", "Legacy-pass-2026!", "管理员", "platform_admin", "org-legacy")
            base = next(rule for rule in engine.load_rules(ROOT / "rules") if rule.id == "R-001")
            logic = deepcopy(base.logic)
            logic["threshold"] = 0.4
            with store.connect() as db:
                db.execute("""INSERT INTO rule_overrides VALUES (?,?,?,?,?,?)""",
                           (base.id, "2.1", json.dumps(logic), "旧版全期间参数", actor["id"], "2026-09-01T00:00:00+00:00"))
            old_store = app_module.store
            try:
                app_module.store = Store(path)
                migrated = app_module.store.rule_versions("R-001")["R-001"]
                self.assertEqual(len(migrated), 1)
                self.assertIsNone(migrated[0]["effective_from"])
                self.assertIsNone(migrated[0]["rule"])
                selected = next(rule for rule in app_module._audit_rules(loader.load(SAMPLE)) if rule.id == "R-001")
                self.assertEqual((selected.version, selected.logic["threshold"]), ("2.1", 0.4))
            finally:
                app_module.store = old_store

    def test_rule_publish_compare_and_swap_across_two_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publish-race.db"
            first = Store(path)
            actor = first.create_user("raceadmin", "Race-pass-2026!", "管理员", "platform_admin", "org-race")
            second = Store(path)
            base = next(rule for rule in engine.load_rules(ROOT / "rules") if rule.id == "R-001")

            def publish(target, version):
                candidate = replace(base, version=version)
                try:
                    target.set_rule_override(base.id, version, candidate.logic, candidate.threshold_basis,
                                             actor, base.version, rule_snapshot=asdict(candidate))
                    return "saved"
                except ValueError as exc:
                    self.assertIn("规则版本已变化", str(exc))
                    return "conflict"

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(publish, first, "2.1"), executor.submit(publish, second, "2.2")]
                self.assertEqual(sorted(future.result() for future in futures), ["conflict", "saved"])
            self.assertEqual(len(first.rule_versions("R-001")["R-001"]), 1)

    def test_dated_versions_conflicts_period_selection_and_frozen_history(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rule-periods.db"
            old_store = app_module.store
            app_module.store = Store(db_path)
            try:
                with TestClient(app_module.app) as client, TestClient(app_module.app) as manager:
                    response = client.post("/api/setup", json={
                        "username": "periodadmin", "password": "platform-pass-2026",
                        "display_name": "平台管理员", "org_id": "org-periods",
                    })
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(self._login(client, "periodadmin", "platform-pass-2026").status_code, 200)
                    manager.cookies.update(client.cookies)
                    app_module.store.create_user('periodowner', 'Owner-pass-2026!', '机构管理员', 'org_admin', 'org-periods')
                    self.assertEqual(self._login(client, 'periodowner', 'Owner-pass-2026!').status_code, 200)
                    old_audit = self._upload(client)
                    old_report = client.get(f"/api/report/{old_audit}/html")
                    self.assertEqual(old_report.status_code, 200, old_report.text[:300])
                    base = self._rule(client, "R-001")
                    self.assertEqual(base["version"], "2.0")

                    def save(version, expected, threshold, start, end=None):
                        logic = deepcopy(base["logic"])
                        logic["threshold"] = threshold
                        return manager.put("/api/rules/R-001/parameters", json={
                            "expected_version": expected, "new_version": version,
                            "logic": logic, "threshold_basis": f"仿真 v{version} 阈值",
                            "effective_from": start, "effective_to": end,
                        })

                    for start, end in (("2026-02-30", None), (None, "2026-06-30"), ("2026-07-01", "2026-06-30")):
                        invalid = save("2.1", "2.0", 0.4, start, end)
                        self.assertEqual(invalid.status_code, 422, invalid.text)

                    first = save("2.1", "2.0", 0.4, "2026-01-01", "2026-06-30")
                    self.assertEqual(first.status_code, 200, first.text)
                    h1_audit = self._upload(client)
                    h1_result = client.get(f"/api/audits/{h1_audit}").json()
                    h1_rule = next(item for item in h1_result["findings"] if item["id"] == "R-001")
                    self.assertEqual((h1_rule["status"], h1_rule["version"]), ("pass", "2.1"))
                    self.assertEqual(h1_rule["effective_from"], "2026-01-01")

                    second = save("2.2", "2.1", 0.05, "2026-09-01")
                    self.assertEqual(second.status_code, 200, second.text)
                    self.assertEqual(self._rule(client, "R-001")["version"], "2.2")
                    h1_again = self._upload(client)
                    h1_result = client.get(f"/api/audits/{h1_again}").json()
                    h1_rule = next(item for item in h1_result["findings"] if item["id"] == "R-001")
                    self.assertEqual((h1_rule["status"], h1_rule["version"]), ("pass", "2.1"))

                    overlap = save("2.3", "2.2", 0.1, "2026-02-15")
                    self.assertEqual(overlap.status_code, 409, overlap.text)
                    self.assertIn("重叠", overlap.text)
                    self.assertEqual(self._rule(client, "R-001")["version"], "2.2")
                    third = save("2.3", "2.2", 0.1, "2027-01-01")
                    self.assertEqual(third.status_code, 200, third.text)
                    versions = manager.get("/api/rules/R-001/versions")
                    self.assertEqual(versions.status_code, 200, versions.text)
                    self.assertEqual([item["version"] for item in versions.json()], ["2.1", "2.2", "2.3"])
                    self.assertEqual(versions.json()[1]["effective_to"], "2026-12-31")
                    self.assertEqual(versions.json()[0]["rule"]["name"], base["name"])
                    changed_library = [replace(rule, name="后续 YAML 名称", version="3.0")
                                       if rule.id == "R-001" else rule
                                       for rule in engine.load_rules(ROOT / "rules")]
                    with patch.object(app_module.engine, "load_rules", return_value=changed_library):
                        selected = next(rule for rule in app_module._audit_rules(loader.load(SAMPLE)) if rule.id == "R-001")
                        self.assertEqual((selected.name, selected.version), (base["name"], "2.1"))
                    backfill = save("2.4", "2.3", 0.2, "2026-07-01", "2026-08-31")
                    self.assertEqual(backfill.status_code, 409, backfill.text)
                    self.assertIn("晚于", backfill.text)

                    year_data = loader.load(SAMPLE)
                    year_data.company.period = "2026"
                    with self.assertRaisesRegex(Exception, "请拆分期间审计"):
                        app_module._audit_rules(year_data)
                    disable = manager.put("/api/rules/R-001/state", json={"enabled": False})
                    self.assertEqual(disable.status_code, 200, disable.text)
                    enabled = app_module.store.enabled_rule_ids()
                    self.assertNotIn("R-001", {rule.id for rule in app_module._audit_rules(year_data, enabled)})
                    self.assertEqual(manager.put("/api/rules/R-001/state", json={"enabled": True}).status_code, 200)
                    old_result = client.get(f"/api/audits/{old_audit}").json()
                    old_rule = next(item for item in old_result["findings"] if item["id"] == "R-001")
                    self.assertEqual((old_rule["status"], old_rule["version"]), ("hit", "2.0"))

                    app_module.store = Store(db_path)
                    self.assertEqual(len(manager.get("/api/rules/R-001/versions").json()), 3)
                    restored = client.get(f"/api/audits/{old_audit}").json()
                    restored_rule = next(item for item in restored["findings"] if item["id"] == "R-001")
                    self.assertEqual((restored_rule["status"], restored_rule["version"]), ("hit", "2.0"))
                    self.assertEqual(client.get(f"/api/report/{old_audit}/html").text, old_report.text)
                    with patch.object(app_module.engine, "load_rules", return_value=changed_library):
                        after_deploy = save("3.1", "3.0", 0.2, "2028-01-01")
                        self.assertEqual(after_deploy.status_code, 200, after_deploy.text)
                        self.assertEqual(after_deploy.json()["name"], "后续 YAML 名称")
                    self.assertEqual(client.get(f"/api/report/{old_audit}/html").text, old_report.text)
            finally:
                app_module.store = old_store

    def test_parameter_edit_trial_permissions_versions_snapshots_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rule-management.db"
            old_store = app_module.store
            app_module.store = Store(db_path)
            try:
                with TestClient(app_module.app) as client:
                    self.assertEqual(client.post("/api/setup", json={
                        "username": "rootadmin", "password": "platform-pass-2026",
                        "display_name": "平台管理员", "org_id": "org-a",
                    }).status_code, 200)
                    self.assertEqual(self._login(client, "rootadmin", "platform-pass-2026").status_code, 200)
                    created_users = {}
                    for username, role, org_id in (
                        ("orgowner", "org_admin", "org-a"),
                        ("teachera", "teacher", "org-a"),
                        ("accountanta", "accountant", "org-a"),
                        ("studenta", "student", "org-a"),
                        ("teacherb", "teacher", "org-b"),
                    ):
                        created_users[username] = app_module.store.create_user(username, f"{username}-pass-2026", username, role, org_id)

                    self.assertEqual(self._login(client, 'orgowner', 'orgowner-pass-2026').status_code, 200)
                    assigned_client = client.post("/api/clients", json={
                        "name": "东莞市启明商贸有限公司（仿真样例）",
                        "taxpayer_id": "91441900MA5TEST0X0",
                        "accountant_id": created_users["accountanta"]["id"],
                    })
                    self.assertEqual(assigned_client.status_code, 200, assigned_client.text)
                    assigned_client_id = assigned_client.json()["id"]

                    self.assertEqual(self._login(client, 'teachera', 'teachera-pass-2026').status_code, 200)
                    first_audit = self._upload(client)
                    rule = self._rule(client, "R-001")
                    self.assertEqual(rule["version"], "2.0")
                    self.assertFalse(rule["customized"])
                    self.assertEqual(rule["logic"]["threshold"], 0.1)

                    draft_logic = deepcopy(rule["logic"])
                    draft_logic["threshold"] = 0.4
                    draft = {
                        "audit_id": first_audit,
                        "expected_version": "2.0", "new_version": "2.1",
                        "logic": draft_logic,
                        "threshold_basis": "相对偏离超过40%为本次试跑参数。",
                    }
                    trial = client.post("/api/rules/R-001/trial", json=draft)
                    self.assertEqual(trial.status_code, 200, trial.text)
                    self.assertEqual(trial.json()["status"], "pass")
                    self.assertEqual(trial.json()["version"], "2.1")

                    # A trial is non-destructive: the persisted audit retains its original rule snapshot.
                    old_result = client.get(f"/api/audits/{first_audit}").json()
                    old_r001 = next(item for item in old_result["findings"] if item["id"] == "R-001")
                    self.assertEqual((old_r001["status"], old_r001["version"]), ("hit", "2.0"))

                    invalid = deepcopy(draft)
                    invalid["logic"] = {"type": "python_eval", "expression": "__import__('os')"}
                    response = client.post("/api/rules/R-001/trial", json=invalid)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertIn("不支持", response.text)

                    unknown = deepcopy(draft)
                    unknown["logic"]["left"] = "未知.指标"
                    response = client.post("/api/rules/R-001/trial", json=unknown)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertIn("未知指标", response.text)

                    not_advanced = deepcopy(draft)
                    not_advanced["new_version"] = "2.0"
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=not_advanced).status_code, 422)
                    not_advanced["new_version"] = "2.0.0"
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=not_advanced).status_code, 422)

                    client.post("/api/logout")
                    self._login(client, "studenta", "studenta-pass-2026")
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=draft).status_code, 403)
                    self.assertEqual(client.put("/api/rules/R-001/parameters", json=draft).status_code, 403)

                    client.post("/api/logout")
                    self._login(client, "teacherb", "teacherb-pass-2026")
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=draft).status_code, 404)

                    client.post("/api/logout")
                    self._login(client, "teachera", "teachera-pass-2026")
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=draft).status_code, 200)
                    self.assertEqual(client.put("/api/rules/R-001/parameters", json=draft).status_code, 403)

                    client.post("/api/logout")
                    self._login(client, "accountanta", "accountanta-pass-2026")
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=draft).status_code, 404)
                    accountant_audit = self._upload(client, assigned_client_id)
                    accountant_draft = {**draft, "audit_id": accountant_audit}
                    self.assertEqual(client.post("/api/rules/R-001/trial", json=accountant_draft).status_code, 200)
                    self.assertEqual(client.put("/api/rules/R-001/parameters", json=accountant_draft).status_code, 403)

                    client.post("/api/logout")
                    self._login(client, "rootadmin", "platform-pass-2026")
                    self.assertEqual(client.post('/api/rules/R-001/trial', json=draft).status_code, 403)
                    saved = client.put("/api/rules/R-001/parameters", json=draft)
                    self.assertEqual(saved.status_code, 200, saved.text)
                    self.assertEqual(saved.json()["version"], "2.1")
                    self.assertTrue(saved.json()["customized"])

                    stale = deepcopy(draft)
                    stale["new_version"] = "2.2"
                    self.assertEqual(client.put("/api/rules/R-001/parameters", json=stale).status_code, 409)

                    self.assertEqual(self._login(client, 'orgowner', 'orgowner-pass-2026').status_code, 200)
                    second_audit = self._upload(client)
                    new_result = client.get(f"/api/audits/{second_audit}").json()
                    new_r001 = next(item for item in new_result["findings"] if item["id"] == "R-001")
                    self.assertEqual((new_r001["status"], new_r001["version"]), ("pass", "2.1"))
                    old_result = client.get(f"/api/audits/{first_audit}").json()
                    old_r001 = next(item for item in old_result["findings"] if item["id"] == "R-001")
                    self.assertEqual((old_r001["status"], old_r001["version"]), ("hit", "2.0"))

                    # Reopening the repository proves rule parameters and sessions survive restart.
                    app_module.store = Store(db_path)
                    reopened = self._rule(client, "R-001")
                    self.assertEqual(reopened["version"], "2.1")
                    self.assertEqual(reopened["logic"]["threshold"], 0.4)
                    actions = [row["action"] for row in client.get("/api/audit-log").json()]
                    self.assertIn("trial_rule_parameters", actions)
                    self.assertIn("update_rule_parameters", actions)
            finally:
                app_module.store = old_store

    @staticmethod
    def _login(client: TestClient, username: str, password: str):
        return client.post("/api/login", json={"username": username, "password": password})

    @staticmethod
    def _upload(client: TestClient, client_id: str | None = None) -> str:
        with SAMPLE.open("rb") as stream:
            response = client.post("/api/audit", files={
                "file": (SAMPLE.name, stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            }, data={"client_id": client_id} if client_id else None)
        if response.status_code != 200:
            raise AssertionError(response.text)
        return response.json()["audit_id"]

    @staticmethod
    def _rule(client: TestClient, rule_id: str) -> dict:
        response = client.get("/api/rules")
        if response.status_code != 200:
            raise AssertionError(response.text)
        return next(item for item in response.json() if item["id"] == rule_id)
