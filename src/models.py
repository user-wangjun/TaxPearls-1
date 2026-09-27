"""核心数据模型。

设计要点：每个指标都携带 source（来源说明）与 detail（计算过程），
使「可举证」成为数据结构的固有属性，而非事后拼装。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class Company:
    """被审计企业基本信息。"""

    name: str
    taxpayer_id: str
    industry: str
    period: str


@dataclass
class Account:
    """科目余额表的一行。"""

    code: str
    name: str
    opening: Decimal | None
    debit: Decimal | None
    credit: Decimal | None
    closing: Decimal | None


@dataclass
class Metric:
    """标准化后的一个指标。

    source  : 取数来源（科目编码 / 申报表项目），用于在报告中举证
    detail  : 计算过程描述，例如 "6001 贷方 1,200,000 + 6051 贷方 80,000"
    """

    name: str
    value: Decimal | float
    source: str
    detail: str = ""


@dataclass
class RelatedSubject:
    key: str
    name: str
    kind: str
    taxpayer_id: str
    source: str


@dataclass
class RelatedRelation:
    key: str
    owner_key: str
    company_key: str
    kind: str
    start_on: str
    end_on: str
    reviewed: bool
    basis: str
    source: str


@dataclass
class RelatedTrade:
    key: str
    seller_key: str
    buyer_key: str
    traded_on: str
    amount: Decimal
    anomaly_basis: str
    reviewed: bool
    source: str


@dataclass
class RelatedGraph:
    subjects: list[RelatedSubject]
    relations: list[RelatedRelation]
    trades: list[RelatedTrade]


@dataclass
class Dataset:
    """一次审计的全部输入，标准化后的形态。"""

    company: Company
    accounts: list[Account]
    declarations: dict[str, Decimal | float]
    metrics: dict[str, Metric]
    related_graph: RelatedGraph | None = None
    # None denotes legacy snapshots, whose available evidence is described conservatively.
    sources: list[str] | None = None

    def values(self) -> dict[str, Decimal | float]:
        """供规则引擎求值用的扁平数值表。"""
        return {k: m.value for k, m in self.metrics.items()}

    def get(self, key: str) -> Decimal | float | None:
        m = self.metrics.get(key)
        return m.value if m else None

    def source_of(self, key: str) -> str:
        m = self.metrics.get(key)
        return m.source if m else "（未取到该指标）"

    def detail_of(self, key: str) -> str:
        m = self.metrics.get(key)
        return m.detail if m else ""


@dataclass
class Rule:
    """一条 YAML 声明式风险规则。"""

    id: str
    name: str
    category: str
    tax_type: str
    severity: str
    logic: dict[str, Any]
    evidence: list[str]
    legal_basis: list[str]
    suggestion: str
    description: str = ""
    source_file: str = ""
    inputs: dict[str, str] = field(default_factory=dict)
    scope: str = ""
    threshold_basis: str = ""
    references: list[str] = field(default_factory=list)
    version: str = "1.0"
    effective_from: str | None = None
    effective_to: str | None = None


@dataclass
class EvidenceItem:
    """证据卡上的一行数据。"""

    label: str
    value: str
    source: str
    emphasis: bool = False


@dataclass
class Finding:
    """一条规则的判定结果。

    status 取值：
        hit      -- 命中风险
        pass     -- 已执行，未发现异常
        skipped  -- 因证据、口径或适用范围等限制未能执行（不算通过，须明示实际原因）
    """

    rule: Rule
    status: str
    measured: float | None
    threshold_desc: str
    conclusion: str
    evidence: list[EvidenceItem] = field(default_factory=list)
    calculation: str = ""
    skip_reason: str = ""

    @property
    def hit(self) -> bool:
        return self.status == "hit"

    @property
    def executed(self) -> bool:
        return self.status != "skipped"

    @property
    def severity_rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}.get(self.rule.severity, 0)


SEVERITY_LABEL = {"high": "高风险", "medium": "中风险", "low": "低风险"}
STATUS_LABEL = {"hit": "命中", "pass": "通过", "skipped": "未执行"}
