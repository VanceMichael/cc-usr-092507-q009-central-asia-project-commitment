"""组合原子：土地、仓容、班列窗口、专家支持作为一个不可分割的占用单元。

生命周期：
    proposed（候选快照，尚未占资源）
      → 双方确认 → held（有截止时间的暂留，四类资源原子占用）
      → converted（暂留转承诺）→ landed（最终落地）
      → expired / terminated（只释放未履行份额）

改量、延期、终止都作用在整个组合上；已发生的交接与费用是不可变事实，
终止后继续留存。资源事件由应用服务与本聚合在同一提交内写入，
因此"要么四类一起占住，要么一类都不占"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import RuleViolation
from .repository import Aggregate
from .resources import KIND_UNIT, RESOURCE_KINDS

STATUSES = ("proposed", "held", "converted", "landed", "expired", "terminated")


@dataclass
class Line:
    kind: str
    resource_id: str
    qty: float                       # 当前暂留/承诺量（基准单位）
    fulfilled: float = 0.0           # 已交接量（终止不释放）

    @property
    def releasable(self) -> float:
        return round(self.qty - self.fulfilled, 6)


class Engagement(Aggregate):
    stream_prefix = "eng"

    def __init__(self, stream_id: str) -> None:
        super().__init__(stream_id)
        self.engagement_id: str | None = None
        self.project_id: str | None = None
        self.status: str | None = None
        self.demand_party_id: str | None = None
        self.supply_party_id: str | None = None
        self.demand_intent_revision: int = 0
        self.supply_intent_revision: int = 0
        self.lines: list[Line] = []
        self.match_snapshot: dict[str, Any] = {}
        self.confirmations: dict[str, str] = {}   # party_id -> 确认时刻
        self.expires_at: str | None = None
        self.held_at: str | None = None
        self.converted_at: str | None = None
        self.terms: dict[str, Any] = {}
        self.handovers: list[dict[str, Any]] = []
        self.fees: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.responsible: dict[str, str] = {}
        self.final_reason: str | None = None
        self.reminders: list[dict[str, Any]] = []

    # ---------- 候选 ----------
    @classmethod
    def propose(
        cls,
        engagement_id: str,
        *,
        project_id: str,
        demand_party_id: str,
        supply_party_id: str,
        demand_intent_revision: int,
        supply_intent_revision: int,
        match_snapshot: dict[str, Any],
        lines: list[dict[str, Any]],
        proposed_expires_at: str,
        responsible: dict[str, str],
        at: str,
    ) -> "Engagement":
        cls._validate_lines(lines)
        if not match_snapshot.get("satisfied"):
            raise RuleViolation("候选匹配没有任何满足项，不能发起")
        engagement = cls(cls.stream_for(engagement_id))
        engagement.record(
            "EngagementProposed",
            {
                "engagement_id": engagement_id,
                "project_id": project_id,
                "demand_party_id": demand_party_id,
                "supply_party_id": supply_party_id,
                "demand_intent_revision": demand_intent_revision,
                "supply_intent_revision": supply_intent_revision,
                "match_snapshot": match_snapshot,
                "lines": lines,
                "proposed_expires_at": proposed_expires_at,
                "responsible": responsible,
                "at": at,
            },
        )
        return engagement

    @staticmethod
    def _validate_lines(lines: list[dict[str, Any]]) -> None:
        """候选阶段允许只有部分资源行，其余行的障碍应出现在缺口/冲突清单。"""
        kinds = [l["kind"] for l in lines]
        if not kinds:
            raise RuleViolation("候选至少包含一项组合资源")
        if any(k not in RESOURCE_KINDS for k in kinds):
            raise RuleViolation("组合资源只能是土地、仓容、班列窗口、专家支持")
        if len(kinds) != len(set(kinds)):
            raise RuleViolation("组合原子内资源类别重复")
        for line in lines:
            if line["qty"] <= 0:
                raise RuleViolation("组合资源数量必须为正数")
            if KIND_UNIT[line["kind"]] != line.get("unit"):
                raise RuleViolation(f"{line['kind']} 数量单位必须为 {KIND_UNIT[line['kind']]}")

    # ---------- 确认与暂留 ----------
    def confirm(self, *, party_id: str, at: str, zone: str = "UTC") -> bool:
        """记录一方确认。双方都确认时返回 True，由服务侧原子占用资源。"""
        self._require_status("proposed")
        if party_id not in (self.demand_party_id, self.supply_party_id):
            raise RuleViolation("只有候选双方可以确认")
        if party_id in self.confirmations:
            raise RuleViolation("该方已确认，不能重复确认")
        self.record("PartyConfirmed", {"engagement_id": self.engagement_id, "party_id": party_id,
                                       "at": at, "zone": zone})
        return len(self.confirmations) == 2

    def hold(self, *, expires_at: str, at: str) -> None:
        """第二次确认成功后：生成有截止时间的暂留。资源占用与本事件同提交。"""
        self._require_status("proposed")
        if len(self.confirmations) != 2:
            raise RuleViolation("双方未全部确认，不能暂留")
        if {l.kind for l in self.lines} != set(RESOURCE_KINDS):
            raise RuleViolation("土地、仓容、班列、专家四类未齐备，不能生成组合暂留")
        if expires_at <= at:
            raise RuleViolation("暂留截止时间必须晚于当前时间")
        self.record(
            "EngagementHeld",
            {"engagement_id": self.engagement_id, "expires_at": expires_at, "at": at},
        )

    def expire(self, *, at: str) -> None:
        """暂留到期：状态转 expired，未履行份额由服务侧释放。"""
        self._require_status("held")
        if self.expires_at is not None and at < self.expires_at:
            raise RuleViolation("暂留尚未到期")
        self.record(
            "EngagementExpired",
            {
                "engagement_id": self.engagement_id,
                "released": [
                    {"kind": l.kind, "resource_id": l.resource_id, "qty": l.releasable}
                    for l in self.lines
                    if l.releasable > 0
                ],
                "retained_fulfilled": [
                    {"kind": l.kind, "resource_id": l.resource_id, "qty": l.fulfilled}
                    for l in self.lines
                    if l.fulfilled > 0
                ],
                "at": at,
            },
        )

    # ---------- 承诺 ----------
    def convert(self, *, terms: dict[str, Any], at: str) -> None:
        self._require_status("held")
        required = {"commitment_no", "obligations"}
        if not required.issubset(terms):
            raise RuleViolation(f"承诺条款缺少：{required - set(terms)}")
        self.record(
            "EngagementConverted",
            {
                "engagement_id": self.engagement_id,
                "commitment_no": terms["commitment_no"],
                "terms": terms,
                "at": at,
            },
        )

    def record_handover(self, *, kind: str, qty: float, detail: str, at: str) -> None:
        """已发生的交接是不可变事实；终止/改量都不能抹去。"""
        if self.status not in ("converted", "held"):
            raise RuleViolation("只有暂留或承诺中的组合可以记录交接")
        line = self._line(kind)
        if line.fulfilled + qty > line.qty + 1e-9:
            raise RuleViolation(f"{kind} 交接量超过占用量")
        self.record(
            "HandoverRecorded",
            {
                "engagement_id": self.engagement_id,
                "kind": kind,
                "resource_id": line.resource_id,
                "qty": qty,
                "detail": detail,
                "at": at,
            },
        )

    def record_fee(self, *, amount: float, currency: str, purpose: str, at: str) -> None:
        """已发生费用继续留存，任何后续操作都不删除。"""
        if self.status not in ("converted", "held", "landed", "terminated"):
            raise RuleViolation("当前状态不能记录费用")
        if amount <= 0 or not purpose:
            raise RuleViolation("费用金额与用途必须有效")
        self.record(
            "FeeRecorded",
            {
                "engagement_id": self.engagement_id,
                "amount": amount,
                "currency": currency,
                "purpose": purpose,
                "at": at,
            },
        )

    # ---------- 改量 / 延期 / 终止 / 落地 ----------
    def change_quantities(self, *, changes: dict[str, float], reason: str, at: str) -> dict[str, dict[str, float]]:
        """改量。返回每种资源的 {increase, reduce}，由服务侧同步资源库存。

        只能减少未履行份额（reduce 不得超过 releasable）；增加量受库存约束，
        库存不足由资源聚合在同一提交中拒绝，整个改量回滚。
        """
        if self.status not in ("held", "converted"):
            raise RuleViolation("只有暂留或承诺中的组合可以改量")
        if not reason:
            raise RuleViolation("改量必须说明原因")
        deltas: dict[str, dict[str, float]] = {}
        for kind, new_qty in changes.items():
            line = self._line(kind)
            if new_qty <= 0:
                raise RuleViolation("改量后数量必须为正数")
            if new_qty < line.fulfilled - 1e-9:
                raise RuleViolation(
                    f"{kind} 不能改到已交接量 {line.fulfilled} 以下，已交接份额继续留存"
                )
            delta = round(new_qty - line.qty, 6)
            deltas[kind] = {"increase": max(delta, 0.0), "reduce": max(-delta, 0.0)}
        self.record(
            "QuantitiesChanged",
            {
                "engagement_id": self.engagement_id,
                "changes": changes,
                "deltas": deltas,
                "reason": reason,
                "at": at,
            },
        )
        return deltas

    def extend(self, *, new_expires_at: str, reason: str, at: str) -> None:
        if self.status not in ("held", "converted"):
            raise RuleViolation("只有暂留或承诺中的组合可以延期")
        current_deadline = self.terms.get("due_at") if self.status == "converted" else self.expires_at
        if current_deadline is not None and new_expires_at <= current_deadline:
            raise RuleViolation("延期只能把截止时间向后推")
        if not reason:
            raise RuleViolation("延期必须说明原因")
        self.record(
            "EngagementExtended",
            {"engagement_id": self.engagement_id, "new_expires_at": new_expires_at, "reason": reason, "at": at},
        )

    def terminate(self, *, reason: str, at: str) -> None:
        if self.status not in ("held", "converted"):
            raise RuleViolation("只有暂留或承诺中的组合可以终止")
        if not reason:
            raise RuleViolation("终止必须说明原因")
        self.record(
            "EngagementTerminated",
            {
                "engagement_id": self.engagement_id,
                "reason": reason,
                "released": [
                    {"kind": l.kind, "resource_id": l.resource_id, "qty": l.releasable}
                    for l in self.lines
                    if l.releasable > 0
                ],
                "retained_fulfilled": [
                    {"kind": l.kind, "resource_id": l.resource_id, "qty": l.fulfilled}
                    for l in self.lines
                    if l.fulfilled > 0
                ],
                "retained_fees": len(self.fees),
                "at": at,
            },
        )

    def land(self, *, reason: str, at: str) -> None:
        self._require_status("converted")
        if not reason:
            raise RuleViolation("落地必须记录最终原因")
        self.record("EngagementLanded", {"engagement_id": self.engagement_id, "reason": reason, "at": at})

    def assign_responsible(self, *, role: str, contact_ref: str, at: str) -> None:
        self.record(
            "ResponsibleAssigned",
            {"engagement_id": self.engagement_id, "role": role, "contact_ref": contact_ref, "at": at},
        )

    def schedule_reminder(self, *, reminder_id: str, kind: str, due_at: str, at: str) -> None:
        """登记履约提醒待办；恢复后由系统继续处理。"""
        if any(r["reminder_id"] == reminder_id for r in self.reminders):
            raise RuleViolation("履约提醒已登记")
        self.record(
            "ReminderScheduled",
            {
                "engagement_id": self.engagement_id,
                "reminder_id": reminder_id,
                "kind": kind,
                "due_at": due_at,
                "at": at,
            },
        )

    def mark_reminder_sent(self, *, reminder_id: str, at: str) -> bool:
        """标记提醒已发出。重复恢复处理时返回 False，不会产生第二份通知。"""
        reminder = next((r for r in self.reminders if r["reminder_id"] == reminder_id), None)
        if reminder is None:
            raise RuleViolation(f"履约提醒不存在：{reminder_id}")
        if reminder["status"] == "sent":
            return False
        self.record("ReminderSent", {"engagement_id": self.engagement_id, "reminder_id": reminder_id, "at": at})
        return True

    # ---------- 查询 ----------
    def releasable_lines(self) -> list[Line]:
        return [l for l in self.lines if l.releasable > 0]

    def _line(self, kind: str) -> Line:
        for line in self.lines:
            if line.kind == kind:
                return line
        raise RuleViolation(f"组合中不存在该类资源：{kind}")

    def _require_status(self, status: str) -> None:
        if self.status != status:
            raise RuleViolation(f"组合当前状态为 {self.status}，需要 {status}")

    # ---------- 回放 ----------
    def apply(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        kind = event["type"]
        if kind == "EngagementProposed":
            self.engagement_id = p["engagement_id"]
            self.project_id = p["project_id"]
            self.status = "proposed"
            self.demand_party_id = p["demand_party_id"]
            self.supply_party_id = p["supply_party_id"]
            self.demand_intent_revision = p["demand_intent_revision"]
            self.supply_intent_revision = p["supply_intent_revision"]
            self.match_snapshot = p["match_snapshot"]
            self.lines = [Line(x["kind"], x["resource_id"], x["qty"]) for x in p["lines"]]
            self.expires_at = p["proposed_expires_at"]
            self.responsible = dict(p["responsible"])
        elif kind == "PartyConfirmed":
            self.confirmations[p["party_id"]] = p["at"]
        elif kind == "EngagementHeld":
            self.status = "held"
            self.held_at = p["at"]
            self.expires_at = p["expires_at"]
        elif kind == "EngagementExpired":
            self.status = "expired"
            self.final_reason = "暂留到期未转承诺"
            for line in self.lines:
                line.qty = line.fulfilled
        elif kind == "EngagementConverted":
            self.status = "converted"
            self.converted_at = p["at"]
            self.terms = p["terms"]
        elif kind == "HandoverRecorded":
            self._line(p["kind"]).fulfilled = round(self._line(p["kind"]).fulfilled + p["qty"], 6)
            self.handovers.append(dict(p))
        elif kind == "FeeRecorded":
            self.fees.append(dict(p))
        elif kind == "QuantitiesChanged":
            for resource_kind, new_qty in p["changes"].items():
                self._line(resource_kind).qty = new_qty
            self.history.append({"type": "change_quantities", **p})
        elif kind == "EngagementExtended":
            if self.status == "converted":
                self.terms = {**self.terms, "due_at": p["new_expires_at"]}
            else:
                self.expires_at = p["new_expires_at"]
            self.history.append({"type": "extend", **p})
        elif kind == "EngagementTerminated":
            self.status = "terminated"
            self.final_reason = p["reason"]
            for line in self.lines:
                line.qty = line.fulfilled
            self.history.append({"type": "terminate", **p})
        elif kind == "EngagementLanded":
            self.status = "landed"
            self.final_reason = p["reason"]
        elif kind == "ResponsibleAssigned":
            self.responsible[p["role"]] = p["contact_ref"]
        elif kind == "ReminderScheduled":
            self.reminders.append(
                {"reminder_id": p["reminder_id"], "kind": p["kind"], "due_at": p["due_at"],
                 "scheduled_at": p["at"], "status": "pending"}
            )
        elif kind == "ReminderSent":
            reminder = next(r for r in self.reminders if r["reminder_id"] == p["reminder_id"])
            reminder["status"] = "sent"
            reminder["sent_at"] = p["at"]
        else:  # pragma: no cover
            raise RuleViolation(f"未知组合事件：{kind}")
