"""确定性 YAML 核对引擎；金额使用 Decimal，不执行 YAML 中的代码。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import yaml
from .models import Dataset, EvidenceItem, Finding, Rule


class RuleError(Exception):
    """规则定义不合法。"""


class MissingMetric(Exception):
    """材料缺失、无效或公式不可计算，不能形成通过结论。"""


class UniqueLoader(yaml.SafeLoader):
    """禁止 YAML 重复键静默覆盖阈值或公式。"""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise RuleError(f"YAML 键必须为字符串且不能重复：{key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
REQUIRED_FIELDS = {
    "id", "name", "category", "tax_type", "severity", "logic", "evidence",
    "legal_basis", "suggestion", "inputs", "scope", "threshold_basis", "references", "version",
}
OPTIONAL_FIELDS = {"description"}
VALID_SEVERITY = {"high", "medium", "low"}
OPS = {"add", "sub", "mul", "div", "min", "max"}


def _decimal(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"必须是数值：{value!r}")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("数值必须有限，不能是 NaN 或 Infinity")
    return number


def expression_metrics(expr, depth=0) -> set[str]:
    """验证白名单表达式并收集依赖；无 eval / Python 表达式。"""
    if depth > 16:
        raise RuleError("公式嵌套超过 16 层")
    if isinstance(expr, str) and expr.strip():
        return {expr}
    if isinstance(expr, dict):
        if len(expr) != 1:
            raise RuleError("公式必须只有一个运算符")
        op, args = next(iter(expr.items()))
        if op not in OPS or not isinstance(args, list) or len(args) < 2:
            raise RuleError(f"非法公式：{expr!r}")
        if op in {"sub", "div"} and len(args) != 2:
            raise RuleError(f"{op} 必须恰好有两个操作数")
        return set().union(*(expression_metrics(a, depth + 1) for a in args))
    try:
        _decimal(expr)
    except (ValueError, InvalidOperation) as exc:
        raise RuleError(str(exc)) from exc
    return set()


def _apply(op, values):
    if op == "add":
        return sum(values, Decimal(0))
    if op == "sub":
        return values[0] - values[1]
    if op == "mul":
        result = Decimal(1)
        for value in values:
            result *= value
        return result
    if op == "div":
        if values[1] <= 0:
            raise MissingMetric("比率分母必须大于零，请复核零值、负值或比较期间口径")
        return values[0] / values[1]
    return min(values) if op == "min" else max(values)


def _constant(expr):
    if isinstance(expr, dict):
        op, args = next(iter(expr.items()))
        try:
            return _apply(op, [_constant(a) for a in args])
        except MissingMetric as exc:
            raise RuleError(str(exc)) from exc
    return _decimal(expr)


def _validate_logic(logic, depth=0) -> set[str]:
    if depth > 8 or not isinstance(logic, dict):
        raise RuleError("logic 必须为映射且嵌套不超过 8 层")
    kind = logic.get("type")
    if kind == "all":
        if set(logic) != {"type", "conditions"}:
            raise RuleError("all 仅支持 type 和 conditions")
        children = logic["conditions"]
        if not isinstance(children, list) or len(children) < 2:
            raise RuleError("all.conditions 至少需要两项")
        return set().union(*(_validate_logic(c, depth + 1) for c in children))
    fields = {
        "deviation": {"left", "right", "threshold"},
        "amount_mismatch": {"left", "right", "tolerance"},
        "ratio_range": {"numerator", "denominator", "min", "max"},
    }.get(kind)
    if fields is None:
        raise RuleError(f"不支持的 logic.type：{kind!r}")
    optional = {"direction"} if kind != "ratio_range" else set()
    if not fields <= set(logic) or set(logic) - fields - {"type"} - optional:
        raise RuleError(f"{kind} 的字段缺失或包含未知字段，要求 {sorted(fields)}")
    if logic.get("direction", "absolute") not in {"absolute", "above", "below"}:
        raise RuleError("direction 仅支持 absolute / above / below")
    metrics = set().union(*(expression_metrics(logic[f]) for f in fields))
    if kind == "ratio_range":
        lo, hi = logic["min"], logic["max"]
        if not expression_metrics(lo) and not expression_metrics(hi):
            if _constant(lo) > _constant(hi):
                raise RuleError("ratio_range.min 不能大于 max")
    else:
        key = "threshold" if kind == "deviation" else "tolerance"
        if expression_metrics(logic[key]) or _constant(logic[key]) < 0:
            raise RuleError(f"{key} 必须是非负常量")
    return metrics


def load_rules(rules_dir: str | Path) -> list[Rule]:
    rules_dir = Path(rules_dir)
    if not rules_dir.is_dir():
        raise RuleError(f"规则目录不存在：{rules_dir}")
    rules, seen = [], set()
    for path in sorted(rules_dir.iterdir()):
        if path.suffix not in {".yaml", ".yml"} or not path.is_file():
            continue
        try:
            doc = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
            if not isinstance(doc, dict):
                raise RuleError("顶层必须是映射")
            missing = REQUIRED_FIELDS - set(doc)
            extra = set(doc) - REQUIRED_FIELDS - OPTIONAL_FIELDS
            if missing or extra:
                raise RuleError(f"缺少字段 {sorted(missing)}；未知字段 {sorted(extra)}")
            for key in ("id", "name", "category", "tax_type", "severity", "suggestion",
                        "scope", "threshold_basis", "version"):
                if not isinstance(doc[key], str) or not doc[key].strip():
                    raise RuleError(f"{key} 必须是非空字符串")
            if not re.fullmatch(r"R-\d{3}", doc["id"]) or doc["id"] in seen:
                raise RuleError(f"规则 ID 格式错误或重复：{doc['id']}")
            if doc["severity"] not in VALID_SEVERITY:
                raise RuleError(f"severity 必须是 {sorted(VALID_SEVERITY)}")
            if doc["category"] not in {"内部勾稽", "外部交叉", "指标偏离"}:
                raise RuleError("category 必须是内部勾稽 / 外部交叉 / 指标偏离")
            for key in ("evidence", "legal_basis", "references"):
                if (not isinstance(doc[key], list) or not doc[key]
                        or any(not isinstance(v, str) or not v.strip() for v in doc[key])):
                    raise RuleError(f"{key} 必须是非空字符串列表")
            if len(doc["evidence"]) != len(set(doc["evidence"])):
                raise RuleError("evidence 不得重复")
            inputs = doc["inputs"]
            if (not isinstance(inputs, dict) or not inputs
                    or any(not isinstance(v, str) or not v.strip() for v in inputs.values())):
                raise RuleError("inputs 必须映射每个指标到取数口径和补充来源")
            deps = _validate_logic(doc["logic"])
            if deps != set(inputs) or deps != set(doc["evidence"]):
                raise RuleError("公式依赖、inputs、evidence 必须一致，含动态阈值的全部原始指标")
            if "description" in doc and not isinstance(doc["description"], str):
                raise RuleError("description 必须为字符串")
            seen.add(doc["id"])
            rules.append(Rule(**doc, source_file=path.name))
        except (yaml.YAMLError, RuleError, ValueError, TypeError) as exc:
            raise RuleError(f"{path.name}：{exc}") from exc
    if not rules:
        raise RuleError(f"{rules_dir} 下没有找到任何规则文件")
    return rules


def validate_rule_update(rule: Rule) -> Rule:
    """Validate a runtime rule update against the immutable rule contract.

    Runtime editing may change the declarative logic, version and threshold
    explanation, but it may not silently add/remove source metrics.  This keeps
    material mapping and evidence requirements aligned with the reviewed YAML
    rule while still allowing no-code parameter tuning.
    """
    if not isinstance(rule.version, str) or not rule.version.strip():
        raise RuleError("version 必须是非空字符串")
    if not isinstance(rule.threshold_basis, str) or not rule.threshold_basis.strip():
        raise RuleError("threshold_basis 必须是非空字符串")
    deps = _validate_logic(rule.logic)
    expected = set(rule.inputs)
    if deps != expected or deps != set(rule.evidence):
        raise RuleError("公式依赖、inputs、evidence 必须一致，不能新增未知指标或移除必需指标")
    return rule


def _need(dataset, key, rule):
    value = dataset.get(key)
    source = rule.inputs.get(key, "对应科目或申报表项目")
    if value is None:
        raise MissingMetric(f"缺少：{key}；补充来源/口径：{source}")
    if not dataset.source_of(key).strip():
        raise MissingMetric(f"指标「{key}」缺少证据来源；请补充：{source}")
    try:
        return _decimal(value)
    except (ValueError, InvalidOperation) as exc:
        raise MissingMetric(f"指标「{key}」无效：{exc}；请复核：{source}") from exc


def _expr(expr, dataset, rule):
    if isinstance(expr, str):
        value = _need(dataset, expr, rule)
        return value, f"{expr}({value:,.6f})"
    if isinstance(expr, dict):
        op, args = next(iter(expr.items()))
        resolved = [_expr(a, dataset, rule) for a in args]
        value = _apply(op, [r[0] for r in resolved])
        symbol = {"add": " + ", "sub": " − ", "mul": " × ", "div": " ÷ "}.get(op)
        label = "(" + symbol.join(r[1] for r in resolved) + ")" if symbol else op + "(" + ", ".join(r[1] for r in resolved) + ")"
        return value, label
    return _decimal(expr), str(expr)


def _logic(logic, dataset, rule):
    kind = logic["type"]
    if kind == "all":
        # 不短路：缺失任一必需证据均未执行，不能掩盖材料缺失。
        results = [_logic(c, dataset, rule) for c in logic["conditions"]]
        hit = all(r[0] for r in results)
        calc = "；且 ".join(f"[{r[2]} → {'命中' if r[0] else '未命中'}]" for r in results)
        return hit, None, calc, "所有子条件同时命中"
    if kind == "ratio_range":
        num, nl = _expr(logic["numerator"], dataset, rule)
        den, dl = _expr(logic["denominator"], dataset, rule)
        lo, ll = _expr(logic["min"], dataset, rule)
        hi, hl = _expr(logic["max"], dataset, rule)
        if lo > hi:
            raise MissingMetric("参考区间下限大于上限，请复核参考值来源")
        value = _apply("div", [num, den])
        desc = f"比率不在闭区间 [{lo:.6f}, {hi:.6f}]"
        return not lo <= value <= hi, value, f"{nl} ÷ {dl} = {value:.6f}；参考 [{ll}, {hl}]", desc
    left, ll = _expr(logic["left"], dataset, rule)
    right, rl = _expr(logic["right"], dataset, rule)
    direction = logic.get("direction", "absolute")
    delta = left - right
    gap = abs(delta) if direction == "absolute" else (delta if direction == "above" else -delta)
    gl = f"|{ll} − {rl}|" if direction == "absolute" else (f"{ll} − {rl}" if direction == "above" else f"{rl} − {ll}")
    limit = _constant(logic["threshold" if kind == "deviation" else "tolerance"])
    if kind == "deviation":
        if right <= 0:
            raise MissingMetric("相对偏离度基数必须大于零；零值/负值须人工核对或使用绝对差额规则")
        value = gap / right
        calc = f"({gl}) ÷ {rl} = {value:.6f} ({value * 100:.4f}%)"
    else:
        value, calc = gap, f"{gl} = {gap:,.6f}"
    desc = f"{'相对偏离度' if kind == 'deviation' else '差额'} > {limit}（{direction}）"
    return value > limit, value, f"{calc}；阈值 > {limit}", desc


def readiness(rule: Rule, dataset: Dataset) -> list[str]:
    """Check input availability/calculability only, without determining risk.

    Unlike evaluate/_logic this never compares measured results with risk
    thresholds and never constructs a Finding. Shared expression evaluation
    keeps zero denominators and invalid references consistent with execution.
    """
    problems = []
    for key in rule.inputs:
        try:
            _need(dataset, key, rule)
        except MissingMetric as exc:
            problems.append(str(exc))
    if problems:
        return problems

    def check(logic):
        kind = logic['type']
        if kind == 'all':
            for child in logic['conditions']:
                check(child)
            return
        if kind == 'ratio_range':
            _expr({'div': [logic['numerator'], logic['denominator']]}, dataset, rule)
            lo = _expr(logic['min'], dataset, rule)[0]
            hi = _expr(logic['max'], dataset, rule)[0]
            if lo > hi:
                raise MissingMetric('参考区间下限大于上限，请复核参考值来源')
        else:
            _expr(logic['left'], dataset, rule)
            right = _expr(logic['right'], dataset, rule)[0]
            if kind == 'deviation' and right <= 0:
                raise MissingMetric('相对偏离度基数必须大于零；零值/负值须人工核对或使用绝对差额规则')

    try:
        check(rule.logic)
    except MissingMetric as exc:
        problems.append(str(exc))
    return problems


def evaluate(rule: Rule, dataset: Dataset) -> Finding:
    try:
        evidence = []
        for key in rule.evidence:
            value = _need(dataset, key, rule)
            detail = dataset.detail_of(key)
            source = dataset.source_of(key) + (f"；{detail}" if detail else "")
            evidence.append(EvidenceItem(key, f"{value:,.6f}".rstrip("0").rstrip("."), source, True))
        hit, measured, calc, threshold = _logic(rule.logic, dataset, rule)
        evidence.append(EvidenceItem("阈值依据", rule.threshold_basis, "规则配置；不是法定违法认定标准"))
        return Finding(
            rule=rule, status="hit" if hit else "pass",
            measured=float(measured) if measured is not None else None,
            threshold_desc=threshold,
            conclusion="触发核对预警，需按适用范围人工复核。" if hit else "本规则已执行，未触发设定的核对条件。",
            evidence=evidence, calculation=calc,
        )
    except MissingMetric as exc:
        return Finding(rule=rule, status="skipped", measured=None, threshold_desc="—",
                       conclusion=f"本项未执行：{exc}", skip_reason=str(exc))


def run(rules: list[Rule], dataset: Dataset) -> list[Finding]:
    findings = [evaluate(rule, dataset) for rule in rules]
    findings.sort(key=lambda f: ({"hit": 0, "pass": 1, "skipped": 2}[f.status], -f.severity_rank, f.rule.id))
    return findings
