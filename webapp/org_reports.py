"""FR-D07: institution overview from frozen findings, not financial consolidation."""
from __future__ import annotations
from src import periods

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import quote
import uuid

from fastapi import Cookie, HTTPException, Query
from fastapi.responses import Response
from jinja2 import Environment
from pydantic import BaseModel, Field

from src import loader, render
from src.report_protection import protect_html
from webapp.report_archive import canonical, digest
from webapp.storage import deserialize_findings

TEMPLATE = render.TEMPLATE_DIR / "org_report.html"
LABELS = {"high":"有高等级命中", "medium":"有中等级命中", "low":"有低等级命中",
          "incomplete":"未命中但材料不足", "clear":"本次检查全部通过", "not_run":"未执行检查", "no_audit":"无匹配审计"}


def period_info(value):
    try:
        return periods.parse_period(value, "机构报告期间")
    except (loader.InputError, ValueError):
        raise ValueError("机构报告期间须为完整的月/季/半年/年度期间。") from None


def overview(sources, period="", client_ids=None):
    target = period_info(period) if period else None
    clients = sources["clients"]
    if client_ids is not None:
        wanted = set(client_ids)
        if not wanted or len(wanted) != len(client_ids):
            raise ValueError("请选择至少一位客户，且不能重复选择。")
        if not wanted <= {client["id"] for client in clients}:
            # Don't disclose whether an unavailable ID exists in another organization.
            raise LookupError("所选客户不存在或不属于当前机构。")
        clients = [client for client in clients if client["id"] in wanted]
    clients_by_id = {client["id"]: client for client in sources["clients"]}
    candidates, periods = {}, {}
    excluded = {"unlinked":0, "identity_mismatch":0, "invalid_period":0}
    for audit in sources["audits"]:
        client = clients_by_id.get(audit["client_id"])
        if client is None:
            excluded["unlinked"] += 1; continue
        if not client["taxpayer_id"] or audit["taxpayer_id"] != client["taxpayer_id"]:
            excluded["identity_mismatch"] += 1; continue
        try:
            parsed = period_info(audit["period"])
        except ValueError:
            excluded["invalid_period"] += 1; continue
        periods[parsed.key] = parsed
        if target is not None and parsed.key != target.key:
            continue
        # Sources arrive in saved-date/rowid descending order, so equal business
        # intervals keep their newest saved revision, including period aliases.
        rank = (parsed.end, parsed.start)
        previous = candidates.get(client["id"])
        if previous is None or rank > previous[0]:
            candidates[client["id"]] = (rank, audit)
    rows, distributions, rule_sets = [], {}, set()
    totals = {"clients":len(clients), "audited":0, "hit":0, "pass":0, "skipped":0,
              **{key:0 for key in LABELS}}
    for client in clients:
        row = {"client_id":client["id"], "name":client["name"], "taxpayer_id":client["taxpayer_id"],
               "accountant":client.get("accountant_name") or "未指派", "audit_id":None,
               "period":None, "audited_at":None, "audit_company_name":None,
               "period_start":None, "period_end":None,
               "counts":{"hit":0,"pass":0,"skipped":0}, "hits":[], "missing":[], "rules":[], "audit_sha256":None}
        candidate = candidates.get(client["id"])
        if candidate:
            audit = candidate[1]
            parsed = period_info(audit["period"])
            findings = deserialize_findings(json.loads(audit["findings_json"]))
            if len({finding.rule.id for finding in findings}) != len(findings):
                raise ValueError("历史审计包含重复规则，请核查原审计。")
            row.update({"audit_id":audit["id"], "period":audit["period"], "audited_at":audit["audited_at"],
                        "period_start":parsed.start.isoformat(),"period_end":parsed.end.isoformat(),
                        "audit_company_name":audit["company_name"],
                        "audit_sha256":digest(canonical({"dataset":json.loads(audit["dataset_json"]),
                                                         "findings":json.loads(audit["findings_json"])}).encode())})
            for finding in findings:
                if finding.status not in row["counts"]:
                    raise ValueError("历史审计包含无效执行状态，请核查原审计。")
                row["counts"][finding.status] += 1
                definition = asdict(finding.rule); definition.pop("source_file",None)
                definition_hash = digest(canonical(definition).encode())
                frozen = {"id":finding.rule.id,"version":finding.rule.version,"definition_sha256":definition_hash}
                row["rules"].append(frozen)
                key = (finding.rule.id, finding.rule.version, definition_hash)
                distribution = distributions.setdefault(key, {**frozen,"name":finding.rule.name,
                    "severity":finding.rule.severity,"hit":0,"pass":0,"skipped":0})
                distribution[finding.status] += 1
                if finding.status == "hit":
                    if finding.rule.severity not in {"high","medium","low"}:
                        raise ValueError("历史命中项风险等级无效，请核查原审计。")
                    row["hits"].append({**frozen,"name":finding.rule.name,"severity":finding.rule.severity,
                                        "explanation":finding.conclusion,"suggestion":finding.rule.suggestion})
                elif finding.status == "skipped":
                    row["missing"].append({**frozen,"name":finding.rule.name,"reason":finding.skip_reason})
            rule_sets.add(digest(canonical(sorted(row["rules"],key=lambda item:item["id"])).encode()))
            totals["audited"] += 1
            for key in row["counts"]: totals[key] += row["counts"][key]
            levels = {hit["severity"] for hit in row["hits"]}
            state = next((level for level in ("high","medium","low") if level in levels),
                         "incomplete" if row["counts"]["skipped"] else "clear" if row["counts"]["pass"] else "not_run")
        else:
            state = "no_audit"
        row.update({"state":state,"state_label":LABELS[state]}); totals[state] += 1; rows.append(row)
    order = {state:index for index,state in enumerate(("high","medium","low","incomplete","not_run","no_audit","clear"))}
    rows.sort(key=lambda row:(order[row["state"]],row["name"],row["client_id"]))
    return {"scope":"当前机构客户档案", "period":period if period else "各客户最新业务期间",
            "mixed_rule_sets":len(rule_sets)>1, "mixed_periods":len({period_info(row["period"]).key for row in rows if row["audit_id"]})>1,
            "totals":totals,"rows":rows,"excluded_audits":excluded,
            "available_periods":[{"value":item.label,"label":item.label} for item in sorted(periods.values(),key=lambda p:(p.end,p.start),reverse=True)],
            "rule_distribution":sorted(distributions.values(),key=lambda row:(-row["hit"],row["id"],row["version"],row["definition_sha256"]))}


class OrgReportBody(BaseModel):
    period: str = Field(default="",max_length=80)
    client_ids: list[str] | None = Field(default=None,max_length=1000)


def register(app, store_provider, user_for_session, allow, cookie_name):
    def actor(session):
        user = user_for_session(session); allow(user,"org_admin"); return user

    def draft(user, period="", client_ids=None):
        sources = store_provider().org_report_sources(user)
        try:
            return overview(sources,period.strip(),client_ids), sources["branding"]
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None

    def archived(user, report_id):
        try:
            result = store_provider().get_org_report(report_id,user)
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from None
        if result is None: raise HTTPException(404,"机构报告不存在。")
        return result

    @app.get("/api/org/overview")
    def get_overview(period: str = Query(default="",max_length=80), session: str | None = Cookie(default=None,alias=cookie_name)):
        user = actor(session)
        result,_ = draft(user,period)
        return result

    @app.post("/api/org/reports")
    def create_report(body: OrgReportBody, session: str | None = Cookie(default=None,alias=cookie_name)):
        user = actor(session); data,branding = draft(user,body.period,body.client_ids)
        if not data["rows"]: raise HTTPException(422,"当前机构暂无客户档案，请先建立客户档案。")
        report_id = uuid.uuid4().hex
        template = TEMPLATE.read_text(encoding="utf-8")
        snapshot = {**data,"schema":1,"id":report_id,"org_id":user["org_id"],"created_by":user["id"],
                    "created_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "report_no":"TP-ORG-"+report_id[:12].upper(),"branding":branding,"template_sha256":digest(template.encode())}
        html = Environment(autoescape=True,trim_blocks=True,lstrip_blocks=True).from_string(template).render(report=snapshot)
        label = f"{branding['display_name']} · 多客户 {len(data['rows'])} 户（名单见正文）"
        html, snapshot["protection"] = protect_html(html,label,snapshot["created_at"][:10],snapshot["report_no"],
                                                   context=canonical(snapshot),registered=True)
        saved = store_provider().save_org_report(user,snapshot,html)
        return {"id":report_id,"snapshot":snapshot,"html_sha256":saved["html_sha256"],"snapshot_sha256":saved["snapshot_sha256"]}

    @app.get("/api/org/reports")
    def list_reports(session: str | None = Cookie(default=None,alias=cookie_name)):
        return store_provider().list_org_reports(actor(session))

    @app.get("/api/org/reports/{report_id}")
    def get_report(report_id: str, session: str | None = Cookie(default=None,alias=cookie_name)):
        result = archived(actor(session),report_id)
        return {key:result[key] for key in ("id","created_at","snapshot","html_sha256","snapshot_sha256","pdf_sha256","pdf_created_at")}

    @app.get("/api/org/reports/{report_id}/html")
    def html_report(report_id: str, session: str | None = Cookie(default=None,alias=cookie_name)):
        user=actor(session); result=archived(user,report_id)
        store_provider().log(user,"view_org_report","org_report",report_id)
        return Response(result["html"],media_type="text/html",headers={"Cache-Control":"private, no-store","X-TaxPearls-SHA256":result["html_sha256"]})

    @app.get("/api/org/reports/{report_id}/pdf")
    def pdf_report(report_id: str, session: str | None = Cookie(default=None,alias=cookie_name)):
        user=actor(session); result=archived(user,report_id)
        if result["pdf_bytes"] is None:
            fd,path=tempfile.mkstemp(suffix=".pdf"); os.close(fd)
            try:
                render.export_pdf(result["html"],path,report_no=result["snapshot"]["report_no"],footer_text=result["snapshot"]["branding"]["footer_text"])
                result=store_provider().attach_org_report_pdf(report_id,user,Path(path).read_bytes())
            except RuntimeError:
                raise HTTPException(500,"机构报告 PDF 生成失败，请稍后重试。") from None
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from None
            finally:
                Path(path).unlink(missing_ok=True)
        store_provider().log(user,"export_org_report","org_report",report_id)
        return Response(result["pdf_bytes"],media_type="application/pdf",headers={"Cache-Control":"private, no-store",
            "Content-Disposition":"attachment; filename*=UTF-8''"+quote("机构风险总览-"+result["snapshot"]["report_no"]+".pdf"),
            "X-TaxPearls-SHA256":result["pdf_sha256"]})
