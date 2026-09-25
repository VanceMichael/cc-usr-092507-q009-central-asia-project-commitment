"""资源账本：土地、仓容、班列窗口、专家支持四类资源池。

占用规则：
- 承诺以组合原子一次性占用多条资源线，任一池容量不足则整组失败；
- 占用份额分 held / fulfilled / released：
  已交接（fulfilled）的份额继续占用、不可释放；
  改量、延期或终止只释放"尚未履行"的份额；
- 容量按占用时间区间相交计算，同一周班列窗口因此会互相争用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .aggregates import Aggregate
from .clock import parse
from .errors import ResourceConflict, StateConflict

RESOURCE_KINDS = ("land", "warehouse", "train_window", "expert")
LEDGER_STREAM = "resource-ledger"


@dataclass
class AllocationLine:
    pool_id: str
    qty_held: float
    qty_fulfilled: float = 0.0
    qty_released: float = 0.0
    start: str = ""
    end: str = ""

    @property
    def occupied(self) -> float:
        return self.qty_held - self.qty_released

    @property
    def unfulfilled(self) -> float:
        return self.qty_held - self.qty_fulfilled - self.qty_released


@dataclass
class ResourcePool:
    pool_id: str
    kind: str
    name: str
    unit: str
    total: float
    region: str = ""
    corridor: str = ""
    attrs: dict[str, Any] = field(default_factory=dict)


def _overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    if not start_a or not start_b:
        return True  # 未给区间视为长期占用，从严判定
    sa, ea = parse(start_a), parse(end_a) if end_a else parse(start_a)
    sb, eb = parse(start_b), parse(end_b) if end_b else parse(start_b)
    return sa <= eb and sb <= ea


@dataclass
class ResourceLedger(Aggregate):
    pools: dict[str, ResourcePool] = field(default_factory=dict)
    # commitment_id -> list[AllocationLine]
    allocations: dict[str, list[AllocationLine]] = field(default_factory=dict)
    # commitment_id -> 已发生的交接与费用记录，终止后继续留存
    handovers: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.stream_id:
            self.stream_id = LEDGER_STREAM

    # ---- 读取模型 ----
    def occupied(
        self, pool_id: str, start: str = "", end: str = "", exclude: str = ""
    ) -> float:
        total = 0.0
        for commitment_id, lines in self.allocations.items():
            if commitment_id == exclude:
                continue
            for line in lines:
                if line.pool_id != pool_id or line.occupied <= 0:
                    continue
                if _overlaps(start, end, line.start, line.end):
                    total += line.occupied
        return total

    def available(self, pool_id: str, start: str = "", end: str = "", exclude: str = "") -> float:
        pool = self.pools.get(pool_id)
        if pool is None:
            raise StateConflict(f"资源池不存在：{pool_id}")
        return pool.total - self.occupied(pool_id, start, end, exclude)

    def find_pool(self, kind: str, region: str = "", corridor: str = "") -> str | None:
        for pool in self.pools.values():
            if pool.kind != kind:
                continue
            if region and pool.region and pool.region != region:
                continue
            if corridor and pool.corridor and pool.corridor != corridor:
                continue
            return pool.pool_id
        return None

    def check_bundle(
        self,
        bundle: list[dict[str, Any]],
        exclude_commitment: str = "",
    ) -> list[dict[str, Any]]:
        """探测组合占用是否可行，返回冲突明细（不写入）。"""
        conflicts: list[dict[str, Any]] = []
        # 同一组合内对同一池的需求要合并计算
        merged: dict[str, dict[str, Any]] = {}
        for line in bundle:
            item = merged.setdefault(
                line["pool_id"],
                {"pool_id": line["pool_id"], "qty": 0.0, "start": line.get("start", ""),
                 "end": line.get("end", "")},
            )
            item["qty"] += line["qty"]
        for line in merged.values():
            pool_id = line["pool_id"]
            pool = self.pools.get(pool_id)
            if pool is None:
                conflicts.append({"pool_id": pool_id, "reason": "资源池不存在", "shortage": line["qty"]})
                continue
            available = self.available(
                pool_id, line["start"], line["end"], exclude=exclude_commitment
            )
            if available + 1e-9 < line["qty"]:
                contenders = self._contenders(pool_id, line["start"], line["end"], exclude_commitment)
                conflicts.append(
                    {
                        "pool_id": pool_id,
                        "kind": pool.kind,
                        "unit": pool.unit,
                        "requested": line["qty"],
                        "available": max(available, 0.0),
                        "shortage": line["qty"] - max(available, 0.0),
                        "interval": [line["start"], line["end"]],
                        "contenders": contenders,
                    }
                )
        return conflicts

    def _contenders(self, pool_id: str, start: str, end: str, exclude: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for commitment_id, lines in self.allocations.items():
            if commitment_id == exclude:
                continue
            for line in lines:
                if line.pool_id == pool_id and line.occupied > 0 and _overlaps(
                    start, end, line.start, line.end
                ):
                    result.append(
                        {"commitment_id": commitment_id, "occupied": line.occupied,
                         "interval": [line.start, line.end]}
                    )
        return result

    # ---- 命令 ----
    def register_pool(
        self, pool_id: str, kind: str, name: str, unit: str, total: float,
        region: str = "", corridor: str = "", attrs: dict[str, Any] | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        if pool_id in self.pools:
            raise StateConflict(f"资源池已存在：{pool_id}")
        if kind not in RESOURCE_KINDS:
            raise ValueError(f"未知资源类型：{kind}")
        return [
            (
                "ResourcePoolRegistered",
                {
                    "pool_id": pool_id, "kind": kind, "name": name, "unit": unit,
                    "total": total, "region": region, "corridor": corridor,
                    "attrs": attrs or {},
                },
            )
        ]

    def hold_bundle(
        self, commitment_id: str, bundle: list[dict[str, Any]]
    ) -> list[tuple[str, dict[str, Any]]]:
        """组合原子占用：有冲突则整组拒绝（由服务层先 check_bundle）。"""
        conflicts = self.check_bundle(bundle)
        if conflicts:
            raise ResourceConflict(conflicts)
        return [
            (
                "ResourceBundleHeld",
                {"commitment_id": commitment_id, "bundle": bundle},
            )
        ]

    def reduce_line(
        self, commitment_id: str, pool_id: str, release_qty: float, reason: str
    ) -> list[tuple[str, dict[str, Any]]]:
        """改量/终止：只释放尚未履行份额。"""
        line = self._line(commitment_id, pool_id)
        if release_qty > line.unfulfilled + 1e-9:
            raise StateConflict(
                f"可释放的未履行份额仅 {line.unfulfilled}，不能释放 {release_qty}"
                f"（已交接 {line.qty_fulfilled} 继续留存）"
            )
        return [
            (
                "ResourceLineReleased",
                {
                    "commitment_id": commitment_id,
                    "pool_id": pool_id,
                    "release_qty": release_qty,
                    "reason": reason,
                },
            )
        ]

    def increase_line(
        self, commitment_id: str, pool_id: str, delta: float, reason: str
    ) -> list[tuple[str, dict[str, Any]]]:
        """改量追加占用（容量冲突由调用方在锁内先 check_bundle）。"""
        self._line(commitment_id, pool_id)
        if delta <= 0:
            raise StateConflict("追加占用必须为正")
        return [
            (
                "ResourceLineIncreased",
                {
                    "commitment_id": commitment_id,
                    "pool_id": pool_id,
                    "delta": delta,
                    "reason": reason,
                },
            )
        ]

    def fulfill_line(
        self, commitment_id: str, pool_id: str, fulfill_qty: float, handover_ref: str, fee: float
    ) -> list[tuple[str, dict[str, Any]]]:
        """记录已履行交接与费用：份额继续占用，费用留存可查。"""
        line = self._line(commitment_id, pool_id)
        if fulfill_qty > line.unfulfilled + 1e-9:
            raise StateConflict(
                f"待履行份额仅 {line.unfulfilled}，不能登记交接 {fulfill_qty}"
            )
        return [
            (
                "ResourceLineFulfilled",
                {
                    "commitment_id": commitment_id,
                    "pool_id": pool_id,
                    "fulfill_qty": fulfill_qty,
                    "handover_ref": handover_ref,
                    "fee": fee,
                },
            )
        ]

    def extend_line(
        self, commitment_id: str, pool_id: str, new_end: str
    ) -> list[tuple[str, dict[str, Any]]]:
        """延期占用区间；延期后与他人争用也要整组冲突检查（由服务层先探测）。"""
        self._line(commitment_id, pool_id)
        return [
            (
                "ResourceLineExtended",
                {"commitment_id": commitment_id, "pool_id": pool_id, "new_end": new_end},
            )
        ]

    def _line(self, commitment_id: str, pool_id: str) -> AllocationLine:
        for line in self.allocations.get(commitment_id, []):
            if line.pool_id == pool_id:
                return line
        raise StateConflict(f"承诺 {commitment_id} 未占用资源池 {pool_id}")

    # ---- 应用事件 ----
    @classmethod
    def apply_event(cls, state: "ResourceLedger", event_type: str, data: dict[str, Any]) -> None:
        if event_type == "ResourcePoolRegistered":
            state.pools[data["pool_id"]] = ResourcePool(
                pool_id=data["pool_id"], kind=data["kind"], name=data["name"],
                unit=data["unit"], total=data["total"], region=data.get("region", ""),
                corridor=data.get("corridor", ""), attrs=data.get("attrs", {}),
            )
        elif event_type == "ResourceBundleHeld":
            lines = [
                AllocationLine(
                    pool_id=item["pool_id"], qty_held=item["qty"],
                    start=item.get("start", ""), end=item.get("end", ""),
                )
                for item in data["bundle"]
            ]
            state.allocations[data["commitment_id"]] = lines
        elif event_type == "ResourceLineReleased":
            state._line(data["commitment_id"], data["pool_id"]).qty_released += data[
                "release_qty"
            ]
        elif event_type == "ResourceLineIncreased":
            state._line(data["commitment_id"], data["pool_id"]).qty_held += data["delta"]
        elif event_type == "ResourceLineFulfilled":
            line = state._line(data["commitment_id"], data["pool_id"])
            line.qty_fulfilled += data["fulfill_qty"]
            state.handovers.setdefault(data["commitment_id"], []).append(
                {
                    "pool_id": data["pool_id"],
                    "fulfill_qty": data["fulfill_qty"],
                    "handover_ref": data["handover_ref"],
                    "fee": data["fee"],
                }
            )
        elif event_type == "ResourceLineExtended":
            state._line(data["commitment_id"], data["pool_id"]).end = data["new_end"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pools": {
                pid: {"kind": p.kind, "name": p.name, "unit": p.unit, "total": p.total,
                      "region": p.region, "corridor": p.corridor}
                for pid, p in self.pools.items()
            },
            "allocations": {
                cid: [
                    {"pool_id": l.pool_id, "held": l.qty_held, "fulfilled": l.qty_fulfilled,
                     "released": l.qty_released, "occupied": l.occupied,
                     "interval": [l.start, l.end]}
                    for l in lines
                ]
                for cid, lines in self.allocations.items()
            },
            "handovers": {
                cid: [dict(item) for item in records]
                for cid, records in self.handovers.items()
            },
        }
