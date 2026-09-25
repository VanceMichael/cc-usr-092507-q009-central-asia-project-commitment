"""尽调材料：按用途授权、访问留痕、译文更正保留原文。

授权三要素：材料、被授权联系人、用途。访问时必须声明同一用途并留痕。
联系人离任时，只有"尚未完成"的授权被终止；已完成的访问记录永久保留。
译文只允许追加更正版本，原文与历次译文都不可删除或改写。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import AuthorizationError, NotFound, RuleViolation
from .repository import Aggregate

DILIGENCE_PURPOSES = (
    "qualification",        # 主体资质核查
    "financial",            # 财务与币种口径核查
    "site_verification",    # 土地与现场核查
    "agri_source",          # 农产品原料溯源
    "processing_audit",     # 加工能力审计
    "logistics_compliance", # 物流与关务合规
)


@dataclass
class Material:
    material_id: str
    owner_party_id: str
    title: str
    language: str
    confidentiality: str
    translations: list[dict[str, Any]]


@dataclass
class Grant:
    grant_id: str
    material_id: str
    party_id: str
    contact_id: str
    purpose: str
    valid_until: str
    granted_at: str
    status: str = "active"          # active | completed | revoked
    revoke_reason: str | None = None
    revoked_at: str | None = None
    accesses: list[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.accesses is None:
            self.accesses = []


class Diligence(Aggregate):
    """每个尽调案卷一个流（可按项目组织），内含多份材料与授权。"""

    stream_prefix = "dd"

    def __init__(self, stream_id: str) -> None:
        super().__init__(stream_id)
        self.dossier_id: str | None = None
        self.project_id: str | None = None
        self.materials: dict[str, Material] = {}
        self.grants: dict[str, Grant] = {}

    @classmethod
    def open(cls, dossier_id: str, *, project_id: str, at: str) -> "Diligence":
        dossier = cls(cls.stream_for(dossier_id))
        dossier.record(
            "DossierOpened",
            {"dossier_id": dossier_id, "project_id": project_id, "at": at},
        )
        return dossier

    # ---------- 材料 ----------
    def register_material(
        self,
        *,
        material_id: str,
        owner_party_id: str,
        title: str,
        language: str,
        confidentiality: str,
        original_text_ref: str,
        at: str,
    ) -> None:
        self._require_open()
        if material_id in self.materials:
            raise RuleViolation(f"材料已登记：{material_id}")
        self.record(
            "MaterialRegistered",
            {
                "dossier_id": self.dossier_id,
                "material_id": material_id,
                "owner_party_id": owner_party_id,
                "title": title,
                "language": language,
                "confidentiality": confidentiality,
                "original_text_ref": original_text_ref,
                "at": at,
            },
        )

    def submit_translation(
        self, *, material_id: str, translator_contact_ref: str, language: str, text_ref: str, at: str
    ) -> None:
        self._require_open()
        material = self._require_material(material_id)
        # 原文占用第 0 版，首份译文从第 1 版开始。
        version = len(material.translations)
        self.record(
            "TranslationSubmitted",
            {
                "dossier_id": self.dossier_id,
                "material_id": material_id,
                "translation_version": version,
                "translator_contact_ref": translator_contact_ref,
                "language": language,
                "text_ref": text_ref,
                "at": at,
            },
        )

    def correct_translation(
        self, *, material_id: str, corrector_contact_ref: str, text_ref: str, note: str, at: str
    ) -> None:
        """译文更正：追加新版本。原文和旧译文原样保留。"""
        self._require_open()
        material = self._require_material(material_id)
        if not material.translations:
            raise RuleViolation("材料尚无译文，不能更正，应先提交译文")
        if not note:
            raise RuleViolation("译文更正必须说明原因")
        version = len(material.translations)
        self.record(
            "TranslationCorrected",
            {
                "dossier_id": self.dossier_id,
                "material_id": material_id,
                "translation_version": version,
                "corrected_from_version": material.translations[-1]["translation_version"],
                "corrector_contact_ref": corrector_contact_ref,
                "language": material.translations[-1]["language"],
                "text_ref": text_ref,
                "note": note,
                "at": at,
            },
        )

    # ---------- 用途授权 ----------
    def grant_access(
        self,
        *,
        grant_id: str,
        material_id: str,
        party_id: str,
        contact_id: str,
        purpose: str,
        valid_until: str,
        at: str,
    ) -> None:
        self._require_open()
        self._require_material(material_id)
        if purpose not in DILIGENCE_PURPOSES:
            raise RuleViolation(f"未知授权用途：{purpose}")
        if valid_until <= at:
            raise RuleViolation("授权截止时间必须晚于当前时间")
        if grant_id in self.grants:
            raise RuleViolation(f"授权已存在：{grant_id}")
        self.record(
            "AccessGranted",
            {
                "dossier_id": self.dossier_id,
                "grant_id": grant_id,
                "material_id": material_id,
                "party_id": party_id,
                "contact_id": contact_id,
                "purpose": purpose,
                "valid_until": valid_until,
                "at": at,
            },
        )

    def use_material(
        self, *, grant_id: str, contact_id: str, purpose: str, at: str, note: str = ""
    ) -> dict[str, Any]:
        """按用途访问材料：用途必须与授权一致，联系人须仍是被授权人且授权有效。"""
        grant = self._require_grant(grant_id)
        if grant.status != "active":
            raise AuthorizationError(f"授权状态为 {grant.status}，不能访问")
        if grant.contact_id != contact_id:
            raise AuthorizationError("联系人与授权对象不一致")
        if grant.purpose != purpose:
            raise AuthorizationError(f"访问用途 {purpose} 与授权用途 {grant.purpose} 不一致")
        if grant.valid_until < at:
            raise AuthorizationError("授权已到期")
        record = {"grant_id": grant_id, "purpose": purpose, "at": at, "note": note}
        self.record(
            "MaterialAccessed",
            {"dossier_id": self.dossier_id, **record},
        )
        return record

    def complete_grant(self, *, grant_id: str, at: str) -> None:
        """用途已完成（如尽调报告已出具）：授权完成，此后离任不再终止它。"""
        grant = self._require_grant(grant_id)
        if grant.status != "active":
            raise RuleViolation("只有生效中的授权可以标记完成")
        self.record(
            "GrantCompleted",
            {"dossier_id": self.dossier_id, "grant_id": grant_id, "at": at},
        )

    def revoke_grant(self, *, grant_id: str, reason: str, at: str) -> bool:
        """终止尚未完成的访问。已完成授权不动；返回是否实际终止。"""
        grant = self.grants.get(grant_id)
        if grant is None:
            raise NotFound(f"授权不存在：{grant_id}")
        if grant.status != "active":
            return False
        self.record(
            "GrantRevoked",
            {"dossier_id": self.dossier_id, "grant_id": grant_id, "reason": reason, "at": at},
        )
        return True

    def grants_for_contact(self, party_id: str, contact_id: str) -> list[Grant]:
        return [
            g for g in self.grants.values()
            if g.party_id == party_id and g.contact_id == contact_id
        ]

    # ---------- 查询 ----------
    def _require_open(self) -> None:
        if self.dossier_id is None:
            raise RuleViolation("尽调案卷尚未建立")

    def _require_material(self, material_id: str) -> Material:
        material = self.materials.get(material_id)
        if material is None:
            raise NotFound(f"材料不存在：{material_id}")
        return material

    def _require_grant(self, grant_id: str) -> Grant:
        grant = self.grants.get(grant_id)
        if grant is None:
            raise NotFound(f"授权不存在：{grant_id}")
        return grant

    # ---------- 回放 ----------
    def apply(self, event: dict[str, Any]) -> None:
        p = event["payload"]
        kind = event["type"]
        if kind == "DossierOpened":
            self.dossier_id = p["dossier_id"]
            self.project_id = p["project_id"]
        elif kind == "MaterialRegistered":
            self.materials[p["material_id"]] = Material(
                material_id=p["material_id"],
                owner_party_id=p["owner_party_id"],
                title=p["title"],
                language=p["language"],
                confidentiality=p["confidentiality"],
                translations=[],
            )
            # 原文引用登记为不可变的第 0 版
            self.materials[p["material_id"]].translations.append(
                {
                    "translation_version": 0,
                    "kind": "original",
                    "language": p["language"],
                    "text_ref": p["original_text_ref"],
                    "at": p["at"],
                }
            )
        elif kind in ("TranslationSubmitted", "TranslationCorrected"):
            material = self.materials[p["material_id"]]
            entry = {
                "translation_version": p["translation_version"],
                "kind": "correction" if kind == "TranslationCorrected" else "translation",
                "language": p["language"],
                "text_ref": p["text_ref"],
                "at": p["at"],
            }
            if kind == "TranslationCorrected":
                entry["corrected_from_version"] = p["corrected_from_version"]
                entry["note"] = p["note"]
            material.translations.append(entry)
        elif kind == "AccessGranted":
            self.grants[p["grant_id"]] = Grant(
                grant_id=p["grant_id"],
                material_id=p["material_id"],
                party_id=p["party_id"],
                contact_id=p["contact_id"],
                purpose=p["purpose"],
                valid_until=p["valid_until"],
                granted_at=p["at"],
            )
        elif kind == "MaterialAccessed":
            self.grants[p["grant_id"]].accesses.append(
                {"purpose": p["purpose"], "at": p["at"], "note": p.get("note", "")}
            )
        elif kind == "GrantCompleted":
            self.grants[p["grant_id"]].status = "completed"
        elif kind == "GrantRevoked":
            grant = self.grants[p["grant_id"]]
            grant.status = "revoked"
            grant.revoke_reason = p["reason"]
            grant.revoked_at = p["at"]
        else:  # pragma: no cover
            raise RuleViolation(f"未知尽调事件：{kind}")
