"""跨境合作主体：版本化档案与授权联系人。

主体类别覆盖辽宁企业、中亚合作方、商协会、地方机构。
档案的每次修改形成新版本，旧版本保留以备"依据版本给出单一结果"。
联系人离任只终止尚未完成的访问（由尽调模块执行），已完成记录继续留存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import NotFound, RuleViolation
from .repository import Aggregate

PARTY_KINDS = ("liaoning_enterprise", "central_asia_partner", "chamber", "local_agency")


@dataclass
class Contact:
    contact_id: str
    name: str
    role: str
    scopes: frozenset[str]
    authorized_at: str
    status: str = "active"  # active | departed
    departed_at: str | None = None


class Party(Aggregate):
    stream_prefix = "party"

    def __init__(self, stream_id: str) -> None:
        super().__init__(stream_id)
        self.party_id: str | None = None
        self.kind: str | None = None
        self.name: str = ""
        self.jurisdiction: str = ""
        self.profile_versions: list[dict[str, Any]] = []
        self.contacts: dict[str, Contact] = {}

    # ---------- 命令 ----------
    @classmethod
    def register(
        cls,
        party_id: str,
        *,
        kind: str,
        name: str,
        jurisdiction: str,
        profile: dict[str, Any],
        at: str,
        zone: str = "UTC",
        bootstrap_contact: dict[str, Any] | None = None,
    ) -> "Party":
        if kind not in PARTY_KINDS:
            raise RuleViolation(f"未知主体类别：{kind}")
        if not name:
            raise RuleViolation("主体名称不能为空")
        party = cls(cls.stream_for(party_id))
        party.party_id = party_id
        party.record(
            "PartyRegistered",
            {
                "party_id": party_id,
                "kind": kind,
                "name": name,
                "jurisdiction": jurisdiction,
                "profile_version": 1,
                "profile": profile,
                "at": at,
                "zone": zone,
            },
        )
        # 首位管理员随登记引导，解决"没有管理员就无法授权管理员"的启动问题。
        if bootstrap_contact is not None:
            party.authorize_contact(
                contact_id=bootstrap_contact["contact_id"],
                name=bootstrap_contact["name"],
                role=bootstrap_contact.get("role", "管理员"),
                scopes=bootstrap_contact.get("scopes", ["party.admin"]),
                at=at,
                zone=zone,
            )
        return party

    def update_profile(self, *, profile: dict[str, Any], reason: str, at: str, zone: str = "UTC") -> None:
        self._require_registered()
        if not reason:
            raise RuleViolation("档案更正必须说明原因")
        self.record(
            "PartyProfileUpdated",
            {
                "party_id": self.party_id,
                "profile_version": len(self.profile_versions) + 1,
                "profile": profile,
                "reason": reason,
                "at": at,
                "zone": zone,
            },
        )

    def authorize_contact(
        self, *, contact_id: str, name: str, role: str, scopes: list[str], at: str, zone: str = "UTC"
    ) -> None:
        self._require_registered()
        if contact_id in self.contacts and self.contacts[contact_id].status == "active":
            raise RuleViolation(f"联系人已存在且在职：{contact_id}")
        if not scopes:
            raise RuleViolation("授权联系人至少需要一个权限范围")
        self.record(
            "ContactAuthorized",
            {
                "party_id": self.party_id,
                "contact_id": contact_id,
                "name": name,
                "role": role,
                "scopes": sorted(scopes),
                "at": at,
                "zone": zone,
            },
        )

    def change_contact_scopes(self, *, contact_id: str, scopes: list[str], at: str) -> None:
        self._require_registered()
        contact = self._require_active_contact(contact_id)
        self.record(
            "ContactScopesChanged",
            {"party_id": self.party_id, "contact_id": contact_id, "scopes": sorted(scopes), "at": at},
        )

    def contact_departs(self, *, contact_id: str, at: str) -> None:
        """联系人离任：主体侧标记离任；未完成访问由尽调模块终止。"""
        self._require_registered()
        self._require_active_contact(contact_id)
        self.record(
            "ContactDeparted",
            {"party_id": self.party_id, "contact_id": contact_id, "at": at},
        )

    # ---------- 查询 ----------
    def active_contact(self, contact_id: str) -> Contact:
        return self._require_active_contact(contact_id)

    def has_scope(self, contact_id: str, scope: str) -> bool:
        contact = self.contacts.get(contact_id)
        return contact is not None and contact.status == "active" and scope in contact.scopes

    def latest_profile(self) -> dict[str, Any]:
        return self.profile_versions[-1]["profile"]

    def profile_at_version(self, version: int) -> dict[str, Any]:
        if not 1 <= version <= len(self.profile_versions):
            raise NotFound(f"主体 {self.party_id} 不存在档案版本 {version}")
        return self.profile_versions[version - 1]["profile"]

    def _require_registered(self) -> None:
        if self.party_id is None:
            raise RuleViolation("主体尚未登记")

    def _require_active_contact(self, contact_id: str) -> Contact:
        contact = self.contacts.get(contact_id)
        if contact is None:
            raise NotFound(f"联系人不存在：{contact_id}")
        if contact.status != "active":
            raise RuleViolation(f"联系人已离任：{contact_id}")
        return contact

    # ---------- 事件回放 ----------
    def apply(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        kind = event["type"]
        if kind == "PartyRegistered":
            self.party_id = p["party_id"]
            self.kind = p["kind"]
            self.name = p["name"]
            self.jurisdiction = p["jurisdiction"]
            self.profile_versions.append(
                {"version": p["profile_version"], "profile": p["profile"], "at": p["at"], "reason": "登记"}
            )
        elif kind == "PartyProfileUpdated":
            self.profile_versions.append(
                {"version": p["profile_version"], "profile": p["profile"], "at": p["at"], "reason": p["reason"]}
            )
        elif kind == "ContactAuthorized":
            self.contacts[p["contact_id"]] = Contact(
                contact_id=p["contact_id"],
                name=p["name"],
                role=p["role"],
                scopes=frozenset(p["scopes"]),
                authorized_at=p["at"],
            )
        elif kind == "ContactScopesChanged":
            contact = self.contacts[p["contact_id"]]
            self.contacts[p["contact_id"]] = Contact(
                contact_id=contact.contact_id,
                name=contact.name,
                role=contact.role,
                scopes=frozenset(p["scopes"]),
                authorized_at=contact.authorized_at,
            )
        elif kind == "ContactDeparted":
            contact = self.contacts[p["contact_id"]]
            contact.status = "departed"
            contact.departed_at = p["at"]
        else:  # pragma: no cover
            raise RuleViolation(f"未知主体事件：{kind}")
