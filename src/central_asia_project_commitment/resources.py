"""资源库存：土地、仓容、班列窗口、专家支持四类可量纲资源。

每个资源是独立事件流，是资源争用的串行点：
两个项目并发争抢同一班列窗口时，只有期望版本未过期的一次提交成功。
占用/释放由组合暂留流程驱动，本聚合只保证库存口径与不超额。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import NotFound, RuleViolation
from .repository import Aggregate
from .intents import normalize_quantity

RESOURCE_KINDS = ("land", "warehouse", "train_slot", "expert_support")
KIND_UNIT = {"land": "m2", "warehouse": "m3", "train_slot": "teu", "expert_support": "person_day"}


@dataclass
class Allocation:
    ref_id: str          # 组合原子（bundle）ID
    qty: float           # 当前占用量（基准单位）
    fulfilled: float     # 已交接量：终止时不释放，继续留存

    @property
    def releasable(self) -> float:
        return self.qty - self.fulfilled


class Resource(Aggregate):
    stream_prefix = "res"

    def __init__(self, stream_id: str) -> None:
        super().__init__(stream_id)
        self.resource_id: str | None = None
        self.kind: str | None = None
        self.capacity: float = 0.0
        self.unit: str = ""
        self.attributes: dict[str, Any] = {}
        self.owner_party_id: str | None = None
        self.allocations: dict[str, Allocation] = {}

    @classmethod
    def stream_for(cls, resource_id: str) -> str:
        return f"res-{resource_id}"

    @classmethod
    def register(
        cls,
        resource_id: str,
        *,
        kind: str,
        capacity_value: float,
        capacity_unit: str,
        owner_party_id: str,
        attributes: dict[str, Any] | None = None,
        at: str,
    ) -> "Resource":
        if kind not in RESOURCE_KINDS:
            raise RuleViolation(f"未知资源类别：{kind}")
        q = normalize_quantity(capacity_value, capacity_unit)
        if q.unit != KIND_UNIT[kind]:
            raise RuleViolation(f"{kind} 容量单位应为 {KIND_UNIT[kind]} 族，收到 {capacity_unit}")
        resource = cls(cls.stream_for(resource_id))
        resource.record(
            "ResourceRegistered",
            {
                "resource_id": resource_id,
                "kind": kind,
                "capacity": q.value,
                "unit": q.unit,
                "owner_party_id": owner_party_id,
                "attributes": attributes or {},
                "at": at,
            },
        )
        return resource

    # ---------- 占用与释放（由组合原子流程调用） ----------
    def allocate(self, *, ref_id: str, qty: float, at: str) -> None:
        self._require_registered()
        if ref_id in self.allocations:
            raise RuleViolation(f"资源 {self.resource_id} 已被同一业务占用：{ref_id}")
        if qty <= 0:
            raise RuleViolation("占用量必须为正数")
        if self.available + 1e-9 < qty:
            raise RuleViolation(
                f"资源 {self.resource_id} 容量不足：需要 {qty}，可占用 {self.available}"
            )
        self.record(
            "QuantityAllocated",
            {"resource_id": self.resource_id, "ref_id": ref_id, "qty": qty, "at": at},
        )

    def increase_allocation(self, *, ref_id: str, delta: float, at: str) -> None:
        self._require_registered()
        allocation = self._require_allocation(ref_id)
        if delta <= 0:
            raise RuleViolation("增量必须为正数")
        if self.available + 1e-9 < delta:
            raise RuleViolation(
                f"资源 {self.resource_id} 容量不足：需要增加 {delta}，可占用 {self.available}"
            )
        self.record(
            "AllocationIncreased",
            {"resource_id": self.resource_id, "ref_id": ref_id, "delta": delta, "at": at},
        )

    def reduce_allocation(self, *, ref_id: str, release_delta: float, at: str, reason: str) -> None:
        """只释放未履行份额；release_delta 不得超过可释放量。"""
        self._require_registered()
        allocation = self._require_allocation(ref_id)
        if release_delta <= 0:
            raise RuleViolation("释放量必须为正数")
        if release_delta > allocation.releasable + 1e-9:
            raise RuleViolation(
                f"资源 {self.resource_id} 只能释放未履行份额 "
                f"{allocation.releasable}，请求 {release_delta}；已交接份额继续留存"
            )
        self.record(
            "AllocationReduced",
            {
                "resource_id": self.resource_id,
                "ref_id": ref_id,
                "delta": release_delta,
                "reason": reason,
                "at": at,
            },
        )

    def mark_fulfilled(self, *, ref_id: str, qty: float, at: str) -> None:
        """记录已交接量：不释放容量，但成为不可释放的留存份额。"""
        allocation = self._require_allocation(ref_id)
        if qty <= 0:
            raise RuleViolation("交接量必须为正数")
        if allocation.fulfilled + qty > allocation.qty + 1e-9:
            raise RuleViolation("交接量超过占用量")
        self.record(
            "AllocationFulfilled",
            {"resource_id": self.resource_id, "ref_id": ref_id, "qty": qty, "at": at},
        )

    # ---------- 查询 ----------
    @property
    def occupied(self) -> float:
        return round(sum(a.qty for a in self.allocations.values()), 6)

    @property
    def available(self) -> float:
        return round(self.capacity - self.occupied, 6)

    def allocation_for(self, ref_id: str) -> Allocation:
        return self._require_allocation(ref_id)

    def _require_registered(self) -> None:
        if self.resource_id is None:
            raise RuleViolation("资源尚未登记")

    def _require_allocation(self, ref_id: str) -> Allocation:
        allocation = self.allocations.get(ref_id)
        if allocation is None:
            raise NotFound(f"资源 {self.resource_id} 上不存在占用 {ref_id}")
        return allocation

    # ---------- 回放 ----------
    def apply(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        kind = event["type"]
        if kind == "ResourceRegistered":
            self.resource_id = p["resource_id"]
            self.kind = p["kind"]
            self.capacity = p["capacity"]
            self.unit = p["unit"]
            self.owner_party_id = p["owner_party_id"]
            self.attributes = p["attributes"]
        elif kind == "QuantityAllocated":
            self.allocations[p["ref_id"]] = Allocation(ref_id=p["ref_id"], qty=p["qty"], fulfilled=0.0)
        elif kind == "AllocationIncreased":
            a = self.allocations[p["ref_id"]]
            a.qty = round(a.qty + p["delta"], 6)
        elif kind == "AllocationReduced":
            a = self.allocations[p["ref_id"]]
            a.qty = round(a.qty - p["delta"], 6)
        elif kind == "AllocationFulfilled":
            a = self.allocations[p["ref_id"]]
            a.fulfilled = round(a.fulfilled + p["qty"], 6)
        else:  # pragma: no cover
            raise RuleViolation(f"未知资源事件：{kind}")
