"""TaxPearls P1 web application.

Persistent audits, Argon2 authentication, role-based access control, client
ownership, audit logs, rule switches and deterministic training/marking.
Uploaded workbook bytes stay in memory; only normalized evidence is stored.
"""
from __future__ import annotations
from src import periods

import base64
from copy import deepcopy
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Annotated, Literal
from urllib.parse import quote, urlsplit

from fastapi import BackgroundTasks, Cookie, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, ConfigDict, AwareDatetime
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool
from webapp.uploads import multipart, single_file

from src import engine, loader, render, training, sandbox_feedback
from src import settings as _environment_settings  # noqa: F401 - Load .env before Store and route initialization.
from src.mailer import MailError, send_password_reset_email, send_registration_code_email, send_login_code_email
from src.models import Dataset, Rule
from webapp import captcha, classroom, members, email_auth
from webapp.access import AccessDenied
from webapp.notifications import NotificationWorker, email_delivery_enabled
from webapp.storage import SetupAlreadyInitialized, Store
from webapp.login_guard import LoginGuard, RateLimiter
from webapp.knowledge import (
    ai_config, ask_graph, audit_narrative_hash, build_graph,
    finding_evidence_hash, generate_audit_narrative, interpret_finding,
)

ROOT = Path(__file__).resolve().parent.parent
RULES_DIR = ROOT / "rules"
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOGO_SVG = ROOT / "logo" / "logo-shui-hai-shi-zhu.svg"
MAX_ORG_LOGO_BYTES = 512 * 1024
MAX_NORMALIZED_LOGO_BYTES = 1024 * 1024
MAX_ORG_LOGO_EDGE = 1200
COOKIE_NAME = "taxpearls_session"
EMAIL_COOKIE_NAME = "taxpearls_email_browser"

@asynccontextmanager
async def lifespan(application):
    global store
    if store is None:
        store = Store()
    worker = NotificationWorker(lambda: store)
    worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title="税海拾珠 · 税务风险审计", version="1.0.0", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.exception_handler(AccessDenied)
async def denied_access(_request: Request, exc: AccessDenied):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


@app.middleware("http")
async def private_api_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers.append('Vary', 'Cookie')
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, exc: RequestValidationError):
    # Do not echo credentials/raw request data, including JSON NaN/Infinity
    # which the default validation response cannot safely JSON-encode.
    return JSONResponse(status_code=422,content={"detail":[
        {"loc":error["loc"],"type":error["type"],"msg":error["msg"]} for error in exc.errors()
    ]},headers={"Cache-Control":"no-store"})
store: Store | None = None
login_guard = LoginGuard()
reset_limiter = RateLimiter({"email": (1, 15 * 60), "ip": (10, 60 * 60)})
register_code_limiter = RateLimiter({"email": (1, 15 * 60), "ip": (10, 60 * 60)})
register_complete_limiter = RateLimiter({"email": (10, 15 * 60), "ip": (60, 60 * 60)})
reset_confirm_limiter = RateLimiter({"token": (5, 15 * 60), "ip": (40, 15 * 60)})
captcha_limiter = RateLimiter({"ip": (30, 60)})
email_verify_limiter = RateLimiter({"token": (10, 15 * 60), "ip": (60, 15 * 60)})
email_login_limiter = RateLimiter({"email": (10, 15 * 60), "ip": (60, 15 * 60)})


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


class SetupBody(BaseModel):
    username: str
    password: str
    display_name: str = ""
    email: str = ""
    org_id: str = "default"


class LoginBody(BaseModel):
    username: str
    password: str


class UserBody(BaseModel):
    username: str
    password: str
    display_name: str
    role: str
    email: str = ""
    org_id: str | None = None


class PasswordResetBody(BaseModel):
    email: str = Field(max_length=254)


class PasswordResetConfirmBody(BaseModel):
    token: str = Field(max_length=512)
    password: str = Field(max_length=128)


class EmailStartBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(max_length=254)
    captcha_id: str = Field(max_length=64)
    captcha_answer: str = Field(max_length=16)
    purpose: Literal['register','login'] = 'register'
    invite_code: str = Field(default='',max_length=128)


class EmailMagicBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(max_length=128)


class EmailLoginBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(max_length=254)
    code: str = Field(max_length=16)


class RegisterCompleteBody(BaseModel):
    model_config = {"extra": "forbid"}
    email: str = Field(max_length=254)
    code: str = Field(max_length=16)
    invite_code: str = Field(max_length=128)
    password: str = Field(max_length=128)
    email_proof: str = Field(default='',max_length=128)


class InviteBody(BaseModel):
    """创始码只捆机构名称：一码一位、私聊交付，邮箱不绑定（注册侧有邮箱验证码兜底）。"""
    org_name: str = Field(min_length=1, max_length=120)
    seats: int = Field(default=1, ge=1, le=200)
    bound_email: str = ""
    expires_days: int = Field(default=7, ge=1, le=30)


class ClientBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    taxpayer_id: str = Field(min_length=1, max_length=64)
    accountant_id: str | None = None


class AssignmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1,max_length=200)
    audit_id: str = Field(min_length=1,max_length=64)
    target_student_id: str | None = None
    weights: dict[str, classroom.Weight] = Field(default_factory=dict)
    false_positive_penalty: float = Field(default=5.0, ge=0, le=100,allow_inf_nan=False)
    published: bool = True
    class_id: str | None = Field(default=None,min_length=1,max_length=64)
    deadline_at: AwareDatetime | None = None


class SubmissionBody(BaseModel):
    selected_rule_ids: list[str]


class SandboxFeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["mark_risk", "calculate"]
    rule_id: str | None = Field(default=None, max_length=32)
    evidence_metrics: list[Annotated[str, Field(max_length=160, strict=True)]] = Field(default_factory=list, max_length=100)
    left: str | None = Field(default=None, max_length=160)
    right: str | None = Field(default=None, max_length=160)
    operation: Literal["difference", "ratio"] = "difference"
    period_mode: Literal["same_period", "year_on_year"] = "same_period"


class ReviewBody(BaseModel):
    adjusted_score: float = Field(ge=0, le=100)
    feedback: str = ""


class RuleStateBody(BaseModel):
    enabled: bool


class NotificationPreferencesBody(BaseModel):
    audit_completed: bool = False
    high_risk: bool = False
    email_enabled: bool = False


class RuleParametersBody(BaseModel):
    expected_version: str = Field(min_length=1, max_length=32)
    new_version: str = Field(min_length=1, max_length=32)
    logic: dict[str, Any]
    threshold_basis: str = Field(min_length=1, max_length=500)
    effective_from: str | None = Field(default=None, max_length=10)
    effective_to: str | None = Field(default=None, max_length=10)


class RuleTrialBody(RuleParametersBody):
    audit_id: str = Field(min_length=1, max_length=64)


class OrgSettingsBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    report_title: str = Field(min_length=1, max_length=120)
    footer_text: str = Field(default="", max_length=300)


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": message})


def _user(session: str | None) -> dict[str, Any]:
    user = store.user_for_token(session)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录。")
    return user


def _allow(user: dict[str, Any], *roles: str) -> None:
    if user["role"] not in roles:
        raise HTTPException(status_code=403, detail="当前角色无权执行此操作。")


def _audit_or_404(audit_id: str, user: dict[str, Any]) -> dict[str, Any]:
    entry = store.get_audit_for_user(audit_id, user)
    if not entry:
        raise HTTPException(status_code=404, detail="审计结果不存在或无权访问。")
    return entry


def _company_dict(dataset: Dataset) -> dict[str, str]:
    c = dataset.company
    return {"name": c.name, "taxpayer_id": c.taxpayer_id, "industry": c.industry, "period": c.period}


def _normalize_org_logo(content: bytes) -> tuple[str, bytes]:
    """Decode, constrain and re-encode logos so stored bytes cannot contain active content."""
    if not content:
        raise ValueError("Logo 文件为空。")
    if len(content) > MAX_ORG_LOGO_BYTES:
        raise ValueError("Logo 文件不得超过 512KB。")
    try:
        with Image.open(BytesIO(content)) as image:
            source_format = image.format
            if source_format not in {"PNG", "JPEG"}:
                raise ValueError("Logo 仅支持 PNG 或 JPEG 图片。")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("Logo 必须是静态图片。")
            width, height = image.size
            if width < 1 or height < 1 or width > MAX_ORG_LOGO_EDGE or height > MAX_ORG_LOGO_EDGE:
                raise ValueError("Logo 宽高须在 1–1200 像素之间。")
            image.load()
            normalized = ImageOps.exif_transpose(image)
            output = BytesIO()
            if source_format == "JPEG":
                normalized.convert("RGB").save(output, format="JPEG", quality=90, optimize=True)
                mime = "image/jpeg"
            else:
                mode = "RGBA" if "A" in normalized.getbands() or "transparency" in normalized.info else "RGB"
                normalized.convert(mode).save(output, format="PNG", optimize=True, compress_level=9)
                mime = "image/png"
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Logo 不是有效的 PNG 或 JPEG 图片。") from exc
    data = output.getvalue()
    if len(data) > MAX_NORMALIZED_LOGO_BYTES:
        raise ValueError("Logo 规范化后过大，请降低图片复杂度或尺寸。")
    return mime, data


def _org_branding(org_id: str) -> dict[str, Any]:
    settings = store.get_org_settings(org_id)
    logo = store.get_org_logo(org_id)
    settings["logo_data_uri"] = (
        f"data:{logo[0]};base64,{base64.b64encode(logo[1]).decode('ascii')}" if logo else ""
    )
    return settings


def _safe_filename_component(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(". ")
    return cleaned[:80] or "审计报告"


def _version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+){1,2}", value.strip()):
        raise ValueError("规则版本须为 2.1 或 2.1.0 形式。")
    parts = [int(part) for part in value.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def _effective_rules() -> list[Any]:
    overrides = store.rule_overrides()
    histories = store.rule_versions()
    effective = []
    for base in engine.load_rules(RULES_DIR):
        override = overrides.get(base.id)
        if override and _version_tuple(override["version"]) > _version_tuple(base.version):
            record = next((item for item in histories.get(base.id, []) if item["version"] == override["version"]), None)
            frozen = Rule(**record["rule"]) if record and record.get("rule") else base
            candidate = replace(
                frozen, version=override["version"], logic=deepcopy(override["logic"]),
                threshold_basis=override["threshold_basis"],
                effective_from=override["effective_from"], effective_to=override["effective_to"],
            )
            engine.validate_rule_update(candidate)
            effective.append(candidate)
        else:
            effective.append(base)
    return effective


def _audit_rules(dataset: Dataset, enabled: set[str] | None = None) -> list[Any]:
    """Select one rule version covering the complete audited period.

    An interval crossing a version boundary needs a split-period audit; choosing
    a version by the end date would silently apply it to earlier transactions.
    """
    period = periods.parse_period(dataset.company.period, "审计所属期")
    histories = store.rule_versions()
    selected = []
    for base in engine.load_rules(RULES_DIR):
        if enabled is not None and base.id not in enabled:
            continue
        versions = histories.get(base.id, [])
        overlapping = [item for item in versions if item["effective_from"]
                       and item["effective_from"] <= period.end.isoformat()
                       and (item["effective_to"] or "9999-12-31") >= period.start.isoformat()]
        if overlapping:
            item = overlapping[0]
            if len(overlapping) != 1 or item["effective_from"] > period.start.isoformat() or (item["effective_to"] or "9999-12-31") < period.end.isoformat():
                raise HTTPException(422, f"规则 {base.id} 生效期跨越审计所属期；请拆分期间审计，不能混用版本。")
        else:
            undated = [item for item in versions if not item["effective_from"]]
            item = undated[-1] if undated else None
        if item:
            frozen = Rule(**item["rule"]) if item.get("rule") else base
            candidate = replace(frozen, version=item["version"], logic=deepcopy(item["logic"]),
                                threshold_basis=item["threshold_basis"],
                                effective_from=item["effective_from"], effective_to=item["effective_to"])
            selected.append(engine.validate_rule_update(candidate))
        else:
            selected.append(base)
    return selected


def _rule_by_id(rule_id: str) -> Any:
    rule = next((item for item in _effective_rules() if item.id == rule_id), None)
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在。")
    return rule


def _candidate_rule(rule: Any, body: RuleParametersBody) -> Any:
    if body.expected_version.strip() != rule.version:
        raise HTTPException(status_code=409, detail=f"规则已更新为 v{rule.version}，请刷新后重试。")
    try:
        current_version = _version_tuple(rule.version)
        new_version = _version_tuple(body.new_version.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if new_version <= current_version:
        raise HTTPException(status_code=422, detail=f"新版本必须高于当前 v{rule.version}。")
    effective_from = body.effective_from or None
    effective_to = body.effective_to or None
    if effective_to and not effective_from:
        raise HTTPException(422, "规则终止日期必须同时提供起始日期。")
    for value in (effective_from, effective_to):
        if value:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise HTTPException(422, "规则生效日期须为 YYYY-MM-DD。")
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(422, "规则生效日期须为有效的 YYYY-MM-DD。") from None
    if effective_from and effective_to and effective_from > effective_to:
        raise HTTPException(422, "规则生效终止日不能早于起始日。")
    candidate = replace(
        rule, version=body.new_version.strip(), logic=deepcopy(body.logic),
        threshold_basis=body.threshold_basis.strip(),
        effective_from=effective_from, effective_to=effective_to,
    )
    try:
        return engine.validate_rule_update(candidate)
    except engine.RuleError as exc:
        raise HTTPException(status_code=422, detail=f"规则参数无效：{exc}") from None


def _rule_payload(rule: Any, enabled: bool, override: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": rule.id, "name": rule.name, "category": rule.category,
        "severity": rule.severity, "version": rule.version, "enabled": enabled,
        "logic": rule.logic, "inputs": rule.inputs,
        "threshold_basis": rule.threshold_basis,
        "effective_from": rule.effective_from, "effective_to": rule.effective_to,
        "customized": bool(override and override.get("version") == rule.version),
        "updated_at": override.get("updated_at") if override else None,
    }


def _trial_payload(finding: Any, audit_id: str) -> dict[str, Any]:
    return {
        "audit_id": audit_id, "rule_id": finding.rule.id,
        "version": finding.rule.version, "name": finding.rule.name,
        "effective_from": finding.rule.effective_from,
        "effective_to": finding.rule.effective_to,
        "status": finding.status, "severity": finding.rule.severity,
        "conclusion": finding.conclusion, "calculation": finding.calculation,
        "threshold_desc": finding.threshold_desc, "skip_reason": finding.skip_reason,
        "evidence": [
            {"label": item.label, "value": item.value, "source": item.source}
            for item in finding.evidence
        ],
    }


def _is_synthetic_dataset(dataset: Dataset) -> bool:
    """Use one definition for teaching-data checks across audit and training."""
    from webapp.access import is_teaching_dataset
    return is_teaching_dataset(dataset)


def _metrics_list(dataset: Dataset) -> list[dict[str, str]]:
    return [
        {"name": m.name, "value": f"{m.value:,.2f}", "source": m.source, "detail": m.detail}
        for m in dataset.metrics.values()
    ]


def _result(entry: dict[str, Any]) -> dict[str, Any]:
    dataset, findings = entry["dataset"], entry["findings"]
    vm = render.build_view_model(dataset, findings)
    narrative = store.get_audit_narrative(entry["id"], audit_narrative_hash(findings))
    return {
        "audit_id": entry["id"], "audited_at": entry["audited_at"],
        "company": _company_dict(dataset), "summary": vm["summary"],
        "findings": vm["findings"], "metrics": _metrics_list(dataset),
        "narrative": narrative,
        "material_reference": entry.get("material_reference"),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/p/{code}")
def member_invitation_landing(code: str) -> FileResponse:
    # This route never authenticates or reveals an institution. Full-hash
    # validation happens only after email proof in the registration transaction.
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html",
                        headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer",
                                 "X-Robots-Tag":"noindex, nofollow"})


@app.get("/logo")
def logo() -> Response:
    return FileResponse(LOGO_SVG, media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return FileResponse(LOGO_SVG, media_type="image/svg+xml")


@app.get("/auth-ocean-data-v1.webp")
def auth_ocean_visual() -> FileResponse:
    return FileResponse(STATIC_DIR / "auth-ocean-data-v1.webp", media_type="image/webp")


@app.get("/auth-pearl-real-v1.webp")
def auth_pearl_visual() -> FileResponse:
    return FileResponse(STATIC_DIR / "auth-pearl-real-v1.webp", media_type="image/webp")


@app.get("/workspace.js")
def workspace_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "workspace.js", media_type="text/javascript")


@app.get("/classroom.js")
def classroom_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "classroom.js", media_type="text/javascript")


@app.get("/mistake-book.js")
def mistake_book_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "mistake-book.js", media_type="text/javascript")


@app.get("/console.js")
def console_script():
    return FileResponse(STATIC_DIR / "console.js", media_type="text/javascript")


@app.get("/enterprise.js")
def enterprise_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "enterprise.js", media_type="text/javascript")


@app.get("/workspace.css")
def workspace_styles() -> FileResponse:
    return FileResponse(STATIC_DIR / "workspace.css", media_type="text/css")


@app.get("/api/dashboard")
def dashboard(session: str | None = Cookie(default=None, alias=COOKIE_NAME),
              company: str | None = Query(default=None, max_length=200),
              page: int = Query(default=1, ge=1), page_size: int = Query(default=24, ge=1, le=100)):
    from webapp.dashboard import collect
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    return collect(store, user, company, page, page_size)


from webapp.org_reports import register as register_org_reports
register_org_reports(app, lambda: store, _user, _allow, COOKIE_NAME)

from webapp.report_verification import register as register_report_verification
register_report_verification(app, lambda: store, _user, _allow, _audit_or_404, COOKIE_NAME)


@app.get("/api/knowledge")
def knowledge(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    _user(session)
    enabled = store.enabled_rule_ids()
    return [{"id": r.id, "name": r.name, "category": r.category,
             "tax_type": r.tax_type, "severity": r.severity, "scope": r.scope,
             "inputs": r.inputs, "evidence": r.evidence, "legal_basis": r.legal_basis,
             "references": r.references, "suggestion": r.suggestion,
             "enabled": enabled is None or r.id in enabled}
            for r in _effective_rules()]


@app.get("/api/status")
def status(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    return {"needs_setup": not store.has_users(), "user": store.user_for_token(session)}


class GraphQuestion(BaseModel):
    node_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=2000)
    audit_id: str | None = None


def _graph_for_user(user, audit_id):
    entry = None
    if audit_id:
        _allow(user, "org_admin", "accountant", "teacher")
        entry = _audit_or_404(audit_id, user)
    return build_graph(_effective_rules(), entry)


@app.get("/api/knowledge/graph")
def knowledge_graph(audit_id: str | None = None,
                    session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    graph = _graph_for_user(user, audit_id)
    base, model, _ = ai_config()
    return {**graph, "ai": {"configured": bool(base and model), "model": model}}


@app.post("/api/knowledge/ask")
def knowledge_ask(body: GraphQuestion,
                  session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    graph = _graph_for_user(user, body.audit_id)
    result = ask_graph(graph, body.node_id, body.question)
    if body.audit_id:
        _audit_or_404(body.audit_id, user)
    return result


@app.post("/api/audits/{audit_id}/findings/{rule_id}/interpretation")
def finding_interpretation(audit_id: str, rule_id: str,
                           session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    entry = _audit_or_404(audit_id, user)
    finding = next((item for item in entry["findings"] if item.rule.id == rule_id), None)
    if not finding:
        raise HTTPException(status_code=404, detail="该审计中不存在指定 Finding。")
    if finding.status != "hit":
        raise HTTPException(status_code=422, detail="仅可对规则引擎已命中的 Finding 生成白话解读。")
    evidence_hash = finding_evidence_hash(finding)
    cached = store.get_finding_interpretation(audit_id, rule_id, evidence_hash)
    if cached:
        _audit_or_404(audit_id, user)
        store.log(user, "interpret_finding", "audit", audit_id,
                  f"rule={rule_id};cache=hit;model={cached['model']}")
        return cached
    result = interpret_finding(finding)
    _audit_or_404(audit_id, user)
    if result["evidence_hash"] != evidence_hash:
        raise HTTPException(status_code=502, detail="模型解读与当前审计证据版本不一致。")
    saved = store.save_finding_interpretation(
        audit_id, rule_id, evidence_hash, result, user,
    )
    store.log(user, "interpret_finding", "audit", audit_id,
              f"rule={rule_id};cache=miss;model={result['model']}")
    return {**saved, "cached": False}


@app.post("/api/audits/{audit_id}/narrative")
def audit_narrative(audit_id: str,
                    session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    entry = _audit_or_404(audit_id, user)
    findings = entry["findings"]
    evidence_hash = audit_narrative_hash(findings)
    cached = store.get_audit_narrative(audit_id, evidence_hash)
    if cached:
        _audit_or_404(audit_id, user)
        store.log(user, "generate_audit_narrative", "audit", audit_id,
                  f"cache=hit;model={cached['model']}")
        return cached
    result = generate_audit_narrative(findings)
    _audit_or_404(audit_id, user)
    if result["evidence_hash"] != evidence_hash:
        raise HTTPException(status_code=502, detail="模型总体结论与当前审计证据版本不一致。")
    saved = store.save_audit_narrative(audit_id, evidence_hash, result, user)
    store.log(user, "generate_audit_narrative", "audit", audit_id,
              f"cache=miss;model={result['model']}")
    return {**saved, "cached": False}


@app.get("/graph.js")
def graph_script():
    return FileResponse(STATIC_DIR / "graph.js", media_type="text/javascript")


@app.post("/api/setup")
def setup(body: SetupBody) -> dict[str, Any]:
    try:
        store.create_initial_admin(body.username, body.password, body.display_name.strip() or body.username,
                                   body.org_id, body.email)
    except SetupAlreadyInitialized:
        raise HTTPException(status_code=409, detail="系统已初始化。") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except sqlite3.IntegrityError as exc:
        if "users.email" in str(exc):
            raise HTTPException(status_code=409, detail="该邮箱已被占用，请换用其他邮箱。") from None
        raise HTTPException(status_code=409, detail="账号与现有数据冲突。") from None
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody, request: Request) -> Response:
    ip = request.client.host if request.client else "unknown"
    with login_guard.reserve(body.username, ip) as wait:
        if wait:
            return JSONResponse(status_code=429, content={"detail": "登录尝试过于频繁，请稍后重试。"},
                                headers={"Retry-After": str(wait)})
        result = store.authenticate(body.username, body.password)
        if not result:
            count = login_guard.record_failure(body.username, ip)
            store.log(None, "login_failed", "account_hash", login_guard.fingerprint(body.username.strip().lower()),
                      f"ip_hash={login_guard.fingerprint(ip)};failure_count={count}")
            return _err(401, "用户名或密码错误。")
        user, token = result
        login_guard.record_success(body.username)
    store.log(user, "login", "session", user["id"])
    response = JSONResponse({"user": user})
    response.set_cookie(
        COOKIE_NAME, token, max_age=12 * 3600, httponly=True,
        samesite="strict", secure=os.environ.get("TAXPEARLS_COOKIE_SECURE") == "1",
    )
    return response


@app.post("/api/logout")
def logout(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> Response:
    user = store.user_for_token(session)
    if user:
        store.log(user, "logout", "session", user["id"])
    store.logout(session)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME)
    response.delete_cookie(EMAIL_COOKIE_NAME)
    return response


def _email_public_base(request: Request) -> str:
    """Do not send secrets to an attacker-controlled Host header."""
    configured = os.getenv('TAXPEARLS_PUBLIC_BASE_URL','').strip().rstrip('/')
    value = configured or str(request.base_url).rstrip('/')
    try:
        parsed = urlsplit(value)
        local = parsed.hostname in {'localhost','127.0.0.1','::1'}
        valid = (parsed.scheme in {'http','https'} and bool(parsed.hostname)
                 and not parsed.username and not parsed.password and parsed.path in {'','/'}
                 and not parsed.query and not parsed.fragment and '\\' not in value
                 and not any(ord(ch)<=32 for ch in value) and (parsed.port is None or 1<=parsed.port<=65535)
                 and (parsed.scheme=='https' or local) and (bool(configured) or local))
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(503,'请配置可信的 TAXPEARLS_PUBLIC_BASE_URL（生产环境须 HTTPS）。')
    return value


def _email_browser_response(content, browser):
    response = JSONResponse(content)
    response.set_cookie(EMAIL_COOKIE_NAME,browser,max_age=30*60,httponly=True,samesite='strict',
                        secure=os.environ.get('TAXPEARLS_COOKIE_SECURE')=='1')
    return response


def _email_login_response(user, token):
    response=JSONResponse({'user':user,'purpose':'login'})
    response.set_cookie(COOKIE_NAME,token,max_age=12*3600,httponly=True,samesite='strict',
                        secure=os.environ.get('TAXPEARLS_COOKIE_SECURE')=='1')
    return response


@app.post("/api/auth/password/reset")
def password_reset(body: PasswordResetBody, request: Request) -> Response:
    """申请重置邮件。防枚举：无论邮箱是否存在，成功响应完全一致。"""
    ip = request.client.host if request.client else "unknown"
    if not reset_limiter.allow(email=body.email, ip=ip):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试。"})
    base = _email_public_base(request)
    browser = email_auth.browser_secret(request.cookies.get(EMAIL_COOKIE_NAME))
    try:
        delivery = email_auth.issue(store,body.email,'reset',browser)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    if delivery:
        try:
            send_password_reset_email(to=delivery['email'], reset_url=f"{base}/#email={delivery['token']}")
        except MailError:
            email_auth.revoke_delivery(store,delivery['token'])
            store.log(None, "password_reset_failed", "email_hash",
                      login_guard.fingerprint(delivery['email']), 'delivery_error')
        else:
            store.log(None, "password_reset_sent", "email_hash",
                      login_guard.fingerprint(delivery['email']),f"ip_hash={login_guard.fingerprint(ip)}")
    return _email_browser_response({"message": "若该邮箱可用，重置邮件将发送至该邮箱。请在发起请求的浏览器打开，10 分钟内有效；未收到可稍后重新申请。"},browser)


@app.post("/api/auth/password/reset/confirm")
def password_reset_confirm(body: PasswordResetConfirmBody, request: Request) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    if not reset_confirm_limiter.allow(token=body.token, ip=ip):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试。"})
    try:
        email_auth.reset_password(store,body.token,body.password,request.cookies.get(EMAIL_COOKIE_NAME,''))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"message": "密码已重置，请使用新密码登录。"}


@app.get("/api/auth/captcha")
def auth_captcha(request: Request) -> dict[str, str]:
    """签发图片人机验证码（PNG data URL）。"""
    ip = request.client.host if request.client else "unknown"
    if not captcha_limiter.allow(ip=ip):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试。"})
    try:
        return captcha.issue()
    except captcha.CaptchaCapacityError:
        return JSONResponse(status_code=503, content={"detail": "验证码容量暂满，请稍后重试。"},
                            headers={"Retry-After": "60"})


@app.post("/api/auth/email/start")
def email_start(body: EmailStartBody, request: Request) -> dict[str, Any]:
    """验证码/魔术链接共用一份绑定请求，注册或邮箱登录均需图片挑战。"""
    ip = request.client.host if request.client else "unknown"
    if not captcha.verify(body.captcha_id, body.captcha_answer):
        return JSONResponse(status_code=422, content={"detail": "人机验证不正确，请重试。"})
    email = body.email.strip().lower()
    if not register_code_limiter.allow(email=email, ip=ip):
        return JSONResponse(status_code=429, content={"detail": "发送过于频繁，请 15 分钟后再试。"})
    base = _email_public_base(request)
    browser = email_auth.browser_secret(request.cookies.get(EMAIL_COOKIE_NAME))
    try:
        delivery=email_auth.issue(store,email,body.purpose,browser,invite_code=body.invite_code)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    if not delivery and body.purpose=='register':
        return _email_browser_response({'message':'该邮箱已注册，请直接登录或申请找回密码。','exists':True},browser)
    if delivery:
        try:
            sender=send_registration_code_email if body.purpose=='register' else send_login_code_email
            sender(to=email,code=delivery['code'],signup_url=f"{base}/#email={delivery['token']}")
        except MailError:
            email_auth.revoke_delivery(store,delivery['token'])
            store.log(None,'email_delivery_failed','email_hash',login_guard.fingerprint(email),'delivery_error')
            if body.purpose=='register':
                raise HTTPException(502,'验证邮件发送失败，请稍后重试。') from None
        else:
            store.log(None,'email_verification_sent','email_hash',login_guard.fingerprint(email),
                      f'purpose={body.purpose};ip_hash={login_guard.fingerprint(ip)}')
    message=('若该邮箱可用，验证邮件将发送至该邮箱。' if body.purpose=='login' else '验证邮件已发送。')
    return _email_browser_response({'message':message+'请在发起请求的浏览器输入验证码或打开邮件链接；10 分钟内有效，输错 5 次作废。','exists':False},browser)


@app.post('/api/auth/email/verify')
def email_verify(body: EmailMagicBody, request: Request) -> Response:
    ip=request.client.host if request.client else 'unknown'
    if not email_verify_limiter.allow(token=body.token,ip=ip):
        return JSONResponse(status_code=429,content={'detail':'请求过于频繁，请稍后再试。'})
    try:
        result=email_auth.redeem_magic(store,body.token,request.cookies.get(EMAIL_COOKIE_NAME,''))
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    if result['purpose']=='login':
        return _email_login_response(result['user'],result['session'])
    return JSONResponse(result)


@app.post('/api/auth/email/login')
def email_login(body: EmailLoginBody, request: Request) -> Response:
    ip=request.client.host if request.client else 'unknown'
    if not email_login_limiter.allow(email=body.email,ip=ip):
        return JSONResponse(status_code=429,content={'detail':'请求过于频繁，请稍后再试。'})
    try:
        user,token=email_auth.login_with_code(store,body.email,body.code,request.cookies.get(EMAIL_COOKIE_NAME,''))
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    return _email_login_response(user,token)


@app.post("/api/register/complete")
def register_complete(body: RegisterCompleteBody, request: Request) -> Response:
    """邮箱验证与完整邀请凭证同事务；角色/机构来自凭证，不接受客户端指定。"""
    ip = request.client.host if request.client else "unknown"
    if not register_complete_limiter.allow(email=body.email, ip=ip):
        return JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试。"})
    try:
        user, token = store.register_with_code(body.email, body.code, body.invite_code, body.password,
                                             browser_session=request.cookies.get(EMAIL_COOKIE_NAME,''),email_proof=body.email_proof)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except sqlite3.IntegrityError as exc:
        if "users.email" in str(exc):
            raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录。") from None
        raise HTTPException(status_code=409, detail="账号与现有数据冲突。") from None
    response = JSONResponse({"user": user, "org_id": user["org_id"]})
    response.set_cookie(
        COOKIE_NAME, token, max_age=12 * 3600, httponly=True,
        samesite="strict", secure=os.environ.get("TAXPEARLS_COOKIE_SECURE") == "1",
    )
    return response


@app.post("/api/invites")
def create_invite(body: InviteBody, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    """平台管理员签发一次性创始码。"""
    actor = _user(session)
    _allow(actor, "platform_admin")
    try:
        invite = store.create_invite_code(actor, body.org_name, body.seats,
                                          body.bound_email, body.expires_days)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return invite


@app.get("/api/invites")
def list_invites(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    actor = _user(session)
    _allow(actor, "platform_admin")
    return store.list_invite_codes(actor=actor)


@app.get("/api/me")
def me(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    return _user(session)


@app.get("/api/notifications/preferences")
def notification_preferences(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    account = store.get_user(user["id"])
    return {**store.notification_preferences(user["id"]), "has_email": bool(account and account.get("email")),
            "delivery_enabled": email_delivery_enabled()}


def _save_notification_preferences(user: dict[str, Any], target_id: str, body: NotificationPreferencesBody) -> dict[str, Any]:
    try:
        saved = store.set_notification_preferences(user, target_id, body.audit_completed, body.high_risk, body.email_enabled)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return saved


@app.put("/api/notifications/preferences")
def update_notification_preferences(body: NotificationPreferencesBody,
        session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    return _save_notification_preferences(user, user["id"], body)


@app.get("/api/notifications/recipients")
def notification_recipients(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin")
    return store.list_notification_recipients(user)


@app.put("/api/notifications/recipients/{user_id}")
def update_notification_recipient(user_id: str, body: NotificationPreferencesBody,
        session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin")
    return _save_notification_preferences(user, user_id, body)


@app.get("/api/notifications")
def notifications(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    return store.list_notifications(user)


@app.put("/api/notifications/{notification_id}/read")
def read_notification(notification_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    if not store.mark_notification_read(user, notification_id):
        raise HTTPException(404, "通知不存在或无权查看。")
    return {"id": notification_id, "read": True}


@app.post("/api/notifications/{notification_id}/retry")
def retry_notification(notification_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME),
                       channel: str = Query(default='email',max_length=32)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    try:
        authorized = store.retry_notification_delivery(user, notification_id,channel=channel)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if not authorized:
        raise HTTPException(404, "通知不存在或无权访问。")
    return {"id": notification_id,"channel":channel,"status":"pending",**({'email_status':'pending'} if channel=='email' else {})}


@app.get("/api/users")
def users(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "platform_admin", "org_admin")
    return store.list_users(None if user["role"] == "platform_admin" else user["org_id"], actor=user)


@app.post("/api/users")
def create_user(body: UserBody, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    actor = _user(session)
    _allow(actor, "org_admin")
    org_id = body.org_id or actor["org_id"]
    if actor["role"] == "org_admin" and (body.role != "accountant" or org_id != actor["org_id"]):
        raise HTTPException(status_code=403, detail="机构管理员只能创建本机构会计账号。")
    try:
        created = store.create_user(body.username, body.password, body.display_name, body.role, org_id, body.email, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except sqlite3.IntegrityError as exc:
        if "users.username" in str(exc):
            raise HTTPException(status_code=409, detail="用户名已存在。") from None
        if "users.email" in str(exc):
            raise HTTPException(status_code=409, detail="该邮箱已被占用，请换用其他邮箱。") from None
        if "users.role" in str(exc):
            raise HTTPException(status_code=409, detail="平台管理员已存在。") from None
        raise HTTPException(status_code=409, detail="账号与现有数据冲突。") from None
    return created


@app.get("/api/clients")
def clients(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "org_admin", "accountant")
    return store.list_clients(user)


@app.post("/api/clients")
def create_client(body: ClientBody, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin")
    name, taxpayer_id = body.name.strip(), body.taxpayer_id.strip()
    if not name or not taxpayer_id:
        raise HTTPException(status_code=422, detail="企业名称和纳税人识别号不能为空。")
    if body.accountant_id:
        accountant = store.get_user(body.accountant_id)
        if not accountant or accountant["role"] != "accountant" or accountant["org_id"] != user["org_id"]:
            raise HTTPException(status_code=422, detail="负责人必须是本机构会计。")
    client = store.upsert_client(user, name, taxpayer_id, body.accountant_id)
    return client


@app.post("/api/audit")
async def audit(request: Request, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> Response:
    user = _user(session)
    # Legacy direct import is teaching-only. Business uploads must retain
    # originals and explicitly confirm a server-side analysis revision.
    _allow(user, "teacher")
    async with multipart(request) as form:
        file = single_file(form)
        if not file.filename or not file.filename.lower().endswith(".xlsx"):
            return _err(422, "仅支持 .xlsx 格式的审计材料。")
        client_id = form.get("client_id") or None
        if client_id is not None and (not isinstance(client_id, str) or len(client_id) > 64):
            return _err(422, "客户档案编号格式错误。")
        data = await file.read()
    try:
        dataset = await run_in_threadpool(loader.load_bytes, data)
    except loader.InputError as exc:
        return _err(422, f"审计材料不符合模板要求：{exc}")
    return JSONResponse(await run_in_threadpool(_save_audit, dataset, user, client_id))


def _save_audit(dataset: Dataset, user: dict[str, Any], client_id: str | None = None,
                *, frozen_rules: list[Rule] | None = None, exercise_metadata: dict | None = None,
                material_context: dict | None = None, frozen_graph_rule: Rule | None = None) -> dict[str, Any]:
    if user["role"] == "teacher" and not _is_synthetic_dataset(dataset):
        raise HTTPException(403, "教师只能导入明确标记为仿真样例的教学数据，严禁使用真实企业账套。")
    if user['role'] == 'teacher' and client_id is not None:
        raise HTTPException(403, "教师备课案例不能关联客户档案。")
    if client_id:
        client = store.get_client(client_id)
        if not client or client["org_id"] != user["org_id"]:
            raise HTTPException(422, "客户档案不存在或不属于当前机构。")
        if user["role"] == "accountant" and client.get("accountant_id") != user["id"]:
            raise HTTPException(403, "会计只能审计自己负责的客户。")
        if client["taxpayer_id"] != dataset.company.taxpayer_id:
            raise HTTPException(422, "审计材料中的纳税人识别号与所选客户档案不一致。")
    try:
        from src import related_graph
        if material_context is not None and frozen_graph_rule != related_graph.definition():
            raise AccessDenied('关联方检查范围或版本已变化，请重新分析并确认材料。', 409)
        enabled = store.enabled_rule_ids()
        rules = _audit_rules(dataset, enabled) if frozen_rules is None else frozen_rules
        findings = engine.run(rules, dataset)
        # FR-B09 is a separate relationship traversal, not a YAML condition.
        findings.extend(related_graph.run(dataset, include_unavailable=material_context is not None))
        findings.sort(key=lambda f: ({"hit": 0, "pass": 1, "skipped": 2}[f.status], -f.severity_rank, f.rule.id))
    except engine.RuleError as exc:
        raise HTTPException(500, f"规则执行失败：{exc}")
    audit_id = uuid.uuid4().hex
    when = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    vm = render.build_view_model(dataset, findings)
    from webapp.report_archive import build_snapshot
    material_reference = ({'batch_id': material_context['batch_id'], 'analysis_revision': material_context['revision'],
                           'confirmation_revision': material_context['revision'] + 1} if material_context else None)
    snapshot = build_snapshot({"id": audit_id, "org_id": user["org_id"], "audited_at": when,
                               "dataset": dataset, "findings": findings, 'material_reference': material_reference},
                              _org_branding(user["org_id"]))
    store.save_audit(audit_id, user, client_id, dataset, findings, vm["summary"], when,
                     report_snapshot=snapshot, exercise_metadata=exercise_metadata, create_client=True,
                     material_context=material_context)
    entry = store.get_audit(audit_id)
    assert entry is not None
    return _result(entry)


from webapp.material_upload import register as register_material_upload
register_material_upload(app, _user, _allow, _save_audit, RULES_DIR, _audit_or_404)

from webapp.enterprise_materials import register as register_enterprise_materials
register_enterprise_materials(app, lambda: store, _user, _allow, RULES_DIR,
                             lambda dataset: _audit_rules(dataset, store.enabled_rule_ids()),
                             _save_audit, lambda audit_id, user: _result(_audit_or_404(audit_id, user)))

from webapp.exercises import register as register_exercises
register_exercises(app, lambda: store, _user, _allow, _audit_or_404, lambda data, enabled: _audit_rules(data,enabled),
                   _save_audit, _trial_payload, COOKIE_NAME)
classroom.register(app,lambda: store,_user,_allow,_is_synthetic_dataset,COOKIE_NAME)
from webapp.mistake_book import register as register_mistake_book
register_mistake_book(app,lambda: store,_user,_allow,COOKIE_NAME)
members.register(app,lambda: store,_user,COOKIE_NAME)


@app.get("/api/audits")
def audits(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    return store.list_audits(user)


@app.get("/api/audits/{audit_id}")
def audit_detail(audit_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    entry = _audit_or_404(audit_id, user)
    store.log(user, "view_audit", "audit", audit_id)
    return _result(entry)


@app.get("/api/audits/{audit_id}/changes")
def audit_changes(audit_id: str, baseline_id: str | None = None,
                  session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    from src import risk_changes

    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    current = _audit_or_404(audit_id, user)
    candidates = risk_changes.baseline_candidates(store.audit_history_for_comparison(current, user), current, latest_only=False)
    choices = [{key: row[key] for key in ("id", "period", "audited_at")} for row in candidates]
    if baseline_id:
        # Check access before identity/period validation; do not reveal foreign IDs.
        baseline = _audit_or_404(baseline_id, user)
    elif candidates:
        baseline = _audit_or_404(candidates[0]["id"], user)
    else:
        return {"status": "no_baseline", "current": {key: current[key] for key in ("id", "period", "audited_at")},
                "baselines": [], "items": [], "counts": {},
                "message": "无可比基期：须同机构/客户/税号且有更早、同粒度的明确期间。首期不推断新增风险。"}
    try:
        result = risk_changes.compare(baseline, current)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    store.log(user, "view_risk_changes", "audit", audit_id, f"baseline={baseline['id']}")
    return {**result, "baselines": choices}


def _archived_report(entry, user, version=None):
    from webapp.report_archive import build_snapshot

    try:
        if version is None:
            narrative = store.get_audit_narrative(entry["id"], audit_narrative_hash(entry["findings"]))
            snapshot = build_snapshot(entry, _org_branding(entry["org_id"]), narrative)
            version, _ = store.archive_report(entry["id"], user, snapshot)
        result = store.get_report_version(entry["id"], version)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if not result:
        raise HTTPException(404, "归档报告版本不存在。")
    return result


@app.get("/api/archive")
def archive_search(q: str = Query(default="", max_length=120), period: str = Query(default="", max_length=80),
                   date_from: date | None = None, date_to: date | None = None,
                   risk: str = Query(default="all", pattern="^(all|hit|high)$"),
                   page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
                   session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "审计日期起始日不能晚于截止日。")
    return store.search_audits(user, q.strip(), period.strip(), date_from.isoformat() if date_from else None,
                             date_to.isoformat() if date_to else None, risk, page, page_size)


@app.get("/api/audits/{audit_id}/report-versions")
def report_versions(audit_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    _audit_or_404(audit_id, user)
    try:
        return store.report_versions(audit_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/audits/{audit_id}/report-versions")
def archive_current_report(audit_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    result = _archived_report(_audit_or_404(audit_id, user), user)
    return {key: result[key] for key in ("version", "html_sha256", "content_sha256", "created_at", "manifest")}


@app.get("/api/report/{audit_id}")
def report(
    audit_id: str, background: BackgroundTasks, confirm: bool = False,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    version: int | None = Query(default=None, ge=1),
) -> Any:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    if user["role"] == "accountant" and not confirm:
        raise HTTPException(status_code=409, detail="会计导出需二次确认，请确认报告用途后重试。")
    entry = _audit_or_404(audit_id, user)
    archived = _archived_report(entry, user, version)
    manifest = archived["manifest"]
    try:
        if archived["pdf_bytes"] is None:
            fd, tmp = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            try:
                render.export_pdf(archived["html"], tmp, report_no=manifest["report_no"], footer_text=manifest["footer_text"])
                archived = store.attach_report_pdf(audit_id, archived["version"], Path(tmp).read_bytes(), actor=user)
            finally:
                Path(tmp).unlink(missing_ok=True)
    except RuntimeError as exc:
        return _err(500, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    short = _safe_filename_component(entry["company_name"].replace("（仿真样例）", "")[:12])
    title = _safe_filename_component(manifest["report_title"])
    pdf_name = f"{title}-{short}-{entry['audited_at'][:10].replace('-', '')}-v{archived['version']}.pdf"
    _audit_or_404(audit_id, user)
    store.log(user, "export_report", "audit", audit_id, f"version={archived['version']}")
    return Response(content=archived["pdf_bytes"], media_type="application/pdf", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(pdf_name),
        "X-TaxPearls-Report-Version": str(archived["version"]), "X-TaxPearls-SHA256": archived["pdf_sha256"],
        "Cache-Control": "private, no-store"})


@app.get("/api/report/{audit_id}/html")
def report_html(audit_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME),
                version: int | None = Query(default=None, ge=1)) -> Response:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    entry = _audit_or_404(audit_id, user)
    archived = _archived_report(entry, user, version)
    _audit_or_404(audit_id, user)
    store.log(user, "view_report", "audit", audit_id, f"version={archived['version']}")
    return Response(content=archived["html"], media_type="text/html", headers={
        "X-TaxPearls-Report-Version": str(archived["version"]), "X-TaxPearls-SHA256": archived["html_sha256"],
        "Cache-Control": "private, no-store"})


@app.get("/api/org/settings")
def get_org_settings(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher", "student")
    return store.get_org_settings(user["org_id"], actor=user)


@app.put("/api/org/settings")
def put_org_settings(
    body: OrgSettingsBody,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin")
    try:
        saved = store.update_org_settings(
            user["org_id"], body.display_name, body.report_title, body.footer_text, actor=user
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return saved


@app.get("/api/org/logo")
def get_org_logo(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> Response:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher", "student")
    logo = store.get_org_logo(user["org_id"], actor=user)
    if not logo:
        raise HTTPException(status_code=404, detail="当前机构尚未配置 Logo。")
    return Response(
        content=logo[1], media_type=logo[0],
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/org/logo")
async def put_org_logo(
    request: Request,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin")
    async with multipart(request, file_limit=MAX_ORG_LOGO_BYTES, max_fields=0) as form:
        content = await single_file(form).read()
    try:
        mime, normalized = _normalize_org_logo(content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    finally:
        content = b""
    saved = store.update_org_logo(user["org_id"], mime, normalized, actor=user)
    return saved


@app.delete("/api/org/logo")
def delete_org_logo(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin")
    saved = store.clear_org_logo(user["org_id"], actor=user)
    return saved


@app.get("/api/rules")
def rules(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    _user(session)
    loaded = _effective_rules()
    overrides = store.rule_overrides()
    enabled = store.enabled_rule_ids()
    return [
        _rule_payload(r, enabled is None or r.id in enabled, overrides.get(r.id))
        for r in loaded
    ]


@app.get("/api/rules/{rule_id}/versions")
def rule_versions(rule_id: str, session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "platform_admin")
    _rule_by_id(rule_id)
    return store.rule_versions(rule_id).get(rule_id, [])


@app.put("/api/rules/{rule_id}/state")
def rule_state(rule_id: str, body: RuleStateBody,
               session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin")
    valid = {rule.id for rule in _effective_rules()}
    if rule_id not in valid:
        raise HTTPException(status_code=404, detail="规则不存在。")
    store.change_rule_state(rule_id, body.enabled, user, valid)
    return {"id": rule_id, "enabled": body.enabled}


@app.put("/api/rules/{rule_id}/parameters")
def rule_parameters(
    rule_id: str, body: RuleParametersBody,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "platform_admin")
    # Capture the database revision before loading the current YAML/library
    # version. A deployment may have a newer base than the stored override.
    previous_override = store.rule_overrides().get(rule_id)
    current = _rule_by_id(rule_id)
    candidate = _candidate_rule(current, body)
    try:
        saved = store.set_rule_override(
            rule_id, candidate.version, candidate.logic, candidate.threshold_basis,
            user, previous_override["version"] if previous_override else current.version,
            candidate.effective_from, candidate.effective_to,
            asdict(candidate),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    enabled = store.enabled_rule_ids()
    return _rule_payload(candidate, enabled is None or rule_id in enabled, saved)


@app.post("/api/rules/{rule_id}/trial")
def rule_trial(
    rule_id: str, body: RuleTrialBody,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "org_admin", "accountant", "teacher")
    entry = _audit_or_404(body.audit_id, user)
    current = _rule_by_id(rule_id)
    candidate = _candidate_rule(current, body)
    finding = engine.evaluate(candidate, entry["dataset"])
    store.log(
        user, "trial_rule_parameters", "audit", body.audit_id,
        f"rule={rule_id}; version={candidate.version}; result={finding.status}",
    )
    return _trial_payload(finding, body.audit_id)


@app.post("/api/assignments")
def create_assignment(body: AssignmentBody,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "teacher")
    entry = _audit_or_404(body.audit_id, user)
    dataset: Dataset = entry["dataset"]
    if not _is_synthetic_dataset(dataset):
        raise HTTPException(status_code=422, detail="实训作业只能使用明确标记的仿真数据。")
    hit_ids = {f.rule.id for f in entry["findings"] if f.status == "hit"}
    if not hit_ids:
        raise HTTPException(status_code=422, detail="该案例没有命中风险，无法生成可评分作业。")
    if set(body.weights) - hit_ids:
        raise HTTPException(status_code=422, detail="权重只能配置该案例实际命中的规则。")
    if body.target_student_id:
        student = store.get_user(body.target_student_id)
        if not student or not student["active"] or student["role"] != "student" or student["org_id"] != user["org_id"]:
            raise HTTPException(status_code=422, detail="指定学生不存在或不属于当前机构。")
    try:
        assignment_id = store.create_assignment(
            user, body.title.strip(), body.audit_id, body.target_student_id,
            body.weights, body.false_positive_penalty, body.published,body.class_id,body.deadline_at,
        )
    except classroom.ClassroomError as exc:
        raise HTTPException(exc.status,str(exc)) from None
    return {"id": assignment_id}


@app.get("/api/assignments")
def assignments(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "teacher", "student")
    return store.list_assignments(user)


@app.get("/api/assignments/{assignment_id}")
def assignment_detail(assignment_id: str,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "teacher", "student")
    assignment = store.get_assignment_for_user(assignment_id,user)
    if not assignment:
        raise HTTPException(status_code=404, detail="作业不存在。")
    entry = store.get_audit(assignment["audit_id"])
    if not entry or entry["org_id"] != user["org_id"]:
        raise HTTPException(404,"作业材料不存在。")
    dataset: Dataset = entry["dataset"]
    public_assignment=dict(assignment)
    if user["role"] == "student":
        public_assignment.pop("weights",None)
    return {
        **public_assignment, "company": _company_dict(dataset),
        "generated_material_available": store.has_generated_exercise(assignment["audit_id"], user["org_id"]),
        "accounts": [
            {"code": a.code, "name": a.name, "opening": str(a.opening), "debit": str(a.debit),
             "credit": str(a.credit), "closing": str(a.closing)} for a in dataset.accounts
        ],
        "declarations": {k: str(v) for k, v in dataset.declarations.items()},
        "metrics": [{"name":m.name,"value":str(m.value),"source":m.source,"detail":m.detail}
                    for m in dataset.metrics.values()],
        # Answer options must not inherit the audit's hit-first ordering.
        "rules": [{"id": f.rule.id, "name": f.rule.name, "category": f.rule.category}
                  for f in sorted(entry["findings"], key=lambda finding: finding.rule.id)],
        "submission": store.get_submission(assignment_id, user["id"]) if user["role"] == "student" else None,
    }


@app.post("/api/assignments/{assignment_id}/submit")
def submit_assignment(assignment_id: str, body: SubmissionBody,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "student")
    assignment = store.get_assignment_for_user(assignment_id,user)
    if not assignment:
        raise HTTPException(status_code=404, detail="作业不存在。")
    if not assignment["can_submit"]:
        raise HTTPException(409,"已到截止时间，不能提交或覆盖已有成绩。")
    entry = store.get_audit(assignment["audit_id"])
    if not entry or entry["org_id"] != user["org_id"]:
        raise HTTPException(404,"作业材料不存在。")
    try:
        result = training.score_submission(
            entry["findings"], body.selected_rule_ids,
            assignment["weights"], assignment["false_positive_penalty"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        store.save_submission(assignment_id, user["id"], body.selected_rule_ids, result["score"], result,user=user)
    except classroom.ClassroomError as exc:
        raise HTTPException(exc.status,str(exc)) from None
    return result


@app.post("/api/assignments/{assignment_id}/feedback")
def teaching_feedback(assignment_id: str, body: SandboxFeedbackBody,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    user = _user(session)
    _allow(user, "student", "teacher")
    assignment = store.get_assignment_for_user(assignment_id,user)
    if not assignment:
        raise HTTPException(404, "作业不存在。")
    entry = store.get_audit(assignment["audit_id"])
    if not entry or entry["org_id"] != user["org_id"]:
        raise HTTPException(404, "作业材料不存在。")
    try:
        result = sandbox_feedback.feedback(entry["dataset"], [f.rule for f in entry["findings"]], **body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return JSONResponse(result, headers={"Cache-Control":"private, no-store"})


@app.put("/api/submissions/{submission_id}/review")
def review_submission(submission_id: str, body: ReviewBody,
                      session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = _user(session)
    _allow(user, "teacher")
    if store.submission_org(submission_id,user=user) != user["org_id"]:
        raise HTTPException(status_code=404, detail="提交记录不存在。")
    store.review_submission(submission_id, user["id"], body.adjusted_score, body.feedback.strip())
    return {"ok": True}


@app.get("/api/submissions")
def submissions(assignment_id: str | None = None,
                session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "teacher")
    return store.list_submissions(user, assignment_id)


@app.get("/api/audit-log")
def audit_log(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> list[dict[str, Any]]:
    user = _user(session)
    _allow(user, "teacher", "org_admin", "platform_admin")
    return store.list_logs(user)
