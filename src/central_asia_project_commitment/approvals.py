"""跨时区审批与人工调整四眼复核。

每个需要裁决的命令开一个审批案：
- 跨时区多方审批：所需各方各背书一次，按 UTC 时刻排序；
  任一拒绝即终结，全部批准才通过。结论一旦产生不可更改。
- 人工调整：发起人不能复核自己，必须由未参与发起的另一人复核。

审批案保存发起时各流的版本快照（basis_versions），执行命令时仍按该版本
乐观提交：若期间资源被他人占走，提交失败并得到唯一结果"版本过期"，
秘书处据此要求重新发起，而不是让两个时区各自得出结论。
"""

from __future__ import annotations

from typing import Any

from .errors import AuthorizationError, RuleViolation
from .repository import Aggregate

CASE_KINDS = ("cross_party", "manual_adjustment")


class ApprovalCase(Aggregate):
    stream_prefix = "apr"

    def __init__(self, stream_id: str) -> None:
        super().__init__(stream_id)
        self.case_id: str | None = None
        self.kind: str | None = None
        self.subject: dict[str, str] = {}
        self.command: str = ""
        self.payload: dict[str, Any] = {}
        self.basis_versions: dict[str, int] = {}
        self.requested_by: str | None = None
        self.requested_by_party: str | None = None
        self.required_parties: list[str] = []
        self.endorsements: list[dict[str, Any]] = []
        self.decision: str = "pending"   # pending | approved | rejected | stale
        self.decided_at: str | None = None
        self.reviewer: str | None = None
        self.executed: bool = False
        self.executed_at: str | None = None

    @classmethod
    def open(
        cls,
        case_id: str,
        *,
        kind: str,
        subject: dict[str, str],
        command: str,
        payload: dict[str, Any],
        basis_versions: dict[str, int],
        requested_by: str,
        requested_by_party: str,
        required_parties: list[str],
        at: str,
        zone: str,
    ) -> "ApprovalCase":
        if kind not in CASE_KINDS:
            raise RuleViolation(f"未知审批类型：{kind}")
        if not required_parties:
            raise RuleViolation("审批案至少需要一个裁决方")
        if kind == "cross_party" and requested_by_party not in required_parties:
            raise RuleViolation("发起方必须在裁决方名单内")
        case = cls(cls.stream_for(case_id))
        case.record(
            "CaseOpened",
            {
                "case_id": case_id,
                "kind": kind,
                "subject": subject,
                "command": command,
                "payload": payload,
                "basis_versions": dict(basis_versions),
                "requested_by": requested_by,
                "requested_by_party": requested_by_party,
                "required_parties": list(required_parties),
                "at": at,
                "zone": zone,
            },
        )
        return case

    def endorse(
        self, *, contact_ref: str, party_id: str, approve: bool, at: str, zone: str, comment: str = ""
    ) -> str:
        """登记一次跨时区背书，返回裁决后的决定。"""
        self._require_open_case()
        if self.decision != "pending":
            raise RuleViolation(f"审批案已有结论：{self.decision}")
        if party_id not in self.required_parties:
            raise AuthorizationError(f"{party_id} 不是本案裁决方")
        if any(e["party_id"] == party_id for e in self.endorsements):
            raise RuleViolation("该方已背书，不能重复背书")

        if self.kind == "manual_adjustment":
            # 四眼原则：复核人必须是未参与发起的另一个人；其一次复核即裁决。
            if contact_ref == self.requested_by:
                raise AuthorizationError("人工调整不能由发起人本人复核")
            self.record(
                "Endorsed",
                {
                    "case_id": self.case_id,
                    "contact_ref": contact_ref,
                    "party_id": party_id,
                    "decision": "approve" if approve else "reject",
                    "comment": comment,
                    "at": at,
                    "zone": zone,
                },
            )
            self.reviewer = contact_ref
            self._decide("approved" if approve else "rejected", at)
            return self.decision

        self.record(
            "Endorsed",
            {
                "case_id": self.case_id,
                "contact_ref": contact_ref,
                "party_id": party_id,
                "decision": "approve" if approve else "reject",
                "comment": comment,
                "at": at,
                "zone": zone,
            },
        )

        if not approve:
            self._decide("rejected", at)
            return self.decision

        approved_parties = {e["party_id"] for e in self.endorsements if e["decision"] == "approve"}
        if approved_parties == set(self.required_parties):
            self._decide("approved", at)
        return self.decision

    def mark_stale(self, *, at: str, latest_versions: dict[str, int]) -> None:
        """执行时发现依据版本已过期：给出唯一的"版本过期"结果。"""
        if self.decision != "approved":
            raise RuleViolation("只有已批准的审批案可以因版本过期关闭")
        self.record(
            "CaseMarkedStale",
            {"case_id": self.case_id, "at": at, "latest_versions": latest_versions},
        )

    def mark_executed(self, *, at: str) -> None:
        """批准命令已执行：防止同一审批案触发第二份承诺。"""
        if self.decision != "approved":
            raise RuleViolation("只有已批准的审批案可以标记执行")
        if self.executed:
            raise RuleViolation("审批案已执行，不能重复执行")
        self.record("CaseExecuted", {"case_id": self.case_id, "at": at})

    def _decide(self, decision: str, at: str) -> None:
        self.record("CaseDecided", {"case_id": self.case_id, "decision": decision, "at": at})

    def _require_open_case(self) -> None:
        if self.case_id is None:
            raise RuleViolation("审批案尚未建立")

    # ---------- 回放 ----------
    def apply(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        kind = event["type"]
        if kind == "CaseOpened":
            self.case_id = p["case_id"]
            self.kind = p["kind"]
            self.subject = p["subject"]
            self.command = p["command"]
            self.payload = p["payload"]
            self.basis_versions = dict(p["basis_versions"])
            self.requested_by = p["requested_by"]
            self.requested_by_party = p["requested_by_party"]
            self.required_parties = list(p["required_parties"])
        elif kind == "Endorsed":
            self.endorsements.append(dict(p))
        elif kind == "CaseDecided":
            self.decision = p["decision"]
            self.decided_at = p["at"]
        elif kind == "CaseMarkedStale":
            self.decision = "stale"
            self.decided_at = p["at"]
        elif kind == "CaseExecuted":
            self.executed = True
            self.executed_at = p["at"]
        else:  # pragma: no cover
            raise RuleViolation(f"未知审批事件：{kind}")
