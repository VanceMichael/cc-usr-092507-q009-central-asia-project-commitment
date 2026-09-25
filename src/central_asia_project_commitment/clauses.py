"""意向条款归一化与逐维比较。

每项意向拆成 demand（需求）与 offer（供给）两组条款，覆盖七个口径：
投资阶段、属地条件、物流资源、农业原料、加工能力、币种口径、保密范围。
比较结果供候选匹配使用：满足项、数值缺口、属性不兼容。
"""

from __future__ import annotations

from typing import Any

DIMENSIONS = (
    "investment_stage",
    "local_condition",
    "logistics",
    "agriculture",
    "processing",
    "currency",
    "confidentiality",
)

# 投资阶段先后次序：供给方所处阶段不得早于需求方要求
STAGE_ORDER = {
    "intention": 1,      # 意向
    "feasibility": 2,    # 可行性研究
    "signed": 3,         # 签约
    "construction": 4,   # 建设
    "operation": 5,      # 运营
}

# 农产品质量等级：供给等级不得低于需求等级
GRADE_ORDER = {"特级": 4, "一级": 3, "二级": 2, "三级": 1}

# 属地分区兼容：同地或政策等价区可匹配
ZONING_COMPAT = {
    "bonded": {"bonded", "comprehensive_bonded"},
    "comprehensive_bonded": {"bonded", "comprehensive_bonded"},
}


def clause(clause_value: dict[str, Any]) -> str:
    return clause_value["dimension"]


def compare(demand: dict[str, Any], offer: dict[str, Any]) -> dict[str, Any]:
    """比较一条需求条款与一条供给条款，返回满足项与缺口说明。"""
    dimension = demand["dimension"]
    if offer["dimension"] != dimension:
        return {
            "dimension": dimension,
            "satisfied": False,
            "gap": f"供给维度为 {offer['dimension']}，无法对应",
        }
    checker = _CHECKERS.get(dimension)
    if checker is None:
        return {"dimension": dimension, "satisfied": False, "gap": "未知条款维度"}
    return checker(demand, offer)


def _compare_stage(demand: dict[str, Any], offer: dict[str, Any]) -> dict[str, Any]:
    need = STAGE_ORDER.get(demand["stage"], 0)
    have = STAGE_ORDER.get(offer["stage"], 0)
    if have >= need:
        return {"dimension": "investment_stage", "satisfied": True,
                "detail": f"供给阶段 {offer['stage']} 不早于要求 {demand['stage']}"}
    return {"dimension": "investment_stage", "satisfied": False,
            "gap": f"供给阶段 {offer['stage']} 早于要求 {demand['stage']}"}


def _regions_compatible(a: str, b: str) -> bool:
    if a == b:
        return True
    # 形如 "辽宁/沈阳" 与 "辽宁" 视为包含
    return a.split("/")[0] == b.split("/")[0]


def _compare_local(demand: dict[str, Any], offer: dict[str, Any]) -> dict[str, Any]:
    gaps: list[str] = []
    if not _regions_compatible(demand.get("region", ""), offer.get("region", "")):
        gaps.append(f"属地 {offer.get('region')} 不满足 {demand.get('region')}")
    need_area = demand.get("area_mu", 0)
    have_area = offer.get("area_mu", 0)
    if have_area < need_area:
        gaps.append(f"土地面积缺口 {need_area - have_area} 亩")
    need_zones = set(demand.get("zoning", []))
    have_zones = set(offer.get("zoning", []))
    for zone in need_zones:
        compatible = ZONING_COMPAT.get(zone, {zone})
        if not (have_zones & compatible):
            gaps.append(f"缺少用地属性 {zone}")
    for utility in demand.get("utilities", []):
        if utility not in offer.get("utilities", []):
            gaps.append(f"缺少配套 {utility}")
    if gaps:
        return {"dimension": "local_condition", "satisfied": False, "gap": "；".join(gaps)}
    return {"dimension": "local_condition", "satisfied": True,
            "detail": f"{offer.get('region')} {have_area} 亩，属性与配套齐备"}


def _compare_logistics(demand: dict[str, Any], offer: dict[str, Any]) -> dict[str, Any]:
    gaps: list[str] = []
    corridors = demand.get("corridors", [demand.get("corridor")] if demand.get("corridor") else [])
    offer_corridors = offer.get("corridors", [offer.get("corridor")] if offer.get("corridor") else [])
    matched_corridor = next((c for c in corridors if c in offer_corridors), None)
    if not matched_corridor:
        gaps.append(f"班列通道不相交（需求 {corridors} / 供给 {offer_corridors}）")
    need_slots = demand.get("train_slots_per_week", 0)
    have_slots = offer.get("train_slots_per_week", 0)
    if have_slots < need_slots:
        gaps.append(f"班列窗口缺口每周 {need_slots - have_slots} 列")
    need_wh = demand.get("warehouse_sqm", 0)
    have_wh = offer.get("warehouse_sqm", 0)
    if have_wh < need_wh:
        gaps.append(f"仓容缺口 {need_wh - have_wh} 平方米")
    if demand.get("dry_port_required") and not offer.get("dry_port_available"):
        gaps.append("需要陆港共建席位但供给方无陆港资源")
    if gaps:
        return {"dimension": "logistics", "satisfied": False, "gap": "；".join(gaps)}
    return {"dimension": "logistics", "satisfied": True,
            "detail": f"通道 {matched_corridor}，窗口 {have_slots} 列/周，仓 {have_wh} ㎡"}


def _compare_agriculture(demand: dict[str, Any], offer: dict[str, Any]) -> dict[str, Any]:
    gaps: list[str] = []
    if demand.get("crop") != offer.get("crop"):
        gaps.append(f"原料品类不符：需求 {demand.get('crop')} / 供给 {offer.get('crop')}")
    need_tons = demand.get("annual_tons", 0)
    have_tons = offer.get("annual_tons", 0)
    if have_tons < need_tons:
        gaps.append(f"原料量缺口 {need_tons - have_tons} 吨/年")
    need_grade = GRADE_ORDER.get(demand.get("grade", "三级"), 1)
    have_grade = GRADE_ORDER.get(offer.get("grade", "三级"), 1)
    if have_grade < need_grade:
        gaps.append(f"原料等级 {offer.get('grade')} 低于要求 {demand.get('grade')}")
    if gaps:
        return {"dimension": "agriculture", "satisfied": False, "gap": "；".join(gaps)}
    return {"dimension": "agriculture", "satisfied": True,
            "detail": f"{offer.get('crop')} {have_tons} 吨/年，{offer.get('grade')}"}


def _compare_processing(demand: dict[str, Any], offer: dict[str, Any]) -> dict[str, Any]:
    gaps: list[str] = []
    if demand.get("product") != offer.get("product"):
        gaps.append(f"加工品项不符：{demand.get('product')} / {offer.get('product')}")
    need_cap = demand.get("capacity_tons_year", 0)
    have_cap = offer.get("capacity_tons_year", 0)
    if have_cap < need_cap:
        gaps.append(f"加工能力缺口 {need_cap - have_cap} 吨/年")
    missing = set(demand.get("certifications", [])) - set(offer.get("certifications", []))
    if missing:
        gaps.append(f"缺少认证 {sorted(missing)}")
    if gaps:
        return {"dimension": "processing", "satisfied": False, "gap": "；".join(gaps)}
    return {"dimension": "processing", "satisfied": True,
            "detail": f"{offer.get('product')} {have_cap} 吨/年"}


def _compare_currency(demand: dict[str, Any], offer: dict[str, Any]) -> dict[str, Any]:
    gaps: list[str] = []
    if demand.get("currency") != offer.get("currency"):
        gaps.append(
            f"币种口径不一致：{demand.get('currency')} / {offer.get('currency')}"
            f"（须先统一结算币种再匹配）"
        )
    else:
        need_min = demand.get("amount_min")
        need_max = demand.get("amount_max")
        have_min = offer.get("amount_min")
        have_max = offer.get("amount_max")
        if need_min is not None and have_max is not None and have_max < need_min:
            gaps.append(f"金额区间不相交：供给上限 {have_max} < 需求下限 {need_min}")
        if need_max is not None and have_min is not None and have_min > need_max:
            gaps.append(f"金额区间不相交：供给下限 {have_min} > 需求上限 {need_max}")
    if demand.get("tax") and offer.get("tax") and demand["tax"] != offer["tax"]:
        gaps.append(f"含税口径不同：{demand['tax']} / {offer['tax']}")
    if gaps:
        return {"dimension": "currency", "satisfied": False, "gap": "；".join(gaps)}
    return {"dimension": "currency", "satisfied": True,
            "detail": f"结算 {offer.get('currency')}，口径一致"}


def _compare_confidentiality(demand: dict[str, Any], offer: dict[str, Any]) -> dict[str, Any]:
    gaps: list[str] = []
    uncovered = set(demand.get("scope", [])) - set(offer.get("scope", []))
    if uncovered:
        gaps.append(f"保密范围未覆盖 {sorted(uncovered)}")
    if demand.get("nda_required") and not offer.get("nda_accepted"):
        gaps.append("需求方要求 NDA，供给方尚未接受")
    if demand.get("embargo_days", 0) > offer.get("embargo_days", 0):
        gaps.append(
            f"保密期 {offer.get('embargo_days', 0)} 天短于要求 {demand.get('embargo_days')} 天"
        )
    if gaps:
        return {"dimension": "confidentiality", "satisfied": False, "gap": "；".join(gaps)}
    return {"dimension": "confidentiality", "satisfied": True,
            "detail": "保密范围与期限覆盖要求"}


_CHECKERS = {
    "investment_stage": _compare_stage,
    "local_condition": _compare_local,
    "logistics": _compare_logistics,
    "agriculture": _compare_agriculture,
    "processing": _compare_processing,
    "currency": _compare_currency,
    "confidentiality": _compare_confidentiality,
}


def validate_clauses(clauses: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for item in clauses:
        dimension = item.get("dimension")
        if dimension not in DIMENSIONS:
            raise ValueError(f"未知条款维度：{dimension}")
        if dimension in seen:
            raise ValueError(f"同一意向中维度重复：{dimension}")
        seen.add(dimension)
