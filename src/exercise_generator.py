"""Rule-driven synthetic exercises. Answers always come from the audit engine.

The neutral teaching ledger below is data, not a table of rule answers. Only
declared inputs and white-listed expressions may be changed by the inverse
solver. Generation is bounded and refuses unsatisfied or invalid conditions.
"""
from __future__ import annotations
from src import periods

from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, localcontext
from hashlib import sha256
import json
from random import Random

from . import config, engine, loader
from .models import Account, Company, Dataset, Finding, Metric, Rule

GENERATOR_VERSION = "1.0"
LEVELS = {"near": Decimal("0.2"), "normal": Decimal("1"), "obvious": Decimal("3")}
# One consistent neutral ledger; no saved samples, uploaded data or providers.
BASE = {
    "营业收入": 1000000, "营业成本": 700000,
    "账面.销项税额": 130000, "账面.进项税额": 100000,
    "增值税.销售额": 1000000, "增值税.销项税额": 130000,
    "增值税.进项税额": 100000, "增值税.应纳税额": 30000,
    "增值税.一般计税应纳税额": 30000, "增值税.期末留抵税额": 0,
    "增值税.期初留抵税额": 0, "账面.抵扣调整": 0, "账面.进项转出": 0,
    "参考.税负率下限": .02, "参考.税负率上限": .05,
    "人力.个税申报人数": 50, "人力.社保参保人数": 50,
    "利润表.营业收入": 1000000, "利润表.营业成本": 700000,
    "利润表.销售费用": 100000, "利润表.管理费用": 50000, "利润表.财务费用": 10000,
    "利润表.利润总额": 140000, "利润表.所得税费用": 35000, "利润表.净利润": 105000,
    "资产负债表.资产总额": 2000000, "资产负债表.负债总额": 1000000,
    "资产负债表.所有者权益": 1000000,
    "权益.期末未分配利润": 405000, "权益.期初未分配利润": 350000,
    "权益.本期利润分配": 30000, "权益.提取盈余公积": 20000, "权益.其他调整": 0,
    "现金.期末现金及等价物": 400000, "现金.期初现金及等价物": 300000,
    "现金流量表.经营净额": 150000, "现金流量表.投资净额": -100000,
    "现金流量表.筹资净额": 50000, "现金流量表.汇率影响": 0,
    "存货.期末余额": 700000, "存货.期初余额": 500000,
    "存货.本期入库成本": 900000, "存货.本期出库成本": 700000, "存货.其他调整": 0,
    "存货.采购可抵扣进项": 117000, "存货.可抵扣采购不含税金额": 900000,
    "存货.采购加权税率": .13, "银行.调节后不含税收入": 1000000,
    "发票.销项净额": 1000000, "销售.未开票申报额": 0, "销售.其他调节": 0,
    "凭证.本期确认抵扣税额": 100000, "凭证.其他抵扣调整": 0,
    "薪酬.本期应申报工资薪金": 300000, "个税.工资薪金申报收入": 300000,
    "发票.采购不含税净额": 900000,
    "历史.上年同期收入": 1000000, "历史.上年同期成本": 700000,
    "参考.毛利率下限": .2, "参考.毛利率上限": .4,
    "年度.本年净利润": 105000, "年度.上年净利润": 105000, "年度.前年净利润": 105000,
    "年度.本年末资产": 2000000, "年度.上年末资产": 2000000, "年度.前年末资产": 2000000,
}


@dataclass
class Exercise:
    dataset: Dataset
    findings: list[Finding]
    metadata: dict


def case_digest(dataset, rules):
    content = {"company":asdict(dataset.company),"accounts":[asdict(a) for a in dataset.accounts],
               "declarations":dataset.declarations,"metrics":{key:asdict(m) for key,m in dataset.metrics.items()},
               "rules":[asdict(r) for r in sorted(rules,key=lambda r:r.id)]}
    encoded=json.dumps(content,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str,allow_nan=False)
    return sha256(encoded.encode()).hexdigest()


def _value(expr, values):
    if isinstance(expr, str):
        return values[expr]
    if isinstance(expr, dict):
        op, args = next(iter(expr.items()))
        return engine._apply(op, [_value(arg, values) for arg in args])
    return engine._decimal(expr)


def _linear(expr, variable, values):
    """Return a*x+b only when this expression really is affine in x."""
    if isinstance(expr, str):
        return (Decimal(1), Decimal(0)) if expr == variable else (Decimal(0), values[expr])
    if not isinstance(expr, dict):
        return Decimal(0), engine._decimal(expr)
    op, args = next(iter(expr.items()))
    pairs = [_linear(arg, variable, values) for arg in args]
    if op == "add":
        return sum(p[0] for p in pairs), sum(p[1] for p in pairs)
    if op == "sub":
        return pairs[0][0]-pairs[1][0], pairs[0][1]-pairs[1][1]
    if op == "mul":
        a, b = Decimal(0), Decimal(1)
        for c, d in pairs:
            if a and c:
                raise ValueError("非线性乘积")
            a, b = a*d+b*c, b*d
        return a, b
    if op == "div" and not pairs[1][0] and pairs[1][1] > 0:
        return pairs[0][0]/pairs[1][1], pairs[0][1]/pairs[1][1]
    if op in {"min", "max"} and not any(p[0] for p in pairs):
        return Decimal(0), engine._apply(op, [p[1] for p in pairs])
    raise ValueError("此变量不支持精确仿射反解")


def _signed(name):
    return (any(s in name for s in ("净利润", "利润总额", "未分配利润", "其他调整", "抵扣调整"))
            or name.startswith("现金流量表."))


def _candidate_number(name, value):
    if not value.is_finite() or abs(value) > Decimal("1e12"):
        raise ValueError("生成数值超出教学范围")
    if value < 0 and not _signed(name):
        raise ValueError("该材料字段不能为负")
    if name.endswith("人数"):
        return value.to_integral_value(rounding=ROUND_CEILING)
    quantum = Decimal("0.000001") if name.startswith("参考.") or name.endswith("税率") else Decimal("0.01")
    return value.quantize(quantum)


def _matches(logic, rule, values, hit):
    try:
        data = Dataset(Company("仿真", "TEST", "仿真", "2026"), [], {},
                       {key: Metric(key, val, "仿真底稿") for key, val in values.items()})
        return engine._logic(logic, data, rule)[0] == hit
    except (engine.MissingMetric, ArithmeticError, ValueError):
        return False


def _solve_equation(left, right, rule, values, logic, hit):
    dependencies = engine.expression_metrics(left) | engine.expression_metrics(right)
    # Never edit reference bands to manufacture a hit. They are fixed inputs.
    candidates = sorted(dependencies - {key for key in rule.inputs if key.startswith("参考.")})
    for variable in candidates:
        try:
            a, b = _linear(left, variable, values)
            c, d = _linear(right, variable, values)
            if a == c:
                continue
            solution = (d-b)/(a-c)
            proposed = _candidate_number(variable, solution)
        except (ArithmeticError, ValueError):
            continue
        original = values[variable]
        # Integer/cents rounding may cross an equality; try both adjacent values.
        step = Decimal(1) if variable.endswith("人数") else Decimal("0.01")
        for number in (proposed, proposed+step, proposed-step):
            try:
                values[variable] = _candidate_number(variable, number)
                if _matches(logic, rule, values, hit):
                    return True
            except ValueError:
                pass
        values[variable] = original
    return False


def _inject(logic, rule, values, level, hit=True):
    if _matches(logic, rule, values, hit):
        return
    kind = logic["type"]
    if kind == "all":
        for _ in range(12):
            for child in logic["conditions"]:
                _inject(child, rule, values, level, hit)
                if not hit and _matches(logic, rule, values, False):
                    return
            if _matches(logic, rule, values, hit):
                return
        raise ValueError("复合规则无法满足全部条件，未生成题目。")
    if kind == "ratio_range":
        lo, hi = _value(logic["min"], values), _value(logic["max"], values)
        if lo > hi:
            raise ValueError("参考区间无效，未生成题目。")
        margin = max(Decimal(".01"), abs(hi-lo)*LEVELS[level])
        goals = (hi+margin, lo-margin) if hit else ((lo+hi)/2,)
        equations = [(logic["numerator"], {"mul": [logic["denominator"], ratio]}) for ratio in goals]
    else:
        left, right = logic["left"], logic["right"]
        threshold = _value(logic["threshold" if kind == "deviation" else "tolerance"], values)
        gap = threshold + max(Decimal(".01"), threshold*LEVELS[level]) if hit else Decimal(0)
        direction = logic.get("direction", "absolute")
        signs = (-1,) if direction == "below" else ((1,) if direction == "above" else (1,-1))
        equations = [(left, {"mul": [right, 1+sign*gap]} if kind == "deviation"
                      else {"add": [right, sign*gap]}) for sign in signs]
    for left, right in equations:
        if _solve_equation(left, right, rule, values, logic, hit):
            return
    raise ValueError(f"规则 {rule.id} 在当前教学材料范围内无法反解，未生成题目。")


def _source(key, rule_inputs, year):
    detail = rule_inputs.get(key, "同一企业同一期标准化教学指标；金额单位元、比例小数。")
    if key.startswith("历史."):
        detail += f" 实际历史期间：{year-1}-01-01 至 {year-1}-12-31，与本期全年同比。"
    elif key.startswith("年度."):
        actual = year-2 if ".前年" in key else (year-1 if ".上年" in key else year)
        detail += f" 实际年度期间：{actual}-01-01 至 {actual}-12-31，连续完整年度。"
    return "仿真底稿—"+key, detail


def materialize(values, company, rules):
    """Build actual account/declaration/statement values, never override sources."""
    inputs = {key: text for rule in rules for key, text in rule.inputs.items()}
    accounts = []
    for name, spec in config.ACCOUNT_MAP.items():
        for index, code in enumerate(spec["accounts"]):
            number = values.get(name, Decimal(0)) if index == 0 else Decimal(0)
            debit, credit = (number, Decimal(0)) if spec["side"] == "debit" else (Decimal(0), number)
            accounts.append(Account(code, name if index == 0 else "其他业务（仿真）", Decimal(0), debit, credit, Decimal(0)))
    declarations = {label: values.get(key, Decimal(0)) for key, label in config.DECLARATION_ITEMS.items()}
    metrics = loader._build_metrics(accounts, declarations)
    for key, value in sorted(values.items()):
        if key in metrics:
            continue
        source, detail = _source(key, inputs, periods.parse_period(company.period,"教学年度").end.year)
        metrics[key] = Metric(key, value, source, detail)
    return Dataset(company, accounts, declarations, metrics)


def generate(rules: list[Rule], rule_id: str, seed: int = 1, level: str = "normal", year: int = 2026) -> Exercise:
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 2147483647:
        raise ValueError("种子须为 0–2147483647 的整数")
    if level not in LEVELS or not isinstance(year, int) or isinstance(year, bool) or not 2000 <= year <= 2099:
        raise ValueError("难度或教学年度无效")
    rules = deepcopy(rules)
    if len({rule.id for rule in rules}) != len(rules):
        raise ValueError("规则 ID 重复")
    for rule in rules:
        engine.validate_rule_update(rule)
    target = next((rule for rule in rules if rule.id == rule_id), None)
    if target is None:
        raise ValueError("所选规则不在本期已启用规则中")
    dependencies = set().union(*(set(rule.inputs) for rule in rules))
    missing = dependencies-set(BASE)
    if missing:
        raise ValueError("缺少仿真来源映射："+"、".join(sorted(missing)))
    rng = Random(seed)
    scale = Decimal(rng.randint(80,150))/100
    values = {key: Decimal(str(value)) for key,value in BASE.items()}
    for key in values:
        if not key.startswith("参考.") and not key.endswith(("人数", "税率")):
            values[key] *= scale
    people = Decimal(rng.randint(30,80))
    values["人力.个税申报人数"] = values["人力.社保参保人数"] = people
    before = values.copy()
    with localcontext() as context:
        context.prec = 40
        _inject(target.logic, target, values, level)
    company = Company("规则生成教学企业（纯仿真）", f"TEST-GEN-{seed:010d}", "批发和零售业", str(year))
    dataset = materialize(values, company, rules)
    findings = engine.run(rules, dataset)
    selected = next(f for f in findings if f.rule.id == rule_id)
    if selected.status != "hit" or any(f.status == "skipped" for f in findings):
        raise ValueError("生成材料未通过全部规则可执行性/目标命中复核，未保存题目。")
    canonical = json.dumps({key:str(metric.value) for key,metric in sorted(dataset.metrics.items())},
                           ensure_ascii=False,sort_keys=True,separators=(",",":"))
    metadata = {"generator_version":GENERATOR_VERSION,"seed":seed,"level":level,"year":year,
                "requested_rule_id":rule_id,"standard_answer":sorted(f.rule.id for f in findings if f.hit),
                "changed_metrics":[key for key in sorted(values) if values[key]!=before[key]],
                "metric_sha256":sha256(canonical.encode()).hexdigest(),
                "case_sha256":case_digest(dataset,rules),"rule_versions":{r.id:r.version for r in rules},
                "notice":"所有材料为纯仿真标准化账套及底稿；关联命中均计入答案，不代表真实原始票据或完整凭证账簿。"}
    return Exercise(dataset, findings, metadata)
