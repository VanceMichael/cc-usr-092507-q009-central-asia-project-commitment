"""承诺暂留聚合。

生命周期：
    proposed（待双方确认）
      → held（双方确认，生成带截止时间的组合资源暂留）
      → landed / expired / terminated（终态）

核心不变量：
- 两个不同主体的授权联系人都确认后才进入 held，暂留截止时间随之确定；
- 土地/仓容/班列/专家支持作为一个组合原子占用，任一资源不足整组拒绝；
- 改量、延期、终止只释放"尚未履行"的份额；已交接数量与费用永久留存；
- 人工调整走"申请人—复核人"分离，复核人不得是发起人，也不得是申请人本人；
- 终态原因（落地/终止/过期）与历次调整、交接全部保留可查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .aggregates import Aggregate
from .errors import PolicyViolation, StateConflict

PROPOSED = "proposed"
HELD = "held"
EXPIRED = "expired"
TERMINATED = "terminated"
LANDED = "landed"
TERMINAL_STATES = {EXPIRED, TERMINATED, LANDED}


@dataclass
class Adjustment:
    request_id: str
    kind: str  # resize / extend / terminate
    requested_by: str
    payload: dict[str, Any]
    status: str = "pending"  # pending / approved / rejected
    reviewer: str = ""
    decided_at: str = ""
    note: str = ""


@dataclass
class Commitment(Aggregate):
    commitment_id: str = ""
    project_id: str = ""
    party_a_id: str = ""
    party_a_version: int = 0
    party_b_id: str = ""
    party_b_version: int = 0
    proposer_party_id: str = ""
    intent_a_id: str = ""
    intent_b_id: str = ""
    proposer_contact_id: str = ""
    expected_counterpart_contact_id: str = ""
    confirmations: dict[str, dict[str, Any]] = field(default_factory=dict)
    bundle: list[dict[str, Any]] = field(default_factory=list)
    status: str = ""
    deadline: str = ""
    ttl_seconds: int = 0
    match_snapshot: dict[str, Any] = field(default_factory=dict)
    handovers: list[dict[str, Any]] = field(default_factory=list)
    fees_total: float = 0.0
    adjustments: dict[str, Adjustment] = field(default_factory=dict)
    current_responsible: str = ""
    final_state: str = ""
    final_reason: str = ""
    timeline: list[dict[str, Any]] = field(default_factory=list)

    # ---- 命令 ----
    def propose(
        self,
        commitment_id: str,
        party_a_id: str,
        party_a_version: int,
        party_b_id: str,
        party_b_version: int,
        intent_a_id: str,
        intent_b_id: str,
        proposer_contact_id: str,
        counterpart_contact_id: str,
        bundle: list[dict[str, Any]],
        ttl_seconds: int,
        match_snapshot: dict[str, Any],
        project_id: str = "",
        proposer_party_id: str = "",
    ) -> list[tuple[str, dict[str, Any]]]:
        if self.version:
            raise StateConflict("承诺已存在")
        if party_a_id == party_b_id:
            raise PolicyViolation("承诺双方必须是不同主体")
        if proposer_contact_id == counterpart_contact_id:
            raise PolicyViolation("双方确认联系人不得为同一人")
        if not bundle:
            raise PolicyViolation("组合资源不能为空，承诺必须整体占用")
        if ttl_seconds <= 0:
            raise PolicyViolation("暂留必须有截止时间")
        if not match_snapshot.get("ready_to_hold"):
            raise PolicyViolation("候选匹配仍有缺口或资源冲突，不能发起承诺")
        self.stream_id = f"commitment:{commitment_id}"
        return [
            (
                "CommitmentProposed",
                {
                    "commitment_id": commitment_id,
                    "party_a_id": party_a_id,
                    "party_a_version": party_a_version,
                    "party_b_id": party_b_id,
                    "party_b_version": party_b_version,
                    "intent_a_id": intent_a_id,
                    "intent_b_id": intent_b_id,
                    "proposer_contact_id": proposer_contact_id,
                    "expected_counterpart_contact_id": counterpart_contact_id,
                    "proposer_party_id": proposer_party_id,
                    "bundle": bundle,
                    "ttl_seconds": ttl_seconds,
                    "match_snapshot": match_snapshot,
                    "project_id": project_id,
                },
            )
        ]

    def confirm(self, party_id: str, contact_id: str, at: str) -> list[tuple[str, dict[str, Any]]]:
        if self.status != PROPOSED:
            raise StateConflict(f"承诺状态为 {self.status}，等待确认阶段才能确认")
        if party_id not in (self.party_a_id, self.party_b_id):
            raise PolicyViolation("确认人不属于承诺双方")
        if party_id in self.confirmations:
            raise StateConflict(f"{party_id} 已确认，不能重复确认")
        expected_contact = (
            self.proposer_contact_id if party_id == self.proposer_party_id
            else self.expected_counterpart_contact_id
        )
        if contact_id != expected_contact:
            raise PolicyViolation(
                f"{party_id} 须由其授权联系人 {expected_contact} 确认，而非 {contact_id}"
            )
        events: list[tuple[str, dict[str, Any]]] = [
            ("CommitmentConfirmed", {"party_id": party_id, "contact_id": contact_id, "at": at})
        ]
        other_party = self.party_b_id if party_id == self.party_a_id else self.party_a_id
        if other_party in self.confirmations:
            # 本次确认后双方齐了：生成带截止时间的暂留
            events.append(
                (
                    "CommitmentHeld",
                    {"deadline": self._deadline_from(at), "at": at},
                )
            )
        return events

    def _deadline_from(self, at: str) -> str:
        from .clock import parse, to_iso

        return to_iso(parse(at) + _seconds(self.ttl_seconds))

    def register_handover(
        self, pool_id: str, qty: float, handover_ref: str, fee: float, at: str
    ) -> list[tuple[str, dict[str, Any]]]:
        if self.status != HELD:
            raise StateConflict("只有暂留中的承诺可以登记履约交接")
        line = self._line(pool_id)
        unfulfilled = line["qty"] - line["fulfilled"]
        if qty - unfulfilled > 1e-9:
            raise StateConflict(
                f"{pool_id} 未履行份额仅 {unfulfilled}，不能交接 {qty}"
            )
        return [
            (
                "CommitmentHandoverRegistered",
                {
                    "pool_id": pool_id, "qty": qty, "handover_ref": handover_ref,
                    "fee": fee, "at": at,
                },
            )
        ]

    # ---- 人工调整：申请 + 独立复核 ----
    def request_adjustment(
        self, request_id: str, kind: str, requested_by: str, payload: dict[str, Any], at: str
    ) -> list[tuple[str, dict[str, Any]]]:
        if self.status not in (PROPOSED, HELD):
            raise StateConflict(f"承诺已终态（{self.status}），不能再调整")
        if kind not in ("resize", "extend", "terminate"):
            raise ValueError(f"未知调整类型：{kind}")
        if request_id in self.adjustments:
            raise StateConflict(f"调整申请已存在：{request_id}")
        if kind == "resize":
            self._validate_resize(payload.get("bundle", []))
        if kind == "extend" and not payload.get("new_deadline"):
            raise PolicyViolation("延期必须给出新的截止时间")
        return [
            (
                "AdjustmentRequested",
                {
                    "request_id": request_id, "kind": kind,
                    "requested_by": requested_by, "payload": payload, "at": at,
                },
            )
        ]

    def approve_adjustment(
        self, request_id: str, reviewer_contact_id: str, at: str, note: str = ""
    ) -> list[tuple[str, dict[str, Any]]]:
        adjustment = self._pending_adjustment(request_id)
        # 发起/复核分离：复核人既不能是承诺发起人，也不能是这次调整的申请人
        if reviewer_contact_id == self.proposer_contact_id:
            raise PolicyViolation("参与发起的人不能复核该人工调整")
        if reviewer_contact_id == adjustment.requested_by:
            raise PolicyViolation("调整申请人不能复核自己的调整")
        return [
            (
                "AdjustmentApproved",
                {
                    "request_id": request_id,
                    "reviewer_contact_id": reviewer_contact_id,
                    "at": at, "note": note,
                    "kind": adjustment.kind,
                    "payload": adjustment.payload,
                },
            )
        ]

    def reject_adjustment(
        self, request_id: str, reviewer_contact_id: str, at: str, note: str
    ) -> list[tuple[str, dict[str, Any]]]:
        self._pending_adjustment(request_id)
        if reviewer_contact_id == self.proposer_contact_id:
            raise PolicyViolation("参与发起的人不能复核该人工调整")
        return [("AdjustmentRejected", {
            "request_id": request_id, "reviewer_contact_id": reviewer_contact_id,
            "at": at, "note": note,
        })]

    def _pending_adjustment(self, request_id: str) -> Adjustment:
        adjustment = self.adjustments.get(request_id)
        if adjustment is None:
            raise StateConflict(f"调整申请不存在：{request_id}")
        if adjustment.status != "pending":
            raise StateConflict(f"调整申请已处理：{adjustment.status}")
        return adjustment

    def _validate_resize(self, new_bundle: list[dict[str, Any]]) -> None:
        if not new_bundle:
            raise PolicyViolation("改量后的组合资源不能为空")
        old = {line["pool_id"]: line for line in self.bundle}
        for item in new_bundle:
            if item["pool_id"] not in old:
                raise PolicyViolation(
                    f"改量不能新增资源池 {item['pool_id']}；组合原子的构成不可替换"
                )
            if item["qty"] < 0:
                raise PolicyViolation("数量不能为负")
            if item["qty"] < old[item["pool_id"]]["fulfilled"] - 1e-9:
                raise PolicyViolation(
                    f"{item['pool_id']} 已交接 {old[item['pool_id']]['fulfilled']}，"
                    "改量后不能低于已履行份额"
                )

    # ---- 系统/终态命令 ----
    def expire(self, at: str) -> list[tuple[str, dict[str, Any]]]:
        if self.status != HELD:
            raise StateConflict("只有暂留中的承诺可以过期")
        return [("CommitmentExpired", {"at": at, "reason": "暂留截止时间到达未完成落地"})]

    def land(self, outcome_summary: str, at: str) -> list[tuple[str, dict[str, Any]]]:
        if self.status != HELD:
            raise StateConflict("只有暂留中的承诺可以登记落地")
        return [("CommitmentLanded", {"outcome_summary": outcome_summary, "at": at})]

    # ---- 读模型辅助 ----
    def _line(self, pool_id: str) -> dict[str, Any]:
        for line in self.bundle:
            if line["pool_id"] == pool_id:
                return line
        raise StateConflict(f"组合中不存在资源池 {pool_id}")

    def unfulfilled_lines(self) -> list[dict[str, Any]]:
        return [
            line for line in self.bundle
            if line["qty"] - line["fulfilled"] > 1e-9
        ]

    def is_expired_at(self, now_iso: str) -> bool:
        from .clock import parse

        return self.status == HELD and bool(self.deadline) and parse(now_iso) > parse(self.deadline)

    # ---- 事件应用 ----
    @classmethod
    def apply_event(cls, state: "Commitment", event_type: str, data: dict[str, Any]) -> None:
        if event_type == "CommitmentProposed":
            state.commitment_id = data["commitment_id"]
            state.project_id = data.get("project_id", "")
            state.party_a_id = data["party_a_id"]
            state.party_a_version = data["party_a_version"]
            state.party_b_id = data["party_b_id"]
            state.party_b_version = data["party_b_version"]
            state.intent_a_id = data["intent_a_id"]
            state.intent_b_id = data["intent_b_id"]
            state.proposer_contact_id = data["proposer_contact_id"]
            state.expected_counterpart_contact_id = data["expected_counterpart_contact_id"]
            state.proposer_party_id = data.get("proposer_party_id", state.party_a_id)
            state.bundle = [
                {**item, "fulfilled": 0.0, "released": 0.0} for item in data["bundle"]
            ]
            state.ttl_seconds = data["ttl_seconds"]
            state.match_snapshot = data["match_snapshot"]
            state.status = PROPOSED
            state.stream_id = f"commitment:{data['commitment_id']}"
            state.current_responsible = data["expected_counterpart_contact_id"]
            state.timeline.append({"event": event_type, "at": None})
        elif event_type == "CommitmentConfirmed":
            state.confirmations[data["party_id"]] = {
                "contact_id": data["contact_id"], "at": data["at"]
            }
            if len(state.confirmations) == 1:
                state.current_responsible = ""  # 等待另一方时由 proposed 逻辑展示
                first = next(iter(state.confirmations))
                waiting = state.party_b_id if first == state.party_a_id else state.party_a_id
                state.current_responsible = state._waiting_contact(waiting)
            state.timeline.append({"event": event_type, "party_id": data["party_id"], "at": data["at"]})
        elif event_type == "CommitmentHeld":
            state.status = HELD
            state.deadline = data["deadline"]
            state.current_responsible = state.proposer_contact_id
            state.timeline.append({"event": event_type, "deadline": data["deadline"], "at": data["at"]})
        elif event_type == "CommitmentHandoverRegistered":
            line = state._line(data["pool_id"])
            line["fulfilled"] += data["qty"]
            state.handovers.append(dict(data))
            state.fees_total += data.get("fee", 0.0)
            state.timeline.append({"event": event_type, "at": data["at"]})
        elif event_type == "AdjustmentRequested":
            state.adjustments[data["request_id"]] = Adjustment(
                request_id=data["request_id"], kind=data["kind"],
                requested_by=data["requested_by"], payload=data["payload"],
            )
            state.timeline.append({"event": event_type, "request_id": data["request_id"], "at": data["at"]})
        elif event_type in ("AdjustmentApproved", "AdjustmentRejected"):
            adjustment = state.adjustments[data["request_id"]]
            adjustment.status = "approved" if event_type == "AdjustmentApproved" else "rejected"
            adjustment.reviewer = data["reviewer_contact_id"]
            adjustment.decided_at = data["at"]
            adjustment.note = data.get("note", "")
            state.timeline.append({"event": event_type, "request_id": data["request_id"], "at": data["at"]})
            if event_type == "AdjustmentApproved":
                state._apply_approved_adjustment(adjustment, data["at"])
        elif event_type == "CommitmentExpired":
            state.status = EXPIRED
            state.final_state = EXPIRED
            state.final_reason = data["reason"]
            state.current_responsible = ""
            state.timeline.append({"event": event_type, "at": data["at"]})
        elif event_type == "CommitmentTerminated":
            state.status = TERMINATED
            state.final_state = TERMINATED
            state.final_reason = data["reason"]
            state.current_responsible = ""
            state.timeline.append({"event": event_type, "at": data["at"]})
        elif event_type == "CommitmentLanded":
            state.status = LANDED
            state.final_state = LANDED
            state.final_reason = data["outcome_summary"]
            state.current_responsible = ""
            state.timeline.append({"event": event_type, "at": data["at"]})

    def _waiting_contact(self, waiting_party: str) -> str:
        if waiting_party == self.party_b_id:
            return self.expected_counterpart_contact_id
        return self.proposer_contact_id

    def _apply_approved_adjustment(self, adjustment: Adjustment, at: str) -> None:
        """审批事件同时承载调整生效结果；资源数量的释放/延期由服务层配套写账本。"""
        if adjustment.kind == "resize":
            new_bundle = adjustment.payload["bundle"]
            wanted = {item["pool_id"]: item["qty"] for item in new_bundle}
            for line in self.bundle:
                old_qty = line["qty"]
                new_qty = wanted[line["pool_id"]]
                if new_qty < old_qty:
                    line["released"] += old_qty - new_qty
                line["qty"] = new_qty
            self.timeline.append({"event": "ResizeEffected", "at": at})
        elif adjustment.kind == "extend":
            self.deadline = adjustment.payload["new_deadline"]
            for line in self.bundle:
                if "new_end" in adjustment.payload:
                    line["end"] = adjustment.payload["new_end"]
            self.timeline.append({"event": "ExtendEffected", "at": at, "new_deadline": self.deadline})
        elif adjustment.kind == "terminate":
            self.status = TERMINATED
            self.final_state = TERMINATED
            self.final_reason = adjustment.payload.get("reason", "人工终止")
            self.current_responsible = ""
            self.timeline.append({"event": "TerminateEffected", "at": at})

    def to_dict(self) -> dict[str, Any]:
        return {
            "commitment_id": self.commitment_id,
            "status": self.status,
            "party_a": {"party_id": self.party_a_id, "version": self.party_a_version},
            "party_b": {"party_id": self.party_b_id, "version": self.party_b_version},
            "intents": [self.intent_a_id, self.intent_b_id],
            "proposer_contact_id": self.proposer_contact_id,
            "confirmations": self.confirmations,
            "deadline": self.deadline,
            "current_responsible": self.current_responsible,
            "bundle": self.bundle,
            "unfulfilled": [
                {"pool_id": l["pool_id"],
                 "qty": l["qty"] - l["fulfilled"]}
                for l in self.unfulfilled_lines()
            ],
            "handovers": self.handovers,
            "fees_total": self.fees_total,
            "adjustments": [
                {
                    "request_id": a.request_id, "kind": a.kind,
                    "requested_by": a.requested_by, "status": a.status,
                    "reviewer": a.reviewer, "note": a.note,
                }
                for a in self.adjustments.values()
            ],
            "final_state": self.final_state,
            "final_reason": self.final_reason,
            "match_snapshot": self.match_snapshot,
        }


def _seconds(value: int):
    from datetime import timedelta

    return timedelta(seconds=value)
