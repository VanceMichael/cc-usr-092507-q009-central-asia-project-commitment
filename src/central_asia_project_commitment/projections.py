"""读模型投影：从全量事件流重建各聚合快照并产出项目接口视图。

系统恢复后只需重放事件日志即可重建全部状态，无需额外快照依赖。
"""

from __future__ import annotations

from typing import Any

from .commitments import Commitment, HELD, PROPOSED, TERMINAL_STATES
from .duediligence import DueDiligenceCase
from .intents import Intent
from .parties import Party
from .projects import Project
from .resources import LEDGER_STREAM, ResourceLedger
from .store import EventStore


class ReadModel:
    def __init__(self, store: EventStore):
        self.parties: dict[str, Party] = {}
        self.intents: dict[str, Intent] = {}
        self.commitments: dict[str, Commitment] = {}
        self.dd_cases: dict[str, DueDiligenceCase] = {}
        self.projects: dict[str, Project] = {}
        self.ledger = ResourceLedger()
        self.rebuild(store)

    def rebuild(self, store: EventStore) -> None:
        self.parties = {}
        self.intents = {}
        self.commitments = {}
        self.dd_cases = {}
        self.projects = {}
        self.ledger = ResourceLedger()
        buckets: dict[str, list[Any]] = {}
        for event in store.read_all():
            if event.stream == LEDGER_STREAM:
                self.ledger.apply_event(self.ledger, event.type, event.data)
                self.ledger.version = event.version
                continue
            buckets.setdefault(event.stream, []).append(event)
        for stream, events in buckets.items():
            if stream.startswith("party:"):
                state = Party().load(events)
                self.parties[state.party_id] = state
            elif stream.startswith("intent:"):
                state = Intent().load(events)
                self.intents[state.intent_id] = state
            elif stream.startswith("commitment:"):
                state = Commitment().load(events)
                self.commitments[state.commitment_id] = state
            elif stream.startswith("dd:"):
                state = DueDiligenceCase().load(events)
                self.dd_cases[state.case_id] = state
            elif stream.startswith("project:"):
                state = Project().load(events)
                self.projects[state.project_id] = state

    # ---- 项目接口：单一事实出口 ----
    def project_view(self, project_id: str, now_iso: str) -> dict[str, Any]:
        project = self.projects.get(project_id)
        related_commitments = [
            c for c in self.commitments.values()
            if c.project_id == project_id
            or c.intent_a_id in (project.linked_intents if project else [])
            or c.intent_b_id in (project.linked_intents if project else [])
        ]
        related_commitments.sort(key=lambda c: c.commitment_id)

        commitment_history = [self._commitment_summary(c) for c in related_commitments]
        active = [c for c in related_commitments if c.status in (PROPOSED, HELD)]

        # 资源缺口：活跃暂留中尚未履行的份额 + 任何待决资源冲突
        resource_gaps: list[dict[str, Any]] = []
        for c in active:
            for line in c.unfulfilled_lines():
                pool = self.ledger.pools.get(line["pool_id"])
                resource_gaps.append(
                    {
                        "commitment_id": c.commitment_id,
                        "pool_id": line["pool_id"],
                        "resource": pool.name if pool else line["pool_id"],
                        "unit": pool.unit if pool else "",
                        "unfulfilled_qty": line["qty"] - line["fulfilled"],
                    }
                )

        # 责任人：优先活跃承诺的当前责任人；否则秘书处
        responsible = ""
        responsible_detail = ""
        if active:
            picked = active[0]
            responsible = picked.current_responsible
            responsible_detail = (
                f"承诺 {picked.commitment_id}（{picked.status}）等待该联系人动作"
            )
        elif project:
            responsible = project.secretary_contact_id
            responsible_detail = "秘书处（无活跃承诺）"

        final_outcomes = [
            {
                "commitment_id": c.commitment_id,
                "final_state": c.final_state,
                "reason": c.final_reason,
            }
            for c in related_commitments
            if c.status in TERMINAL_STATES
        ]

        dd_summary = [
            {
                "case_id": cid,
                "open_grants": sum(
                    1 for g in case.grants.values() if g.status == "open"
                ),
                "material_versions": len(case.materials),
            }
            for cid, case in self.dd_cases.items()
            if project and case.project_id == project_id
        ]

        return {
            "project_id": project_id,
            "title": project.title if project else "",
            "generated_at": now_iso,
            "current_responsible": responsible,
            "responsible_detail": responsible_detail,
            "linked_intents": project.linked_intents if project else [],
            "resource_gaps": resource_gaps,
            "active_commitment_count": len(active),
            "commitment_history": commitment_history,
            "final_outcomes": final_outcomes,
            "due_diligence": dd_summary,
        }

    def _commitment_summary(self, c: Commitment) -> dict[str, Any]:
        return {
            "commitment_id": c.commitment_id,
            "status": c.status,
            "parties": [c.party_a_id, c.party_b_id],
            "party_versions": [c.party_a_version, c.party_b_version],
            "deadline": c.deadline,
            "bundle": c.bundle,
            "handovers_recorded": len(c.handovers),
            "fees_total": c.fees_total,
            "adjustments": [
                {"request_id": a.request_id, "kind": a.kind, "status": a.status,
                 "requested_by": a.requested_by, "reviewer": a.reviewer}
                for a in c.adjustments.values()
            ],
            "final_state": c.final_state,
            "final_reason": c.final_reason,
        }
