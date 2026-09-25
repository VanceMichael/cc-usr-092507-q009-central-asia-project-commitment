"""匹配引擎：把两份意向的最新版本比成满足项、缺口、资源冲突三张清单。

匹配结果是纯数据快照，随后被组合原子原样保存；跨时区争用资源时，
审批依据的就是这份带版本号的快照，结论唯一。

资源型条目（土地/仓容/班列/专家）通过 ``attributes.resource_kind``
与 ``attributes.resource_id`` 关联具体库存；其余条目按维度内属性比较。
"""

from __future__ import annotations

from typing import Any

from .catalog import CONFIDENTIALITY_LEVELS
from .errors import RuleViolation
from .intents import Intent
from .resources import RESOURCE_KINDS, Resource

RESOURCE_LABEL = {
    "land": "土地",
    "warehouse": "仓容",
    "train_slot": "班列窗口",
    "expert_support": "专家支持",
}


def _confidentiality_ok(required: str, offered: str) -> bool:
    return CONFIDENTIALITY_LEVELS.index(offered) >= CONFIDENTIALITY_LEVELS.index(required)


def _compare_attributes(dimension: str, demand_attrs: dict, supply_attrs: dict) -> tuple[bool, str]:
    """维度内属性兼容规则。返回（是否满足，说明）。"""
    if dimension == "investment_stage":
        supported = supply_attrs.get("supports") or (
            [supply_attrs["stage"]] if supply_attrs.get("stage") else []
        )
        wanted = demand_attrs.get("stage")
        return (wanted in supported, f"阶段 {wanted} vs 供给 {sorted(set(supported))}")

    if dimension == "locality":
        wanted_region = demand_attrs.get("region")
        if wanted_region and supply_attrs.get("region") != wanted_region:
            return False, f"属地不符：需要 {wanted_region}，供给 {supply_attrs.get('region')}"
        missing_prefs = set(demand_attrs.get("tax_preferences", [])) - set(
            supply_attrs.get("tax_preferences", [])
        )
        if missing_prefs:
            return False, f"缺少税收优惠：{sorted(missing_prefs)}"
        missing_utils = set(demand_attrs.get("utilities", [])) - set(supply_attrs.get("utilities", []))
        if missing_utils:
            return False, f"缺少配套条件：{sorted(missing_utils)}"
        return True, f"属地 {supply_attrs.get('region')} 条件满足"

    if dimension == "logistics":
        common = set(demand_attrs.get("routes", [])) & set(supply_attrs.get("routes", []))
        if not common:
            return False, "没有共同物流通道"
        if demand_attrs.get("destination") and demand_attrs["destination"] != supply_attrs.get("destination"):
            return False, "班列目的地不一致"
        return True, f"共同通道：{sorted(common)}"

    if dimension == "agriculture":
        if demand_attrs.get("product") != supply_attrs.get("product"):
            return False, "农产品类别不一致"
        missing_cert = set(demand_attrs.get("certifications", [])) - set(
            supply_attrs.get("certifications", [])
        )
        if missing_cert:
            return False, f"缺少资质：{sorted(missing_cert)}"
        return True, f"原料 {supply_attrs.get('product')} 匹配"

    if dimension == "processing":
        if demand_attrs.get("field") != supply_attrs.get("field"):
            return False, "加工领域不一致"
        missing_cert = set(demand_attrs.get("certifications", [])) - set(
            supply_attrs.get("certifications", [])
        )
        if missing_cert:
            return False, f"缺少加工资质：{sorted(missing_cert)}"
        return True, f"加工能力 {supply_attrs.get('field')} 匹配"

    if dimension == "expert":
        if demand_attrs.get("field") != supply_attrs.get("field"):
            return False, "专家领域不一致"
        langs = set(demand_attrs.get("languages", []))
        if langs and not (langs & set(supply_attrs.get("languages", []))):
            return False, "没有共同工作语言"
        return True, f"专家 {supply_attrs.get('field')} 匹配"

    if dimension == "currency":
        wanted = demand_attrs.get("currency")
        accepted = set(supply_attrs.get("accepted_currencies", []))
        convertible = set(supply_attrs.get("convertible_currencies", []))
        if wanted in accepted:
            return True, f"币种 {wanted} 直接接受"
        if wanted in convertible:
            return True, f"币种 {wanted} 按汇率版本折算"
        return False, f"币种 {wanted} 既不被接受也不可折算"

    if dimension == "confidentiality":
        wanted = demand_attrs.get("level", "L1")
        offered = supply_attrs.get("level", "L1")
        ok = _confidentiality_ok(wanted, offered)
        return ok, f"保密级别 需求 {wanted} / 供给 {offered}"

    return False, f"未知维度：{dimension}"


def _qty(item: dict[str, Any]) -> float | None:
    if item.get("quantity"):
        return item["quantity"]["value"]
    return None


def match_intents(
    *,
    demand_intent: Intent,
    supply_intent: Intent,
    resources: dict[str, Resource],
    at: str,
    active_refs_ignore: str | None = None,
) -> dict[str, Any]:
    """生成候选匹配快照。

    resources: 系统当前全部资源库存（resource_id -> Resource）。
    active_refs_ignore: 计算班列/土地争用时忽略某个组合自身的占用（改量场景）。
    """
    demand = demand_intent.latest()
    supply = supply_intent.latest()
    demand_items = [i for i in demand["items"] if i["role"] == "demand"]
    supply_items = [i for i in supply["items"] if i["role"] == "supply"]

    satisfied: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    lines: dict[str, dict[str, Any]] = {}
    gapped_kinds: set[str] = set()

    supply_by_dimension: dict[str, list[dict[str, Any]]] = {}
    for item in supply_items:
        supply_by_dimension.setdefault(item["dimension"], []).append(item)

    for demand_item in demand_items:
        code = demand_item["code"]
        dimension = demand_item["dimension"]
        candidates = supply_by_dimension.get(dimension, [])

        if not _confidentiality_ok(demand_item["confidentiality"], max(
            (c["confidentiality"] for c in candidates), default="L1",
            key=lambda lvl: CONFIDENTIALITY_LEVELS.index(lvl),
        )):
            gaps.append({"demand_code": code, "dimension": dimension, "reason": "供给方保密级别不足"})
            continue

        # ---------- 资源型条目：土地/仓容/班列/专家 ----------
        resource_kind = demand_item["attributes"].get("resource_kind")
        if resource_kind is not None:
            if resource_kind not in RESOURCE_KINDS:
                raise RuleViolation(f"未知资源类别：{resource_kind}")
            wanted_qty = _qty(demand_item)
            if wanted_qty is None:
                gaps.append({"demand_code": code, "dimension": dimension, "kind": resource_kind,
                             "reason": "资源需求缺少数量"})
                gapped_kinds.add(resource_kind)
                continue
            resource_candidates = [
                c for c in candidates
                if c["attributes"].get("resource_kind") == resource_kind
                and (_qty(c) or 0.0) + 1e-9 >= wanted_qty
            ]
            if not resource_candidates:
                gaps.append(
                    {"demand_code": code, "dimension": dimension, "kind": resource_kind,
                     "reason": f"无足量{RESOURCE_LABEL[resource_kind]}供给"}
                )
                gapped_kinds.add(resource_kind)
                continue
            chosen = resource_candidates[0]
            resource_id = chosen["attributes"].get("resource_id")
            resource = resources.get(resource_id)
            if resource is None:
                gaps.append(
                    {"demand_code": code, "dimension": dimension, "kind": resource_kind,
                     "reason": f"供给资源未登记：{resource_id}"}
                )
                gapped_kinds.add(resource_kind)
                continue
            available = resource.available
            extra = 0.0
            if active_refs_ignore and resource.allocations.get(active_refs_ignore):
                # 改量时把自身当前占用加回可用量。
                available = round(available + resource.allocations[active_refs_ignore].qty, 6)
            if wanted_qty > available + 1e-9:
                occupants = [
                    {"ref_id": a.ref_id, "qty": a.qty, "fulfilled": a.fulfilled}
                    for rid, a in resource.allocations.items()
                    if rid != active_refs_ignore
                ]
                conflicts.append(
                    {
                        "demand_code": code,
                        "kind": resource_kind,
                        "resource_id": resource_id,
                        "requested": wanted_qty,
                        "available": available,
                        "occupants": occupants,
                    }
                )
                gapped_kinds.add(resource_kind)
                continue
            lines[resource_kind] = {
                "kind": resource_kind,
                "resource_id": resource_id,
                "qty": wanted_qty,
                "unit": resource.unit,
                "supply_code": chosen["code"],
            }
            satisfied.append(
                {
                    "demand_code": code,
                    "supply_code": chosen["code"],
                    "dimension": dimension,
                    "detail": f"{RESOURCE_LABEL[resource_kind]} {resource_id} 预留 {wanted_qty}{resource.unit}",
                }
            )
            continue

        # ---------- 普通属性条目 ----------
        if not candidates:
            gaps.append({"demand_code": code, "dimension": dimension, "reason": "供给方无此口径条目"})
            continue
        best = None
        for candidate in candidates:
            ok, detail = _compare_attributes(dimension, demand_item["attributes"], candidate["attributes"])
            qty_ok = True
            if _qty(demand_item) is not None:
                qty_ok = (_qty(candidate) or 0.0) + 1e-9 >= _qty(demand_item)
                detail += f"；数量 需要 {_qty(demand_item)} / 可供 {_qty(candidate)}"
            if ok and qty_ok:
                best = (candidate, detail)
                break
        if best is None:
            # 给出最接近候选的不兼容原因
            candidate = candidates[0]
            _, detail = _compare_attributes(dimension, demand_item["attributes"], candidate["attributes"])
            gaps.append({"demand_code": code, "dimension": dimension, "reason": detail})
        else:
            candidate, detail = best
            satisfied.append(
                {"demand_code": code, "supply_code": candidate["code"], "dimension": dimension, "detail": detail}
            )

    # 组合原子四类必须齐备，缺类即缺口。
    for kind in RESOURCE_KINDS:
        if kind not in lines and kind not in gapped_kinds:
            gaps.append(
                {"demand_code": None, "dimension": "bundle", "kind": kind,
                 "reason": f"组合原子缺少{RESOURCE_LABEL[kind]}"}
            )

    return {
        "at": at,
        "demand_intent_id": demand_intent.intent_id,
        "supply_intent_id": supply_intent.intent_id,
        "demand_revision": demand["revision"],
        "supply_revision": supply["revision"],
        "fx_version_demand": demand["fx_version"],
        "fx_version_supply": supply["fx_version"],
        "satisfied": satisfied,
        "gaps": gaps,
        "conflicts": conflicts,
        "lines": [lines[k] for k in RESOURCE_KINDS if k in lines],
        "compatible": not gaps and not conflicts and len(lines) == len(RESOURCE_KINDS),
    }
