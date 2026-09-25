"""测试共用：可操纵时钟与完整可匹配场景构造。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from central_asia_project_commitment.backend import Backend
from central_asia_project_commitment.clock import Clock, Moment

LN = "party-ln"          # 辽宁企业
CA = "party-ca"          # 中亚合作方
CHAMBER = "party-ch"     # 商协会
AGENCY = "party-ag"      # 地方机构

LN_ADMIN = "ln-admin"
LN_MGR = "ln-mgr"
CA_MGR = "ca-mgr"
CA_ADMIN = "ca-admin"
CH_SEC = "ch-sec"

LAND = "land-1"
WAREHOUSE = "wh-1"
TRAIN = "train-1"
EXPERT = "expert-1"

ALL_SCOPES = [
    "party.admin", "intent.edit", "resource.manage",
    "commitment.confirm", "diligence.grant", "diligence.manage",
]


class FakeClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        super().__init__()
        self._at = start or datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)

    def now(self, zone: str | None = None) -> Moment:
        return Moment(self._at, zone or "UTC")

    def advance(self, **kwargs: int) -> None:
        self._at += timedelta(**kwargs)

    def set(self, value: datetime) -> None:
        self._at = value

    @property
    def iso(self) -> str:
        return self._at.isoformat()


def demand_items() -> list[dict]:
    return [
        {"code": "d-stage", "dimension": "investment_stage", "role": "demand",
         "confidentiality": "L2", "attributes": {"stage": "construction"}},
        {"code": "d-site", "dimension": "locality", "role": "demand", "confidentiality": "L2",
         "attributes": {"region": "pavlodar", "tax_preferences": ["cross_border_zone"],
                        "utilities": ["power", "water"]}},
        {"code": "d-route", "dimension": "logistics", "role": "demand", "confidentiality": "L1",
         "attributes": {"routes": ["ln_central_asia_rail"], "destination": "tashkent"}},
        {"code": "d-wheat", "dimension": "agriculture", "role": "demand", "confidentiality": "L2",
         "attributes": {"product": "wheat", "certifications": ["gacc"]},
         "quantity": {"value": 5000, "unit": "ton"}},
        {"code": "d-process", "dimension": "processing", "role": "demand", "confidentiality": "L2",
         "attributes": {"field": "food_processing", "certifications": ["haccp"]}},
        {"code": "d-fx", "dimension": "currency", "role": "demand", "confidentiality": "L1",
         "attributes": {"currency": "USD"}},
        {"code": "d-secret", "dimension": "confidentiality", "role": "demand", "confidentiality": "L2",
         "attributes": {"level": "L2"}},
        {"code": "d-land", "dimension": "locality", "role": "demand", "confidentiality": "L2",
         "attributes": {"resource_kind": "land"}, "quantity": {"value": 5, "unit": "ha"}},
        {"code": "d-wh", "dimension": "logistics", "role": "demand", "confidentiality": "L1",
         "attributes": {"resource_kind": "warehouse"}, "quantity": {"value": 2000, "unit": "m3"}},
        {"code": "d-train", "dimension": "logistics", "role": "demand", "confidentiality": "L1",
         "attributes": {"resource_kind": "train_slot"}, "quantity": {"value": 35, "unit": "teu"}},
        {"code": "d-expert", "dimension": "expert", "role": "demand", "confidentiality": "L1",
         "attributes": {"resource_kind": "expert_support", "field": "customs_compliance",
                        "languages": ["zh", "ru"]},
         "quantity": {"value": 1, "unit": "person_month"}},
    ]


def supply_items() -> list[dict]:
    return [
        {"code": "s-stage", "dimension": "investment_stage", "role": "supply", "confidentiality": "L3",
         "attributes": {"supports": ["feasibility", "contracting", "construction", "operation"]}},
        {"code": "s-site", "dimension": "locality", "role": "supply", "confidentiality": "L3",
         "attributes": {"region": "pavlodar", "tax_preferences": ["cross_border_zone"],
                        "utilities": ["power", "water", "gas"]}},
        {"code": "s-route", "dimension": "logistics", "role": "supply", "confidentiality": "L2",
         "attributes": {"routes": ["ln_central_asia_rail", "multimodal_caspian"], "destination": "tashkent"}},
        {"code": "s-wheat", "dimension": "agriculture", "role": "supply", "confidentiality": "L3",
         "attributes": {"product": "wheat", "certifications": ["gacc", "organic"]},
         "quantity": {"value": 10000, "unit": "ton"}},
        {"code": "s-process", "dimension": "processing", "role": "supply", "confidentiality": "L3",
         "attributes": {"field": "food_processing", "certifications": ["haccp", "iso22000"]},
         "quantity": {"value": 20000, "unit": "ton_per_year"}},
        {"code": "s-fx", "dimension": "currency", "role": "supply", "confidentiality": "L1",
         "attributes": {"accepted_currencies": ["CNY", "USD"], "convertible_currencies": ["EUR"]}},
        {"code": "s-secret", "dimension": "confidentiality", "role": "supply", "confidentiality": "L3",
         "attributes": {"level": "L3"}},
        {"code": "s-land", "dimension": "locality", "role": "supply", "confidentiality": "L2",
         "attributes": {"resource_kind": "land", "resource_id": LAND},
         "quantity": {"value": 100000, "unit": "m2"}},
        {"code": "s-wh", "dimension": "logistics", "role": "supply", "confidentiality": "L2",
         "attributes": {"resource_kind": "warehouse", "resource_id": WAREHOUSE},
         "quantity": {"value": 5000, "unit": "m3"}},
        {"code": "s-train", "dimension": "logistics", "role": "supply", "confidentiality": "L2",
         "attributes": {"resource_kind": "train_slot", "resource_id": TRAIN},
         "quantity": {"value": 40, "unit": "teu"}},
        {"code": "s-expert", "dimension": "expert", "role": "supply", "confidentiality": "L2",
         "attributes": {"resource_kind": "expert_support", "resource_id": EXPERT,
                        "field": "customs_compliance", "languages": ["zh", "ru"]},
         "quantity": {"value": 60, "unit": "person_day"}},
    ]


def build_scenario(backend: Backend, *, project_id: str = "proj-1",
                   demand_id: str = "int-ln", supply_id: str = "int-ca") -> dict:
    backend.register_party(LN, kind="liaoning_enterprise", name="辽宁某装备企业",
                           jurisdiction="CN-LN", profile={"credit": "A"},
                           bootstrap_admin={"contact_id": LN_ADMIN, "name": "辽宁管理员", "scopes": ALL_SCOPES})
    backend.register_party(CA, kind="central_asia_partner", name="中亚某农业集团",
                           jurisdiction="KZ-PAV", profile={"credit": "B+"},
                           bootstrap_admin={"contact_id": CA_ADMIN, "name": "中亚管理员", "scopes": ALL_SCOPES})
    backend.register_party(CHAMBER, kind="chamber", name="辽洽会商协会",
                           jurisdiction="CN-LN", profile={},
                           bootstrap_admin={"contact_id": CH_SEC, "name": "秘书处专员", "scopes": ALL_SCOPES})
    backend.register_party(AGENCY, kind="local_agency", name="巴甫洛达尔地方项目局",
                           jurisdiction="KZ-PAV", profile={})

    backend.authorize_contact(LN, admin_contact_id=LN_ADMIN, contact_id=LN_MGR,
                              name="辽宁项目经理", role="项目经理",
                              scopes=["intent.edit", "commitment.confirm", "diligence.grant",
                                      "diligence.manage", "resource.manage"])
    backend.authorize_contact(CA, admin_contact_id=CA_ADMIN, contact_id=CA_MGR,
                              name="中亚项目经理", role="项目经理",
                              scopes=["intent.edit", "commitment.confirm", "diligence.grant",
                                      "diligence.manage", "resource.manage"])

    for rid, kind, cap, unit in (
        (LAND, "land", 100000, "m2"),
        (WAREHOUSE, "warehouse", 5000, "m3"),
        (TRAIN, "train_slot", 40, "teu"),
        (EXPERT, "expert_support", 60, "person_day"),
    ):
        backend.register_resource(rid, owner_party_id=CA, contact_id=CA_ADMIN, kind=kind,
                                  capacity_value=cap, capacity_unit=unit)

    backend.register_intent(demand_id, party_id=LN, contact_id=LN_MGR,
                            title="本地化生产综合意向", items=demand_items())
    backend.register_intent(supply_id, party_id=CA, contact_id=CA_MGR,
                            title="属地资源综合供给", items=supply_items())
    backend.open_project(project_id, title="辽洽会-中亚农产品加工园",
                         secretary_contact_ref=CH_SEC, parties=[LN, CA, CHAMBER, AGENCY])
    return {"project_id": project_id, "demand_id": demand_id, "supply_id": supply_id}


def hold_engagement(backend: Backend, *, engagement_id: str = "eng-1",
                    project_id: str = "proj-1", expires_in_days: int = 14) -> dict:
    ctx = {"project_id": project_id, "demand_id": "int-ln", "supply_id": "int-ca"}
    clock = backend.clock
    deadline = (clock._at + timedelta(days=expires_in_days)).isoformat()
    proposal = backend.propose_match(
        engagement_id, project_id=project_id,
        demand_intent_id=ctx["demand_id"], supply_intent_id=ctx["supply_id"],
        proposed_expires_at=deadline,
        responsible={"demand": LN_MGR, "supply": CA_MGR, "secretary": CH_SEC},
        request_id=f"req-propose-{engagement_id}",
    )
    assert proposal["compatible"] is True, proposal
    backend.confirm_engagement(engagement_id, party_id=LN, contact_id=LN_MGR, zone="Asia/Shanghai")
    second = backend.confirm_engagement(engagement_id, party_id=CA, contact_id=CA_MGR, zone="Asia/Almaty")
    assert second["held"] is True
    return second
