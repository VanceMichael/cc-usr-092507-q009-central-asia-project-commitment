"""合作意向聚合。

每项意向引用发起主体的具体版本与授权联系人，条款分需求/供给两侧，
覆盖七维口径。意向只描述"要什么、给什么"，不占用任何资源；
资源占用发生在双方确认后的承诺暂留。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .aggregates import Aggregate
from .clauses import DIMENSIONS, validate_clauses
from .errors import StateConflict

INTENT_KINDS = ("localized_production", "dry_port", "agro_processing", "other")


@dataclass
class Intent(Aggregate):
    intent_id: str = ""
    owner_party_id: str = ""
    owner_version: int = 0
    contact_id: str = ""
    kind: str = ""
    title: str = ""
    demands: list[dict[str, Any]] = field(default_factory=list)
    offers: list[dict[str, Any]] = field(default_factory=list)
    status: str = ""
    revision: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def file(
        self,
        intent_id: str,
        owner_party_id: str,
        owner_version: int,
        contact_id: str,
        kind: str,
        title: str,
        demands: list[dict[str, Any]],
        offers: list[dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any]]]:
        if self.version:
            raise StateConflict("意向已存在")
        if kind not in INTENT_KINDS:
            raise ValueError(f"未知意向类型：{kind}")
        validate_clauses(demands)
        validate_clauses(offers)
        self.stream_id = f"intent:{intent_id}"
        return [
            (
                "IntentFiled",
                {
                    "intent_id": intent_id,
                    "owner_party_id": owner_party_id,
                    "owner_version": owner_version,
                    "contact_id": contact_id,
                    "kind": kind,
                    "title": title,
                    "demands": demands,
                    "offers": offers,
                },
            )
        ]

    def amend(
        self,
        contact_id: str,
        demands: list[dict[str, Any]],
        offers: list[dict[str, Any]],
        reason: str,
    ) -> list[tuple[str, dict[str, Any]]]:
        if self.status != "open":
            raise StateConflict(f"意向状态为 {self.status}，不能修改")
        validate_clauses(demands)
        validate_clauses(offers)
        return [
            (
                "IntentAmended",
                {
                    "revision": self.revision + 1,
                    "contact_id": contact_id,
                    "demands": demands,
                    "offers": offers,
                    "reason": reason,
                },
            )
        ]

    def withdraw(self, reason: str) -> list[tuple[str, dict[str, Any]]]:
        if self.status != "open":
            raise StateConflict(f"意向状态为 {self.status}，不能撤回")
        return [("IntentWithdrawn", {"reason": reason})]

    def demand_dimensions(self) -> set[str]:
        return {c["dimension"] for c in self.demands}

    @classmethod
    def apply_event(cls, state: "Intent", event_type: str, data: dict[str, Any]) -> None:
        if event_type == "IntentFiled":
            state.intent_id = data["intent_id"]
            state.owner_party_id = data["owner_party_id"]
            state.owner_version = data["owner_version"]
            state.contact_id = data["contact_id"]
            state.kind = data["kind"]
            state.title = data["title"]
            state.demands = list(data["demands"])
            state.offers = list(data["offers"])
            state.status = "open"
            state.stream_id = f"intent:{data['intent_id']}"
            state.history.append({"revision": 0})
        elif event_type == "IntentAmended":
            state.history.append(
                {
                    "revision": data["revision"],
                    "demands": list(state.demands),
                    "offers": list(state.offers),
                    "reason": data.get("reason", ""),
                }
            )
            state.revision = data["revision"]
            state.demands = list(data["demands"])
            state.offers = list(data["offers"])
            state.contact_id = data["contact_id"]
        elif event_type == "IntentWithdrawn":
            state.status = "withdrawn"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "owner_party_id": self.owner_party_id,
            "owner_version": self.owner_version,
            "contact_id": self.contact_id,
            "kind": self.kind,
            "title": self.title,
            "demands": self.demands,
            "offers": self.offers,
            "status": self.status,
            "revision": self.revision,
        }
