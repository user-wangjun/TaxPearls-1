"""Immediate teaching feedback based on actions and source facts, never Findings.

These checks coach evidence, units and periods; they do not determine whether a
rule is a correct answer, re-run the audit, change material or award points.
"""
from src import periods
from decimal import Decimal, InvalidOperation, localcontext
import re

from . import loader


def _unit(key):
    if key.endswith("人数"):
        return "人"
    if key.startswith("参考.") or key.endswith("税率"):
        return "比例小数"
    return "元"


def _period(dataset, key):
    if not key.startswith(("历史.", "年度.", "趋势.")):
        return periods.parse_period(dataset.company.period, "案例期间")
    metric = dataset.metrics.get(key)
    if not metric:
        return None
    # Use explicit provenance, not a period guessed from the metric name.
    match = re.search(r"实际(?:历史|年度)?期间[:：]?\s*(\d{4}-\d{2}-\d{2}\s*至\s*\d{4}-\d{2}-\d{2})", metric.detail)
    if match:
        text = match.group(1)
    else:
        match = re.search(r"实际期间\s+([^；]+)", metric.source)
        if not match:
            return None
        text = match.group(1).strip()
    try:
        return periods.parse_period(text, "来源期间")
    except loader.InputError:
        return None


def feedback(dataset, rules, *, action, rule_id=None, evidence_metrics=(),
             left=None, right=None, operation="difference", period_mode="same_period"):
    if action not in {"mark_risk", "calculate"} or operation not in {"difference", "ratio"}:
        raise ValueError("不支持的沙箱操作。")
    if period_mode not in {"same_period", "year_on_year"}:
        raise ValueError("不支持的期间比较方式。")
    messages = []

    def message(code, text, level="warning"):
        messages.append({"code":code, "level":level, "text":text})

    result = {"action":action, "messages":messages, "guide":None, "calculation":None,
              "notice":"提示仅检查操作和证据口径，不判定答案正确性，不保存答案或改变评分。"}
    if action == "mark_risk":
        rule = next((r for r in rules if r.id == rule_id), None)
        if rule is None:
            raise ValueError("所选规则不属于当前作业。")
        if set(evidence_metrics) - set(rule.inputs):
            raise ValueError("定位的证据不属于所选规则输入，不能借用其他数据作为依据。")
        methods = {"amount_mismatch":"核对同口径金额差额、方向和容差。",
                   "deviation":"先核对基准与分母，再按规则定义比较偏离。",
                   "ratio_range":"计算比例时保留小数口径，不把百分数的显示值当作原始比例。",
                   "all":"复合规则需要逐项核对全部条件，不能只凭其中一项作结论。"}
        result["guide"] = {"rule_id":rule.id, "name":rule.name,
                           "method":methods.get(rule.logic.get("type"), "核对原始关系、交易和完整证据路径；数值试算不能替代路径证据。"),
                           "inputs":[]}
        if not rule.inputs:
            message("path_evidence", "此规则需要原始证据路径，请复核材料；仅勾选选项不是完整证据。", "info")
        for key, description in sorted(rule.inputs.items()):
            metric = dataset.metrics.get(key)
            result["guide"]["inputs"].append({"name":key, "description":description,
                "source":metric.source if metric else "", "detail":metric.detail if metric else "",
                "present":bool(metric and metric.value is not None), "unit":_unit(key)})
            if not metric or metric.value is None:
                message("missing_material", f"缺少「{key}」原始指标，不能把缺项当作零或已查明风险。")
            elif key not in evidence_metrics:
                message("unlocated_evidence", f"尚未定位「{key}」：请查看来源与口径，并勾选已核对的证据。")
        if rule.inputs and not messages:
            message("evidence_located", "已定位所需输入。仍需自行核对期间、计算与规则条件；这不表示答案正确。", "info")
        return result

    if not left or not right:
        message("missing_operand", "请选择两项原始指标，不能把未选择或缺失的材料当作零。")
        return result
    if left not in dataset.metrics or right not in dataset.metrics:
        raise ValueError("试算只能使用当前作业的原始指标。")
    a, b = dataset.metrics[left], dataset.metrics[right]
    try:
        av, bv = Decimal(str(a.value)), Decimal(str(b.value))
        if not av.is_finite() or not bv.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        message("invalid_value", "指标缺失或不是有限数值，不能试算，也不能据此认为无风险。")
        return result
    if _unit(left) != _unit(right):
        message("unit_mismatch", f"单位不一致：左侧是{_unit(left)}，右侧是{_unit(right)}。请先确认合法换算口径，不能直接相减或作为同量纲比例。")
        return result
    lp, rp = _period(dataset, left), _period(dataset, right)
    if not lp or not rp:
        message("unknown_period", "历史来源实际期间未明确，无法确认可比性；请先核查口径说明。")
    elif period_mode == "same_period" and lp.key != rp.key:
        message("period_mismatch", "选用了不同期间，却按同期间比较。同比请明确选择同比方式，并核对相同粒度及范围。")
    elif period_mode == "year_on_year" and not (
            lp.months == rp.months and lp.start.year == rp.start.year + 1
            and lp.start.month == rp.start.month and lp.end.month == rp.end.month):
        message("invalid_year_on_year", "同比要求左侧为本期、右侧为上年同期且期间粒度一致；不能把隔年、月度和年度混在一起。")
    if messages:
        return result  # Do not present an unqualified number for incomparable inputs.
    if operation == "ratio" and bv == 0:
        message("zero_denominator", "分母为零，比例无定义。不能当作 0% 或判为正常，应复核材料与适用口径。")
        return result
    with localcontext() as context:
        context.prec = 40
        number = av - bv if operation == "difference" else av / bv
        result["calculation"] = {"left":left, "right":right, "left_value":str(av), "right_value":str(bv),
                                 "operation":operation, "value":str(number),
                                 "unit":_unit(left) if operation == "difference" else "比例小数"}
        if operation == "ratio":
            result["calculation"]["percent"] = str(number * 100)
            message("ratio_unit", "比例小数乘 100 才是百分数显示值；比较阈值时必须使用相同单位，试算结果不是风险结论。", "info")
        else:
            message("difference_direction", "保留差额正负方向；是否取绝对值、容差大小和是否构成风险，须自行核对规则。", "info")
    return result
