"""项目聚合与项目接口读模型。

项目把同一线索下的意向、承诺、尽调案件串在一起。项目本身只保存
标题与关联；项目接口的完整视图由投影器从全部事件流重建，直接说明：
当前责任人、资源缺口、历次承诺、最终落地或终止的原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .aggregates import Aggregate
from .errors import StateConflict


@dataclass
class Project(Aggregate):
    project_id: str = ""
    title: str = ""
    secretary_contact_id: str = ""
    linked_intents: list[str] = field(default_factory=list)
    linked_dd_cases: list[str] = field(default_factory=list)

    def open(self, project_id: str, title: str, secretary_contact_id: str) -> list[tuple[str, dict[str, Any]]]:
        if self.version:
            raise StateConflict("项目已存在")
        self.stream_id = f"project:{project_id}"
        return [
            (
                "ProjectOpened",
                {"project_id": project_id, "title": title,
                 "secretary_contact_id": secretary_contact_id},
            )
        ]

    def link_intent(self, intent_id: str) -> list[tuple[str, dict[str, Any]]]:
        if intent_id in self.linked_intents:
            raise StateConflict(f"意向已关联：{intent_id}")
        return [("ProjectIntentLinked", {"intent_id": intent_id})]

    def link_dd_case(self, case_id: str) -> list[tuple[str, dict[str, Any]]]:
        if case_id in self.linked_dd_cases:
            raise StateConflict(f"尽调案件已关联：{case_id}")
        return [("ProjectDDLinked", {"case_id": case_id})]

    @classmethod
    def apply_event(cls, state: "Project", event_type: str, data: dict[str, Any]) -> None:
        if event_type == "ProjectOpened":
            state.project_id = data["project_id"]
            state.title = data["title"]
            state.secretary_contact_id = data["secretary_contact_id"]
            state.stream_id = f"project:{data['project_id']}"
        elif event_type == "ProjectIntentLinked":
            state.linked_intents.append(data["intent_id"])
        elif event_type == "ProjectDDLinked":
            state.linked_dd_cases.append(data["case_id"])
