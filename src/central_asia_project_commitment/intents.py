"""意向聚合：把洽谈意向解释成版本化、可比较的需求条目与供给条目。

每份意向按七个口径拆分：投资阶段、属地条件、物流资源、农业原料、
加工能力、币种口径、保密范围。数量在入库时归一到基准单位，
保密标签随条目携带，后续匹配与尽调授权直接使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .catalog import (
    AGRI_PRODUCTS,
    CONFIDENTIALITY_LEVELS,
    EXPERT_FIELDS,
    INVESTMENT_STAGES,
    LOGISTICS_ROUTES,
    PROCESS_FIELDS,
    TAX_PREFERENCES,
    UNIT_FAMILIES,
    convert,
)
from .errors import RuleViolation
from .repository import Aggregate

DIMENSIONS = (
    "investment_stage",
    "locality",
    "logistics",
    "agriculture",
    "processing",
    "expert",
    "currency",
    "confidentiality",
)


@dataclass(frozen=True)
class Quantity:
    value: float          # 已归一到基准单位
    unit: str             # 基准单位
    raw_value: float
    raw_unit: str

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "unit": self.unit, "raw_value": self.raw_value, "raw_unit": self.raw_unit}


def normalize_quantity(raw_value: float, raw_unit: str) -> Quantity:
    for family_name, family in UNIT_FAMILIES.items():
        if raw_unit in family:
            base_unit = next(iter(family))  # 字典首项即基准单位
            value = convert(raw_value, raw_unit, base_unit)
            return Quantity(value=value, unit=base_unit, raw_value=raw_value, raw_unit=raw_unit)
    raise RuleViolation(f"未知计量单位：{raw_unit}")


def validate_item(item: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化单条供需，返回规范化副本。"""
    required = {"code", "dimension", "role"}
    if not required.issubset(item):
        raise RuleViolation(f"供需条目缺少字段：{required - set(item)}")
    if item["dimension"] not in DIMENSIONS:
        raise RuleViolation(f"未知口径：{item['dimension']}")
    if item["role"] not in ("demand", "supply"):
        raise RuleViolation("条目的 role 必须是 demand 或 supply")
    level = item.get("confidentiality", "L1")
    if level not in CONFIDENTIALITY_LEVELS:
        raise RuleViolation(f"未知保密级别：{level}")

    normalized = {
        "code": item["code"],
        "dimension": item["dimension"],
        "role": item["role"],
        "confidentiality": level,
        "attributes": dict(item.get("attributes", {})),
        "quantity": None,
    }
    if "quantity" in item and item["quantity"] is not None:
        q = item["quantity"]
        normalized["quantity"] = normalize_quantity(q["value"], q["unit"]).to_dict()

    # 维度内枚举校验，错误口径在入口就被拒绝，而不是留到匹配时。
    attrs = normalized["attributes"]
    dim = item["dimension"]
    if dim == "investment_stage":
        if item["role"] == "supply" and "supports" in attrs:
            invalid = set(attrs["supports"]) - set(INVESTMENT_STAGES)
            if invalid:
                raise RuleViolation(f"投资阶段取值无效：{sorted(invalid)}")
        elif attrs.get("stage") not in INVESTMENT_STAGES:
            raise RuleViolation("投资阶段取值无效")
    if dim == "locality":
        for pref in attrs.get("tax_preferences", []):
            if pref not in TAX_PREFERENCES:
                raise RuleViolation(f"未知税收优惠项：{pref}")
    if dim == "logistics":
        for route in attrs.get("routes", []):
            if route not in LOGISTICS_ROUTES:
                raise RuleViolation(f"未知物流通道：{route}")
    if dim == "agriculture" and attrs.get("product") not in AGRI_PRODUCTS:
        raise RuleViolation("农产品类别取值无效")
    if dim == "processing" and attrs.get("field") not in PROCESS_FIELDS:
        raise RuleViolation("加工领域取值无效")
    if dim == "expert" and attrs.get("field") not in EXPERT_FIELDS:
        raise RuleViolation("专家领域取值无效")
    return normalized


class Intent(Aggregate):
    stream_prefix = "intent"

    def __init__(self, stream_id: str) -> None:
        super().__init__(stream_id)
        self.intent_id: str | None = None
        self.party_id: str | None = None
        self.title: str = ""
        self.fx_version: int | None = None
        self.versions: list[dict[str, Any]] = []

    @classmethod
    def register(
        cls,
        intent_id: str,
        *,
        party_id: str,
        title: str,
        items: list[dict[str, Any]],
        fx_version: int,
        at: str,
        zone: str = "UTC",
    ) -> "Intent":
        if not items:
            raise RuleViolation("意向至少包含一条供需")
        normalized = [validate_item(i) for i in items]
        cls._check_codes_unique(normalized)
        intent = cls(cls.stream_for(intent_id))
        intent.record(
            "IntentRegistered",
            {
                "intent_id": intent_id,
                "party_id": party_id,
                "title": title,
                "items": normalized,
                "fx_version": fx_version,
                "revision": 1,
                "at": at,
                "zone": zone,
            },
        )
        return intent

    def revise(
        self, *, items: list[dict[str, Any]], fx_version: int, reason: str, at: str, zone: str = "UTC"
    ) -> None:
        if self.intent_id is None:
            raise RuleViolation("意向尚未登记")
        if not reason:
            raise RuleViolation("意向修订必须说明原因")
        normalized = [validate_item(i) for i in items]
        self._check_codes_unique(normalized)
        self.record(
            "IntentRevised",
            {
                "intent_id": self.intent_id,
                "party_id": self.party_id,
                "items": normalized,
                "fx_version": fx_version,
                "revision": len(self.versions) + 1,
                "reason": reason,
                "at": at,
                "zone": zone,
            },
        )

    @staticmethod
    def _check_codes_unique(items: list[dict[str, Any]]) -> None:
        codes = [i["code"] for i in items]
        if len(codes) != len(set(codes)):
            raise RuleViolation("同一意向内条目编码重复")

    def latest(self) -> dict[str, Any]:
        return self.versions[-1]

    def items_by_role(self, role: str) -> list[dict[str, Any]]:
        return [i for i in self.latest()["items"] if i["role"] == role]

    def apply(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        if event["type"] in ("IntentRegistered", "IntentRevised"):
            self.intent_id = p["intent_id"]
            self.party_id = p["party_id"]
            self.fx_version = p["fx_version"]
            if event["type"] == "IntentRegistered":
                self.title = p["title"]
            self.versions.append(
                {
                    "revision": p["revision"],
                    "items": p["items"],
                    "fx_version": p["fx_version"],
                    "at": p["at"],
                    "reason": p.get("reason", "登记"),
                }
            )
        else:  # pragma: no cover
            raise RuleViolation(f"未知意向事件：{event['type']}")
