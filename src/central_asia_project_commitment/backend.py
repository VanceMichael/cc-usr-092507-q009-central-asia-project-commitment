"""后端门面：鉴权、请求幂等、跨聚合同一提交、审批执行、恢复处理。

秘书处的一切操作都经过这里：
- 同一业务请求（request_id）重放只返回首次结果，绝不产生第二份承诺；
- 组合暂留/改量/终止把组合聚合与四类资源放进同一提交，全有或全无；
- 恢复后继续处理暂留到期、尽调待办、履约提醒，且重复恢复不产生重复效果。
"""

from __future__ import annotations

from typing import Any

from .approvals import ApprovalCase
from .catalog import FxTable
from .clock import Clock
from .diligence import Diligence
from .engagement import Engagement
from .errors import (
    AuthorizationError,
    IdempotencyReplay,
    NotFound,
    RuleViolation,
)
from .events import EventStore
from .intents import Intent
from .matching import match_intents
from .parties import Party
from .projects import Project, build_project_view
from .repository import Aggregate, Repository
from .resources import RESOURCE_KINDS, Resource


class IdempotencyRecord(Aggregate):
    stream_prefix = "idem"

    def __init__(self, stream_id: str) -> None:
        super().__init__(stream_id)
        self.request_id: str | None = None
        self.result: dict[str, Any] = {}

    @classmethod
    def capture(cls, request_id: str, result: dict[str, Any]) -> "IdempotencyRecord":
        record = cls(cls.stream_for(request_id))
        record.record("IdempotencyRecorded", {"request_id": request_id, "result": result})
        return record

    def apply(self, event: dict[str, Any]) -> None:
        self.request_id = event["payload"]["request_id"]
        self.result = event["payload"]["result"]


class Backend:
    def __init__(self, store_path: str | None = None, *, clock: Clock | None = None) -> None:
        self.store = EventStore(store_path)
        self.repo = Repository(self.store)
        self.clock = clock or Clock()
        self.fx = FxTable()
        self.outbox: list[dict[str, Any]] = []  # 履约提醒等对外通知，恢复后继续补发

    # ================= 基础辅助 =================
    def _now(self, zone: str = "UTC") -> tuple[str, str]:
        moment = self.clock.now(zone)
        return moment.utc.isoformat(), zone

    def _load(self, cls: type[Aggregate], entity_id: str) -> Aggregate:
        aggregate = cls.load(self.store, cls.stream_for(entity_id))
        if aggregate is None:
            raise NotFound(f"{cls.stream_prefix} 不存在：{entity_id}")
        return aggregate

    def party(self, party_id: str) -> Party:
        return self._load(Party, party_id)  # type: ignore[return-value]

    def intent(self, intent_id: str) -> Intent:
        return self._load(Intent, intent_id)  # type: ignore[return-value]

    def resource(self, resource_id: str) -> Resource:
        return self._load(Resource, resource_id)  # type: ignore[return-value]

    def engagement(self, engagement_id: str) -> Engagement:
        return self._load(Engagement, engagement_id)  # type: ignore[return-value]

    def dossier(self, dossier_id: str) -> Diligence:
        return self._load(Diligence, dossier_id)  # type: ignore[return-value]

    def project(self, project_id: str) -> Project:
        return self._load(Project, project_id)  # type: ignore[return-value]

    def approval(self, case_id: str) -> ApprovalCase:
        return self._load(ApprovalCase, case_id)  # type: ignore[return-value]

    def _all(self, prefix: str, cls: type[Aggregate]) -> list[Aggregate]:
        return [cls.load(self.store, sid) for sid in self.store.stream_ids(prefix)]  # type: ignore[list-item,misc]

    def _all_resources(self) -> dict[str, Resource]:
        result: dict[str, Resource] = {}
        for sid in self.store.stream_ids("res-"):
            resource = Resource.load(self.store, sid)
            result[resource.resource_id] = resource  # type: ignore[assignment]
        return result

    def _require_new_request(self, request_id: str | None) -> None:
        """命令产生任何变更之前先拦截重放，保证重放不产生第二份效果。"""
        if request_id is None:
            return
        stream = IdempotencyRecord.stream_for(request_id)
        if self.store.exists(stream):
            replay = IdempotencyRecord.load(self.store, stream)
            raise IdempotencyReplay(f"请求 {request_id} 已处理", result=replay.result)  # type: ignore[union-attr]

    def _save(
        self,
        aggregates: list[Aggregate],
        *,
        at: str,
        actor: str | None = None,
        request_id: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if request_id is not None:
            stream = IdempotencyRecord.stream_for(request_id)
            if self.store.exists(stream):
                replay = IdempotencyRecord.load(self.store, stream)
                raise IdempotencyReplay(f"请求 {request_id} 已处理", result=replay.result)  # type: ignore[union-attr]
            aggregates = aggregates + [IdempotencyRecord.capture(request_id, result or {})]
        self.repo.save(aggregates, at=at, actor_id=actor, request_id=request_id)
        return result or {}

    def require_contact(self, party_id: str, contact_id: str, scope: str) -> Party:
        """鉴权：联系人在职且具备所需权限范围。"""
        party = self.party(party_id)
        if not party.has_scope(contact_id, scope):
            raise AuthorizationError(f"联系人 {contact_id} 缺少权限 {scope} 或已离任")
        return party

    # ================= 主体与联系人 =================
    def register_party(
        self,
        party_id: str,
        *,
        kind: str,
        name: str,
        jurisdiction: str,
        profile: dict[str, Any],
        zone: str = "UTC",
        bootstrap_admin: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        at, zone = self._now(zone)
        party = Party.register(
            party_id, kind=kind, name=name, jurisdiction=jurisdiction, profile=profile, at=at, zone=zone,
            bootstrap_contact=bootstrap_admin,
        )
        self._save([party], at=at, result={"party_id": party_id, "profile_version": 1})
        return {"party_id": party_id, "profile_version": 1}

    def update_party_profile(
        self, party_id: str, *, contact_id: str, profile: dict[str, Any], reason: str, zone: str = "UTC"
    ) -> dict[str, Any]:
        at, zone = self._now(zone)
        party = self.require_contact(party_id, contact_id, "party.admin")
        party.update_profile(profile=profile, reason=reason, at=at, zone=zone)
        result = {"party_id": party_id, "profile_version": len(party.profile_versions)}
        self._save([party], at=at, actor=contact_id, result=result)
        return result

    def authorize_contact(
        self,
        party_id: str,
        *,
        admin_contact_id: str,
        contact_id: str,
        name: str,
        role: str,
        scopes: list[str],
        zone: str = "UTC",
    ) -> dict[str, Any]:
        at, zone = self._now(zone)
        party = self.require_contact(party_id, admin_contact_id, "party.admin")
        party.authorize_contact(
            contact_id=contact_id, name=name, role=role, scopes=scopes, at=at, zone=zone
        )
        self._save([party], at=at, actor=admin_contact_id)
        return {"party_id": party_id, "contact_id": contact_id}

    def contact_departs(
        self, party_id: str, *, admin_contact_id: str, contact_id: str
    ) -> dict[str, Any]:
        """联系人离任：主体标记离任，并终止其在全部案卷中尚未完成的访问。

        已完成的授权与访问记录原样保留。
        """
        at, _ = self._now()
        party = self.require_contact(party_id, admin_contact_id, "party.admin")
        party.contact_departs(contact_id=contact_id, at=at)

        aggregates: list[Aggregate] = [party]
        revoked: list[dict[str, str]] = []
        for dossier in self._all("dd-", Diligence):
            for grant in dossier.grants_for_contact(party_id, contact_id):
                if dossier.revoke_grant(grant_id=grant.grant_id, reason="联系人离任，终止尚未完成的访问", at=at):
                    revoked.append({"dossier_id": dossier.dossier_id, "grant_id": grant.grant_id})  # type: ignore[union-attr]
                    if dossier not in aggregates:
                        aggregates.append(dossier)
        result = {"party_id": party_id, "contact_id": contact_id, "revoked_active_grants": revoked}
        self._save(aggregates, at=at, actor=admin_contact_id, result=result)
        return result

    # ================= 意向与汇率 =================
    def publish_fx(self, rates: dict[str, float]) -> int:
        return self.fx.publish(rates)

    def register_intent(
        self,
        intent_id: str,
        *,
        party_id: str,
        contact_id: str,
        title: str,
        items: list[dict[str, Any]],
        fx_version: int | None = None,
        zone: str = "UTC",
    ) -> dict[str, Any]:
        at, zone = self._now(zone)
        self.require_contact(party_id, contact_id, "intent.edit")
        intent = Intent.register(
            intent_id,
            party_id=party_id,
            title=title,
            items=items,
            fx_version=fx_version if fx_version is not None else self.fx.version,
            at=at,
            zone=zone,
        )
        result = {"intent_id": intent_id, "revision": 1}
        self._save([intent], at=at, actor=contact_id, result=result)
        return result

    def revise_intent(
        self,
        intent_id: str,
        *,
        contact_id: str,
        items: list[dict[str, Any]],
        reason: str,
        fx_version: int | None = None,
        zone: str = "UTC",
    ) -> dict[str, Any]:
        at, zone = self._now(zone)
        intent = self.intent(intent_id)
        self.require_contact(intent.party_id, contact_id, "intent.edit")  # type: ignore[arg-type]
        intent.revise(
            items=items,
            fx_version=fx_version if fx_version is not None else self.fx.version,
            reason=reason,
            at=at,
            zone=zone,
        )
        result = {"intent_id": intent_id, "revision": len(intent.versions)}
        self._save([intent], at=at, actor=contact_id, result=result)
        return result

    # ================= 资源与项目 =================
    def register_resource(
        self,
        resource_id: str,
        *,
        owner_party_id: str,
        contact_id: str,
        kind: str,
        capacity_value: float,
        capacity_unit: str,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        at, _ = self._now()
        self.require_contact(owner_party_id, contact_id, "resource.manage")
        resource = Resource.register(
            resource_id,
            kind=kind,
            capacity_value=capacity_value,
            capacity_unit=capacity_unit,
            owner_party_id=owner_party_id,
            attributes=attributes,
            at=at,
        )
        self._save([resource], at=at, actor=contact_id)
        return {"resource_id": resource_id, "kind": kind}

    def open_project(
        self, project_id: str, *, title: str, secretary_contact_ref: str, parties: list[str]
    ) -> dict[str, Any]:
        at, _ = self._now()
        for party_id in parties:
            self.party(party_id)
        project = Project.open_project(
            project_id, title=title, secretary_contact_ref=secretary_contact_ref, parties=parties, at=at
        )
        self._save([project], at=at)
        return {"project_id": project_id}

    # ================= 尽调 =================
    def open_dossier(self, dossier_id: str, *, project_id: str) -> dict[str, Any]:
        at, _ = self._now()
        project = self.project(project_id)
        dossier = Diligence.open(dossier_id, project_id=project_id, at=at)
        project.attach_dossier(dossier_id)
        self._save([dossier, project], at=at)
        return {"dossier_id": dossier_id, "project_id": project_id}

    def register_material(
        self,
        dossier_id: str,
        *,
        owner_party_id: str,
        contact_id: str,
        material_id: str,
        title: str,
        language: str,
        confidentiality: str,
        original_text_ref: str,
    ) -> dict[str, Any]:
        at, _ = self._now()
        self.require_contact(owner_party_id, contact_id, "diligence.manage")
        dossier = self.dossier(dossier_id)
        dossier.register_material(
            material_id=material_id,
            owner_party_id=owner_party_id,
            title=title,
            language=language,
            confidentiality=confidentiality,
            original_text_ref=original_text_ref,
            at=at,
        )
        self._save([dossier], at=at, actor=contact_id)
        return {"dossier_id": dossier_id, "material_id": material_id}

    def submit_translation(
        self, dossier_id: str, *, material_id: str, translator_contact_ref: str, language: str, text_ref: str
    ) -> dict[str, Any]:
        at, _ = self._now()
        dossier = self.dossier(dossier_id)
        dossier.submit_translation(
            material_id=material_id,
            translator_contact_ref=translator_contact_ref,
            language=language,
            text_ref=text_ref,
            at=at,
        )
        self._save([dossier], at=at, actor=translator_contact_ref)
        return {"material_id": material_id, "translation_version": len(dossier.materials[material_id].translations) - 1}

    def correct_translation(
        self,
        dossier_id: str,
        *,
        material_id: str,
        corrector_contact_ref: str,
        text_ref: str,
        note: str,
    ) -> dict[str, Any]:
        """译文更正：保留原文与旧译文，只追加更正版本。"""
        at, _ = self._now()
        dossier = self.dossier(dossier_id)
        dossier.correct_translation(
            material_id=material_id,
            corrector_contact_ref=corrector_contact_ref,
            text_ref=text_ref,
            note=note,
            at=at,
        )
        version = dossier.materials[material_id].translations[-1]["translation_version"]
        self._save([dossier], at=at, actor=corrector_contact_ref)
        return {"material_id": material_id, "corrected_version": version}

    def grant_access(
        self,
        dossier_id: str,
        *,
        granter_party_id: str,
        granter_contact_id: str,
        grant_id: str,
        material_id: str,
        grantee_party_id: str,
        grantee_contact_id: str,
        purpose: str,
        valid_until: str,
    ) -> dict[str, Any]:
        at, _ = self._now()
        self.require_contact(granter_party_id, granter_contact_id, "diligence.grant")
        # 被授权联系人必须在职
        self.party(grantee_party_id).active_contact(grantee_contact_id)
        dossier = self.dossier(dossier_id)
        dossier.grant_access(
            grant_id=grant_id,
            material_id=material_id,
            party_id=grantee_party_id,
            contact_id=grantee_contact_id,
            purpose=purpose,
            valid_until=valid_until,
            at=at,
        )
        self._save([dossier], at=at, actor=granter_contact_id)
        return {"grant_id": grant_id, "purpose": purpose, "valid_until": valid_until}

    def use_material(
        self, dossier_id: str, *, grant_id: str, contact_id: str, purpose: str, note: str = ""
    ) -> dict[str, Any]:
        at, _ = self._now()
        dossier = self.dossier(dossier_id)
        record = dossier.use_material(
            grant_id=grant_id, contact_id=contact_id, purpose=purpose, at=at, note=note
        )
        self._save([dossier], at=at, actor=contact_id)
        return record

    def complete_grant(self, dossier_id: str, *, grant_id: str, contact_id: str) -> dict[str, Any]:
        at, _ = self._now()
        dossier = self.dossier(dossier_id)
        dossier.complete_grant(grant_id=grant_id, at=at)
        self._save([dossier], at=at, actor=contact_id)
        return {"grant_id": grant_id, "status": "completed"}

    # ================= 匹配与暂留 =================
    def propose_match(
        self,
        engagement_id: str,
        *,
        project_id: str,
        demand_intent_id: str,
        supply_intent_id: str,
        proposed_expires_at: str,
        responsible: dict[str, str],
        request_id: str,
    ) -> dict[str, Any]:
        """按当前版本计算候选匹配并保存快照。满足项、缺口、冲突同时列出。"""
        at, _ = self._now()
        self._require_new_request(request_id)
        project = self.project(project_id)
        demand_intent = self.intent(demand_intent_id)
        supply_intent = self.intent(supply_intent_id)
        snapshot = match_intents(
            demand_intent=demand_intent,
            supply_intent=supply_intent,
            resources=self._all_resources(),
            at=at,
        )
        engagement = Engagement.propose(
            engagement_id,
            project_id=project_id,
            demand_party_id=demand_intent.party_id,  # type: ignore[arg-type]
            supply_party_id=supply_intent.party_id,  # type: ignore[arg-type]
            demand_intent_revision=snapshot["demand_revision"],
            supply_intent_revision=snapshot["supply_revision"],
            match_snapshot=snapshot,
            lines=snapshot["lines"],
            proposed_expires_at=proposed_expires_at,
            responsible=responsible,
            at=at,
        )
        project.attach_engagement(engagement_id)
        result = {
            "engagement_id": engagement_id,
            "compatible": snapshot["compatible"],
            "satisfied": snapshot["satisfied"],
            "gaps": snapshot["gaps"],
            "conflicts": snapshot["conflicts"],
        }
        self._save([engagement, project], at=at, request_id=request_id, result=result)
        return result

    def confirm_engagement(
        self, engagement_id: str, *, party_id: str, contact_id: str, zone: str = "UTC"
    ) -> dict[str, Any]:
        """一方确认；双方确认后原子占用四类资源生成暂留。"""
        at, zone = self._now(zone)
        self.require_contact(party_id, contact_id, "commitment.confirm")
        engagement = self.engagement(engagement_id)
        both_confirmed = engagement.confirm(party_id=party_id, at=at, zone=zone)
        aggregates: list[Aggregate] = [engagement]
        held = False
        if both_confirmed:
            if not engagement.match_snapshot.get("compatible"):
                raise RuleViolation("匹配仍存在缺口或资源冲突，不能暂留；请解决后重新发起匹配")
            engagement.hold(expires_at=engagement.expires_at, at=at)  # type: ignore[arg-type]
            # 在提交前的当前资源版本上再占一次，容量校验失败则整体失败。
            for line in engagement.lines:
                resource = self.resource(line.resource_id)
                resource.allocate(ref_id=engagement_id, qty=line.qty, at=at)
                aggregates.append(resource)
            held = True
        result = {
            "engagement_id": engagement_id,
            "confirmations": dict(engagement.confirmations),
            "held": held,
            "expires_at": engagement.expires_at if held else None,
        }
        self._save(aggregates, at=at, actor=contact_id, result=result)
        return result

    # ================= 承诺生命周期 =================
    def convert_to_commitment(
        self, engagement_id: str, *, contact_id: str, terms: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        at, _ = self._now()
        self._require_new_request(request_id)
        engagement = self.engagement(engagement_id)
        self.require_contact(engagement.demand_party_id, contact_id, "commitment.confirm")  # type: ignore[arg-type]
        engagement.convert(terms=terms, at=at)
        due_at = terms.get("due_at")
        if due_at:
            engagement.schedule_reminder(
                reminder_id=f"{engagement_id}-due", kind="performance_due", due_at=due_at, at=at
            )
        result = {
            "engagement_id": engagement_id,
            "commitment_no": terms["commitment_no"],
            "status": "converted",
        }
        self._save([engagement], at=at, actor=contact_id, request_id=request_id, result=result)
        return result

    def record_handover(
        self, engagement_id: str, *, kind: str, qty: float, detail: str, contact_id: str
    ) -> dict[str, Any]:
        at, _ = self._now()
        engagement = self.engagement(engagement_id)
        engagement.record_handover(kind=kind, qty=qty, detail=detail, at=at)
        line = engagement._line(kind)
        resource = self.resource(line.resource_id)
        resource.mark_fulfilled(ref_id=engagement_id, qty=qty, at=at)
        self._save([engagement, resource], at=at, actor=contact_id)
        return {"engagement_id": engagement_id, "kind": kind, "fulfilled": line.fulfilled}

    def record_fee(
        self, engagement_id: str, *, amount: float, currency: str, purpose: str, contact_id: str
    ) -> dict[str, Any]:
        at, _ = self._now()
        engagement = self.engagement(engagement_id)
        engagement.record_fee(amount=amount, currency=currency, purpose=purpose, at=at)
        self._save([engagement], at=at, actor=contact_id)
        return {"engagement_id": engagement_id, "fees_recorded": len(engagement.fees)}

    def change_quantities(
        self,
        engagement_id: str,
        *,
        changes: dict[str, float],
        reason: str,
        contact_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """改量：增/减与库存同步原子提交；只能减少未履行份额。"""
        at, _ = self._now()
        self._require_new_request(request_id)
        engagement, aggregates, deltas = self._build_change(engagement_id, changes, reason, at)
        result = {"engagement_id": engagement_id, "deltas": deltas, "reason": reason}
        self._save(aggregates, at=at, actor=contact_id, request_id=request_id, result=result)
        return result

    def _build_change(
        self, engagement_id: str, changes: dict[str, float], reason: str, at: str
    ) -> tuple[Engagement, list[Aggregate], dict[str, Any]]:
        engagement = self.engagement(engagement_id)
        deltas = engagement.change_quantities(changes=changes, reason=reason, at=at)
        aggregates: list[Aggregate] = [engagement]
        for kind, move in deltas.items():
            line = engagement._line(kind)
            resource = self.resource(line.resource_id)
            if move["increase"]:
                resource.increase_allocation(ref_id=engagement_id, delta=move["increase"], at=at)
            if move["reduce"]:
                resource.reduce_allocation(
                    ref_id=engagement_id, release_delta=move["reduce"], at=at, reason=reason
                )
            aggregates.append(resource)
        return engagement, aggregates, deltas

    def extend_engagement(
        self,
        engagement_id: str,
        *,
        new_expires_at: str,
        reason: str,
        contact_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        at, _ = self._now()
        self._require_new_request(request_id)
        engagement = self.engagement(engagement_id)
        engagement.extend(new_expires_at=new_expires_at, reason=reason, at=at)
        result = {"engagement_id": engagement_id, "expires_at": new_expires_at}
        self._save([engagement], at=at, actor=contact_id, request_id=request_id, result=result)
        return result

    def _build_termination(
        self, engagement: Engagement, *, reason: str | None, at: str, expiry: bool
    ) -> tuple[list[Aggregate], dict[str, Any]]:
        # 先算释放计划（终止/到期事件会把行量改为已交接量）。
        release_plan = [
            {"kind": l.kind, "resource_id": l.resource_id, "release": l.releasable, "retain": l.fulfilled}
            for l in engagement.lines
        ]
        if expiry:
            engagement.expire(at=at)
        else:
            engagement.terminate(reason=reason, at=at)  # type: ignore[arg-type]
        aggregates: list[Aggregate] = [engagement]
        for plan in release_plan:
            if plan["release"] <= 0:
                continue
            resource = self.resource(plan["resource_id"])
            resource.reduce_allocation(
                ref_id=engagement.engagement_id,  # type: ignore[arg-type]
                release_delta=plan["release"],
                at=at,
                reason="暂留到期" if expiry else f"终止：{reason}",
            )
            aggregates.append(resource)
        result = {
            "engagement_id": engagement.engagement_id,
            "status": engagement.status,
            "released": [p for p in release_plan if p["release"] > 0],
            "retained_fulfilled": [
                {"kind": p["kind"], "qty": p["retain"]} for p in release_plan if p["retain"] > 0
            ],
            "retained_fees": len(engagement.fees),
        }
        return aggregates, result

    def _terminate_or_expire(
        self,
        engagement: Engagement,
        *,
        reason: str | None,
        at: str,
        expiry: bool,
        actor: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        aggregates, result = self._build_termination(engagement, reason=reason, at=at, expiry=expiry)
        self._save(aggregates, at=at, actor=actor, request_id=request_id, result=result)
        return result

    def terminate_engagement(
        self, engagement_id: str, *, reason: str, contact_id: str, request_id: str
    ) -> dict[str, Any]:
        at, _ = self._now()
        self._require_new_request(request_id)
        engagement = self.engagement(engagement_id)
        return self._terminate_or_expire(
            engagement, reason=reason, at=at, expiry=False, actor=contact_id, request_id=request_id
        )

    def land_engagement(self, engagement_id: str, *, reason: str, contact_id: str) -> dict[str, Any]:
        at, _ = self._now()
        engagement = self.engagement(engagement_id)
        engagement.land(reason=reason, at=at)
        result = {"engagement_id": engagement_id, "status": "landed", "reason": reason}
        self._save([engagement], at=at, actor=contact_id, result=result)
        return result

    def assign_responsible(
        self, engagement_id: str, *, role: str, contact_ref: str, contact_id: str
    ) -> dict[str, Any]:
        at, _ = self._now()
        engagement = self.engagement(engagement_id)
        engagement.assign_responsible(role=role, contact_ref=contact_ref, at=at)
        self._save([engagement], at=at, actor=contact_id)
        return {"engagement_id": engagement_id, "role": role, "contact_ref": contact_ref}

    # ================= 跨时区审批与人工调整 =================
    def open_approval(
        self,
        case_id: str,
        *,
        kind: str,
        engagement_id: str,
        command: str,
        payload: dict[str, Any],
        requested_by: str,
        requested_by_party: str,
        required_parties: list[str],
        zone: str = "UTC",
    ) -> dict[str, Any]:
        at, zone = self._now(zone)
        engagement = self.engagement(engagement_id)
        # 依据版本：组合流 + 组合内四类资源流。
        basis = {engagement.stream_id: engagement.version}
        for line in engagement.lines:
            resource = self.resource(line.resource_id)
            basis[resource.stream_id] = resource.version
        case = ApprovalCase.open(
            case_id,
            kind=kind,
            subject={"engagement_id": engagement_id, "project_id": engagement.project_id},  # type: ignore[arg-type]
            command=command,
            payload=payload,
            basis_versions=basis,
            requested_by=requested_by,
            requested_by_party=requested_by_party,
            required_parties=required_parties,
            at=at,
            zone=zone,
        )
        self._save([case], at=at, actor=requested_by)
        return {"case_id": case_id, "basis_versions": basis, "decision": "pending"}

    def endorse_approval(
        self,
        case_id: str,
        *,
        contact_ref: str,
        party_id: str,
        approve: bool,
        zone: str = "UTC",
        comment: str = "",
    ) -> dict[str, Any]:
        at, zone = self._now(zone)
        case = self.approval(case_id)
        decision = case.endorse(
            contact_ref=contact_ref, party_id=party_id, approve=approve, at=at, zone=zone, comment=comment
        )
        self._save([case], at=at, actor=contact_ref)
        return {"case_id": case_id, "decision": decision}

    def execute_approved(self, case_id: str, *, contact_id: str, request_id: str) -> dict[str, Any]:
        """执行已批准命令；依据版本过期则把审批案判为 stale，单一结果。

        业务变更、审批案"已执行"标记、幂等记录在同一提交内完成，
        命令成功与标记执行不可能分离，重放不会产生第二份承诺。
        """
        at, _ = self._now()
        self._require_new_request(request_id)
        case = self.approval(case_id)
        if case.decision != "approved":
            raise RuleViolation(f"审批案未批准：{case.decision}")
        if case.executed:
            raise RuleViolation("审批案已执行")

        # 版本核对：依据流是否被他人先行改动。
        latest = {stream_id: self.store.stream_version(stream_id) for stream_id in case.basis_versions}
        if latest != case.basis_versions:
            case.mark_stale(at=at, latest_versions=latest)
            self._save([case], at=at, actor=contact_id)
            return {"case_id": case_id, "decision": "stale", "latest_versions": latest}

        engagement_id = case.subject["engagement_id"]
        if case.command == "terminate_engagement":
            engagement = self.engagement(engagement_id)
            aggregates, outcome = self._build_termination(
                engagement, reason=case.payload["reason"], at=at, expiry=False
            )
        elif case.command == "change_quantities":
            engagement, aggregates, deltas = self._build_change(
                engagement_id, case.payload["changes"], case.payload["reason"], at
            )
            outcome = {"engagement_id": engagement_id, "deltas": deltas, "reason": case.payload["reason"]}
        elif case.command == "extend_engagement":
            engagement = self.engagement(engagement_id)
            engagement.extend(
                new_expires_at=case.payload["new_expires_at"], reason=case.payload["reason"], at=at
            )
            aggregates = [engagement]
            outcome = {
                "engagement_id": engagement_id,
                "expires_at": case.payload["new_expires_at"],
            }
        else:
            raise RuleViolation(f"审批案不支持的命令：{case.command}")

        case.mark_executed(at=at)
        aggregates.append(case)
        result = {"case_id": case_id, "decision": "executed", "outcome": outcome}
        self._save(aggregates, at=at, actor=contact_id, request_id=request_id, result=result)
        return result

    # ================= 项目接口 =================
    def project_view(self, project_id: str, *, now: str | None = None) -> dict[str, Any]:
        at, _ = self._now()
        project = self.project(project_id)
        engagements = [
            self.engagement(eid)
            for eid in self._engagement_ids_for_project(project_id)
        ]
        dossiers = [
            self.dossier(did)
            for did in project.dossier_ids
        ]
        view = build_project_view(project, engagements, dossiers, now=now or at)
        return {
            "project_id": view.project_id,
            "title": view.title,
            "status": view.status,
            "secretary": view.secretary,
            "responsible": view.responsible,
            "resource_gaps": view.resource_gaps,
            "resource_conflicts": view.resource_conflicts,
            "commitments": view.commitments,
            "diligence_todos": view.diligence_todos,
            "final_outcome": view.final_outcome,
        }

    def _engagement_ids_for_project(self, project_id: str) -> list[str]:
        ids: list[str] = []
        for engagement in self._all("eng-", Engagement):
            if engagement.project_id == project_id:  # type: ignore[union-attr]
                ids.append(engagement.engagement_id)  # type: ignore[union-attr]
        return ids

    # ================= 恢复处理 =================
    def recover(self) -> dict[str, Any]:
        """系统恢复后继续处理：暂留到期、尽调待办到期、履约提醒。

        全部转移由聚合当前状态守卫，重跑幂等，不会重复释放或重复通知。
        """
        at, _ = self._now()
        expired_holds: list[dict[str, Any]] = []
        expired_grants: list[dict[str, Any]] = []
        sent_reminders: list[dict[str, Any]] = []

        # 1. 暂留到期：只释放未履行份额。
        for engagement in self._all("eng-", Engagement):
            if engagement.status == "held" and engagement.expires_at and engagement.expires_at <= at:  # type: ignore[union-attr]
                expired_holds.append(
                    self._terminate_or_expire(engagement, reason=None, at=at, expiry=True)  # type: ignore[arg-type]
                )

        # 2. 尽调待办：生效授权到期未完成 → 终止；已完成记录保留。
        for dossier in self._all("dd-", Diligence):
            changed = False
            for grant in list(dossier.grants.values()):  # type: ignore[union-attr]
                if grant.status == "active" and grant.valid_until <= at:
                    dossier.revoke_grant(  # type: ignore[union-attr]
                        grant_id=grant.grant_id, reason="授权到期，尽调待办终止", at=at
                    )
                    expired_grants.append(
                        {"dossier_id": dossier.dossier_id, "grant_id": grant.grant_id}  # type: ignore[union-attr]
                    )
                    changed = True
            if changed:
                self.repo.save([dossier], at=at)

        # 3. 履约提醒：到期未发 → 发出并入 outbox；已发的不重复。
        for engagement in self._all("eng-", Engagement):
            changed = False
            for reminder in engagement.reminders:  # type: ignore[union-attr]
                if reminder["status"] == "pending" and reminder["due_at"] <= at:
                    if engagement.mark_reminder_sent(reminder_id=reminder["reminder_id"], at=at):  # type: ignore[arg-type]
                        notice = {
                            "type": "performance_reminder",
                            "engagement_id": engagement.engagement_id,
                            "reminder_id": reminder["reminder_id"],
                            "responsible": dict(engagement.responsible),  # type: ignore[arg-type]
                            "due_at": reminder["due_at"],
                            "at": at,
                        }
                        self.outbox.append(notice)
                        sent_reminders.append(notice)
                        changed = True
            if changed:
                self.repo.save([engagement], at=at)

        return {
            "at": at,
            "expired_holds": expired_holds,
            "expired_grants": expired_grants,
            "sent_reminders": sent_reminders,
        }

    def drain_outbox(self) -> list[dict[str, Any]]:
        items = list(self.outbox)
        self.outbox.clear()
        return items
