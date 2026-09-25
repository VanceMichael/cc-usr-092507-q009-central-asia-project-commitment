"""尽调授权案件。

- 每份尽调材料按"用途 + 到期时间 + 被授权联系人"授权，超范围使用被拒；
- 译文更正保留原文：文档版本链中原文永不删除，更正以新版本叠加；
- 联系人离任时，只终止其"尚未完成"的访问；已完成的访问记录留存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .aggregates import Aggregate
from .clock import parse
from .errors import PolicyViolation, StateConflict

GRANT_OPEN = "open"
GRANT_TERMINATED = "terminated"
GRANT_EXPIRED = "expired"

VISIT_PENDING = "pending"
VISIT_DONE = "done"


@dataclass
class MaterialVersion:
    version_no: int
    language: str
    title: str
    ref: str
    corrects_version: int | None = None
    note: str = ""
    at: str = ""


@dataclass
class Grant:
    grant_id: str
    contact_id: str
    purpose: str
    material_refs: list[str]
    expires_at: str
    status: str = GRANT_OPEN
    visits: list[dict[str, Any]] = field(default_factory=list)
    terminated_at: str | None = None
    terminate_reason: str = ""


@dataclass
class DueDiligenceCase(Aggregate):
    case_id: str = ""
    project_id: str = ""
    owner_party_id: str = ""
    materials: list[MaterialVersion] = field(default_factory=list)
    grants: dict[str, Grant] = field(default_factory=dict)

    # ---- 命令 ----
    def open_case(self, case_id: str, project_id: str, owner_party_id: str) -> list[tuple[str, dict[str, Any]]]:
        if self.version:
            raise StateConflict("尽调案件已存在")
        self.stream_id = f"dd:{case_id}"
        return [
            (
                "DDCaseOpened",
                {"case_id": case_id, "project_id": project_id, "owner_party_id": owner_party_id},
            )
        ]

    def add_material(
        self, language: str, title: str, ref: str, at: str,
        corrects_version: int | None = None, note: str = "",
    ) -> list[tuple[str, dict[str, Any]]]:
        if corrects_version is not None and not (
            1 <= corrects_version <= len(self.materials)
        ):
            raise StateConflict(f"被更正的原文版本不存在：{corrects_version}")
        version_no = len(self.materials) + 1
        return [
            (
                "DDMaterialAdded",
                {
                    "version_no": version_no, "language": language, "title": title,
                    "ref": ref, "corrects_version": corrects_version,
                    "note": note, "at": at,
                },
            )
        ]

    def grant_access(
        self, grant_id: str, contact_id: str, purpose: str,
        material_refs: list[str], expires_at: str,
    ) -> list[tuple[str, dict[str, Any]]]:
        if not self.version:
            raise StateConflict("尽调案件尚未建立")
        if not purpose:
            raise PolicyViolation("授权必须声明用途")
        if not material_refs:
            raise PolicyViolation("授权必须指定材料范围")
        unknown = set(material_refs) - {m.ref for m in self.materials}
        if unknown:
            raise PolicyViolation(f"材料不存在：{sorted(unknown)}")
        if grant_id in self.grants:
            raise StateConflict(f"授权已存在：{grant_id}")
        return [
            (
                "DDGrantIssued",
                {
                    "grant_id": grant_id, "contact_id": contact_id, "purpose": purpose,
                    "material_refs": material_refs, "expires_at": expires_at,
                },
            )
        ]

    def request_visit(
        self, grant_id: str, visit_id: str, purpose: str, material_ref: str, now_iso: str
    ) -> list[tuple[str, dict[str, Any]]]:
        grant = self._live_grant(grant_id, now_iso)
        if purpose != grant.purpose:
            raise PolicyViolation(
                f"访问用途 '{purpose}' 超出授权用途 '{grant.purpose}'"
            )
        if material_ref not in grant.material_refs:
            raise PolicyViolation(f"材料 {material_ref} 不在授权范围内")
        return [
            (
                "DDVisitRequested",
                {
                    "grant_id": grant_id, "visit_id": visit_id, "purpose": purpose,
                    "material_ref": material_ref, "at": now_iso,
                },
            )
        ]

    def complete_visit(self, visit_id: str, at: str) -> list[tuple[str, dict[str, Any]]]:
        grant_id, visit = self._find_visit(visit_id)
        if visit["status"] != VISIT_PENDING:
            raise StateConflict("访问已结束")
        return [("DDVisitCompleted", {"grant_id": grant_id, "visit_id": visit_id, "at": at})]

    def revoke_for_contact_departure(
        self, contact_id: str, at: str
    ) -> list[tuple[str, dict[str, Any]]]:
        """联系人离任：只终止尚未完成的访问/授权；已完成访问记录保留。"""
        events: list[tuple[str, dict[str, Any]]] = []
        for grant in self.grants.values():
            if grant.contact_id != contact_id or grant.status != GRANT_OPEN:
                continue
            has_pending = any(v["status"] == VISIT_PENDING for v in grant.visits)
            if has_pending:
                events.append(
                    (
                        "DDGrantTerminated",
                        {
                            "grant_id": grant.grant_id, "at": at,
                            "reason": "授权联系人离任，终止尚未完成的访问",
                            "pending_visits": [
                                v["visit_id"] for v in grant.visits
                                if v["status"] == VISIT_PENDING
                            ],
                        },
                    )
                )
        return events

    def expire_due(self, now_iso: str) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        for grant in self.grants.values():
            if grant.status == GRANT_OPEN and parse(now_iso) > parse(grant.expires_at):
                events.append(
                    ("DDGrantExpired", {"grant_id": grant.grant_id, "at": now_iso})
                )
        return events

    def pending_todos(self, now_iso: str) -> list[dict[str, Any]]:
        todos: list[dict[str, Any]] = []
        for grant in self.grants.values():
            if grant.status != GRANT_OPEN or parse(now_iso) > parse(grant.expires_at):
                continue
            for visit in grant.visits:
                if visit["status"] == VISIT_PENDING:
                    todos.append(
                        {
                            "case_id": self.case_id, "grant_id": grant.grant_id,
                            "visit_id": visit["visit_id"],
                            "contact_id": grant.contact_id,
                            "purpose": grant.purpose,
                            "material_ref": visit["material_ref"],
                            "requested_at": visit["requested_at"],
                        }
                    )
        return todos

    def _live_grant(self, grant_id: str, now_iso: str) -> Grant:
        grant = self.grants.get(grant_id)
        if grant is None:
            raise StateConflict(f"授权不存在：{grant_id}")
        if grant.status == GRANT_TERMINATED:
            raise PolicyViolation("授权已终止（联系人离任或人工撤销）")
        if parse(now_iso) > parse(grant.expires_at):
            raise PolicyViolation("授权已到期")
        return grant

    def _find_visit(self, visit_id: str) -> tuple[str, dict[str, Any]]:
        for grant in self.grants.values():
            for visit in grant.visits:
                if visit["visit_id"] == visit_id:
                    return grant.grant_id, visit
        raise StateConflict(f"访问不存在：{visit_id}")

    # ---- 事件应用 ----
    @classmethod
    def apply_event(cls, state: "DueDiligenceCase", event_type: str, data: dict[str, Any]) -> None:
        if event_type == "DDCaseOpened":
            state.case_id = data["case_id"]
            state.project_id = data["project_id"]
            state.owner_party_id = data["owner_party_id"]
            state.stream_id = f"dd:{data['case_id']}"
        elif event_type == "DDMaterialAdded":
            state.materials.append(
                MaterialVersion(
                    version_no=data["version_no"], language=data["language"],
                    title=data["title"], ref=data["ref"],
                    corrects_version=data.get("corrects_version"),
                    note=data.get("note", ""), at=data.get("at", ""),
                )
            )
        elif event_type == "DDGrantIssued":
            state.grants[data["grant_id"]] = Grant(
                grant_id=data["grant_id"], contact_id=data["contact_id"],
                purpose=data["purpose"], material_refs=list(data["material_refs"]),
                expires_at=data["expires_at"],
            )
        elif event_type == "DDVisitRequested":
            state.grants[data["grant_id"]].visits.append(
                {
                    "visit_id": data["visit_id"], "purpose": data["purpose"],
                    "material_ref": data["material_ref"], "requested_at": data["at"],
                    "status": VISIT_PENDING,
                }
            )
        elif event_type == "DDVisitCompleted":
            visit = state.grants[data["grant_id"]]
            for item in visit.visits:
                if item["visit_id"] == data["visit_id"]:
                    item["status"] = VISIT_DONE
                    item["completed_at"] = data["at"]
        elif event_type == "DDGrantTerminated":
            grant = state.grants[data["grant_id"]]
            grant.status = GRANT_TERMINATED
            grant.terminated_at = data["at"]
            grant.terminate_reason = data.get("reason", "")
            for visit in grant.visits:
                if visit["status"] == VISIT_PENDING:
                    visit["status"] = GRANT_TERMINATED
        elif event_type == "DDGrantExpired":
            state.grants[data["grant_id"]].status = GRANT_EXPIRED

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "project_id": self.project_id,
            "owner_party_id": self.owner_party_id,
            "materials": [
                {
                    "version_no": m.version_no, "language": m.language, "title": m.title,
                    "ref": m.ref, "corrects_version": m.corrects_version, "note": m.note,
                }
                for m in self.materials
            ],
            "grants": {
                gid: {
                    "contact_id": g.contact_id, "purpose": g.purpose,
                    "material_refs": g.material_refs, "expires_at": g.expires_at,
                    "status": g.status, "visits": g.visits,
                    "terminate_reason": g.terminate_reason,
                }
                for gid, g in self.grants.items()
            },
        }
