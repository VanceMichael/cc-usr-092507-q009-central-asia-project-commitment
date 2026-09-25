"""跨境合作主体：辽宁企业 / 中亚合作方 / 商协会 / 地方机构。

- 主体资料以不可变版本保存，承诺与意向引用具体版本号；
- 授权联系人挂在主体下；离任只做标记，历史授权记录保留；
- 联系人是否仍有效，由尽调授权等模块据此判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .aggregates import Aggregate
from .errors import PolicyViolation, StateConflict

PARTY_KINDS = ("liaoning_enterprise", "central_asia_enterprise", "chamber", "local_agency")


@dataclass
class Contact:
    contact_id: str
    name: str
    role: str
    email: str = ""
    phone: str = ""
    added_at: str = ""
    departed_at: str | None = None

    @property
    def active(self) -> bool:
        return self.departed_at is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "name": self.name,
            "role": self.role,
            "email": self.email,
            "phone": self.phone,
            "added_at": self.added_at,
            "departed_at": self.departed_at,
            "active": self.active,
        }


@dataclass
class Party(Aggregate):
    party_id: str = ""
    kind: str = ""
    name: str = ""
    country: str = ""
    timezone: str = "UTC"
    profile_versions: list[dict[str, Any]] = field(default_factory=list)
    contacts: dict[str, Contact] = field(default_factory=dict)

    @property
    def stream_id_(self) -> str:
        return f"party:{self.party_id}"

    # ---- 读取模型 ----
    @property
    def latest_version_no(self) -> int:
        return len(self.profile_versions)

    def profile_at(self, version_no: int) -> dict[str, Any]:
        if version_no < 1 or version_no > len(self.profile_versions):
            raise StateConflict(f"主体 {self.party_id} 不存在版本 {version_no}")
        return self.profile_versions[version_no - 1]["profile"]

    def active_contact(self, contact_id: str) -> Contact:
        contact = self.contacts.get(contact_id)
        if contact is None:
            raise PolicyViolation(f"联系人 {contact_id} 不属于主体 {self.party_id}")
        if not contact.active:
            raise PolicyViolation(f"联系人 {contact_id} 已离任，不能再发起或访问")
        return contact

    def active_contacts(self) -> list[Contact]:
        return [c for c in self.contacts.values() if c.active]

    # ---- 命令 ----
    def register(
        self,
        party_id: str,
        kind: str,
        name: str,
        timezone: str,
        profile: dict[str, Any],
        country: str = "",
    ) -> list[tuple[str, dict[str, Any]]]:
        if self.version:
            raise StateConflict("主体已登记")
        if kind not in PARTY_KINDS:
            raise PolicyViolation(f"未知主体类型：{kind}")
        self.stream_id = f"party:{party_id}"
        return [
            (
                "PartyRegistered",
                {
                    "party_id": party_id,
                    "kind": kind,
                    "name": name,
                    "country": country,
                    "timezone": timezone,
                    "profile": profile,
                },
            )
        ]

    def new_profile_version(
        self, profile: dict[str, Any], reason: str
    ) -> list[tuple[str, dict[str, Any]]]:
        if not self.version:
            raise StateConflict("主体尚未登记")
        return [
            (
                "PartyVersioned",
                {
                    "version_no": len(self.profile_versions) + 1,
                    "profile": profile,
                    "reason": reason,
                },
            )
        ]

    def add_contact(self, contact: Contact) -> list[tuple[str, dict[str, Any]]]:
        if not self.version:
            raise StateConflict("主体尚未登记")
        if contact.contact_id in self.contacts:
            raise StateConflict(f"联系人已存在：{contact.contact_id}")
        return [
            (
                "PartyContactAdded",
                {
                    "contact_id": contact.contact_id,
                    "name": contact.name,
                    "role": contact.role,
                    "email": contact.email,
                    "phone": contact.phone,
                    "added_at": contact.added_at,
                },
            )
        ]

    def contact_departed(self, contact_id: str, at: str) -> list[tuple[str, dict[str, Any]]]:
        contact = self.contacts.get(contact_id)
        if contact is None:
            raise StateConflict(f"联系人不存在：{contact_id}")
        if not contact.active:
            raise StateConflict(f"联系人已离任：{contact_id}")
        return [("PartyContactDeparted", {"contact_id": contact_id, "at": at})]

    # ---- 应用事件 ----
    @classmethod
    def apply_event(cls, state: "Party", event_type: str, data: dict[str, Any]) -> None:
        if event_type == "PartyRegistered":
            state.party_id = data["party_id"]
            state.kind = data["kind"]
            state.name = data["name"]
            state.country = data.get("country", "")
            state.timezone = data.get("timezone", "UTC")
            state.stream_id = f"party:{data['party_id']}"
            state.profile_versions.append(
                {"version_no": 1, "profile": data["profile"], "reason": "登记"}
            )
        elif event_type == "PartyVersioned":
            state.profile_versions.append(
                {
                    "version_no": data["version_no"],
                    "profile": data["profile"],
                    "reason": data["reason"],
                }
            )
        elif event_type == "PartyContactAdded":
            state.contacts[data["contact_id"]] = Contact(
                contact_id=data["contact_id"],
                name=data["name"],
                role=data["role"],
                email=data.get("email", ""),
                phone=data.get("phone", ""),
                added_at=data.get("added_at", ""),
            )
        elif event_type == "PartyContactDeparted":
            state.contacts[data["contact_id"]].departed_at = data["at"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "party_id": self.party_id,
            "kind": self.kind,
            "name": self.name,
            "country": self.country,
            "timezone": self.timezone,
            "latest_version": self.latest_version_no,
            "profile_versions": self.profile_versions,
            "contacts": {k: c.to_dict() for k, c in self.contacts.items()},
        }
