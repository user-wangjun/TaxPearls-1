"""Backward-compatible serialization of frozen audit evidence."""
from __future__ import annotations
from dataclasses import asdict
from decimal import Decimal
from typing import Any
from .models import Account, Company, Dataset, EvidenceItem, Finding, Metric, RelatedGraph, RelatedRelation, RelatedSubject, RelatedTrade, Rule


def serialize_dataset(dataset: Dataset) -> dict[str, Any]:
    return {
        **({"sources": dataset.sources} if dataset.sources is not None else {}),
        "company": asdict(dataset.company),
        "accounts": [
            {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(a).items()}
            for a in dataset.accounts
        ],
        "declarations": {k: str(v) for k, v in dataset.declarations.items()},
        "metrics": {
            k: {"name": m.name, "value": str(m.value), "source": m.source, "detail": m.detail}
            for k, m in dataset.metrics.items()
        },
        "related_graph": None if dataset.related_graph is None else {
            "subjects": [asdict(item) for item in dataset.related_graph.subjects],
            "relations": [asdict(item) for item in dataset.related_graph.relations],
            "trades": [{**asdict(item), "amount": str(item.amount)} for item in dataset.related_graph.trades],
        },
    }


def deserialize_dataset(data: dict[str, Any]) -> Dataset:
    def dec(value: Any) -> Decimal | None:
        return None if value is None else Decimal(str(value))

    return Dataset(
        sources=data.get("sources"),
        company=Company(**data["company"]),
        accounts=[
            Account(
                code=a["code"], name=a["name"], opening=dec(a["opening"]),
                debit=dec(a["debit"]), credit=dec(a["credit"]), closing=dec(a["closing"]),
            )
            for a in data["accounts"]
        ],
        declarations={k: Decimal(v) for k, v in data["declarations"].items()},
        metrics={
            k: Metric(name=m["name"], value=Decimal(m["value"]), source=m["source"], detail=m["detail"])
            for k, m in data["metrics"].items()
        },
        related_graph=(RelatedGraph(
            subjects=[RelatedSubject(**item) for item in data["related_graph"]["subjects"]],
            relations=[RelatedRelation(**item) for item in data["related_graph"]["relations"]],
            trades=[RelatedTrade(**{**item, "amount": Decimal(item["amount"])})
                    for item in data["related_graph"]["trades"]],
        ) if data.get("related_graph") is not None else None),
    )


def serialize_findings(findings: list[Finding]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for finding in findings:
        item = asdict(finding)
        item["measured"] = finding.measured
        out.append(item)
    return out


def deserialize_findings(data: list[dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    for item in data:
        rule = Rule(**item["rule"])
        evidence = [EvidenceItem(**row) for row in item.get("evidence", [])]
        findings.append(Finding(
            rule=rule, status=item["status"], measured=item.get("measured"),
            threshold_desc=item.get("threshold_desc", ""), conclusion=item.get("conclusion", ""),
            evidence=evidence, calculation=item.get("calculation", ""),
            skip_reason=item.get("skip_reason", ""),
        ))
    return findings
