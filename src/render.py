"""审计报告渲染。

流程：Finding 列表 → 视图模型 → Jinja2 渲染 HTML → Playwright 驱动本机 Chrome 导出 A4 PDF。

排版要点（中文正式报告规范）：
    - 正文宋体 10.5pt / 行距 1.75 / 首行缩进 2em / 两端对齐
    - 标题黑体，与正文形成字重与字形对比
    - 表格数字使用等宽数字（tabular-nums），金额右对齐便于纵向比对
    - 风险卡片 break-inside: avoid，避免跨页断裂
    - 页眉页脚由 Chromium 打印模板提供，含页码「第 X 页 / 共 Y 页」
"""
from __future__ import annotations

import hashlib
import os
from html import escape
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import __version__
from .models import (
    SEVERITY_LABEL,
    STATUS_LABEL,
    Dataset,
    Finding,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "templates"
OUTPUT_DIR = ROOT / "output"

SEVERITY_CSS = {"高风险": "high", "中风险": "medium", "低风险": "low"}


def make_report_no(company_name: str, when: datetime) -> str:
    """报告编号：TP-日期-指纹，同一企业同日生成结果稳定。"""
    digest = hashlib.sha1(company_name.encode("utf-8")).hexdigest()[:4].upper()
    return f"TP-{when:%Y%m%d}-{digest}"


def _evidence_dict(f: Finding) -> list[dict]:
    return [
        {
            "label": e.label,
            "value": e.value,
            "source": e.source,
            "emphasis": e.emphasis,
        }
        for e in f.evidence
    ]


def _finding_dict(f: Finding) -> dict:
    sev_label = SEVERITY_LABEL.get(f.rule.severity, f.rule.severity)
    return {
        "id": f.rule.id,
        "name": f.rule.name,
        "category": f.rule.category,
        "tax_type": f.rule.tax_type,
        "severity": SEVERITY_CSS.get(sev_label, "low"),
        "severity_label": sev_label,
        "status": f.status,
        "status_label": STATUS_LABEL.get(f.status, f.status),
        "conclusion": f.conclusion,
        "calculation": f.calculation,
        "threshold_desc": f.threshold_desc,
        "description": f.rule.description,
        "scope": f.rule.scope,
        "threshold_basis": f.rule.threshold_basis,
        "references": f.rule.references,
        "version": f.rule.version,
        "effective_from": f.rule.effective_from,
        "effective_to": f.rule.effective_to,
        "evidence": _evidence_dict(f),
        "legal_basis": f.rule.legal_basis,
        "suggestion": f.rule.suggestion,
        "skip_reason": f.skip_reason,
    }


def material_sources(dataset: Dataset) -> list[str]:
    """Use captured material names; never invent missing legacy provenance."""
    if dataset.sources is not None:
        return list(dict.fromkeys(dataset.sources)) or ["未记录材料来源"]
    sources = []
    if dataset.accounts:
        sources.append("已保存的科目余额记录")
    if dataset.declarations:
        sources.append("已保存的申报记录")
    if dataset.metrics:
        sources.append("已保存的指标及取数证据")
    if dataset.related_graph is not None:
        sources.append("已保存的关联主体、关系及交易证据")
    return sources + ["历史记录未保存完整材料清单；具体来源见证据卡"]


def build_view_model(dataset: Dataset, findings: list[Finding]) -> dict:
    vm_findings = [_finding_dict(f) for f in findings]
    hit = [f for f in vm_findings if f["status"] == "hit"]
    passed = [f for f in vm_findings if f["status"] == "pass"]
    skipped = [f for f in vm_findings if f["status"] == "skipped"]

    return {
        "company": dataset.company,
        "findings": vm_findings,
        "hit_findings": hit,
        "pass_findings": passed,
        "skipped_findings": skipped,
        "sources": material_sources(dataset),
        "summary": {
            "total": len(vm_findings),
            "hit": len(hit),
            "pass": len(passed),
            "skipped": len(skipped),
            "high": sum(1 for f in hit if f["severity"] == "high"),
            "medium": sum(1 for f in hit if f["severity"] == "medium"),
            "low": sum(1 for f in hit if f["severity"] == "low"),
        },
        "engine_version": __version__,
    }


def render_html(
    dataset: Dataset,
    findings: list[Finding],
    when: datetime | None = None,
    write: bool = True,
    org_name: str = "税海拾珠",
    report_title: str = "税务风险审计报告",
    footer_text: str = "",
    logo_data_uri: str = "",
    ai_narrative: dict | None = None,
    template_source: str | None = None,
    protect: bool = True,
) -> tuple[str, Path | None]:
    """渲染报告 HTML。write=True 时落盘供浏览器预览（CLI 路径），
    write=False 仅返回 HTML 字符串（Web 路径：产物按需生成，不覆盖已有预览）。
    org_name / report_title / logo_data_uri 来自机构设置，缺省用平台默认抬头。"""
    when = when or datetime.now()
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tpl = env.from_string(template_source) if template_source is not None else env.get_template("report.html")

    vm = build_view_model(dataset, findings)
    vm["report_no"] = make_report_no(dataset.company.name, when)
    vm["generated_date"] = f"{when:%Y年%m月%d日}"
    vm["generated_at"] = f"{when:%Y-%m-%d %H:%M:%S}"
    vm["org_name"] = org_name
    vm["report_title"] = report_title
    vm["footer_text"] = footer_text
    vm["logo_data_uri"] = logo_data_uri
    vm["ai_narrative"] = ai_narrative

    html = tpl.render(**vm)
    if protect:
        from .report_protection import protect_html
        html, _ = protect_html(html,dataset.company.name,when.date().isoformat(),vm["report_no"])

    if not write:
        return html, None

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    html_path = OUTPUT_DIR / "report.html"
    html_path.write_text(html, encoding="utf-8")
    return html, html_path


def _header_template(verification_id: str = "") -> str:
    """D08 traceability belongs in the reserved print margin on every page.

    Legacy HTML has no mark and keeps the original empty header.
    """
    if not verification_id:
        return '<div style="height:0"></div>'
    note=("登录工作台核对原件；标识/水印可复制，不是第三方签名，不能阻止转发。"
          if verification_id.startswith("TPV-") else "本地文件未登记服务器原件；不是服务器来源认证。")
    return ('<div style="width:100%;padding:0 20mm;font-size:7pt;color:#63758b;'
            'font-family:Microsoft YaHei,sans-serif;">'
            f'追溯标识 {escape(verification_id)}<br>{note}</div>')


def _footer_template(report_no: str, footer_text: str = "") -> str:
    # ⚠️ Chromium 要求页码模板必须显式声明 font-size，否则文字不可见
    safe_footer = escape(" ".join(footer_text.split()))
    suffix = f"　{safe_footer}" if safe_footer else ""
    return (
        '<div style="width:100%;padding:0 20mm;font-size:8pt;color:#8A94A6;'
        "font-family:'Microsoft YaHei','PingFang SC',sans-serif;"
        'display:flex;justify-content:space-between;">'
        f"<span>税海拾珠 · 税务风险审计报告　{report_no}{suffix}</span>"
        '<span>第 <span class="pageNumber"></span> 页 / 共 <span class="totalPages"></span> 页</span>'
        "</div>"
    )


def export_pdf(
    html: str,
    out_path: str | Path,
    report_no: str = "",
    footer_text: str = "",
) -> Path:
    """Use installed Chrome locally or bundled Chromium in the deployment image."""
    from playwright.sync_api import sync_playwright
    from .report_protection import verification_id_from_html

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            browser_kind = os.getenv("TAXPEARLS_PDF_BROWSER", "chrome" if os.name == "nt" else "chromium")
            if browser_kind not in {"chrome", "chromium"}:
                raise ValueError("TAXPEARLS_PDF_BROWSER 仅支持 chrome / chromium")
            browser = p.chromium.launch(**({"channel": "chrome"} if browser_kind == "chrome" else {}), headless=True)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "未能启动报告浏览器。Chrome 模式需安装 Google Chrome；"
                f"Chromium 模式需执行 python -m playwright install chromium。原始错误：{e}"
            ) from e

        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            page.emulate_media(media="print")
            # 等待字体加载完成，避免中文回退或字重错乱
            page.wait_for_function("document.fonts.status === 'loaded'", timeout=15_000)
            page.pdf(
                path=str(out_path),
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template=_header_template(verification_id_from_html(html)),
                footer_template=_footer_template(report_no, footer_text),
                margin={"top": "18mm", "bottom": "18mm", "left": "20mm", "right": "20mm"},
            )
        finally:
            browser.close()

    return out_path


def build(
    dataset: Dataset,
    findings: list[Finding],
    pdf_name: str | None = None,
    when: datetime | None = None,
) -> tuple[Path, Path]:
    """一站式：渲染 HTML 并导出 PDF。返回 (html_path, pdf_path)。"""
    when = when or datetime.now()
    html, html_path = render_html(dataset, findings, when)

    report_no = make_report_no(dataset.company.name, when)
    if pdf_name is None:
        short = dataset.company.name.replace("（仿真样例）", "")[:12]
        pdf_name = f"税务风险审计报告-{short}-{when:%Y%m%d}.pdf"

    pdf_path = export_pdf(
        html,
        OUTPUT_DIR / pdf_name,
        report_no=report_no,
    )
    return html_path, pdf_path
