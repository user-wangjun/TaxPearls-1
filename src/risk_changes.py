"""FR-C06: compare frozen findings, never infer resolution from missing checks."""
from __future__ import annotations
from src import periods

from dataclasses import asdict
from decimal import Decimal
import math

from src import engine, loader

CHANGE_LABELS = {
    "new": "新增风险", "resolved": "风险消失", "worsened": "偏离恶化",
    "improved": "偏离改善", "persistent": "持续命中", "clear": "持续通过",
    "incomparable": "无法比较", "rule_changed": "规则口径变化",
}


def same_subject(before: dict, after: dict) -> bool:
    # A name is not an identity. Do not mix unassigned and registered clients.
    return (bool(after["taxpayer_id"]) and before["org_id"] == after["org_id"]
            and before["client_id"] == after["client_id"]
            and before["taxpayer_id"] == after["taxpayer_id"])


def comparable_periods(before: dict, after: dict) -> bool:
    try:
        old = periods.parse_period(before["period"], "基期")
        new = periods.parse_period(after["period"], "本期")
    except (loader.InputError, ValueError):
        return False
    return old.group == new.group and old.end < new.start


def baseline_candidates(history: list[dict], current: dict, latest_only: bool = True) -> list[dict]:
    """History must be newest saved revision first; aliases count as one period."""
    chosen = {}
    eligible = []
    for row in history:
        if not same_subject(row, current) or not comparable_periods(row, current):
            continue
        period = periods.parse_period(row["period"], "基期")
        eligible.append(row)
        chosen.setdefault(period.key, row)
    if not latest_only:
        return sorted(eligible, key=lambda row: periods.parse_period(row["period"], "基期").key, reverse=True)
    return [chosen[key] for key in sorted(chosen, reverse=True)]


def _definition(rule) -> dict:
    definition = asdict(rule)
    # Dates and local paths change packaging, not the meaning of a finding.
    for key in ("effective_from", "effective_to", "source_file"):
        definition.pop(key, None)
    return definition


def _range(finding, dataset):
    if finding.rule.logic.get("type") != "ratio_range":
        return None
    logic = finding.rule.logic
    return tuple(engine._expr(logic[key], dataset, finding.rule)[0] for key in ("min", "max"))


def _distance(finding, bounds):
    value = finding.measured
    if value is None or not math.isfinite(value):
        return None
    value = Decimal(str(value))
    kind = finding.rule.logic.get("type")
    if kind in {"deviation", "amount_mismatch"}:
        return value
    if kind == "ratio_range" and bounds is not None:
        lo, hi = bounds
        return max(lo - value, value - hi, Decimal(0))
    # Compound and graph predicates have no single ordered risk magnitude.
    return None


def _brief(entry):
    return {key: entry[key] for key in ("id", "period", "audited_at")}


def compare(before: dict, after: dict) -> dict:
    if not same_subject(before, after):
        raise ValueError("仅可比较同机构、同客户档案且纳税人识别号一致的审计。")
    if not comparable_periods(before, after):
        raise ValueError("基期须早于本期、互不重叠且期间粒度相同；同期间重审不属于跨期比较。")
    old_period = periods.parse_period(before["period"], "基期")
    new_period = periods.parse_period(after["period"], "本期")
    old = {f.rule.id: f for f in before["findings"]}
    new = {f.rule.id: f for f in after["findings"]}
    items = []
    counts = {key: 0 for key in CHANGE_LABELS}
    for rule_id in sorted(old.keys() | new.keys()):
        left, right = old.get(rule_id), new.get(rule_id)
        item = {"rule_id": rule_id, "name": (right or left).rule.name,
                "before_status": left.status if left else "absent",
                "after_status": right.status if right else "absent",
                "before_version": left.rule.version if left else None,
                "after_version": right.rule.version if right else None,
                "before_measured": left.measured if left and left.measured is not None and math.isfinite(left.measured) else None,
                "after_measured": right.measured if right and right.measured is not None and math.isfinite(right.measured) else None}
        old_distance = new_distance = None
        if not left or not right:
            change, reason = "incomparable", "某期无该规则的执行快照（可能停用或新增），不推断风险新增/消失。"
        elif "skipped" in {left.status, right.status}:
            change, reason = "incomparable", "某期未执行，不能当作通过或风险消失。"
        elif _definition(left.rule) != _definition(right.rule):
            change, reason = "rule_changed", "规则版本或定义变化，不能直接归因于业务改善/恶化。"
        else:
            try:
                old_range = _range(left, before["dataset"])
                new_range = _range(right, after["dataset"])
            except (engine.MissingMetric, engine.RuleError):
                old_range, new_range = None, ()
            if old_range != new_range:
                change, reason = "rule_changed", "参考区间值变化，口径不一致，不能直接比较。"
            elif left.status == "pass" and right.status == "hit":
                change, reason = "new", "同口径规则由已执行通过转为命中。"
            elif left.status == "hit" and right.status == "pass":
                change, reason = "resolved", "同口径规则由命中转为已执行通过；仍须人工核查。"
            elif left.status == right.status == "pass":
                change, reason = "clear", "两期均已执行通过。"
            else:
                old_distance, new_distance = _distance(left, old_range), _distance(right, new_range)
                change, reason = "persistent", "两期持续命中；没有可排序的单一偏离值或偏离值未变。"
                if old_distance is not None and new_distance is not None and not math.isclose(float(old_distance), float(new_distance), rel_tol=1e-9, abs_tol=1e-12):
                    change = "worsened" if new_distance > old_distance else "improved"
                    reason = "同规则偏离值" + ("增大" if change == "worsened" else "减小") + "，不代表法律风险等级或税款变化。"
        counts[change] += 1
        items.append({**item, "change": change, "label": CHANGE_LABELS[change], "reason": reason,
                      "before_distance": str(old_distance) if old_distance is not None else None,
                      "after_distance": str(new_distance) if new_distance is not None else None})
    priority = {key: index for index, key in enumerate(CHANGE_LABELS)}
    items.sort(key=lambda item: (priority[item["change"]], item["rule_id"]))
    return {"status": "ready", "baseline": _brief(before), "current": _brief(after),
            "gap": new_period.key[0] - old_period.key[0] != new_period.months,
            "counts": counts, "items": items}
