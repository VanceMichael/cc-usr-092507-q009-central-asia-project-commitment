"""候选匹配：把两个意向的需求/供给逐维对照。

输出必须同时列出：
- satisfied：满足项（含说明）；
- gaps：条款缺口与不兼容（属性/数量）；
- resource_conflicts：条款本身满足、但组合资源被其他承诺争用的部分。
匹配只读，不落任何占用；占用要等双方确认后由承诺服务完成。
"""

from __future__ import annotations

from typing import Any

from .clauses import compare
from .resources import ResourceLedger


def _by_dimension(clauses: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {c["dimension"]: c for c in clauses}


def evaluate_pair(
    intent_a: dict[str, Any],
    intent_b: dict[str, Any],
    ledger: ResourceLedger,
    bundle: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """双向对照：A 的需求对 B 的供给，B 的需求对 A 的供给。"""
    a_demands = _by_dimension(intent_a["demands"])
    a_offers = _by_dimension(intent_a["offers"])
    b_demands = _by_dimension(intent_b["demands"])
    b_offers = _by_dimension(intent_b["offers"])

    satisfied: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []

    def _pair(demands: dict[str, dict[str, Any]], offers: dict[str, dict[str, Any]], side: str) -> None:
        for dimension, demand in sorted(demands.items()):
            offer = offers.get(dimension)
            if offer is None:
                gaps.append(
                    {"side": side, "dimension": dimension,
                     "gap": "对方意向未提供该维度供给"}
                )
                continue
            result = compare(demand, offer)
            if result["satisfied"]:
                satisfied.append(
                    {"side": side, "dimension": dimension, "detail": result.get("detail", "")}
                )
            else:
                gaps.append({"side": side, "dimension": dimension, "gap": result.get("gap", "")})

    _pair(a_demands, b_offers, "a_to_b")
    _pair(b_demands, a_offers, "b_to_a")

    resource_conflicts: list[dict[str, Any]] = []
    if bundle:
        resource_conflicts = ledger.check_bundle(bundle)

    all_dimensions = set(a_demands) | set(b_demands)
    return {
        "intent_a_id": intent_a["intent_id"],
        "intent_b_id": intent_b["intent_id"],
        "matched_dimensions": sorted({item["dimension"] for item in satisfied}),
        "satisfied": satisfied,
        "gaps": gaps,
        "resource_conflicts": resource_conflicts,
        "clause_match": len(gaps) == 0,
        "resource_available": len(resource_conflicts) == 0,
        "ready_to_hold": len(gaps) == 0 and len(resource_conflicts) == 0,
        "dimensions_total": len(all_dimensions),
    }
