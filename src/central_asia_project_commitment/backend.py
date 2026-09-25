"""跨境合作承诺后端：应用服务门面。

所有用例在此编排：主体版本、意向、匹配、双方确认、组合原子暂留、
发起/复核分离的调整、尽调授权、系统恢复后的到期/待办/提醒扫描。
所有产生承诺或资源效果的命令都接受 request_key，重放不产生第二份结果。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .clock import Clock, to_iso
from .commitments import Commitment, HELD
from .duediligence import DueDiligenceCase
from .errors import IdempotencyReplayed, NotFound, PolicyViolation, StateConflict, VersionConflict
from .intents import Intent
from .matching import evaluate_pair
from .parties import Contact, Party
from .projects import Project
from .projections import ReadModel
from .resources import LEDGER_STREAM, ResourceLedger
from .store import EventStore, fingerprint

SCHEDULER_STREAM = "scheduler"


class Backend:
    def __init__(self, store: EventStore, clock: Clock):
        self.store = store
        self.clock = clock

    # ---------- 基础读取 ----------
    def now(self) -> str:
        return to_iso(self.clock.current())

    def read_model(self) -> ReadModel:
        return ReadModel(self.store)

    def _party(self, party_id: str) -> Party:
        return self._load(f"party:{party_id}", Party)

    def _intent(self, intent_id: str) -> Intent:
        return self._load(f"intent:{intent_id}", Intent)

    def _commitment(self, commitment_id: str) -> Commitment:
        return self._load(f"commitment:{commitment_id}", Commitment)

    def _dd(self, case_id: str) -> DueDiligenceCase:
        return self._load(f"dd:{case_id}", DueDiligenceCase)

    def _project(self, project_id: str) -> Project:
        return self._load(f"project:{project_id}", Project)

    def _ledger(self) -> ResourceLedger:
        ledger = ResourceLedger()
        return ledger.load(self.store.read_stream(LEDGER_STREAM))

    def _load(self, stream: str, factory):
        state = factory()
        events = self.store.read_stream(stream)
        if not events:
            raise NotFound(f"不存在：{stream}")
        return state.load(events)

    def _replayed(self, request_key: str | None, payload: Any) -> dict[str, Any] | None:
        """命令最前面的重放预查：请求已处理过则直接返回首次结果。

        这样即使对方节点已把承诺推进到下一状态（如 held），
        跨时区重放同一请求也不会因状态校验失败，仍返回单一结果。
        """
        if request_key is None:
            return None
        record = self.store.request_record(request_key)
        if record is None:
            return None
        if record["fingerprint"] != fingerprint(payload):
            raise IdempotencyReplayed(f"请求键 {request_key} 已用于不同载荷")
        return {**record["result_ref"], "replayed": True}

    def _save(self, state, events: list, extra_writes=None, request_key=None,
              payload=None, result_ref=None, verifier=None) -> dict[str, Any]:
        writes: list = [(state.stream_id, state.version, events)]
        if extra_writes:
            writes.extend(extra_writes)
        ts = self.now()
        ref = dict(result_ref or {})
        if verifier is not None or request_key is not None:
            result = self.store.guarded_append_many(
                writes,
                ts,
                verifier or (lambda: None),
                request_key=request_key,
                payload_fingerprint=fingerprint(payload) if request_key else "",
                result_ref=ref,
            )
        else:
            self.store.append_many(writes, ts)
            result = {**ref, "replayed": False}
        if not result.get("replayed"):
            for event_type, data in events:
                state.apply_event(state, event_type, data)
                state.version += 1
        return result

    # ---------- 主体与联系人 ----------
    def register_party(
        self, party_id: str, kind: str, name: str, timezone: str,
        profile: dict[str, Any], country: str = "", request_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {"op": "register_party", "party_id": party_id, "profile": profile}
        if (replayed := self._replayed(request_key, payload)) is not None:
            return replayed
        party = Party()
        events = party.register(party_id, kind, name, timezone, profile, country)
        return self._save(
            party, events, request_key=request_key, payload=payload,
            result_ref={"party_id": party_id, "version": 1},
        )

    def new_party_version(self, party_id: str, profile: dict[str, Any], reason: str) -> dict[str, Any]:
        party = self._party(party_id)
        events = party.new_profile_version(profile, reason)
        result = self._save(party, events, result_ref={
            "party_id": party_id, "version": party.version + len(events)
        })
        return result

    def add_contact(self, party_id: str, contact: Contact) -> dict[str, Any]:
        if not contact.added_at:
            contact.added_at = self.now()
        party = self._party(party_id)
        events = party.add_contact(contact)
        return self._save(party, events, result_ref={"contact_id": contact.contact_id})

    def contact_departed(self, party_id: str, contact_id: str) -> dict[str, Any]:
        """联系人离任：标记离任，并终止其在所有尽调案件中尚未完成的访问。

        已完成访问与历史授权记录继续留存。
        """
        at = self.now()
        party = self._party(party_id)
        events = party.contact_departed(contact_id, at)
        extra_writes: list = []
        for case in self.read_model().dd_cases.values():
            dd_events = case.revoke_for_contact_departure(contact_id, at)
            if dd_events:
                extra_writes.append((case.stream_id, case.version, dd_events))
        self._save(party, events, extra_writes=extra_writes)
        return {"contact_id": contact_id, "departed_at": at,
                "cases_updated": len(extra_writes)}

    def _require_active_contact(self, contact_id: str) -> None:
        for party in self.read_model().parties.values():
            contact = party.contacts.get(contact_id)
            if contact is not None:
                party.active_contact(contact_id)
                return
        raise PolicyViolation(f"联系人不在册：{contact_id}")

    # ---------- 项目与意向 ----------
    def open_project(self, project_id: str, title: str, secretary_contact_id: str,
                     request_key: str | None = None) -> dict[str, Any]:
        payload = {"op": "open_project", "project_id": project_id}
        if (replayed := self._replayed(request_key, payload)) is not None:
            return replayed
        project = Project()
        events = project.open(project_id, title, secretary_contact_id)
        return self._save(project, events, request_key=request_key,
                          payload=payload, result_ref={"project_id": project_id})

    def link_intent(self, project_id: str, intent_id: str) -> None:
        project = self._project(project_id)
        self._save(project, project.link_intent(intent_id))

    def link_dd_case(self, project_id: str, case_id: str) -> None:
        project = self._project(project_id)
        self._save(project, project.link_dd_case(case_id))

    def file_intent(
        self, intent_id: str, owner_party_id: str, contact_id: str, kind: str,
        title: str, demands: list[dict[str, Any]], offers: list[dict[str, Any]],
        owner_version: int | None = None, request_key: str | None = None,
    ) -> dict[str, Any]:
        owner = self._party(owner_party_id)
        owner.active_contact(contact_id)  # 联系人必须在册且未离任
        version = owner_version or owner.latest_version_no
        owner.profile_at(version)  # 引用的主体版本必须存在
        payload = {
            "op": "file_intent", "intent_id": intent_id,
            "owner": owner_party_id, "version": version,
            "demands": demands, "offers": offers,
        }
        if (replayed := self._replayed(request_key, payload)) is not None:
            return replayed
        intent = Intent()
        events = intent.file(
            intent_id, owner_party_id, version, contact_id, kind, title, demands, offers
        )
        return self._save(intent, events, request_key=request_key, payload=payload,
                          result_ref={"intent_id": intent_id})

    # ---------- 资源池 ----------
    def register_pool(self, pool_id: str, kind: str, name: str, unit: str, total: float,
                      region: str = "", corridor: str = "", attrs: dict[str, Any] | None = None) -> None:
        ledger = self._ledger()
        self._save(ledger, ledger.register_pool(
            pool_id, kind, name, unit, total, region, corridor, attrs))

    # ---------- 候选匹配（只读） ----------
    def match(self, intent_a_id: str, intent_b_id: str,
              candidate_bundle: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        intent_a = self._intent(intent_a_id)
        intent_b = self._intent(intent_b_id)
        if intent_a.status != "open" or intent_b.status != "open":
            raise StateConflict("只有开放中的意向可以匹配")
        ledger = self._ledger()
        return evaluate_pair(intent_a.to_dict(), intent_b.to_dict(), ledger, candidate_bundle)

    # ---------- 承诺：发起 ----------
    def propose_commitment(
        self, commitment_id: str, intent_a_id: str, intent_b_id: str,
        proposer_party_id: str, proposer_contact_id: str, counterpart_contact_id: str,
        bundle: list[dict[str, Any]], ttl_seconds: int, project_id: str = "",
        request_key: str | None = None,
    ) -> dict[str, Any]:
        idem_payload = {
            "op": "propose_commitment", "commitment_id": commitment_id,
            "intents": [intent_a_id, intent_b_id], "bundle": bundle,
            "ttl_seconds": ttl_seconds,
        }
        if (replayed := self._replayed(request_key, idem_payload)) is not None:
            return replayed
        intent_a = self._intent(intent_a_id)
        intent_b = self._intent(intent_b_id)
        party_a = self._party(intent_a.owner_party_id)
        party_b = self._party(intent_b.owner_party_id)
        proposer_party = party_a if proposer_party_id == intent_a.owner_party_id else party_b
        if proposer_party_id not in (intent_a.owner_party_id, intent_b.owner_party_id):
            raise PolicyViolation("发起人必须是意向双方之一")
        proposer_party.active_contact(proposer_contact_id)
        counterpart_party = party_b if proposer_party is party_a else party_a
        counterpart_party.active_contact(counterpart_contact_id)
        # 组合原子：土地/仓容/班列/专家支持四类齐全
        ledger = self._ledger()
        unknown = [item["pool_id"] for item in bundle if item["pool_id"] not in ledger.pools]
        if unknown:
            raise PolicyViolation(f"资源池不存在：{unknown}")
        kinds = {ledger.pools[item["pool_id"]].kind for item in bundle}
        missing = {"land", "warehouse", "train_window", "expert"} - kinds
        if missing:
            raise PolicyViolation(f"组合原子缺少资源类别：{sorted(missing)}")
        match = evaluate_pair(
            intent_a.to_dict(), intent_b.to_dict(), ledger, bundle
        )
        if not match["ready_to_hold"]:
            raise PolicyViolation(f"匹配未就绪：缺口 {match['gaps']}；冲突 {match['resource_conflicts']}")
        commitment = Commitment()
        events = commitment.propose(
            commitment_id,
            intent_a.owner_party_id, intent_a.owner_version,
            intent_b.owner_party_id, intent_b.owner_version,
            intent_a_id, intent_b_id,
            proposer_contact_id, counterpart_contact_id,
            bundle, ttl_seconds, match, project_id=project_id,
            proposer_party_id=proposer_party_id,
        )
        return self._save(commitment, events, request_key=request_key, payload=idem_payload,
                          result_ref={"commitment_id": commitment_id, "status": "proposed"})

    # ---------- 承诺：双方确认并暂留 ----------
    def confirm_commitment(self, commitment_id: str, party_id: str, contact_id: str,
                           request_key: str | None = None) -> dict[str, Any]:
        idem_payload = {"op": "confirm", "commitment_id": commitment_id, "party_id": party_id}
        if (replayed := self._replayed(request_key, idem_payload)) is not None:
            return replayed
        at = self.now()
        self._party(party_id).active_contact(contact_id)
        # 先在当前状态做一次校验，尽早给出清晰错误（锁内仍会重算）
        commitment = self._commitment(commitment_id)
        commitment.confirm(party_id, contact_id, at)

        def decider():
            # 锁内依据最新版本重新决策：双方可能在其他时区刚刚确认
            fresh = self._commitment(commitment_id)
            events = fresh.confirm(party_id, contact_id, at)
            becomes_held = any(e[0] == "CommitmentHeld" for e in events)
            writes = [(fresh.stream_id, fresh.version, events)]
            if becomes_held:
                fresh_ledger = self._ledger()
                ledger_events = fresh_ledger.hold_bundle(commitment_id, fresh.bundle)
                writes.append(
                    (LEDGER_STREAM, fresh_ledger.version, ledger_events)
                )
            ref = {"commitment_id": commitment_id,
                   "status": "held" if becomes_held else "awaiting_counterpart"}
            return writes, ref, [(fresh, events)]

        replayed_flag, ref, apply_pairs = self.store.commit_unit(
            decider, at, request_key=request_key,
            payload_fingerprint=fingerprint(idem_payload),
        )
        if not replayed_flag:
            for state, state_events in apply_pairs:
                for event_type, data in state_events:
                    state.apply_event(state, event_type, data)
                    state.version += 1
        return {**ref, "replayed": replayed_flag}

    # ---------- 履约交接 ----------
    def register_handover(self, commitment_id: str, pool_id: str, qty: float,
                          handover_ref: str, fee: float,
                          request_key: str | None = None) -> dict[str, Any]:
        idem_payload = {"op": "handover", "commitment_id": commitment_id,
                        "pool_id": pool_id, "qty": qty,
                        "handover_ref": handover_ref, "fee": fee}
        if (replayed := self._replayed(request_key, idem_payload)) is not None:
            return replayed
        at = self.now()
        commitment = self._commitment(commitment_id)
        ledger = self._ledger()
        c_events = commitment.register_handover(pool_id, qty, handover_ref, fee, at)
        l_events = ledger.fulfill_line(commitment_id, pool_id, qty, handover_ref, fee)
        result = self._save(
            commitment, c_events,
            extra_writes=[(LEDGER_STREAM, ledger.version, l_events)],
            request_key=request_key,
            payload=idem_payload,
            result_ref={"handover_ref": handover_ref},
        )
        return result

    # ---------- 人工调整：申请与独立复核 ----------
    def request_adjustment(self, commitment_id: str, request_id: str, kind: str,
                           requested_by_contact_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_active_contact(requested_by_contact_id)
        commitment = self._commitment(commitment_id)
        events = commitment.request_adjustment(
            request_id, kind, requested_by_contact_id, payload, self.now()
        )
        return self._save(commitment, events, result_ref={"request_id": request_id, "status": "pending"})

    def approve_adjustment(self, commitment_id: str, request_id: str,
                           reviewer_contact_id: str, note: str = "",
                           request_key: str | None = None) -> dict[str, Any]:
        """复核通过并生效；资源侧同步只释放未履行份额。"""
        idem_payload = {"op": "approve_adjustment", "commitment_id": commitment_id,
                        "request_id": request_id}
        if (replayed := self._replayed(request_key, idem_payload)) is not None:
            return replayed
        self._require_active_contact(reviewer_contact_id)
        at = self.now()
        # 锁外先校验一次以尽早失败（职责分离、申请状态）；锁内仍会重算
        early = self._commitment(commitment_id)
        early.approve_adjustment(request_id, reviewer_contact_id, at, note)
        kind = early.adjustments[request_id].kind

        def decider():
            # 锁内依据最新版本重新决策：他方可能刚登记交接或生效了另一调整
            fresh = self._commitment(commitment_id)
            c_events = fresh.approve_adjustment(request_id, reviewer_contact_id, at, note)
            payload = fresh.adjustments[request_id].payload
            fresh_ledger = self._ledger()
            ledger_events = self._ledger_effects_for_adjustment(
                commitment_id, kind, payload, fresh_ledger
            )
            # 改量追加 / 延期可能引入新的资源争用，锁内重检
            if kind in ("resize", "extend"):
                lines = fresh_ledger.allocations.get(commitment_id, [])
                if kind == "resize":
                    wanted = {item["pool_id"]: item["qty"] for item in payload["bundle"]}
                    probe = [
                        {"pool_id": line.pool_id, "qty": wanted[line.pool_id],
                         "start": line.start, "end": line.end}
                        for line in lines
                    ]
                else:
                    new_end = payload.get("new_end") or payload["new_deadline"]
                    probe = [
                        {"pool_id": line.pool_id,
                         "qty": line.qty_held - line.qty_released,
                         "start": line.start, "end": new_end}
                        for line in lines
                    ]
                conflicts = fresh_ledger.check_bundle(probe, exclude_commitment=commitment_id)
                if conflicts:
                    from .errors import ResourceConflict

                    raise ResourceConflict(conflicts)
            writes = [(fresh.stream_id, fresh.version, c_events)]
            if ledger_events:
                writes.append((LEDGER_STREAM, fresh_ledger.version, ledger_events))
            ref = {"request_id": request_id, "approved": True, "kind": kind}
            return writes, ref, [(fresh, c_events)]

        replayed_flag, ref, apply_pairs = self.store.commit_unit(
            decider, at, request_key=request_key,
            payload_fingerprint=fingerprint(idem_payload),
        )
        if not replayed_flag:
            for state, state_events in apply_pairs:
                for event_type, data in state_events:
                    state.apply_event(state, event_type, data)
                    state.version += 1
        return {**ref, "replayed": replayed_flag}

    def reject_adjustment(self, commitment_id: str, request_id: str,
                          reviewer_contact_id: str, note: str) -> dict[str, Any]:
        self._require_active_contact(reviewer_contact_id)
        commitment = self._commitment(commitment_id)
        events = commitment.reject_adjustment(request_id, reviewer_contact_id, self.now(), note)
        return self._save(commitment, events,
                          result_ref={"request_id": request_id, "approved": False})

    def _ledger_effects_for_adjustment(self, commitment_id: str, kind: str,
                                       payload: dict[str, Any], ledger: ResourceLedger) -> list:
        events: list = []
        lines = ledger.allocations.get(commitment_id, [])
        if kind == "resize":
            wanted = {item["pool_id"]: item["qty"] for item in payload["bundle"]}
            for line in lines:
                new_qty = wanted[line.pool_id]
                if new_qty < line.qty_fulfilled - 1e-9:
                    raise PolicyViolation(
                        f"{line.pool_id} 已交接 {line.qty_fulfilled}，"
                        f"改量后 {new_qty} 不能低于已履行份额"
                    )
                current_occupied = line.qty_held - line.qty_released
                if new_qty < current_occupied - 1e-9:
                    events.extend(ledger.reduce_line(
                        commitment_id, line.pool_id, current_occupied - new_qty,
                        "改量：释放未履行份额"))
                elif new_qty > current_occupied + 1e-9:
                    events.extend(ledger.increase_line(
                        commitment_id, line.pool_id, new_qty - current_occupied,
                        "改量：追加占用"))
        elif kind == "extend":
            new_end = payload.get("new_end") or payload["new_deadline"]
            for line in lines:
                events.extend(ledger.extend_line(commitment_id, line.pool_id, new_end))
        elif kind == "terminate":
            reason = payload.get("reason", "人工终止：释放未履行份额")
            for line in lines:
                unfulfilled = line.unfulfilled
                if unfulfilled > 1e-9:
                    events.extend(
                        ledger.reduce_line(commitment_id, line.pool_id, unfulfilled, reason)
                    )
        return events

    def _release_unfulfilled(self, ledger: ResourceLedger, commitment_id: str,
                             reason: str) -> list:
        events: list = []
        for line in ledger.allocations.get(commitment_id, []):
            if line.unfulfilled > 1e-9:
                events.extend(
                    ledger.reduce_line(commitment_id, line.pool_id, line.unfulfilled, reason)
                )
        return events

    # ---------- 终态：落地 ----------
    def land_commitment(self, commitment_id: str, outcome_summary: str,
                        request_key: str | None = None) -> dict[str, Any]:
        idem_payload = {"op": "land", "commitment_id": commitment_id}
        if (replayed := self._replayed(request_key, idem_payload)) is not None:
            return replayed
        at = self.now()
        commitment = self._commitment(commitment_id)
        ledger = self._ledger()
        events = commitment.land(outcome_summary, at)
        ledger_events = self._release_unfulfilled(ledger, commitment_id, "落地：剩余未履行份额释放")
        extra = [(LEDGER_STREAM, ledger.version, ledger_events)] if ledger_events else []
        return self._save(commitment, events, extra_writes=extra,
                          request_key=request_key,
                          payload=idem_payload,
                          result_ref={"commitment_id": commitment_id, "status": "landed"})

    # ---------- 尽调 ----------
    def open_dd_case(self, case_id: str, project_id: str, owner_party_id: str) -> dict[str, Any]:
        case = DueDiligenceCase()
        return self._save(case, case.open_case(case_id, project_id, owner_party_id),
                          result_ref={"case_id": case_id})

    def add_dd_material(self, case_id: str, language: str, title: str, ref: str,
                        corrects_version: int | None = None, note: str = "") -> dict[str, Any]:
        case = self._dd(case_id)
        events = case.add_material(
            language, title, ref, self.now(), corrects_version, note
        )
        return self._save(case, events, result_ref={"ref": ref})

    def grant_dd_access(self, case_id: str, grant_id: str, contact_id: str, purpose: str,
                        material_refs: list[str], ttl: timedelta) -> dict[str, Any]:
        case = self._dd(case_id)
        events = case.grant_access(
            grant_id, contact_id, purpose, material_refs,
            to_iso(self.clock.current() + ttl),
        )
        return self._save(case, events, result_ref={"grant_id": grant_id})

    def request_dd_visit(self, case_id: str, grant_id: str, visit_id: str,
                         purpose: str, material_ref: str,
                         request_key: str | None = None) -> dict[str, Any]:
        idem_payload = {"op": "dd_visit", "visit_id": visit_id}
        if (replayed := self._replayed(request_key, idem_payload)) is not None:
            return replayed
        case = self._dd(case_id)
        events = case.request_visit(grant_id, visit_id, purpose, material_ref, self.now())
        return self._save(case, events, request_key=request_key,
                          payload=idem_payload,
                          result_ref={"visit_id": visit_id, "status": "pending"})

    def complete_dd_visit(self, case_id: str, visit_id: str) -> None:
        case = self._dd(case_id)
        self._save(case, case.complete_visit(visit_id, self.now()))

    # ---------- 系统恢复后的到期/待办/提醒扫描 ----------
    def run_due_jobs(self, reminder_lead: timedelta = timedelta(hours=24)) -> dict[str, Any]:
        """恢复后调用：处理暂留到期、尽调到期、履约提醒。幂等，可重复运行。"""
        now = self.now()
        expired_commitments: list[str] = []
        expired_grants: list[str] = []
        reminders: list[dict[str, Any]] = []
        dd_todos: list[dict[str, Any]] = []

        model = self.read_model()

        # 1) 暂留到期：过期并释放全部未履行份额（已交接与费用留存）
        for commitment in model.commitments.values():
            if commitment.is_expired_at(now):
                try:
                    self._expire_commitment(commitment.commitment_id, now)
                    expired_commitments.append(commitment.commitment_id)
                except (StateConflict, VersionConflict):
                    # 其他时区的节点已处理，争用只产生一个结果
                    continue

        # 2) 尽调授权到期
        for case in model.dd_cases.values():
            events = case.expire_due(now)
            if events:
                try:
                    self._save(case, events)
                    expired_grants.extend(e[1]["grant_id"] for e in events)
                except (StateConflict, VersionConflict):
                    continue
            dd_todos.extend(case.pending_todos(now))

        # 3) 履约提醒：临期仍有未履行份额，每个承诺每轮只发一次
        model = self.read_model()
        emitted = self._emitted_reminders()
        for commitment in model.commitments.values():
            if commitment.status != HELD or not commitment.deadline:
                continue
            from .clock import parse

            if parse(commitment.deadline) - parse(now) <= reminder_lead and commitment.unfulfilled_lines():
                key = f"deadline:{commitment.commitment_id}:{commitment.deadline}"
                if key in emitted:
                    continue
                self._emit_reminder(key, {
                    "type": "deadline_approaching",
                    "commitment_id": commitment.commitment_id,
                    "responsible": commitment.current_responsible,
                    "deadline": commitment.deadline,
                    "unfulfilled": commitment.unfulfilled_lines(),
                }, now)
                reminders.append(key)

        return {
            "ran_at": now,
            "expired_commitments": expired_commitments,
            "expired_dd_grants": expired_grants,
            "dd_pending_todos": dd_todos,
            "reminders": reminders,
        }

    def _expire_commitment(self, commitment_id: str, now: str) -> bool:
        commitment = self._commitment(commitment_id)
        ledger = self._ledger()
        events = commitment.expire(now)
        ledger_events = self._release_unfulfilled(ledger, commitment_id, "暂留到期：释放未履行份额")
        writes = [(commitment.stream_id, commitment.version, events)]
        if ledger_events:
            writes.append((LEDGER_STREAM, ledger.version, ledger_events))
        self.store.append_many(writes, now)
        return True

    def _emitted_reminders(self) -> set[str]:
        emitted: set[str] = set()
        for event in self.store.read_stream(SCHEDULER_STREAM):
            if event.type == "ReminderEmitted":
                emitted.add(event.data["reminder_key"])
        return emitted

    def _emit_reminder(self, key: str, payload: dict[str, Any], now: str) -> None:
        version = self.store.stream_version(SCHEDULER_STREAM)
        self.store.append_many(
            [(SCHEDULER_STREAM, version,
              [("ReminderEmitted", {"reminder_key": key, **payload})])],
            now,
        )

    # ---------- 项目接口 ----------
    def project_view(self, project_id: str) -> dict[str, Any]:
        model = self.read_model()
        if project_id not in model.projects:
            raise NotFound(f"项目不存在：{project_id}")
        return model.project_view(project_id, self.now())
