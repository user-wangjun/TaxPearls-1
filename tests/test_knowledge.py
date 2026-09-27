import json
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from fastapi import HTTPException
from fastapi.testclient import TestClient
from src import engine, loader
from src.settings import AISettings
from webapp import app as app_module
from webapp.knowledge import (
    ask_graph, audit_narrative_hash, build_graph, finding_evidence_hash,
    generate_audit_narrative, interpret_finding,
)
from webapp.storage import Store
from tests.enterprise_support import audit as enterprise_audit, material_key

ROOT = Path(__file__).resolve().parents[1]


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.rules = engine.load_rules(ROOT / "rules")
        self.graph = build_graph(self.rules)

    def test_graph_references_and_audit_evidence(self):
        ids = {n["id"] for n in self.graph["nodes"]}
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in self.graph["edges"]))
        self.assertFalse(any(n["kind"] == "risk" for n in self.graph["nodes"]))
        data = loader.load(ROOT / "samples/样例企业-审计材料.xlsx")
        findings = engine.run(self.rules, data)
        graph = build_graph([], {"id": "example", "dataset": data, "findings": findings})
        self.assertEqual(len([n for n in graph["nodes"] if n["kind"] == "risk"]), len(findings))
        revenue = next(n for n in graph["nodes"] if n["label"] == "营业收入")
        self.assertEqual(revenue["value"], str(data.get("营业收入")))
        self.assertTrue(any(n["kind"] == "source" for n in graph["nodes"]))

    def test_model_citations_and_config(self):
        settings = AISettings(enabled=True, api_key="synthetic-test-key")
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "answer": "请核对收入口径。", "citations": ["R-001", "invented"]})}}]}
        opener = Mock()
        opener.open.return_value = BytesIO(json.dumps(response).encode())
        with patch("webapp.knowledge.AISettings.from_env", return_value=settings), patch("src.ai_transport.build_opener", return_value=opener):
            result = ask_graph(self.graph, "R-001", "解释当前规则")
            self.assertEqual([n["id"] for n in result["citations"]], ["R-001"])
            payload = json.loads(opener.open.call_args.args[0].data)
            self.assertEqual(payload["model"], settings.effective_model)
            self.assertIn("R-001", payload["messages"][1]["content"])
        with patch("webapp.knowledge.AISettings.from_env", return_value=AISettings()):
            with self.assertRaises(HTTPException) as error:
                ask_graph(self.graph, "R-001", "解释")
            self.assertEqual(error.exception.status_code, 503)

    def test_reject_unreferenced_model_answer(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": '{"answer":"unsupported","citations":["invented"]}'}}]}
        opener = Mock()
        opener.open.return_value = BytesIO(json.dumps(response).encode())
        with patch("webapp.knowledge.AISettings.from_env", return_value=AISettings(enabled=True, api_key="test")), patch("src.ai_transport.build_opener", return_value=opener):
            with self.assertRaises(HTTPException) as error:
                ask_graph(self.graph, "R-001", "解释")
            self.assertEqual(error.exception.status_code, 502)

    def test_hit_interpretation_locks_verdict_citation_and_evidence(self):
        data = loader.load(ROOT / "samples/样例企业-审计材料.xlsx")
        finding = next(item for item in engine.run(self.rules, data) if item.status == "hit")
        answer = {
            "verdict": "hit", "citation": finding.rule.id,
            "plain_language": "账面收入高于申报收入，需要核对两者统计口径。",
            "why_flagged": "规则根据审计底稿中的确定性差额和阈值判定为命中。",
            "review_steps": ["先核对账面收入的取数范围。", "再核对申报表销售额口径。"],
        }
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer, ensure_ascii=False)}}]}
        opener = Mock()
        opener.open.return_value = BytesIO(json.dumps(response, ensure_ascii=False).encode())
        settings = AISettings(enabled=True, api_key="synthetic-test-key")
        with patch("webapp.knowledge.AISettings.from_env", return_value=settings), patch("src.ai_transport.build_opener", return_value=opener):
            result = interpret_finding(finding)
        self.assertEqual(result["verdict"], "hit")
        self.assertEqual(result["citation"], finding.rule.id)
        self.assertEqual(result["evidence_hash"], finding_evidence_hash(finding))
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(payload["model"], settings.effective_model)
        self.assertIn(finding.conclusion, payload["messages"][1]["content"])
        self.assertNotIn("synthetic-test-key", payload["messages"][1]["content"])

        invalid_answers = [
            {**answer, "verdict": "pass"},
            {**answer, "citation": "invented"},
            {key: value for key, value in answer.items() if key != "review_steps"},
        ]
        for invalid_answer in invalid_answers:
            with self.subTest(invalid_answer=invalid_answer):
                invalid = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(invalid_answer, ensure_ascii=False)}}]}
                bad_opener = Mock()
                bad_opener.open.return_value = BytesIO(json.dumps(invalid, ensure_ascii=False).encode())
                with patch("webapp.knowledge.AISettings.from_env", return_value=settings), patch("src.ai_transport.build_opener", return_value=bad_opener):
                    with self.assertRaises(HTTPException) as error:
                        interpret_finding(finding)
                self.assertEqual(error.exception.status_code, 502)

        with patch("webapp.knowledge.AISettings.from_env", return_value=AISettings()):
            with self.assertRaises(HTTPException) as error:
                interpret_finding(finding)
        self.assertEqual(error.exception.status_code, 503)

    def test_audit_narrative_requires_locked_summary_and_paragraph_citations(self):
        data = loader.load(ROOT / "samples/样例企业-审计材料.xlsx")
        findings = engine.run(self.rules, data)
        hit_ids = [item.rule.id for item in findings if item.status == "hit"]
        summary = {
            "total": len(findings),
            "hit": sum(item.status == "hit" for item in findings),
            "pass": sum(item.status == "pass" for item in findings),
            "skipped": sum(item.status == "skipped" for item in findings),
        }
        answer = {
            "summary": summary,
            "overall_assessment": [{"text": f"审计发现已锁定的风险事项，详见{hit_ids[0]}。", "citations": [hit_ids[0]]}],
            "recommendations": [{"text": "请先按证据链核对账面与申报口径。", "citations": hit_ids}],
        }
        settings = AISettings(enabled=True, api_key="synthetic-test-key")

        def opener_for(content):
            response = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
            opener = Mock()
            opener.open.return_value = BytesIO(json.dumps(response, ensure_ascii=False).encode())
            return opener

        opener = opener_for(json.dumps(answer, ensure_ascii=False))
        with patch("webapp.knowledge.AISettings.from_env", return_value=settings), patch("src.ai_transport.build_opener", return_value=opener):
            result = generate_audit_narrative(findings)
        self.assertEqual(result["summary"], summary)
        self.assertEqual(result["evidence_hash"], audit_narrative_hash(findings))
        self.assertEqual(result["overall_assessment"][0]["citations"], [hit_ids[0]])
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertIn(hit_ids[0], payload["messages"][1]["content"])
        self.assertNotIn("synthetic-test-key", payload["messages"][1]["content"])

        invalid_answers = [
            {**answer, "summary": {**summary, "hit": summary["hit"] + 1}},
            {**answer, "overall_assessment": [{"text": "没有引用。", "citations": []}]},
            {**answer, "recommendations": [{"text": "引用不存在。", "citations": ["R-999"]}]},
            {key: value for key, value in answer.items() if key != "recommendations"},
        ]
        for invalid_answer in invalid_answers:
            with self.subTest(invalid_answer=invalid_answer):
                bad_opener = opener_for(json.dumps(invalid_answer, ensure_ascii=False))
                with patch("webapp.knowledge.AISettings.from_env", return_value=settings), patch("src.ai_transport.build_opener", return_value=bad_opener):
                    with self.assertRaises(HTTPException) as error:
                        generate_audit_narrative(findings)
                self.assertEqual(error.exception.status_code, 502)

        malformed = opener_for("not-json")
        with patch("webapp.knowledge.AISettings.from_env", return_value=settings), patch("src.ai_transport.build_opener", return_value=malformed):
            with self.assertRaises(HTTPException) as error:
                generate_audit_narrative(findings)
        self.assertEqual(error.exception.status_code, 502)

        timeout = Mock()
        timeout.open.side_effect = TimeoutError()
        with patch("webapp.knowledge.AISettings.from_env", return_value=settings), patch("src.ai_transport.build_opener", return_value=timeout):
            with self.assertRaises(HTTPException) as error:
                generate_audit_narrative(findings)
        self.assertEqual(error.exception.status_code, 502)

        with patch("webapp.knowledge.AISettings.from_env", return_value=AISettings()):
            with self.assertRaises(HTTPException) as error:
                generate_audit_narrative(findings)
        self.assertEqual(error.exception.status_code, 503)


@material_key
class FindingInterpretationWebTests(unittest.TestCase):
    def test_hit_only_permissions_cache_and_restart_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interpretation.db"
            old_store = app_module.store
            app_module.store = Store(path)
            try:
                app_module.store.create_user("admin", "interpret-test-2026", "管理员", "org_admin", "org-a")
                app_module.store.create_user("student", "interpret-test-2026", "学生", "student", "org-a")
                with TestClient(app_module.app) as client:
                    login = client.post("/api/login", json={"username": "admin", "password": "interpret-test-2026"})
                    self.assertEqual(login.status_code, 200, login.text)
                    with (ROOT / "samples" / "样例企业-审计材料.xlsx").open("rb") as stream:
                        audit = enterprise_audit(client, files={"file": ("sample.xlsx", stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
                    self.assertEqual(audit.status_code, 200, audit.text)
                    body = audit.json()
                    audit_id = body["audit_id"]
                    hit_id = next(item["id"] for item in body["findings"] if item["status"] == "hit")
                    pass_id = next(item["id"] for item in body["findings"] if item["status"] == "pass")

                    def fake_interpret(finding):
                        return {
                            "verdict": "hit", "citation": finding.rule.id,
                            "plain_language": "这是持久化测试解读。", "why_flagged": "确定性规则已经命中。",
                            "review_steps": ["核对证据来源。"], "model": "test-model",
                            "evidence_hash": finding_evidence_hash(finding),
                        }

                    with patch("webapp.app.interpret_finding", side_effect=fake_interpret) as model_call:
                        endpoint = f"/api/audits/{audit_id}/findings/{hit_id}/interpretation"
                        first = client.post(endpoint)
                        self.assertEqual(first.status_code, 200, first.text)
                        self.assertFalse(first.json()["cached"])
                        app_module.store = Store(path)
                        second = client.post(endpoint)
                        self.assertEqual(second.status_code, 200, second.text)
                        self.assertTrue(second.json()["cached"])
                        self.assertEqual(model_call.call_count, 1)

                    rejected = client.post(f"/api/audits/{audit_id}/findings/{pass_id}/interpretation")
                    self.assertEqual(rejected.status_code, 422, rejected.text)
                    client.post("/api/logout")
                    client.post("/api/login", json={"username": "student", "password": "interpret-test-2026"})
                    forbidden = client.post(endpoint)
                    self.assertEqual(forbidden.status_code, 403, forbidden.text)

                reopened = Store(path)
                saved = reopened.get_finding_interpretation(
                    audit_id, hit_id, first.json()["evidence_hash"],
                )
                self.assertIsNotNone(saved)
                self.assertEqual(saved["citation"], hit_id)
            finally:
                app_module.store = old_store

    def test_audit_narrative_permissions_cache_reports_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "narrative.db"
            old_store = app_module.store
            app_module.store = Store(path)
            try:
                app_module.store.create_user("admin", "narrative-test-2026", "管理员", "org_admin", "org-a")
                app_module.store.create_user("student", "narrative-test-2026", "学生", "student", "org-a")
                with TestClient(app_module.app) as client:
                    login = client.post("/api/login", json={"username": "admin", "password": "narrative-test-2026"})
                    self.assertEqual(login.status_code, 200, login.text)
                    with (ROOT / "samples" / "样例企业-审计材料.xlsx").open("rb") as stream:
                        audit = enterprise_audit(client, files={"file": ("sample.xlsx", stream, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
                    self.assertEqual(audit.status_code, 200, audit.text)
                    body = audit.json()
                    audit_id = body["audit_id"]
                    hit_ids = [item["id"] for item in body["findings"] if item["status"] == "hit"]

                    def fake_narrative(findings):
                        return {
                            "summary": {
                                "total": len(findings),
                                "hit": sum(item.status == "hit" for item in findings),
                                "pass": sum(item.status == "pass" for item in findings),
                                "skipped": sum(item.status == "skipped" for item in findings),
                            },
                            "overall_assessment": [{"text": "总体测试<script>alert(1)</script>", "citations": [hit_ids[0]]}],
                            "recommendations": [{"text": "按证据链复核。", "citations": hit_ids}],
                            "model": "test-model", "evidence_hash": audit_narrative_hash(findings),
                        }

                    with patch("webapp.app.generate_audit_narrative", side_effect=fake_narrative) as model_call:
                        endpoint = f"/api/audits/{audit_id}/narrative"
                        first = client.post(endpoint)
                        self.assertEqual(first.status_code, 200, first.text)
                        self.assertFalse(first.json()["cached"])
                        app_module.store = Store(path)
                        second = client.post(endpoint)
                        self.assertEqual(second.status_code, 200, second.text)
                        self.assertTrue(second.json()["cached"])
                        self.assertEqual(model_call.call_count, 1)

                    detail = client.get(f"/api/audits/{audit_id}")
                    self.assertEqual(detail.status_code, 200, detail.text)
                    self.assertEqual(detail.json()["narrative"]["evidence_hash"], first.json()["evidence_hash"])
                    html = client.get(f"/api/report/{audit_id}/html")
                    self.assertEqual(html.status_code, 200, html.text)
                    self.assertIn("AI 辅助总体评价", html.text)
                    self.assertIn("来源规则：R-001", html.text)
                    self.assertIn("总体测试&lt;script&gt;alert(1)&lt;/script&gt;", html.text)
                    self.assertNotIn("总体测试<script>alert(1)</script>", html.text)
                    pdf = client.get(f"/api/report/{audit_id}")
                    self.assertEqual(pdf.status_code, 200, pdf.text)
                    self.assertTrue(pdf.content.startswith(b"%PDF"))

                    client.post("/api/logout")
                    client.post("/api/login", json={"username": "student", "password": "narrative-test-2026"})
                    forbidden = client.post(endpoint)
                    self.assertEqual(forbidden.status_code, 403, forbidden.text)

                reopened = Store(path)
                saved = reopened.get_audit_narrative(audit_id, first.json()["evidence_hash"])
                self.assertIsNotNone(saved)
                self.assertEqual(saved["overall_assessment"][0]["citations"], [hit_ids[0]])
            finally:
                app_module.store = old_store
