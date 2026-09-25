"""项目聚合与项目接口读模型。

项目本身只登记参与方与秘书处责任人；项目接口所需的"当前责任人、资源缺口、
历次承诺、最终落地或终止原因"全部从组合原子、匹配快照、尽调案卷实时重建，
保证秘书处看到的是依据当前版本的单一事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import RuleViolation
from .repository import Aggregate


class Project(Aggregate):
    stream_prefix = "proj"

    def __init__(self, stream_id: str) -> None:
        super().__init__(stream_id)
        self.project_id: str | None = None
        self.title: str = ""
        self.secretary_contact_ref: str | None = None
        self.parties: list[str] = []
        self.dossier_ids: list[str] = []
        self.engagement_ids: list[str] = []
        self.status: str = "open"   # open | closed
        self.closed_reason: str | None = None

    @classmethod
    def open_project(
        cls,
        project_id: str,
        *,
        title: str,
        secretary_contact_ref: str,
        parties: list[str],
        at: str,
    ) -> "Project":
        if len(set(parties)) < 2:
            raise RuleViolation("项目至少登记两方主体")
        project = cls(cls.stream_for(project_id))
        project.record(
            "ProjectOpened",
            {
                "project_id": project_id,
                "title": title,
                "secretary_contact_ref": secretary_contact_ref,
                "parties": list(parties),
                "at": at,
            },
        )
        return project

    def attach_dossier(self, dossier_id: str) -> None:
        if dossier_id not in self.dossier_ids:
            self.record(
                "DossierAttached",
                {"project_id": self.project_id, "dossier_id": dossier_id},
            )

    def attach_engagement(self, engagement_id: str) -> None:
        if engagement_id not in self.engagement_ids:
            self.record(
                "EngagementAttached",
                {"project_id": self.project_id, "engagement_id": engagement_id},
            )

    def close(self, *, reason: str, at: str) -> None:
        if self.status != "open":
            raise RuleViolation("项目已关闭")
        self.record("ProjectClosed", {"project_id": self.project_id, "reason": reason, "at": at})

    def apply(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        kind = event["type"]
        if kind == "ProjectOpened":
            self.project_id = p["project_id"]
            self.title = p["title"]
            self.secretary_contact_ref = p["secretary_contact_ref"]
            self.parties = list(p["parties"])
            self.status = "open"
        elif kind == "DossierAttached":
            self.dossier_ids.append(p["dossier_id"])
        elif kind == "EngagementAttached":
            self.engagement_ids.append(p["engagement_id"])
        elif kind == "ProjectClosed":
            self.status = "closed"
            self.closed_reason = p["reason"]
        else:  # pragma: no cover
            raise RuleViolation(f"未知项目事件：{kind}")


@dataclass
class ProjectView:
    project_id: str
    title: str
    status: str
    secretary: str | None
    responsible: dict[str, str]
    resource_gaps: list[dict[str, Any]]
    resource_conflicts: list[dict[str, Any]]
    commitments: list[dict[str, Any]]
    diligence_todos: list[dict[str, Any]]
    final_outcome: dict[str, Any] | None


def build_project_view(
    project: Project,
    engagements: list[Any],
    dossiers: list[Any] | None = None,
    *,
    now: str,
) -> ProjectView:
    """从事件重建的聚合生成项目接口视图。"""
    responsible: dict[str, str] = {}
    gaps: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    commitments: list[dict[str, Any]] = []
    todos: list[dict[str, Any]] = []
    final_outcome = None

    for eng in engagements:
        responsible.update(eng.responsible)
        snapshot = eng.match_snapshot or {}
        if eng.status == "proposed":
            for gap in snapshot.get("gaps", []):
                gaps.append({"engagement_id": eng.engagement_id, **gap})
            for conflict in snapshot.get("conflicts", []):
                conflicts.append({"engagement_id": eng.engagement_id, **conflict})
        elif eng.status == "held":
            # 暂留中：缺口只看尚未齐备的部分（快照保留历史缺口以备追溯）
            if eng.expires_at and eng.expires_at <= now:
                conflicts.append(
                    {"engagement_id": eng.engagement_id, "reason": "暂留已到期待系统处理", "resource_id": None}
                )

        commitments.append(
            {
                "engagement_id": eng.engagement_id,
                "status": eng.status,
                "demand_party_id": eng.demand_party_id,
                "supply_party_id": eng.supply_party_id,
                "demand_intent_revision": eng.demand_intent_revision,
                "supply_intent_revision": eng.supply_intent_revision,
                "lines": [
                    {
                        "kind": l.kind,
                        "resource_id": l.resource_id,
                        "qty": l.qty,
                        "fulfilled": l.fulfilled,
                        "releasable": l.releasable,
                    }
                    for l in eng.lines
                ],
                "held_at": eng.held_at,
                "expires_at": eng.expires_at,
                "converted_at": eng.converted_at,
                "commitment_no": eng.terms.get("commitment_no"),
                "handovers": list(eng.handovers),
                "fees": list(eng.fees),
                "history": list(eng.history),
                "final_reason": eng.final_reason,
            }
        )
        if eng.status in ("landed", "terminated", "expired") and final_outcome is None:
            final_outcome = {
                "engagement_id": eng.engagement_id,
                "status": eng.status,
                "reason": eng.final_reason,
            }

    for dossier in dossiers or []:
        for grant in dossier.grants.values():
            if grant.status == "active":
                todos.append(
                    {
                        "dossier_id": dossier.dossier_id,
                        "grant_id": grant.grant_id,
                        "material_id": grant.material_id,
                        "party_id": grant.party_id,
                        "contact_id": grant.contact_id,
                        "purpose": grant.purpose,
                        "valid_until": grant.valid_until,
                        "overdue": grant.valid_until <= now,
                    }
                )

    return ProjectView(
        project_id=project.project_id,  # type: ignore[arg-type]
        title=project.title,
        status=project.status,
        secretary=project.secretary_contact_ref,
        responsible=responsible,
        resource_gaps=gaps,
        resource_conflicts=conflicts,
        commitments=commitments,
        diligence_todos=todos,
        final_outcome=final_outcome,
    )
