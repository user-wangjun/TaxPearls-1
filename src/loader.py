"""Excel 材料标准化；空值保持缺失，来源与期间随指标保留。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
import re
from .workbooks import open_workbook
from . import config
from .input_errors import InputError
from .periods import _Period as _Period, _PeriodValue, parse_period as _parse_period, derive_period_metrics as _derive_period_metrics
from .models import Account, Company, Dataset, Metric









@dataclass(frozen=True)
class _Invoice:
    """One normalized invoice row before period validation and aggregation."""

    number: str
    issued_on: str
    kind: str
    amount: Decimal
    tax: Decimal
    status: str
    deductible_tax: Decimal | None
    deductible_period: str
    seller_id: str
    buyer_id: str
    source: str
    detail: str = ""


@dataclass(frozen=True)
class _BankTransaction:
    """One normalized posted bank transaction; never treated as revenue by itself."""

    key: str
    transaction_id: str
    transacted_on: str
    summary: str
    income: Decimal
    expense: Decimal
    category: str
    currency: str
    counterparty: str
    account_hint: str
    explicit_id: bool
    source: str
    detail: str = ""


@dataclass(frozen=True)
class _BankAdjustment:
    """A reviewed bridge from bank receipts to accrual-basis, tax-exclusive revenue."""

    number: str
    transaction_id: str
    category: str
    period: str
    recognized_amount: Decimal | None
    adjustment_amount: Decimal | None
    direction: str
    workpaper: str
    reviewed: bool
    detail: str
    source: str


@dataclass(frozen=True)
class _HumanRecord:
    """A privacy-minimized monthly tax/social/provident-fund record."""

    kind: str
    person_key: str
    month: str
    active: bool
    amount: Decimal | None
    source: str


@dataclass(frozen=True)
class _Contract:
    """One contract master record; amount is tax-inclusive."""

    number: str
    kind: str
    counterparty: str
    signed_on: str
    amount: Decimal
    performance_start: str
    performance_end: str
    reviewed: bool
    workpaper: str
    detail: str
    source: str


@dataclass(frozen=True)
class _Fulfillment:
    """A reviewed goods/service delivery record supporting the performance flow."""

    number: str
    contract_number: str
    fulfilled_on: str
    kind: str
    amount: Decimal
    reviewed: bool
    workpaper: str
    detail: str
    source: str


@dataclass(frozen=True)
class _ContractLink:
    """A manual allocation connecting contract, invoice, funds and performance evidence."""

    number: str
    contract_number: str
    invoice_number: str
    transaction_id: str
    fulfillment_number: str
    amount: Decimal
    reviewed: bool
    workpaper: str
    detail: str
    source: str














def _sheet(wb, name, header):
    if name not in wb.sheetnames:
        raise InputError(f"缺少工作表「{name}」。当前工作表：{wb.sheetnames}")
    ws = wb[name]
    actual = [c.value for c in ws[1]]
    if actual[:len(header)] != header:
        raise InputError(f"「{name}」表头不符。期望：{header}；实际：{actual}")
    return ws


def _number(value, location):
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool):
        raise InputError(f"{location}：布尔值不能作为金额或人数")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InputError(f"{location}：不是有效数值：{value!r}") from None
    if not number.is_finite():
        raise InputError(f"{location}：数值必须有限")
    return number


def _read_company(wb):
    ws = _sheet(wb, config.SHEET_COMPANY, ["项目", "内容"])
    data = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is None:
            continue
        key = str(row[0]).strip()
        if key in data:
            raise InputError(f"企业信息项目重复：{key}")
        data[key] = "" if row[1] is None else str(row[1]).strip()
    missing = [key for key in config.COMPANY_FIELDS if not data.get(key)]
    if missing:
        raise InputError(f"企业信息缺少字段：{missing}")
    return Company(*(data[k] for k in config.COMPANY_FIELDS))


def _read_accounts(wb):
    ws = _sheet(wb, config.SHEET_ACCOUNTS, config.COL_ACCOUNTS)
    out, seen = [], set()
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if row[0] is None:
            continue
        code = str(row[0]).strip()
        if code in seen:
            raise InputError(f"科目余额表 A{idx}：科目编码重复 {code}；请先汇总到唯一口径")
        seen.add(code)
        numbers = [_number(row[col], f"科目余额表 {chr(65+col)}{idx}") for col in range(2, 6)]
        out.append(Account(code, str(row[1] or "").strip(), *numbers))
    if not out:
        raise InputError("科目余额表没有数据行")
    return out


def _read_declarations(wb):
    ws = _sheet(wb, config.SHEET_DECLARATION, ["项目", "金额"])
    data, seen = {}, set()
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if row[0] is None:
            continue
        key = str(row[0]).strip()
        if key in seen:
            raise InputError(f"增值税申报 A{idx}：项目重复 {key}")
        seen.add(key)
        value = _number(row[1], f"增值税申报 B{idx}")
        if value is not None:
            data[key] = value
    return data


def _build_metrics(accounts, declarations):
    metrics = {}
    by_code = {a.code: a for a in accounts}
    for name, spec in config.ACCOUNT_MAP.items():
        selected = [by_code.get(code) for code in spec["accounts"]]
        if any(a is None or getattr(a, spec["side"]) is None for a in selected):
            continue
        amounts = [getattr(a, spec["side"]) for a in selected]
        metrics[name] = Metric(
            name, sum(amounts, Decimal(0)), "科目余额表—" + spec["label"],
            " + ".join(f"{a.code} {a.name} {v:,.2f}" for a, v in zip(selected, amounts)),
        )
    for name, item in config.DECLARATION_ITEMS.items():
        if item in declarations:
            metrics[name] = Metric(name, declarations[item], f"增值税申报—{item}", f"{item} {declarations[item]:,.2f}")
    return metrics


def _read_supplement(wb, company, metrics):
    if config.SHEET_SUPPLEMENT not in wb.sheetnames:
        return
    ws = _sheet(wb, config.SHEET_SUPPLEMENT, config.COL_SUPPLEMENT)
    reserved = set(config.ACCOUNT_MAP) | set(config.DECLARATION_ITEMS) | set(metrics)
    seen = set()
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if row[0] is None:
            continue
        key = str(row[0]).strip()
        if key in seen or key in reserved:
            raise InputError(f"补充指标 A{idx}：指标重复或试图覆盖原始表指标「{key}」")
        seen.add(key)
        value = _number(row[1], f"补充指标 B{idx}")
        if value is None:
            continue
        source, period, detail = [str(v).strip() if v is not None else "" for v in row[2:5]]
        if not source or not detail:
            raise InputError(f"补充指标第 {idx} 行「{key}」必须填写来源和口径说明")
        if period != company.period:
            raise InputError(f"补充指标第 {idx} 行「{key}」所属期必须与企业信息一致；历史实际期间写入口径说明")
        if key.startswith("人力.") and key.endswith("人数") and (value < 0 or value != value.to_integral_value()):
            raise InputError(f"补充指标第 {idx} 行「{key}」人数必须为非负整数")
        metrics[key] = Metric(key, value, f"补充指标!B{idx} ← {source}；核对期 {period}", detail)


def _read_statement(wb, sheet_name, prefix, metrics):
    """Read a normalized financial statement without guessing vendor layouts."""
    if sheet_name not in wb.sheetnames:
        return
    ws = _sheet(wb, sheet_name, config.COL_STATEMENT)
    seen = set()
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if row[0] is None:
            continue
        item = str(row[0]).strip()
        key = f"{prefix}.{item}"
        if not item or key in seen or key in metrics:
            raise InputError(f"{sheet_name} A{idx}：项目为空、重复或覆盖已有指标「{key}」")
        seen.add(key)
        value = _number(row[1], f"{sheet_name} B{idx}")
        if value is not None:
            metrics[key] = Metric(key, value, f"{sheet_name}!B{idx}—{item}", f"{item} {value:,.2f}")


def _read_history(wb, company, metrics):
    if config.SHEET_HISTORY not in wb.sheetnames:
        return {}
    ws = _sheet(wb, config.SHEET_HISTORY, config.COL_HISTORY)
    seen, series = set(), {}
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if row[0] is None:
            continue
        key = str(row[0]).strip()
        value = _number(row[1], f"历史指标 B{idx}")
        source, period, detail = [str(v).strip() if v is not None else "" for v in row[2:5]]
        if value is None:
            continue
        if not source or not period or not detail:
            raise InputError(f"历史指标第 {idx} 行「{key}」必须填写来源、实际所属期和口径说明")
        metric = Metric(key, value, f"历史指标!B{idx} ← {source}；实际期间 {period}", detail)
        if key.startswith("历史.") or key.startswith("年度."):
            if key in seen or key in metrics:
                raise InputError(f"历史指标 A{idx}：指标重复「{key}」")
            seen.add(key)
            metrics[key] = metric
            continue
        if key not in config.PERIOD_SERIES:
            allowed = "、".join(config.PERIOD_SERIES)
            raise InputError(f"历史指标 A{idx}：期间序列仅支持 {allowed}；原有成品指标须以「历史.」或「年度.」开头")
        normalized = _parse_period(period, f"历史指标 D{idx}")
        values = series.setdefault(key, {})
        if normalized.key in values:
            raise InputError(f"历史指标第 {idx} 行「{key}」存在重复所属期「{period}」")
        values[normalized.key] = _PeriodValue(normalized, metric, idx)
    _derive_period_metrics(company, metrics, series)
    return series


_INVOICE_HEADERS = {
    "number": {"发票号码", "数电票号码", "发票号", "invoice number", "invoicenumber"},
    "issued_on": {"开票日期", "开票时间", "申请时间", "request time", "requesttime", "issuedate"},
    "kind": {"类型", "发票方向", "业务方向", "收支类型", "invoicedirection"},
    "amount": {"不含税金额", "合计金额", "金额合计", "金额", "totalamwithouttax"},
    "tax": {"税额", "合计税额", "税额合计", "totaltaxam"},
    "total": {"价税合计", "价税合计小写", "含税金额", "totaltaxincludedamount"},
    "status": {"状态", "发票状态", "开票状态", "invoicestatus"},
    "seller_id": {"销方纳税人识别号", "销售方纳税人识别号", "销方税号", "selleridnum"},
    "buyer_id": {"购方纳税人识别号", "购买方纳税人识别号", "购方税号", "buyeridnum", "purchaseridnum"},
    "usage": {"用途确认", "抵扣用途", "用途", "usageconfirmation"},
    "confirmed": {"是否确认用途", "是否抵扣勾选", "抵扣状态", "用途确认状态", "whethereinvoiceusagehasbeenconfirmed"},
    "deductible_period": {"用途确认所属期", "抵扣所属期", "确认所属期", "periodofusageconfirmation"},
    "transferred": {"是否进项转出", "是否进项税额转出", "whetherinputvathasbeentransferredout"},
    "transferred_tax": {"进项转出税额", "转出税额", "amountoftransferredoutinputvat"},
}


def _header_token(value: object) -> str:
    return re.sub(r"[\s_()（）【】\[\]:：/\\-]+", "", str(value or "")).lower()


_INVOICE_HEADER_TOKENS = {
    field: {_header_token(alias) for alias in aliases}
    for field, aliases in _INVOICE_HEADERS.items()
}


def _invoice_columns(ws) -> dict[str, int]:
    columns = {}
    for index, cell in enumerate(ws[1]):
        token = _header_token(cell.value)
        for field, aliases in _INVOICE_HEADER_TOKENS.items():
            if token in aliases:
                if field in columns:
                    raise InputError(f"{ws.title} 表头存在重复口径「{cell.value}」")
                columns[field] = index
                break
    return columns


def _invoice_number(value: object, location: str) -> str:
    if isinstance(value, bool):
        raise InputError(f"{location}：发票号码格式无效")
    if isinstance(value, float) and value.is_integer():
        value = format(value, ".0f")
    number = str(value or "").strip()
    if not number or len(number) > 64 or not re.fullmatch(r"[0-9A-Za-z._/-]+", number):
        raise InputError(f"{location}：发票号码为空或格式无效")
    return number.upper()


def _invoice_date(value: object, location: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    match = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日|[ T].*)?$", text)
    if not match:
        raise InputError(f"{location}：开票日期须为完整日期")
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        raise InputError(f"{location}：开票日期无效「{text}」") from None


def _invoice_kind(value: object, sheet_name: str, seller_id: object, buyer_id: object, company: Company) -> str:
    text = str(value or "").strip().lower()
    if text in {"销项", "销项发票", "销售", "开出", "开具", "sale", "sales"}:
        return "销项"
    if text in {"采购", "进项", "进项发票", "购进", "受票", "purchase", "input"}:
        return "采购"
    if text:
        raise InputError(f"{sheet_name}：无法识别发票方向「{value}」；仅支持销项/采购")
    title = sheet_name.lower()
    if "销项" in title:
        return "销项"
    if any(word in title for word in ("进项", "采购", "受票")):
        return "采购"
    taxpayer_id = re.sub(r"\s+", "", company.taxpayer_id or "").upper()
    seller = re.sub(r"\s+", "", str(seller_id or "")).upper()
    buyer = re.sub(r"\s+", "", str(buyer_id or "")).upper()
    if taxpayer_id and seller == taxpayer_id and buyer != taxpayer_id:
        return "销项"
    if taxpayer_id and buyer == taxpayer_id and seller != taxpayer_id:
        return "采购"
    return ""


def _invoice_status(value: object, amount: Decimal, tax: Decimal, location: str) -> str:
    text = str(value or "").strip().lower()
    if any(word in text for word in ("作废", "撤销", "void", "cancel")):
        status = "作废"
    elif any(word in text for word in ("红字", "红票", "红冲", "冲红", "负数", "red")):
        status = "红字"
    elif text in {"", "正常", "有效", "蓝字", "已开具", "normal", "valid", "blue"}:
        status = "正常"
    else:
        raise InputError(f"{location}：无法识别发票状态「{value}」")
    if amount < 0 or tax < 0:
        if status == "正常" and text:
            raise InputError(f"{location}：正常发票金额不能为负数")
        if status == "正常":
            status = "红字"
    return status


def _invoice_bool(value: object, location: str) -> bool | None:
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "是", "已确认", "已勾选", "已抵扣", "确认", "yes"}:
        return True
    if text in {"false", "0", "否", "未确认", "未勾选", "未抵扣", "no"}:
        return False
    raise InputError(f"{location}：布尔状态无法识别「{value}」")


def _invoice_row(ws, row: tuple, idx: int, columns: dict[str, int], company: Company, template_mode: bool) -> _Invoice:
    def value(field):
        index = columns.get(field)
        return row[index] if index is not None and index < len(row) else None

    location = f"{ws.title}第 {idx} 行"
    number = _invoice_number(value("number"), f"{location}发票号码")
    issued_on = _invoice_date(value("issued_on"), f"{location}开票日期")
    tax = _number(value("tax"), f"{location}税额")
    amount = _number(value("amount"), f"{location}不含税金额")
    total = _number(value("total"), f"{location}价税合计")
    if tax is None:
        raise InputError(f"{location}：税额不能为空；零税额须明确填 0")
    if amount is None:
        if total is None:
            raise InputError(f"{location}：不含税金额或价税合计至少填写一项")
        amount = (abs(total) - abs(tax)) * (-1 if total < 0 else 1)
        if abs(total) < abs(tax):
            raise InputError(f"{location}：价税合计不能小于税额")
    if total is not None and abs(abs(total) - abs(amount) - abs(tax)) > Decimal("0.01"):
        raise InputError(f"{location}：价税合计与不含税金额加税额不一致")
    kind = _invoice_kind(value("kind"), ws.title, value("seller_id"), value("buyer_id"), company)
    if not kind:
        raise InputError(f"{location}：无法根据类型、工作表名或购销双方税号判定销项/采购")
    status = _invoice_status(value("status"), amount, tax, location)
    confirmed = _invoice_bool(value("confirmed"), f"{location}用途确认状态")
    usage = str(value("usage") or "").strip()
    transferred = _invoice_bool(value("transferred"), f"{location}进项转出状态")
    transferred_tax = _number(value("transferred_tax"), f"{location}进项转出税额")
    if transferred is False and transferred_tax not in {None, Decimal(0)}:
        raise InputError(f"{location}：未转出时进项转出税额必须为空或 0")
    deductible_tax = None
    if kind == "采购" and status != "作废":
        explicitly_deductible = confirmed is True and (not usage or "抵扣" in usage)
        if template_mode and confirmed is None and not usage:
            explicitly_deductible = True
        if explicitly_deductible:
            deductible_tax = abs(tax)
            if transferred:
                transferred_tax = abs(transferred_tax or Decimal(0))
                if transferred_tax > deductible_tax:
                    raise InputError(f"{location}：进项转出税额不能大于发票税额")
                deductible_tax -= transferred_tax
    detail = f"{kind}{status}；不含税 {abs(amount):,.2f}；税额 {abs(tax):,.2f}"
    if total is not None:
        detail += f"；价税合计 {abs(total):,.2f}"
    return _Invoice(
        number, issued_on, kind, abs(amount), abs(tax), status, deductible_tax,
        str(value("deductible_period") or "").strip(),
        str(value("seller_id") or "").strip(), str(value("buyer_id") or "").strip(),
        f"{ws.title}!第{idx}行", detail,
    )


def _read_invoices(wb, company: Company) -> list[_Invoice]:
    known = {
        config.SHEET_COMPANY, config.SHEET_ACCOUNTS, config.SHEET_DECLARATION,
        config.SHEET_INCOME, config.SHEET_BALANCE, config.SHEET_CASHFLOW,
        config.SHEET_HISTORY, config.SHEET_BANK, config.SHEET_BANK_ADJUSTMENTS,
        config.SHEET_HUMAN, config.SHEET_CONTRACTS, config.SHEET_FULFILLMENTS,
        config.SHEET_CONTRACT_LINKS, config.SHEET_SUPPLEMENT,
    }
    candidates = []
    for ws in wb.worksheets:
        if ws.title in known:
            continue
        columns = _invoice_columns(ws)
        required = {"number", "issued_on", "tax"}
        looks_like_invoice = required.issubset(columns) and ({"amount", "total"} & columns.keys())
        if ws.title == config.SHEET_INVOICES and not looks_like_invoice:
            raise InputError(f"「{config.SHEET_INVOICES}」表头不符；必须包含发票号码、开票日期、不含税金额或价税合计、税额")
        if looks_like_invoice:
            candidates.append((ws, columns))
    seen, invoices = set(), []
    for ws, columns in candidates:
        template_mode = [cell.value for cell in ws[1]][:len(config.COL_INVOICES)] == config.COL_INVOICES
        for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
            if all(value is None or isinstance(value, str) and not value.strip() for value in row):
                continue
            invoice = _invoice_row(ws, row, idx, columns, company, template_mode)
            if invoice.number in seen:
                raise InputError(f"发票号码重复「{invoice.number}」；请先去重，禁止重复计入")
            seen.add(invoice.number)
            invoices.append(invoice)
    return invoices


def _invoice_metrics(company: Company, invoices: list[_Invoice]) -> dict[str, Metric]:
    if not invoices:
        return {}
    audit_period = _parse_period(company.period, "企业信息核对所属期")
    totals = {kind: {"amount": Decimal(0), "tax": Decimal(0), "count": 0, "red": 0, "void": 0}
              for kind in ("销项", "采购")}
    deductible_total, deductible_count = Decimal(0), 0
    seen, sources = set(), []
    for invoice in invoices:
        if invoice.number in seen:
            raise InputError(f"发票号码重复「{invoice.number}」；请先去重，禁止重复计入")
        seen.add(invoice.number)
        issued_on = date.fromisoformat(invoice.issued_on)
        if not audit_period.start <= issued_on <= audit_period.end:
            raise InputError(
                f"发票「{invoice.number}」开票日期 {invoice.issued_on} 不在核对期间 {company.period} 内；"
                "请拆分期间或更正核对所属期"
            )
        kind = invoice.kind or _invoice_kind("", "XML 发票", invoice.seller_id, invoice.buyer_id, company)
        if not kind:
            raise InputError(
                f"发票「{invoice.number}」无法根据购销双方税号与企业纳税人识别号判定销项/采购"
            )
        bucket = totals[kind]
        if invoice.status == "作废":
            bucket["void"] += 1
            continue
        sign = Decimal(-1) if invoice.status == "红字" else Decimal(1)
        bucket["amount"] += sign * invoice.amount
        bucket["tax"] += sign * invoice.tax
        bucket["count"] += 1
        bucket["red"] += invoice.status == "红字"
        sources.append(invoice.source)
        if kind == "采购" and invoice.deductible_tax is not None:
            in_period = True
            if invoice.deductible_period:
                confirmation = _parse_period(invoice.deductible_period, f"发票「{invoice.number}」抵扣所属期")
                in_period = audit_period.start <= confirmation.start and confirmation.end <= audit_period.end
            if in_period:
                deductible_total += sign * invoice.deductible_tax
                deductible_count += 1
    source = "；".join(dict.fromkeys(sources)) or "发票明细"
    metrics = {}
    for kind, amount_key, tax_key in (
        ("销项", "发票.销项净额", "发票.销项税额净额"),
        ("采购", "发票.采购不含税净额", "发票.采购税额净额"),
    ):
        bucket = totals[kind]
        if bucket["count"]:
            detail = (
                f"{bucket['count']} 张有效票（红字 {bucket['red']} 张、另剔除作废 {bucket['void']} 张）；"
                f"不含税净额 {bucket['amount']:,.2f}；税额净额 {bucket['tax']:,.2f}"
            )
            metrics[amount_key] = Metric(amount_key, bucket["amount"], source + f" / {kind}有效发票", detail)
            metrics[tax_key] = Metric(tax_key, bucket["tax"], source + f" / {kind}有效发票税额", detail)
    if deductible_count:
        metrics["凭证.本期确认抵扣税额"] = Metric(
            "凭证.本期确认抵扣税额", deductible_total,
            source + " / 已明确确认抵扣且抵扣所属期落在本期的采购发票",
            f"{deductible_count} 张确认抵扣发票税额净计 {deductible_total:,.2f}；未确认用途的采购税额不计入",
        )
    return metrics


_BANK_HEADERS = {
    "transaction_id": {"流水号", "交易流水号", "交易序号", "唯一编号", "主机流水号", "银行流水号", "transactionid"},
    "date": {"交易日期", "记账日期", "入账日期", "交易时间", "交易日", "transactiondate"},
    "summary": {"摘要", "交易摘要", "用途", "附言", "备注", "交易备注", "description"},
    "income": {"收入金额", "收入", "贷方发生额", "贷方金额", "转入金额", "creditamount"},
    "expense": {"支出金额", "支出", "借方发生额", "借方金额", "转出金额", "debitamount"},
    "amount": {"交易金额", "发生额", "金额", "transactionamount"},
    "direction": {"收支方向", "交易方向", "借贷标志", "借贷方向", "方向", "debitcreditindicator"},
    "category": {"分类", "业务分类", "核对分类"},
    "currency": {"币别", "币种", "货币", "currency"},
    "counterparty": {"对方户名", "对方名称", "交易对手名称", "counterpartyname"},
    "account": {"账号", "本方账号", "交易账号", "accountnumber"},
    "counterparty_account": {"对方账号", "交易对手账号", "counterpartyaccount"},
    "balance": {"余额", "账户余额", "balance"},
}

_BANK_HEADER_TOKENS = {
    field: {_header_token(alias) for alias in aliases}
    for field, aliases in _BANK_HEADERS.items()
}

_BANK_LINKED_CATEGORIES = {
    "经营回款", "借款", "内部转账", "资本金", "往来款", "预收款", "保证金", "退款", "其他非经营",
}
_BANK_STANDALONE_CATEGORIES = {
    "现金收入补计", "应收收入补计", "跨期收入补计", "跨期收入扣减", "退款折让扣减", "其他有据调节",
}


def _bank_header(ws) -> tuple[int, dict[str, int]] | None:
    """Locate a flat bank-export header in the first 30 rows."""

    best = None
    for row_index in range(1, min(ws.max_row, 30) + 1):
        columns = {}
        for index, cell in enumerate(ws[row_index]):
            token = _header_token(cell.value)
            for field, aliases in _BANK_HEADER_TOKENS.items():
                if token in aliases:
                    if field in columns:
                        raise InputError(f"{ws.title} 第 {row_index} 行存在重复银行字段「{cell.value}」")
                    columns[field] = index
                    break
        money_columns = {"income", "expense"} & columns.keys()
        has_money = bool(money_columns) or {"amount", "direction"}.issubset(columns)
        if "date" in columns and has_money and ("transaction_id" in columns or ws.title == config.SHEET_BANK):
            score = len(columns)
            if best is None or score > best[0]:
                best = (score, row_index, columns)
    return (best[1], best[2]) if best else None


def _bank_identifier(value: object, location: str) -> str:
    if isinstance(value, bool):
        raise InputError(f"{location}：编号格式无效")
    if isinstance(value, float) and value.is_integer():
        value = format(value, ".0f")
    text = str(value or "").strip()
    if not text or len(text) > 128 or any(char in text for char in "\r\n\t"):
        raise InputError(f"{location}：编号为空或格式无效")
    return re.sub(r"\s+", "", text).upper()


def _bank_date(value: object, location: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    match = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日|[ T].*)?$", text)
    if not match:
        raise InputError(f"{location}：交易日期须为完整日期")
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        raise InputError(f"{location}：交易日期无效「{text}」") from None


def _bank_number(value: object, location: str) -> Decimal | None:
    if isinstance(value, str):
        value = value.strip().replace(",", "").replace("￥", "").replace("¥", "")
    return _number(value, location)


def _bank_direction(value: object, location: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"收入", "收", "转入", "入账", "贷", "贷方", "c", "credit"}:
        return "收入"
    if text in {"支出", "付", "转出", "出账", "借", "借方", "d", "debit"}:
        return "支出"
    raise InputError(f"{location}：无法识别收支方向「{value}」")


def _bank_currency(value: object, location: str) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    if text in {"", "人民币", "人民币元", "CNY", "RMB", "156"}:
        return "CNY"
    raise InputError(f"{location}：当前仅支持人民币流水，实际为「{value}」")


def _mask_account(value: object) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    return ("*" * min(max(len(text) - 4, 0), 8) + text[-4:]) if text else ""


def _read_bank_transactions(wb) -> list[_BankTransaction]:
    known = {
        config.SHEET_COMPANY, config.SHEET_ACCOUNTS, config.SHEET_DECLARATION,
        config.SHEET_INCOME, config.SHEET_BALANCE, config.SHEET_CASHFLOW,
        config.SHEET_HISTORY, config.SHEET_INVOICES, config.SHEET_BANK_ADJUSTMENTS,
        config.SHEET_HUMAN, config.SHEET_CONTRACTS, config.SHEET_FULFILLMENTS,
        config.SHEET_CONTRACT_LINKS,
        config.SHEET_SUPPLEMENT,
    }
    candidates = []
    for ws in wb.worksheets:
        if ws.title in known:
            continue
        header = _bank_header(ws)
        if ws.title == config.SHEET_BANK and header is None:
            raise InputError(
                f"「{config.SHEET_BANK}」表头不符；须包含交易日期及收入/支出金额，"
                "来源导出表还须包含唯一编号或交易流水号"
            )
        if header is not None:
            candidates.append((ws, *header))

    seen, transactions = set(), []
    for ws, header_row, columns in candidates:
        header_values = [cell.value for cell in ws[header_row]]
        legacy = header_row == 1 and header_values[:len(config.COL_BANK)] == config.COL_BANK

        def value(row, field):
            column = columns.get(field)
            return row[column] if column is not None and column < len(row) else None

        for idx, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True), header_row + 1):
            if all(item is None or isinstance(item, str) and not item.strip() for item in row):
                continue
            row_text = "".join(str(item or "") for item in row)
            if not value(row, "date") and not value(row, "transaction_id") and "合计" in row_text:
                continue
            location = f"{ws.title}第 {idx} 行"
            explicit_id = value(row, "transaction_id") not in {None, ""}
            if not explicit_id and not legacy:
                raise InputError(f"{location}：来源银行流水必须提供唯一编号或交易流水号")
            transaction_id = (
                _bank_identifier(value(row, "transaction_id"), f"{location}流水号")
                if explicit_id else f"{ws.title}-{idx}"
            )
            key = ("ID:" if explicit_id else "LEGACY:") + transaction_id
            if key in seen:
                raise InputError(f"银行流水号重复「{transaction_id}」；请先去重，禁止重复计入")
            seen.add(key)
            transacted_on = _bank_date(value(row, "date"), f"{location}交易日期")
            income = expense = Decimal(0)
            if "income" in columns or "expense" in columns:
                income = _bank_number(value(row, "income"), f"{location}收入金额") or Decimal(0)
                expense = _bank_number(value(row, "expense"), f"{location}支出金额") or Decimal(0)
                if income < 0 or expense < 0:
                    raise InputError(f"{location}：收入和支出金额须为非负数")
            else:
                amount = _bank_number(value(row, "amount"), f"{location}交易金额")
                if amount is None or amount == 0:
                    raise InputError(f"{location}：交易金额不能为空或零")
                direction = _bank_direction(value(row, "direction"), f"{location}收支方向")
                if direction == "收入":
                    income = abs(amount)
                else:
                    expense = abs(amount)
            if (income > 0) == (expense > 0):
                raise InputError(f"{location}：收入和支出必须且只能有一项大于零")
            category = str(value(row, "category") or "").strip()
            if legacy and not category:
                raise InputError(f"{location}：统一模板必须填写分类，避免把借款或内部转账误当收入")
            currency = _bank_currency(value(row, "currency"), f"{location}币种")
            summary = str(value(row, "summary") or "").strip()
            counterparty = str(value(row, "counterparty") or "").strip()
            account_hint = _mask_account(value(row, "account"))
            counterparty_hint = _mask_account(value(row, "counterparty_account"))
            detail = (
                f"{transacted_on}；{'收入' if income else '支出'} {(income or expense):,.2f}；"
                f"分类 {category or '待调节底稿复核'}；币种 {currency}"
            )
            if counterparty:
                detail += f"；对方 {counterparty}"
            if counterparty_hint:
                detail += f"（账号尾号 {counterparty_hint}）"
            transactions.append(_BankTransaction(
                key, transaction_id, transacted_on, summary, income, expense, category, currency,
                counterparty, account_hint, explicit_id, f"{ws.title}!第{idx}行", detail,
            ))
    return transactions


def _bank_reviewed(value: object, location: str) -> bool:
    text = str(value or "").strip().lower()
    if value is True or text in {"已复核", "复核通过", "是", "true", "1", "yes"}:
        return True
    if value is False or text in {"", "待复核", "未复核", "否", "false", "0", "no"}:
        return False
    raise InputError(f"{location}：复核状态无法识别「{value}」")


def _read_bank_adjustments(wb) -> list[_BankAdjustment]:
    if config.SHEET_BANK_ADJUSTMENTS not in wb.sheetnames:
        return []
    ws = _sheet(wb, config.SHEET_BANK_ADJUSTMENTS, config.COL_BANK_ADJUSTMENTS)
    adjustments, seen = [], set()
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if all(value is None or isinstance(value, str) and not value.strip() for value in row):
            continue
        location = f"{config.SHEET_BANK_ADJUSTMENTS}第 {idx} 行"
        number = _bank_identifier(row[0], f"{location}调节编号")
        if number in seen:
            raise InputError(f"{location}：调节编号重复「{number}」")
        seen.add(number)
        transaction_id = _bank_identifier(row[1], f"{location}流水号") if str(row[1] or "").strip() else ""
        category = str(row[2] or "").strip()
        if category not in _BANK_LINKED_CATEGORIES | _BANK_STANDALONE_CATEGORIES:
            allowed = "、".join(sorted(_BANK_LINKED_CATEGORIES | _BANK_STANDALONE_CATEGORIES))
            raise InputError(f"{location}：分类「{category}」无效；支持 {allowed}")
        period = str(row[3] or "").strip()
        _parse_period(period, f"{location}权责所属期")
        recognized = _number(row[4], f"{location}本期不含税收入")
        amount = _number(row[5], f"{location}调节金额")
        direction_raw = str(row[6] or "").strip()
        direction = ""
        if direction_raw:
            if direction_raw in {"加计", "增加", "+"}:
                direction = "加计"
            elif direction_raw in {"扣减", "减少", "-"}:
                direction = "扣减"
            else:
                raise InputError(f"{location}：调节方向仅支持加计或扣减")
        workpaper = str(row[7] or "").strip()
        detail = str(row[9] or "").strip()
        if not workpaper or not detail:
            raise InputError(f"{location}：必须填写底稿编号和说明")
        if len(workpaper) > 128 or len(detail) > 2000:
            raise InputError(f"{location}：底稿编号或说明过长")
        adjustments.append(_BankAdjustment(
            number, transaction_id, category, period, recognized, amount, direction, workpaper,
            _bank_reviewed(row[8], f"{location}复核状态"), detail, f"{ws.title}!第{idx}行",
        ))
    return adjustments


def _bank_metrics(
    company: Company,
    transactions: list[_BankTransaction],
    adjustments: list[_BankAdjustment],
) -> dict[str, Metric]:
    if not transactions and not adjustments:
        return {}
    if adjustments and not transactions:
        raise InputError("银行调节存在但没有原始银行流水；请同时上传来源流水")
    audit_period = _parse_period(company.period, "企业信息核对所属期")
    receipts = payments = Decimal(0)
    sources, by_id, seen_keys = [], {}, set()
    for transaction in transactions:
        if transaction.key in seen_keys:
            raise InputError(f"银行流水重复「{transaction.transaction_id}」；请先去重，禁止重复计入")
        seen_keys.add(transaction.key)
        transacted_on = date.fromisoformat(transaction.transacted_on)
        if not audit_period.start <= transacted_on <= audit_period.end:
            raise InputError(
                f"银行流水「{transaction.transaction_id}」交易日期 {transaction.transacted_on} "
                f"不在核对期间 {company.period} 内；请拆分期间或更正核对所属期"
            )
        receipts += transaction.income
        payments += transaction.expense
        sources.append(transaction.source)
        if transaction.explicit_id:
            if transaction.transaction_id in by_id:
                raise InputError(f"银行流水号重复「{transaction.transaction_id}」；请先去重，禁止重复计入")
            by_id[transaction.transaction_id] = transaction
    raw_source = "；".join(dict.fromkeys(sources)) or "银行流水"
    metrics = {
        "银行.收入流水合计": Metric(
            "银行.收入流水合计", receipts, raw_source + " / 原始入账流水",
            f"{len(transactions)} 行已入账人民币流水；收入合计 {receipts:,.2f}；不得直接作为营业收入",
        ),
        "银行.支出流水合计": Metric(
            "银行.支出流水合计", payments, raw_source + " / 原始出账流水",
            f"{len(transactions)} 行已入账人民币流水；支出合计 {payments:,.2f}",
        ),
    }
    if not adjustments:
        return metrics
    if any(not transaction.explicit_id and transaction.income > 0 for transaction in transactions):
        raise InputError("生成银行收入调节数前，每笔收入流水必须具有银行唯一编号或交易流水号")

    classified, adjustment_numbers = set(), set()
    recognized_total = adjustment_total = Decimal(0)
    categories, workpapers, adjustment_sources = {}, [], []
    for adjustment in adjustments:
        if adjustment.number in adjustment_numbers:
            raise InputError(f"银行调节编号重复「{adjustment.number}」")
        adjustment_numbers.add(adjustment.number)
        if not adjustment.reviewed:
            raise InputError(f"银行调节「{adjustment.number}」尚未复核，不能生成调节后收入")
        period = _parse_period(adjustment.period, f"银行调节「{adjustment.number}」权责所属期")
        in_period = audit_period.start <= period.start and period.end <= audit_period.end
        workpapers.append(adjustment.workpaper)
        adjustment_sources.append(adjustment.source)
        categories[adjustment.category] = categories.get(adjustment.category, 0) + 1
        if adjustment.transaction_id:
            if adjustment.category not in _BANK_LINKED_CATEGORIES:
                raise InputError(f"银行调节「{adjustment.number}」：带流水号时必须使用流水分类")
            transaction = by_id.get(adjustment.transaction_id)
            if transaction is None:
                raise InputError(f"银行调节「{adjustment.number}」引用不存在的流水号「{adjustment.transaction_id}」")
            if transaction.income <= 0:
                raise InputError(f"银行调节「{adjustment.number}」只能关联收入流水")
            if adjustment.transaction_id in classified:
                raise InputError(f"收入流水「{adjustment.transaction_id}」被重复分类")
            classified.add(adjustment.transaction_id)
            recognized = adjustment.recognized_amount
            if recognized is None or recognized < 0 or recognized > transaction.income:
                raise InputError(
                    f"银行调节「{adjustment.number}」本期不含税收入须明确填写，且在 0 与该笔收入流水之间"
                )
            if adjustment.adjustment_amount not in {None, Decimal(0)} or adjustment.direction:
                raise InputError(f"银行调节「{adjustment.number}」关联流水时不得同时填写独立调节金额或方向")
            if adjustment.category != "经营回款" and recognized != 0:
                raise InputError(f"银行调节「{adjustment.number}」非经营/往来分类的本期不含税收入必须为 0")
            if not in_period and recognized != 0:
                raise InputError(f"银行调节「{adjustment.number}」权责所属期不在本期时，本期不含税收入必须为 0")
            if in_period:
                recognized_total += recognized
        else:
            if adjustment.category not in _BANK_STANDALONE_CATEGORIES:
                raise InputError(f"银行调节「{adjustment.number}」：无流水号时必须使用独立调节分类")
            if adjustment.recognized_amount not in {None, Decimal(0)}:
                raise InputError(f"银行调节「{adjustment.number}」独立调节不得填写本期不含税收入")
            if adjustment.adjustment_amount is None or adjustment.adjustment_amount <= 0 or not adjustment.direction:
                raise InputError(f"银行调节「{adjustment.number}」须填写正数调节金额及加计/扣减方向")
            if not in_period:
                raise InputError(f"银行调节「{adjustment.number}」独立调节的权责所属期必须落在本核对期")
            adjustment_total += adjustment.adjustment_amount * (Decimal(1) if adjustment.direction == "加计" else Decimal(-1))

    unclassified = sorted(
        transaction.transaction_id for transaction in transactions
        if transaction.income > 0 and transaction.transaction_id not in classified
    )
    if unclassified:
        shown = "、".join(unclassified[:5]) + ("……" if len(unclassified) > 5 else "")
        raise InputError(f"仍有 {len(unclassified)} 笔收入流水未在银行调节底稿分类：{shown}")
    final = recognized_total + adjustment_total
    if final < 0:
        raise InputError("银行调节后不含税收入不能为负数，请复核扣减项")
    bridge_source = "；".join(dict.fromkeys(adjustment_sources))
    category_detail = "、".join(f"{name}{count}笔" for name, count in sorted(categories.items()))
    workpaper_detail = "、".join(dict.fromkeys(workpapers))
    metrics["银行.经营回款已复核不含税额"] = Metric(
        "银行.经营回款已复核不含税额", recognized_total,
        raw_source + "；" + bridge_source + " / 逐笔分类与价税复核",
        f"逐笔确认的本期经营回款不含税额 {recognized_total:,.2f}；分类：{category_detail}；底稿：{workpaper_detail}",
    )
    metrics["银行.权责口径调节额"] = Metric(
        "银行.权责口径调节额", adjustment_total,
        bridge_source + " / 现金、往来及跨期调节",
        f"独立加减调节净额 {adjustment_total:,.2f}；底稿：{workpaper_detail}",
    )
    metrics["银行.调节后不含税收入"] = Metric(
        "银行.调节后不含税收入", final,
        raw_source + "；" + bridge_source + " / 已复核收入调节桥",
        f"已复核经营回款不含税额 {recognized_total:,.2f} + 权责口径调节额 {adjustment_total:,.2f} = {final:,.2f}；"
        f"原始收入流水 {receipts:,.2f} 仅作对照，不直接作为收入；底稿：{workpaper_detail}",
    )
    return metrics


_CONTRACT_KINDS = {"销售": "销售", "销售合同": "销售", "采购": "采购", "采购合同": "采购"}
_FULFILLMENT_KINDS = {"发货", "收货", "服务验收", "其他履约"}


def _contract_date(value: object, location: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text)
    if not match:
        raise InputError(f"{location}：须为完整日期")
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        raise InputError(f"{location}：日期无效「{text}」") from None


def _contract_note(workpaper: object, detail: object, reviewed: bool, location: str) -> tuple[str, str]:
    workpaper_text = str(workpaper or "").strip()
    detail_text = str(detail or "").strip()
    if reviewed and (not workpaper_text or not detail_text):
        raise InputError(f"{location}：已复核记录必须填写底稿编号和说明")
    if len(workpaper_text) > 128 or len(detail_text) > 2000:
        raise InputError(f"{location}：底稿编号或说明过长")
    return workpaper_text, detail_text


def _read_contracts(wb) -> list[_Contract]:
    if config.SHEET_CONTRACTS not in wb.sheetnames:
        return []
    ws = _sheet(wb, config.SHEET_CONTRACTS, config.COL_CONTRACTS)
    contracts, seen = [], set()
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if all(value is None or isinstance(value, str) and not value.strip() for value in row):
            continue
        location = f"{config.SHEET_CONTRACTS}第 {idx} 行"
        number = _bank_identifier(row[0], f"{location}合同编号")
        if number in seen:
            raise InputError(f"{location}：合同编号重复「{number}」")
        seen.add(number)
        kind = _CONTRACT_KINDS.get(str(row[1] or "").strip())
        if not kind:
            raise InputError(f"{location}：合同类型仅支持销售或采购")
        counterparty = str(row[2] or "").strip()
        if not counterparty or len(counterparty) > 200:
            raise InputError(f"{location}：对方名称不能为空且不能超过 200 字")
        signed_on = _contract_date(row[3], f"{location}签订日期")
        amount = _number(row[4], f"{location}合同含税金额")
        if amount is None or amount <= 0:
            raise InputError(f"{location}：合同含税金额必须为正数")
        performance_start = _contract_date(row[5], f"{location}履约起始日")
        performance_end = _contract_date(row[6], f"{location}履约结束日")
        if performance_start > performance_end:
            raise InputError(f"{location}：履约起始日不能晚于履约结束日")
        reviewed = _bank_reviewed(row[7], f"{location}复核状态")
        workpaper, detail = _contract_note(row[8], row[9], reviewed, location)
        contracts.append(_Contract(
            number, kind, counterparty, signed_on, amount, performance_start, performance_end,
            reviewed, workpaper, detail, f"{ws.title}!第{idx}行",
        ))
    return contracts


def _read_fulfillments(wb) -> list[_Fulfillment]:
    if config.SHEET_FULFILLMENTS not in wb.sheetnames:
        return []
    ws = _sheet(wb, config.SHEET_FULFILLMENTS, config.COL_FULFILLMENTS)
    records, seen = [], set()
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if all(value is None or isinstance(value, str) and not value.strip() for value in row):
            continue
        location = f"{config.SHEET_FULFILLMENTS}第 {idx} 行"
        number = _bank_identifier(row[0], f"{location}履约单据号")
        if number in seen:
            raise InputError(f"{location}：履约单据号重复「{number}」")
        seen.add(number)
        contract_number = _bank_identifier(row[1], f"{location}合同编号")
        fulfilled_on = _contract_date(row[2], f"{location}履约日期")
        kind = str(row[3] or "").strip()
        if kind not in _FULFILLMENT_KINDS:
            raise InputError(f"{location}：履约类型仅支持发货、收货、服务验收或其他履约")
        amount = _number(row[4], f"{location}含税金额")
        if amount is None or amount <= 0:
            raise InputError(f"{location}：含税金额必须为正数")
        reviewed = _bank_reviewed(row[5], f"{location}复核状态")
        workpaper, detail = _contract_note(row[6], row[7], reviewed, location)
        records.append(_Fulfillment(
            number, contract_number, fulfilled_on, kind, amount, reviewed, workpaper, detail,
            f"{ws.title}!第{idx}行",
        ))
    return records


def _read_contract_links(wb) -> list[_ContractLink]:
    if config.SHEET_CONTRACT_LINKS not in wb.sheetnames:
        return []
    ws = _sheet(wb, config.SHEET_CONTRACT_LINKS, config.COL_CONTRACT_LINKS)
    links, seen = [], set()
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if all(value is None or isinstance(value, str) and not value.strip() for value in row):
            continue
        location = f"{config.SHEET_CONTRACT_LINKS}第 {idx} 行"
        number = _bank_identifier(row[0], f"{location}勾稽编号")
        if number in seen:
            raise InputError(f"{location}：勾稽编号重复「{number}」")
        seen.add(number)
        contract_number = _bank_identifier(row[1], f"{location}合同编号")
        invoice_number = _invoice_number(row[2], f"{location}发票号码") if str(row[2] or "").strip() else ""
        transaction_id = _bank_identifier(row[3], f"{location}银行流水号") if str(row[3] or "").strip() else ""
        fulfillment_number = _bank_identifier(row[4], f"{location}履约单据号") if str(row[4] or "").strip() else ""
        if not any((invoice_number, transaction_id, fulfillment_number)):
            raise InputError(f"{location}：发票、银行流水和履约单据至少关联一项")
        amount = _number(row[5], f"{location}勾稽含税金额")
        if amount is None or amount <= 0:
            raise InputError(f"{location}：勾稽含税金额必须为正数")
        reviewed = _bank_reviewed(row[6], f"{location}复核状态")
        workpaper, detail = _contract_note(row[7], row[8], reviewed, location)
        links.append(_ContractLink(
            number, contract_number, invoice_number, transaction_id, fulfillment_number,
            amount, reviewed, workpaper, detail, f"{ws.title}!第{idx}行",
        ))
    return links


def _contract_metrics(
    company: Company,
    contracts: list[_Contract],
    fulfillments: list[_Fulfillment],
    links: list[_ContractLink],
    invoices: list[_Invoice],
    transactions: list[_BankTransaction],
) -> dict[str, Metric]:
    if not contracts and not fulfillments and not links:
        return {}
    if (fulfillments or links) and not contracts:
        raise InputError("履约记录或四流勾稽存在，但没有合同台账")

    audit_period = _parse_period(company.period, "企业信息核对所属期")
    contract_map, fulfillment_map, invoice_map, transaction_map = {}, {}, {}, {}
    contract_sources, fulfillment_sources, link_sources, external_sources = [], [], [], []
    for contract in contracts:
        if contract.number in contract_map:
            raise InputError(f"合同编号重复「{contract.number}」；请先去重")
        if contract.kind not in {"销售", "采购"} or contract.amount <= 0:
            raise InputError(f"合同「{contract.number}」类型或金额无效")
        start, end = date.fromisoformat(contract.performance_start), date.fromisoformat(contract.performance_end)
        if start > end:
            raise InputError(f"合同「{contract.number}」履约起始日不能晚于结束日")
        if end < audit_period.start or start > audit_period.end:
            raise InputError(f"合同「{contract.number}」履约期间与核对期间 {company.period} 不相交")
        if contract.reviewed and (not contract.workpaper or not contract.detail):
            raise InputError(f"合同「{contract.number}」已复核但缺少底稿编号或说明")
        contract_map[contract.number] = contract
        contract_sources.append(contract.source)

    for invoice in invoices:
        if invoice.number in invoice_map:
            raise InputError(f"发票号码重复「{invoice.number}」；请先去重")
        invoice_map[invoice.number] = invoice
    for transaction in transactions:
        if transaction.explicit_id:
            if transaction.transaction_id in transaction_map:
                raise InputError(f"银行流水号重复「{transaction.transaction_id}」；请先去重")
            transaction_map[transaction.transaction_id] = transaction

    for record in fulfillments:
        if record.number in fulfillment_map:
            raise InputError(f"履约单据号重复「{record.number}」；请先去重")
        contract = contract_map.get(record.contract_number)
        if contract is None:
            raise InputError(f"履约单据「{record.number}」引用不存在的合同「{record.contract_number}」")
        fulfilled_on = date.fromisoformat(record.fulfilled_on)
        if not audit_period.start <= fulfilled_on <= audit_period.end:
            raise InputError(f"履约单据「{record.number}」日期不在核对期间 {company.period} 内")
        if not date.fromisoformat(contract.performance_start) <= fulfilled_on <= date.fromisoformat(contract.performance_end):
            raise InputError(f"履约单据「{record.number}」日期不在合同履约期间内")
        if contract.kind == "销售" and record.kind == "收货":
            raise InputError(f"销售合同「{contract.number}」不能关联收货记录")
        if contract.kind == "采购" and record.kind == "发货":
            raise InputError(f"采购合同「{contract.number}」不能关联发货记录")
        if record.amount <= 0:
            raise InputError(f"履约单据「{record.number}」金额必须为正数")
        if record.reviewed and (not record.workpaper or not record.detail):
            raise InputError(f"履约单据「{record.number}」已复核但缺少底稿编号或说明")
        fulfillment_map[record.number] = record
        fulfillment_sources.append(record.source)

    contract_alloc, full_alloc = {}, {}
    invoice_alloc, transaction_alloc, fulfillment_alloc = {}, {}, {}
    link_numbers = set()
    for link in links:
        if link.number in link_numbers:
            raise InputError(f"四流勾稽编号重复「{link.number}」")
        link_numbers.add(link.number)
        contract = contract_map.get(link.contract_number)
        if contract is None:
            raise InputError(f"四流勾稽「{link.number}」引用不存在的合同「{link.contract_number}」")
        if link.amount <= 0:
            raise InputError(f"四流勾稽「{link.number}」金额必须为正数")
        if link.reviewed and (not link.workpaper or not link.detail):
            raise InputError(f"四流勾稽「{link.number}」已复核但缺少底稿编号或说明")
        if link.reviewed and not contract.reviewed:
            raise InputError(f"四流勾稽「{link.number}」已复核，但合同「{contract.number}」尚未复核")
        if not any((link.invoice_number, link.transaction_id, link.fulfillment_number)):
            raise InputError(f"四流勾稽「{link.number}」至少关联一项外部流转证据")

        invoice = invoice_map.get(link.invoice_number) if link.invoice_number else None
        if link.invoice_number and invoice is None:
            raise InputError(f"四流勾稽「{link.number}」引用不存在的发票「{link.invoice_number}」")
        if invoice:
            expected = "销项" if contract.kind == "销售" else "采购"
            invoice_kind = invoice.kind or _invoice_kind(
                "", "合同四流勾稽", invoice.seller_id, invoice.buyer_id, company
            )
            if not invoice_kind:
                raise InputError(f"四流勾稽「{link.number}」无法根据购销双方税号判定发票方向")
            if invoice_kind != expected:
                raise InputError(f"四流勾稽「{link.number}」发票方向与{contract.kind}合同不一致")
            if invoice.status != "正常":
                raise InputError(f"四流勾稽「{link.number}」当前仅支持关联正常有效发票")
            invoice_alloc[invoice.number] = invoice_alloc.get(invoice.number, Decimal(0)) + link.amount
            external_sources.append(invoice.source)

        transaction = transaction_map.get(link.transaction_id) if link.transaction_id else None
        if link.transaction_id and transaction is None:
            raise InputError(f"四流勾稽「{link.number}」引用不存在或无唯一编号的银行流水「{link.transaction_id}」")
        if transaction:
            bank_amount = transaction.income if contract.kind == "销售" else transaction.expense
            if bank_amount <= 0:
                raise InputError(f"四流勾稽「{link.number}」银行收支方向与{contract.kind}合同不一致")
            transaction_alloc[transaction.transaction_id] = (
                transaction_alloc.get(transaction.transaction_id, Decimal(0)) + link.amount
            )
            external_sources.append(transaction.source)

        fulfillment = fulfillment_map.get(link.fulfillment_number) if link.fulfillment_number else None
        if link.fulfillment_number and fulfillment is None:
            raise InputError(f"四流勾稽「{link.number}」引用不存在的履约单据「{link.fulfillment_number}」")
        if fulfillment and fulfillment.contract_number != contract.number:
            raise InputError(f"四流勾稽「{link.number}」履约单据不属于合同「{contract.number}」")
        if fulfillment:
            if link.reviewed and not fulfillment.reviewed:
                raise InputError(
                    f"四流勾稽「{link.number}」已复核，但履约单据「{fulfillment.number}」尚未复核"
                )
            fulfillment_alloc[fulfillment.number] = fulfillment_alloc.get(fulfillment.number, Decimal(0)) + link.amount

        if link.reviewed:
            contract_alloc[contract.number] = contract_alloc.get(contract.number, Decimal(0)) + link.amount
            if invoice and transaction and fulfillment:
                full_alloc[contract.number] = full_alloc.get(contract.number, Decimal(0)) + link.amount
        link_sources.append(link.source)

    for number, allocated in contract_alloc.items():
        if allocated > contract_map[number].amount:
            raise InputError(f"合同「{number}」已复核勾稽金额超过合同含税金额")
    for number, allocated in invoice_alloc.items():
        invoice = invoice_map[number]
        if allocated > invoice.amount + invoice.tax:
            raise InputError(f"发票「{number}」被分摊的勾稽金额超过价税合计")
    for number, allocated in transaction_alloc.items():
        transaction = transaction_map[number]
        if allocated > transaction.income + transaction.expense:
            raise InputError(f"银行流水「{number}」被分摊的勾稽金额超过交易金额")
    for number, allocated in fulfillment_alloc.items():
        if allocated > fulfillment_map[number].amount:
            raise InputError(f"履约单据「{number}」被分摊的勾稽金额超过履约金额")

    complete = [
        contract for contract in contracts
        if full_alloc.get(contract.number, Decimal(0)) == contract.amount
        and contract_alloc.get(contract.number, Decimal(0)) == contract.amount
    ]
    pending = len(contracts) - len(complete)
    total = sum((contract.amount for contract in contracts), Decimal(0))
    reviewed_total = sum((contract.amount for contract in contracts if contract.reviewed), Decimal(0))
    complete_total = sum((contract.amount for contract in complete), Decimal(0))
    sources = "；".join(dict.fromkeys(
        contract_sources + external_sources + fulfillment_sources + link_sources
    )) or "合同台账"
    complete_numbers = "、".join(contract.number for contract in complete) or "无"
    detail = (
        f"合同 {len(contracts)} 份，已复核 {sum(contract.reviewed for contract in contracts)} 份；"
        f"四流完整 {len(complete)} 份，待完善 {pending} 份；完整合同：{complete_numbers}。"
        "四流完整仅统计合同、正常发票、方向一致的银行流水、已复核履约单据均已关联，"
        "且已复核分摊金额与合同含税金额完全一致的合同"
    )
    metrics = {
        "合同.合同数量": Metric("合同.合同数量", Decimal(len(contracts)), sources, detail),
        "合同.合同含税金额": Metric("合同.合同含税金额", total, sources, detail),
        "合同.已复核合同金额": Metric("合同.已复核合同金额", reviewed_total, sources, detail),
        "合同.四流完整合同数量": Metric("合同.四流完整合同数量", Decimal(len(complete)), sources, detail),
        "合同.四流完整勾稽金额": Metric("合同.四流完整勾稽金额", complete_total, sources, detail),
        "合同.四流待完善合同数量": Metric("合同.四流待完善合同数量", Decimal(pending), sources, detail),
    }
    for kind in ("销售", "采购"):
        selected = [contract for contract in contracts if contract.kind == kind]
        if not selected:
            continue
        key = f"合同.{kind}合同含税金额"
        value = sum((contract.amount for contract in selected), Decimal(0))
        kind_source = "；".join(dict.fromkeys(contract.source for contract in selected))
        metrics[key] = Metric(
            key, value, kind_source + f" / {kind}合同台账",
            f"{len(selected)} 份{kind}合同含税金额合计 {value:,.2f}",
        )
    return metrics


_HUMAN_KINDS = {
    "个税": "个税", "个税申报": "个税", "工资薪金申报": "个税",
    "社保": "社保", "社保参保": "社保", "社会保险": "社保",
    "公积金": "公积金", "公积金缴存": "公积金", "住房公积金": "公积金",
}
_HUMAN_ACTIVE = {"正常", "有效", "已申报", "正常参保", "在保", "正常缴存", "缴存"}
_HUMAN_INACTIVE = {"作废", "停保", "退保", "封存", "停缴", "销户"}


def _human_month(value: object, location: str) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return f"{value.year:04d}-{value.month:02d}"
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})[-年/.](\d{1,2})月?", text)
    if not match:
        raise InputError(f"{location}：所属月须为 YYYY-MM")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise InputError(f"{location}：所属月无效「{text}」")
    return f"{year:04d}-{month:02d}"


def _human_person_key(value: object, location: str) -> str:
    text = re.sub(r"\s+", "", str(value or "")).upper()
    if not 4 <= len(text) <= 64 or any(ord(char) < 32 for char in text):
        raise InputError(f"{location}：证件号码为空或格式无效")
    return sha256(text.encode("utf-8")).hexdigest()


def _read_human_records(wb) -> list[_HumanRecord]:
    if config.SHEET_HUMAN not in wb.sheetnames:
        return []
    ws = _sheet(wb, config.SHEET_HUMAN, config.COL_HUMAN)
    records, seen = [], set()
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if all(value is None or isinstance(value, str) and not value.strip() for value in row):
            continue
        location = f"{config.SHEET_HUMAN}第 {idx} 行"
        kind = _HUMAN_KINDS.get(str(row[0] or "").strip())
        if not kind:
            raise InputError(f"{location}：记录类型仅支持个税申报、社保参保、公积金缴存")
        if not str(row[1] or "").strip():
            raise InputError(f"{location}：姓名不能为空")
        person_key = _human_person_key(row[2], location)
        month = _human_month(row[3], location)
        status = str(row[4] or "").strip()
        if status in _HUMAN_ACTIVE:
            active = True
        elif status in _HUMAN_INACTIVE:
            active = False
        else:
            raise InputError(f"{location}：无法识别状态「{status}」")
        amount = _number(row[5], f"{location}金额")
        if amount is not None and amount < 0:
            raise InputError(f"{location}：金额不能为负数")
        declared_source = str(row[6] or "").strip()
        if not declared_source:
            raise InputError(f"{location}：来源不能为空")
        key = (kind, person_key, month)
        if key in seen:
            raise InputError(f"{location}：同一人员、记录类型和所属月重复；请先去重")
        seen.add(key)
        records.append(_HumanRecord(
            kind, person_key, month, active, amount,
            f"{config.SHEET_HUMAN}!第{idx}行 ← {declared_source}",
        ))
    return records


def _human_metrics(company: Company, records: list[_HumanRecord]) -> dict[str, Metric]:
    if not records:
        return {}
    audit_period = _parse_period(company.period, "企业信息核对所属期")
    groups: dict[str, dict[str, list[_HumanRecord]]] = {}
    seen = set()
    for record in records:
        if record.kind not in {"个税", "社保", "公积金"}:
            raise InputError(f"人力记录类型无效「{record.kind}」")
        if not re.fullmatch(r"[0-9a-f]{64}", record.person_key):
            raise InputError("人力记录人员标识无效")
        month_period = _parse_period(record.month, "人力记录所属月")
        if not audit_period.start <= month_period.start or not month_period.end <= audit_period.end:
            raise InputError(f"人力记录所属月 {record.month} 不在核对期间 {company.period} 内")
        key = (record.kind, record.person_key, record.month)
        if key in seen:
            raise InputError("同一人员、记录类型和所属月跨文件重复；请先去重")
        seen.add(key)
        groups.setdefault(record.kind, {}).setdefault(record.month, []).append(record)

    tax_months = set(groups.get("个税", {}))
    social_months = set(groups.get("社保", {}))
    comparison_month = ""
    if tax_months and social_months:
        common = tax_months & social_months
        if not common:
            raise InputError("个税与社保记录没有相同所属月，不能进行同月人数比对")
        comparison_month = max(common)

    specs = {
        "个税": ("人力.个税申报人数", "个税.工资薪金申报收入"),
        "社保": ("人力.社保参保人数", "社保.单位缴费金额"),
        "公积金": ("人力.公积金缴存人数", "公积金.单位缴存金额"),
    }
    metrics = {}
    for kind, months in groups.items():
        month = comparison_month if comparison_month and comparison_month in months else max(months)
        selected = months[month]
        active = [record for record in selected if record.active]
        excluded = len(selected) - len(active)
        people_key, amount_key = specs[kind]
        source = "；".join(dict.fromkeys(record.source for record in selected))
        detail = f"核对月 {month}；有效去重人数 {len(active)}；停保/作废等剔除 {excluded} 条；不持久化姓名和证件号码"
        metrics[people_key] = Metric(people_key, Decimal(len(active)), source + " / 月度去重", detail)
        if all(record.amount is not None for record in active):
            total = sum((record.amount for record in active if record.amount is not None), Decimal(0))
            metrics[amount_key] = Metric(
                amount_key, total, source + " / 有效记录金额汇总",
                f"核对月 {month}；{len(active)} 条有效记录金额合计 {total:,.2f}",
            )
    return metrics


def _from_workbook(wb) -> Dataset:
    try:
        # data_only=False 使未重算公式显式报错，而不是把空缓存误认为零。
        company = _read_company(wb)
        accounts = _read_accounts(wb)
        declarations = _read_declarations(wb)
        metrics = _build_metrics(accounts, declarations)
        _read_statement(wb, config.SHEET_INCOME, "利润表", metrics)
        _read_statement(wb, config.SHEET_BALANCE, "资产负债表", metrics)
        _read_statement(wb, config.SHEET_CASHFLOW, "现金流量表", metrics)
        _read_history(wb, company, metrics)
        invoices = _read_invoices(wb, company)
        bank_transactions = _read_bank_transactions(wb)
        bank_adjustments = _read_bank_adjustments(wb)
        invoice_metrics = _invoice_metrics(company, invoices)
        for key, metric in invoice_metrics.items():
            if key in metrics and metrics[key].value != metric.value:
                raise InputError(f"指标「{key}」与发票明细计算值冲突")
            metrics[key] = metric
        bank_metrics = _bank_metrics(company, bank_transactions, bank_adjustments)
        for key, metric in bank_metrics.items():
            if key in metrics and metrics[key].value != metric.value:
                raise InputError(f"指标「{key}」与银行流水/调节底稿计算值冲突")
            metrics[key] = metric
        human_metrics = _human_metrics(company, _read_human_records(wb))
        for key, metric in human_metrics.items():
            if key in metrics and metrics[key].value != metric.value:
                raise InputError(f"指标「{key}」与人力记录计算值冲突")
            metrics[key] = metric
        contract_metrics = _contract_metrics(
            company,
            _read_contracts(wb),
            _read_fulfillments(wb),
            _read_contract_links(wb),
            invoices,
            bank_transactions,
        )
        for key, metric in contract_metrics.items():
            if key in metrics and metrics[key].value != metric.value:
                raise InputError(f"指标「{key}」与合同四流勾稽计算值冲突")
            metrics[key] = metric
        _read_supplement(wb, company, metrics)
        from . import related_graph
        graph = related_graph.read_workbook(wb, company)
        return Dataset(company, accounts, declarations, metrics, graph, sources=list(wb.sheetnames))
    finally:
        wb.close()


def load(path: str | Path) -> Dataset:
    path = Path(path)
    if not path.is_file():
        raise InputError(f"文件不存在：{path}")
    try:
        wb = open_workbook(path.read_bytes())
    except Exception as exc:
        raise InputError(f"无法读取 Excel 工作簿：{exc}") from exc
    return _from_workbook(wb)


def load_bytes(data: bytes) -> Dataset:
    """从内存解析审计材料（Web 上传路径：原始文件不落盘，导入即解析）。"""
    if not data:
        raise InputError("上传文件为空")
    try:
        wb = open_workbook(data)
    except Exception as exc:
        raise InputError(f"无法读取 Excel 工作簿：请确认上传的是 .xlsx 格式的审计材料。原始错误：{exc}") from exc
    return _from_workbook(wb)
