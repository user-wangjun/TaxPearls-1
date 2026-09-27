from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from fastapi.testclient import TestClient

from src import engine, materials
from src.ai_extraction import AIExtractor, Extraction, ExtractionError, call_model
from src.settings import AISettings
from webapp import app as app_module
from webapp.storage import Store
from test_materials import COMPANY, FIXTURES, workbook

ROOT = Path(__file__).resolve().parents[1]
CATALOG = {key: value for rule in engine.load_rules(ROOT / "rules") for key, value in rule.inputs.items()}
SETTINGS = AISettings(enabled=True, api_key="synthetic-secret", vision=False)


def row(value="10", unit="万元", quote="销售额 10", **kw):
    return {"name": "增值税.销售额", "raw_value": value, "unit": unit, "page": 1,
            "quote": quote, "detail": "销售额本期不含税合计", "uncertain": False, **kw}


def answer(rows, company=COMPANY):
    return Extraction(company=company, rows=rows, warnings=[])


def document(text="单位：万元\n销售额 10", pages=None):
    return {"id": "0", "name": "合成.pdf", "kind": "pdf", "company": deepcopy(COMPANY), "rows": [],
            "pages": pages or [{"page": 1, "text": text}], "page_count": len(pages) if pages else 1,
            "accounts": [], "declarations": {}, "warnings": [], "fingerprint": "test", "error": ""}


def envelope(content, finish="stop"):
    return json.dumps({"choices": [{"finish_reason": finish, "message": {"content": content}}]}).encode()


class AIContractTests(unittest.TestCase):
    def test_alias_config_and_no_secret_in_public_status(self):
        self.assertEqual(SETTINGS.effective_model, "deepseek-flash")
        proxy = replace(SETTINGS, base_url="https://gateway.example/v1")
        self.assertEqual(proxy.effective_model, "ds-v4.1f")
        self.assertNotIn(SETTINGS.api_key, json.dumps(SETTINGS.public_status()))
        self.assertNotIn(SETTINGS.api_key, repr(SETTINGS))
        self.assertTrue(replace(SETTINGS, api_key="").problem())
        for url in ("http://external.example", "https://user:secret@example.com", "https://example.com?key=secret", "https://example.com/chat/completions"):
            self.assertTrue(replace(SETTINGS, base_url=url).problem())

    def test_real_http_protocol_without_external_model(self):
        observed = {}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                observed.update(path=self.path, authorization=self.headers["Authorization"],
                                payload=json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                data = envelope(answer([row()]).model_dump_json())
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = call_model(replace(SETTINGS, base_url=f"http://127.0.0.1:{server.server_port}/v1"), [{"role":"user","content":"synthetic"}], 5)
            self.assertEqual(result.rows[0].raw_value, "10")
            self.assertEqual(observed["path"], "/v1/chat/completions")
            self.assertEqual(observed["authorization"], "Bearer synthetic-secret")
            self.assertEqual(observed["payload"]["model"], "ds-v4.1f")
            self.assertEqual(observed["payload"]["response_format"], {"type":"json_object"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_bad_json_truncation_timeout_and_upstream_errors_are_safe(self):
        cases = [envelope("{}"), envelope("```json\n{}\n```"), envelope(answer([row()]).model_dump_json(), "length"), b'{}']
        for data in cases:
            opener = Mock()
            opener.open.return_value = BytesIO(data)
            with patch("src.ai_transport.build_opener", return_value=opener), self.assertRaises(ExtractionError):
                call_model(SETTINGS, [], 5)
        for error in (TimeoutError("synthetic-secret"), HTTPError("https://example.com", 429, "synthetic-secret", {}, BytesIO(b"private data"))):
            opener = Mock()
            opener.open.side_effect = error
            with patch("src.ai_transport.build_opener", return_value=opener):
                with self.assertRaises(ExtractionError) as caught:
                    call_model(SETTINGS, [], 5)
                self.assertNotIn("synthetic-secret", str(caught.exception))
                self.assertNotIn("private data", str(caught.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_conversion_reference_validation_and_missing_values(self):
        doc = document("单位：万元\n销售额 10\n进项税额 0\n应纳税额 6")
        response = answer([row(), row("0", "元", "进项税额 0", name="增值税.进项税额"),
                           row(None, "元", "应纳税额 6", name="增值税.应纳税额"),
                           row("99", "元", "销售额 10", uncertain=True),
                           row("999", "元", "不存在的原文 999"), row(name="虚构指标")])
        AIExtractor(SETTINGS, CATALOG, lambda *_: response).enrich(doc, b"")
        self.assertEqual([r["value"] for r in doc["rows"]], ["100000", "0", "", "", ""])
        self.assertEqual(doc["extraction"]["method"], "ai")
        self.assertTrue(doc["rows"][-1]["ai_issues"])
        self.assertTrue(any("未知指标" in w for w in doc["warnings"]))

    def test_scan_rasterization_and_review_required(self):
        captured = []
        def transport(settings, messages, timeout):
            captured.extend(messages[1]["content"])
            return answer([row("100000", "元", "增值税.销售额：100000")])
        docs = materials.preview([("scan.pdf", (FIXTURES/"materials-scanned.pdf").read_bytes())], set(CATALOG),
                                 AIExtractor(replace(SETTINGS, vision=True), CATALOG, transport))
        doc = docs[0]
        self.assertFalse(doc["error"], doc)
        self.assertEqual(doc["extraction"]["method"], "ai", doc)
        self.assertTrue(any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in captured))
        with self.assertRaisesRegex(materials.InputError, "核对"):
            materials.build_dataset(docs, {"0":{"id":"0"}}, {}, set(CATALOG))
        dataset = materials.build_dataset(docs, {"0":{"id":"0", "reviewed":True}}, {}, set(CATALOG))
        self.assertEqual(dataset.get("增值税.销售额"), 100000)
        self.assertIn("AI 提取模型 deepseek-flash", dataset.source_of("增值税.销售额"))
        self.assertIn("100000 元", dataset.detail_of("增值税.销售额"))

    def test_invoice_tax_cannot_become_declaration_or_ledger_value(self):
        source = "纸质增值税普通发票，发票号码 TEST-1，税额 13000.00 元"
        doc = document("", pages=[{"page": 1, "text": ""}])
        result = answer([row("13000.00", "元", "税额 13000.00", name="增值税.销项税额",
                             detail=source),
                         row("13000.00", "元", "税额 13000.00", name="账面.销项税额",
                             detail=source)])
        AIExtractor(replace(SETTINGS, vision=True), CATALOG, lambda *_: result).enrich(
            doc, (FIXTURES / "materials-scanned.pdf").read_bytes())
        self.assertEqual([item["value"] for item in doc["rows"]], ["", ""])
        self.assertTrue(all("不能直接作为" in item["ai_issues"][-1] for item in doc["rows"]))

    def test_conflicting_candidates_are_flagged_and_cannot_be_committed(self):
        doc = document("单位：元\n销售额 本期 100000 累计 200000")
        response = answer([row("100000", "元", "销售额 本期 100000 累计 200000"),
                           row("200000", "元", "销售额 本期 100000 累计 200000")])
        AIExtractor(SETTINGS, CATALOG, lambda *_: response).enrich(doc, b"")
        self.assertEqual(doc["extraction"]["needs_attention"], 2)
        self.assertTrue(all("不同候选值" in item["ai_issues"][-1] for item in doc["rows"]))
        with self.assertRaisesRegex(materials.InputError, "冲突"):
            materials.build_dataset([doc], {"0": {"id": "0", "reviewed": True}}, {}, set(CATALOG))
        # Decimal-equivalent representations are not a conflict.
        response.rows[1].raw_value = "100000.00"
        AIExtractor(SETTINGS, CATALOG, lambda *_: response).enrich(doc, b"")
        self.assertEqual(doc["extraction"]["needs_attention"], 0)

    def test_unknown_unit_retains_missing_value_after_review(self):
        doc = document("销售额 100000")
        AIExtractor(SETTINGS, CATALOG, lambda *_: answer([
            row("100000", "不明", "销售额 100000", uncertain=True)
        ])).enrich(doc, b"")
        self.assertEqual(doc["rows"][0]["value"], "")
        self.assertIn("原始单位不明", doc["rows"][0]["ai_issues"])
        with self.assertRaisesRegex(materials.InputError, "没有可执行"):
            materials.build_dataset([doc], {"0": {"id": "0", "reviewed": True}}, {}, set(CATALOG))

    def test_failure_preserves_local_candidates_without_claiming_ai(self):
        def fail(*_):
            raise ExtractionError("模拟超时")
        doc = materials.preview([("text.pdf", (FIXTURES/"materials-text.pdf").read_bytes())], set(CATALOG), AIExtractor(SETTINGS, CATALOG, fail))[0]
        self.assertFalse(doc["error"])
        self.assertEqual(doc["extraction"]["method"], "ai_failed")
        self.assertTrue(doc["rows"])
        self.assertIn("模拟超时", doc["warnings"][0])

    def test_unmapped_excel_and_standard_errors_are_not_overwritten(self):
        raw = workbook("导出明细", [["单位", "万元"], ["销售额", 10]])
        extractor = AIExtractor(SETTINGS, CATALOG, lambda *_: answer([row(quote="A2=销售额 | B2=10")]))
        docs = materials.preview([("raw.xlsx", raw)], set(CATALOG), extractor)
        self.assertFalse(docs[0]["error"], docs)
        data = materials.build_dataset(docs, {"0":{"id":"0","reviewed":True}}, {}, set(CATALOG))
        self.assertEqual(data.get("增值税.销售额"), 100000)
        self.assertIn("工作表：导出明细", data.source_of("增值税.销售额"))
        raw = workbook("利润表", [["项目", "本期金额"], ["营业收入", "=1+2"]], COMPANY)
        doc = materials.preview([("bad.xlsx", raw)], set(CATALOG), extractor)[0]
        self.assertIn("标准工作表校验失败", doc["error"])

    def test_limits_and_conflicting_identity_discard_partial_response(self):
        doc = document(pages=[{"page":i,"text":"销售额 10"} for i in range(1,5)])
        calls=[]
        def transport(*_):
            calls.append(1)
            return answer([row()])
        with self.assertRaises(ExtractionError):
            AIExtractor(replace(SETTINGS, max_calls=1), CATALOG, transport).enrich(doc,b"")
        self.assertEqual(doc["rows"], [])
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(ExtractionError, "不一致"):
            AIExtractor(SETTINGS, CATALOG, lambda *_: answer([], {**COMPANY,"taxpayer_id":"OTHER"})).enrich(document(), b"")


class AIWebTests(unittest.TestCase):
    def test_background_job_owner_schema_status_and_audit(self):
        with tempfile.TemporaryDirectory() as td:
            old = app_module.store
            app_module.store = Store(Path(td)/"ai.db")
            try:
                app_module.store.create_user("aiadmin", "ai-test-pass-2026", "AI测试", "org_admin", "default")
                with TestClient(app_module.app) as client, patch("webapp.material_upload.AISettings.from_env", return_value=replace(SETTINGS,vision=True)), patch("src.ai_extraction.call_model", return_value=answer([row("100000","元","增值税.销售额：100000")])):
                    client.post("/api/login",json={"username":"aiadmin","password":"ai-test-pass-2026"})
                    status = client.get("/api/materials/config")
                    self.assertTrue(status.json()["ready"])
                    self.assertNotIn(SETTINGS.api_key, status.text)
                    started = client.post("/api/materials/preview", data={"extraction":"ai"},files={"files":("scan.pdf",(FIXTURES/"materials-scanned.pdf").read_bytes())})
                    self.assertEqual(started.status_code, 202, started.text)
                    url = "/api/materials/jobs/" + started.json()["job_id"]
                    job = client.get(url).json()
                    self.assertEqual(job["state"], "done", job)
                    draft = job["result"]
                    result=client.post("/api/materials/audit",json={"token":draft["token"],"mode":"separate","selections":[{"id":"0","reviewed":True}]})
                    self.assertEqual(len(result.json()["results"]),1,result.text)
                    client.post("/api/logout")
                    self.assertEqual(client.get(url).status_code,401)
                    app_module.store.create_user("otherai", "ai-test-pass-2026", "其他", "org_admin", "default")
                    client.post("/api/login",json={"username":"otherai","password":"ai-test-pass-2026"})
                    self.assertEqual(client.get(url).status_code,404)
            finally:
                app_module.store = old


if __name__ == "__main__":
    unittest.main()
