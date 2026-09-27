"""Calendar interval normalization and comparable-period metric derivation."""
from __future__ import annotations
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
from . import config
from .input_errors import InputError
from .models import Company, Metric


@dataclass(frozen=True)
class _Period:
    """A normalized, complete calendar interval used for deterministic comparisons."""

    start: date
    end: date
    group: str
    months: int
    label: str

    @property
    def key(self) -> tuple[int, int]:
        return (self.start.year * 12 + self.start.month - 1, self.months)


@dataclass(frozen=True)
class _PeriodValue:
    period: _Period
    metric: Metric
    row: int


def _period_from_month(year: int, month: int, months: int, group: str, label: str) -> _Period:
    if not 1 <= month <= 12:
        raise ValueError
    end_index = year * 12 + month - 1 + months - 1
    end_year, end_month_zero = divmod(end_index, 12)
    end_month = end_month_zero + 1
    return _Period(
        date(year, month, 1),
        date(end_year, end_month, monthrange(end_year, end_month)[1]),
        group,
        months,
        label,
    )


def parse_period(value: object, location: str) -> _Period:
    """Accept only full calendar month/quarter/half/year periods."""

    text = "" if value is None else str(value).strip()
    if not text:
        raise InputError(f"{location}：所属期不能为空")

    range_match = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})\s*(?:至|~|—)\s*(\d{4})-(\d{2})-(\d{2})",
        text,
    )
    if range_match:
        try:
            ys, ms, ds, ye, me, de = map(int, range_match.groups())
            start, end = date(ys, ms, ds), date(ye, me, de)
        except ValueError:
            raise InputError(f"{location}：所属期日期无效「{text}」") from None
        months = (ye - ys) * 12 + me - ms + 1
        group = {1: "month", 3: "quarter", 6: "half", 12: "year"}.get(months)
        aligned = (
            ds == 1
            and de == monthrange(ye, me)[1]
            and ((months == 1) or (months == 3 and ms in {1, 4, 7, 10})
                 or (months == 6 and ms in {1, 7}) or (months == 12 and ms == 1))
        )
        if start > end or not group or not aligned:
            raise InputError(f"{location}：仅支持完整自然月、季度、半年或年度「{text}」")
        return _Period(start, end, group, months, text)

    match = re.fullmatch(r"(\d{4})[-年/.](\d{1,2})月?", text)
    if match:
        try:
            return _period_from_month(int(match.group(1)), int(match.group(2)), 1, "month", text)
        except ValueError:
            raise InputError(f"{location}：月份无效「{text}」") from None
    match = re.fullmatch(r"(\d{4})[- ]?Q([1-4])", text, re.IGNORECASE)
    if match:
        return _period_from_month(int(match.group(1)), (int(match.group(2)) - 1) * 3 + 1, 3, "quarter", text)
    match = re.fullmatch(r"(\d{4})年第([1-4])季度", text)
    if match:
        return _period_from_month(int(match.group(1)), (int(match.group(2)) - 1) * 3 + 1, 3, "quarter", text)
    match = re.fullmatch(r"(\d{4})[- ]?H([12])", text, re.IGNORECASE)
    if match:
        return _period_from_month(int(match.group(1)), (int(match.group(2)) - 1) * 6 + 1, 6, "half", text)
    match = re.fullmatch(r"(\d{4})年?(上|下)半年", text)
    if match:
        return _period_from_month(int(match.group(1)), 1 if match.group(2) == "上" else 7, 6, "half", text)
    match = re.fullmatch(r"(\d{4})年?", text)
    if match:
        return _period_from_month(int(match.group(1)), 1, 12, "year", text)
    raise InputError(f"{location}：无法识别所属期「{text}」；请使用完整月、季度、半年或年度")


def _shift_period(period: _Period, months: int) -> tuple[int, int]:
    return (period.key[0] + months, period.months)


def _put_derived(metrics: dict[str, Metric], key: str, metric: Metric) -> None:
    current = metrics.get(key)
    if current is not None:
        if current.value != metric.value:
            raise InputError(f"历史指标自动归集结果与已提供指标「{key}」冲突")
        return
    metrics[key] = metric


def _comparison_metric(name: str, value: _PeriodValue) -> Metric:
    return Metric(name, value.metric.value, value.metric.source, value.metric.detail)


def derive_period_metrics(company: Company, metrics: dict[str, Metric], series: dict[str, dict[tuple[int, int], _PeriodValue]]) -> None:
    if not company.period or not series:
        return
    current_period = parse_period(company.period, "企业信息所属期")
    end_month_key = current_period.end.year * 12 + current_period.end.month - 1

    for base_key, values in series.items():
        spec = config.PERIOD_SERIES[base_key]
        label = spec["label"]
        trend = f"趋势.{label}"
        supplied_current = values.get(current_period.key)
        base_metric = metrics.get(base_key)
        if supplied_current and base_metric and supplied_current.metric.value != base_metric.value:
            raise InputError(f"历史指标中「{base_key}」的本期值与本期报表不一致")
        if supplied_current and base_metric:
            combined = Metric(
                base_key,
                base_metric.value,
                base_metric.source + "；" + supplied_current.metric.source,
                base_metric.detail + "；期间序列核对一致：" + supplied_current.metric.detail,
            )
            current = _PeriodValue(current_period, combined, supplied_current.row)
        elif supplied_current:
            metrics[base_key] = supplied_current.metric
            current = supplied_current
        elif base_metric:
            current = _PeriodValue(current_period, base_metric, 0)
        else:
            current = None

        if current:
            _put_derived(metrics, f"{trend}.本期", _comparison_metric(f"{trend}.本期", current))
            previous = values.get(_shift_period(current_period, -current_period.months))
            prior_year = values.get(_shift_period(current_period, -12))
            for suffix, comparison in (("上期", previous), ("上年同期", prior_year)):
                if comparison:
                    _put_derived(metrics, f"{trend}.{suffix}", _comparison_metric(f"{trend}.{suffix}", comparison))
            if previous and previous.metric.value != 0:
                value = current.metric.value / previous.metric.value - Decimal(1)
                _put_derived(metrics, f"{trend}.环比率", Metric(
                    f"{trend}.环比率", value,
                    current.metric.source + "；" + previous.metric.source,
                    f"({current.metric.value} ÷ {previous.metric.value}) - 1 = {value}",
                ))
            if prior_year:
                alias = spec.get("yoy_alias")
                if alias:
                    _put_derived(metrics, alias, _comparison_metric(alias, prior_year))
                if prior_year.metric.value != 0:
                    value = current.metric.value / prior_year.metric.value - Decimal(1)
                    _put_derived(metrics, f"{trend}.同比率", Metric(
                        f"{trend}.同比率", value,
                        current.metric.source + "；" + prior_year.metric.source,
                        f"({current.metric.value} ÷ {prior_year.metric.value}) - 1 = {value}",
                    ))

        if spec["kind"] == "flow":
            monthly = {key[0]: value for key, value in values.items() if key[1] == 1}
            if current and current_period.months == 1:
                monthly[current_period.key[0]] = current
            required = [end_month_key - offset for offset in range(11, -1, -1)]
            if all(index in monthly for index in required):
                selected = [monthly[index] for index in required]
                total = sum((item.metric.value for item in selected), Decimal(0))
                start_label, end_label = selected[0].period.label, selected[-1].period.label
                _put_derived(metrics, f"{trend}.滚动12月", Metric(
                    f"{trend}.滚动12月", total,
                    "；".join(dict.fromkeys(item.metric.source for item in selected)),
                    " + ".join(str(item.metric.value) for item in selected) + f" = {total}（{start_label} 至 {end_label}）",
                ))

        annual_aliases = spec.get("annual_aliases")
        if annual_aliases and current_period.group == "year":
            annual_values = dict(values)
            if current:
                annual_values[current_period.key] = current
            for offset, alias in enumerate(annual_aliases):
                item = annual_values.get(_shift_period(current_period, -12 * offset))
                if item:
                    _put_derived(metrics, alias, _comparison_metric(alias, item))
