from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from tests.enterprise_support import audit, material_key

from src import loader
from webapp import app as app_module
from webapp.storage import Store


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "样例企业-审计材料.xlsx"


@material_key
class ClientArchiveTests(unittest.TestCase):
    def test_management_scope_assignment_audit_link_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "clients.db"
            old_store = app_module.store
            app_module.store = Store(db_path)
            try:
                dataset = loader.load(SAMPLE)
                with TestClient(app_module.app) as client:
                    self.assertEqual(client.post("/api/setup", json={
                        "username": "rootadmin", "password": "platform-pass-2026",
                        "display_name": "平台管理员", "org_id": "org-a",
                    }).status_code, 200)
                    self.assertEqual(client.post("/api/login", json={
                        "username": "rootadmin", "password": "platform-pass-2026",
                    }).status_code, 200)

                    users = {}
                    for username, role, org_id in (
                        ("orgadmin", "org_admin", "org-a"),
                        ("accountant1", "accountant", "org-a"),
                        ("accountant2", "accountant", "org-a"),
                        ("student1", "student", "org-a"),
                        ("outsideadmin", "org_admin", "org-b"),
                        ("outsideacct", "accountant", "org-b"),
                    ):
                        users[username] = app_module.store.create_user(username, f"{username}-pass-2026", username, role, org_id)

                    self.assertEqual(client.get('/api/clients').status_code, 403)
                    self.assertEqual(client.post('/api/login', json={'username':'orgadmin','password':'orgadmin-pass-2026'}).status_code, 200)
                    rejected = client.post("/api/clients", json={
                        "name": "跨机构客户", "taxpayer_id": "OTHER-ORG",
                        "accountant_id": users["outsideacct"]["id"],
                    })
                    self.assertEqual(rejected.status_code, 422)

                    mismatch = client.post("/api/clients", json={
                        "name": "识别号不匹配客户", "taxpayer_id": "WRONG-TAX-ID",
                        "accountant_id": users["accountant1"]["id"],
                    })
                    self.assertEqual(mismatch.status_code, 200, mismatch.text)
                    with SAMPLE.open("rb") as stream:
                        response = audit(client,
                            data={"client_id": mismatch.json()["id"]},
                            files={"file": (SAMPLE.name, stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                        )
                    self.assertEqual(response.status_code, 422)
                    self.assertIn("纳税人识别号", response.text)

                    created = client.post("/api/clients", json={
                        "name": dataset.company.name,
                        "taxpayer_id": dataset.company.taxpayer_id,
                        "accountant_id": users["accountant1"]["id"],
                    })
                    self.assertEqual(created.status_code, 200, created.text)
                    client_id = created.json()["id"]
                    listed = client.get("/api/clients").json()
                    selected = next(row for row in listed if row["id"] == client_id)
                    self.assertEqual(selected["accountant_name"], "accountant1")
                    dashboard = client.get("/api/dashboard").json()
                    self.assertIn(client_id, [row["id"] for row in dashboard["clients"]])
                    self.assertEqual(dashboard["records"], [])

                    with SAMPLE.open("rb") as stream:
                        response = audit(client,
                            data={"client_id": client_id},
                            files={"file": (SAMPLE.name, stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                        )
                    self.assertEqual(response.status_code, 200, response.text)
                    audit_id = response.json()["audit_id"]
                    self.assertEqual(client.get("/api/dashboard").json()["records"][0]["client_id"], client_id)
                    client.post("/api/logout")

                    self.assertEqual(client.post("/api/login", json={
                        "username": "accountant1", "password": "accountant1-pass-2026",
                    }).status_code, 200)
                    self.assertIn(client_id, [row["id"] for row in client.get("/api/clients").json()])
                    self.assertEqual(client.get(f"/api/audits/{audit_id}").status_code, 200)
                    client.post("/api/logout")

                    self.assertEqual(client.post("/api/login", json={
                        "username": "accountant2", "password": "accountant2-pass-2026",
                    }).status_code, 200)
                    self.assertEqual(client.get("/api/clients").json(), [])
                    self.assertEqual(client.get(f"/api/audits/{audit_id}").status_code, 404)
                    client.post("/api/logout")

                    self.assertEqual(client.post("/api/login", json={
                        "username": "student1", "password": "student1-pass-2026",
                    }).status_code, 200)
                    self.assertEqual(client.get("/api/clients").status_code, 403)
                    self.assertEqual(client.post("/api/clients", json={
                        "name": "越权", "taxpayer_id": "DENIED",
                    }).status_code, 403)
                    client.post("/api/logout")

                    self.assertEqual(client.post("/api/login", json={
                        "username": "outsideadmin", "password": "outsideadmin-pass-2026",
                    }).status_code, 200)
                    self.assertEqual(client.get("/api/clients").json(), [])

                reopened = Store(db_path)
                self.assertEqual(reopened.get_client(client_id)["taxpayer_id"], dataset.company.taxpayer_id)
                self.assertIsNotNone(reopened.get_audit(audit_id))
            finally:
                app_module.store = old_store


if __name__ == "__main__":
    unittest.main()
