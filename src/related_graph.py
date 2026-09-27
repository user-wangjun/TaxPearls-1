"""Independent FR-B09 relationship traversal; never evaluates YAML conditions.

The graph can identify a reviewed shareholder -> controlled company -> trade
path. An anomaly remains a review lead, not a tax-law violation or AI verdict.
"""
from __future__ import annotations
from src import periods

from datetime import date, datetime
from decimal import Decimal
import re
from typing import Iterator

from .models import (
    Company, Dataset, EvidenceItem, Finding, RelatedGraph, RelatedRelation,
    RelatedSubject, RelatedTrade, Rule,
)

SHEET_SUBJECTS = "关联方主体"
SHEET_RELATIONS = "关联关系"
SHEET_TRADES = "关联交易"
SUBJECT_HEADERS = ["主体编号", "主体名称", "主体类型", "纳税人识别号"]
RELATION_HEADERS = ["关系编号", "出资或控制主体编号", "被持股或控制公司编号", "关系类型", "关系起始日", "关系终止日", "复核状态", "证据说明"]
TRADE_HEADERS = ["交易编号", "销售方主体编号", "购买方主体编号", "交易日期", "交易含税金额", "异常依据", "复核状态"]


def _rows(wb, sheet_name: str, headers: list[str], limit: int):
    from . import loader

    sheet = loader._sheet(wb, sheet_name, headers)
    if sheet.max_row - 1 > limit:
        raise loader.InputError(f"{sheet_name}超过 {limit} 行限制")
    for number, cells in enumerate(sheet.iter_rows(min_row=2), 2):
        if all(cell.value is None or str(cell.value).strip() == "" for cell in cells):
            continue
        if any(cell.data_type == "f" for cell in cells):
            raise loader.InputError(f"{sheet_name}第 {number} 行含公式；请先导出固定值")
        if any(cell.value is not None for cell in cells[len(headers):]):
            raise loader.InputError(f"{sheet_name}第 {number} 行存在未定义的附加列")
        values = [
            "" if cell.value is None else
            cell.value.date().isoformat() if isinstance(cell.value, datetime) else
            cell.value.isoformat() if isinstance(cell.value, date) else
            str(cell.value).strip()
            for cell in cells[:len(headers)]
        ]
        yield number, values


def _required(value: str, location: str, maximum: int = 120) -> str:
    from . import loader

    if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise loader.InputError(f"{location}为空、过长或含控制字符")
    return value


def _optional(value: str, location: str, maximum: int) -> str:
    return _required(value, location, maximum) if value else ""


def _date(value: str, location: str) -> date:
    from . import loader

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise loader.InputError(f"{location}须为 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise loader.InputError(f"{location}须为 YYYY-MM-DD") from None


def read_workbook(wb, company: Company) -> RelatedGraph | None:
    """Read three explicit source tables; a partial graph cannot be judged."""
    from . import loader

    names = {SHEET_SUBJECTS, SHEET_RELATIONS, SHEET_TRADES}
    present = names.intersection(wb.sheetnames)
    if not present:
        return None
    if present != names:
        raise loader.InputError("关联方图材料须同时包含关联方主体、关联关系、关联交易三张表")
    subjects: dict[str, RelatedSubject] = {}
    company_ids: set[str] = set()
    for row_no, (key, name, kind, taxpayer_id) in _rows(wb, SHEET_SUBJECTS, SUBJECT_HEADERS, 500):
        source = f"{SHEET_SUBJECTS}!第{row_no}行"
        key = _required(key, source + "主体编号", 64)
        name = _required(name, source + "主体名称")
        if kind not in {"个人", "企业"} or key in subjects:
            raise loader.InputError(f"{source}：主体类型无效或编号重复")
        if kind == "个人" and taxpayer_id:
            raise loader.InputError(f"{source}：个人主体不采集证件号码")
        taxpayer_id = _optional(taxpayer_id, source + "纳税人识别号", 64)
        if kind == "企业" and taxpayer_id:
            if taxpayer_id in company_ids:
                raise loader.InputError(f"{source}：企业纳税人识别号重复")
            company_ids.add(taxpayer_id)
        subjects[key] = RelatedSubject(key, name, kind, taxpayer_id, source)
    audited = [s for s in subjects.values() if s.taxpayer_id == company.taxpayer_id and s.kind == "企业"]
    if len(audited) != 1 or audited[0].name != company.name:
        raise loader.InputError("关联方主体须恰有一个与企业信息名称、纳税人识别号一致的被审计企业")
    relations: list[RelatedRelation] = []
    seen = set()
    for row_no, (key, owner, target, kind, start_on, end_on, state, basis) in _rows(wb, SHEET_RELATIONS, RELATION_HEADERS, 1000):
        source = f"{SHEET_RELATIONS}!第{row_no}行"
        key = _required(key, source + "关系编号", 64)
        if key in seen or owner not in subjects or target not in subjects or owner == target:
            raise loader.InputError(f"{source}：关系编号重复或主体引用无效")
        if subjects[target].kind != "企业" or kind not in {"股东", "控制"} or state not in {"已复核", "待复核"}:
            raise loader.InputError(f"{source}：关系类型、目标主体或复核状态无效")
        start_date = _date(start_on, f"{source}关系起始日")
        end_date = _date(end_on, f"{source}关系终止日") if end_on else None
        if end_date and end_date < start_date:
            raise loader.InputError(f"{source}：关系终止日早于起始日")
        basis = _required(basis, source + "证据说明", 500) if state == "已复核" else _optional(basis, source + "证据说明", 500)
        seen.add(key)
        relations.append(RelatedRelation(key, owner, target, kind, start_on, end_on,
                                         state == "已复核", basis, source))
    trades: list[RelatedTrade] = []
    seen.clear()
    period = periods.parse_period(company.period, "关联交易所属期")
    for row_no, (key, seller, buyer, traded_on, amount, anomaly, state) in _rows(wb, SHEET_TRADES, TRADE_HEADERS, 5000):
        source = f"{SHEET_TRADES}!第{row_no}行"
        key = _required(key, source + "交易编号", 64)
        if key in seen or seller not in subjects or buyer not in subjects or seller == buyer:
            raise loader.InputError(f"{source}：交易编号重复或主体引用无效")
        if subjects[seller].kind != "企业" or subjects[buyer].kind != "企业":
            raise loader.InputError(f"{source}：交易双方须为企业")
        transaction_date = _date(traded_on, f"{source}交易日期")
        if not period.start <= transaction_date <= period.end:
            raise loader.InputError(f"{source}：交易日期不在审计所属期内")
        amount_value = loader._number(amount, source + "交易含税金额")
        if (amount_value is None or amount_value <= 0 or amount_value > Decimal("1000000000000000")
                or amount_value.as_tuple().exponent < -2):
            raise loader.InputError(f"{source}：交易金额须为正数、不超过 10^15 元且最多两位小数")
        if state not in {"已复核", "待复核"}:
            raise loader.InputError(f"{source}：复核状态或异常依据无效")
        anomaly = _optional(anomaly, source + "异常依据", 500)
        seen.add(key)
        trades.append(RelatedTrade(key, seller, buyer, traded_on, amount_value, anomaly,
                                   state == "已复核", source))
    return RelatedGraph(list(subjects.values()), relations, trades)


def candidate_paths(dataset: Dataset) -> Iterator[tuple[RelatedRelation, RelatedRelation, RelatedTrade]]:
    """Stream reviewed relationship paths without materializing their cartesian product."""
    graph = dataset.related_graph
    if graph is None:
        return
    subjects = {item.key: item for item in graph.subjects}
    audited = next(item for item in graph.subjects if item.taxpayer_id == dataset.company.taxpayer_id)
    shareholders: dict[str, list[RelatedRelation]] = {}
    for item in graph.relations:
        if item.company_key == audited.key and item.kind == "股东" and item.reviewed:
            shareholders.setdefault(item.owner_key, []).append(item)
    trades_by_pair: dict[frozenset[str], list[RelatedTrade]] = {}
    for trade in graph.trades:
        trades_by_pair.setdefault(frozenset((trade.seller_key, trade.buyer_key)), []).append(trade)
    for control in graph.relations:
        if not control.reviewed or control.kind != "控制" or control.owner_key not in shareholders:
            continue
        other = subjects[control.company_key]
        if other.key == audited.key:
            continue
        for trade in trades_by_pair.get(frozenset((audited.key, other.key)), []):
            control_active = control.start_on <= trade.traded_on and (not control.end_on or trade.traded_on <= control.end_on)
            if not control_active:
                continue
            for share in shareholders[control.owner_key]:
                share_active = share.start_on <= trade.traded_on and (not share.end_on or trade.traded_on <= share.end_on)
                if share_active:
                    yield share, control, trade


def run(dataset: Dataset) -> list[Finding]:
    """Traverse a reviewed A <- shareholder -> controlled B -> A/B trade path."""
    graph = dataset.related_graph
    if graph is None:
        return []
    subjects = {item.key: item for item in graph.subjects}
    audited = next(item for item in graph.subjects if item.taxpayer_id == dataset.company.taxpayer_id)
    path_count = hit_count = 0
    evidence = []
    for share, control, trade in candidate_paths(dataset):
        path_count += 1
        if not trade.reviewed or not trade.anomaly_basis:
            continue
        hit_count += 1
        if hit_count > 20:
            continue
        owner = subjects[share.owner_key]
        other = subjects[control.company_key]
        evidence.extend([
            EvidenceItem("股东关系", f"{owner.name} → {audited.name}；{share.start_on} 至 {share.end_on or '持续'}", share.source + "；" + share.basis),
            EvidenceItem("控制关系", f"{owner.name} → {other.name}；{control.start_on} 至 {control.end_on or '持续'}", control.source + "；" + control.basis),
            EvidenceItem("关联交易待核查线索", f"{trade.key} {trade.amount:,.2f} 元；{trade.anomaly_basis}", trade.source),
        ])
    rule = Rule(
        id="G-001", name="关联方异常交易线索", category="关联方图", tax_type="企业所得税",
        severity="medium", logic={}, evidence=[], legal_basis=[],
        suggestion="核实控制关系、交易真实性与定价公允性；当前结果只是待核查线索，不直接认定违法。",
        description="独立图遍历：被审计企业的股东控制其他公司，且两公司有经复核的异常交易线索。",
        source_file="src/related_graph.py", version="1.0",
    )
    if hit_count:
        return [Finding(rule, "hit", None, "经复核的关系链与交易异常依据",
                        f"发现 {hit_count} 条关联方交易关系路径存在待核查异常线索，不等于违法认定。",
                        evidence, f"股东→控制企业→两企业交易，命中 {hit_count} 条路径；证据最多展示前 20 条")]
    reason = ("已发现关联交易，但交易未复核或缺少异常依据，不能判定为异常。" if path_count else
              "未取得完整且已复核的股东→控制企业→两企业交易证据链。")
    return [Finding(rule, "skipped", None, "需完整证据链", "关联方图规则未执行。",
                    skip_reason=reason)]
