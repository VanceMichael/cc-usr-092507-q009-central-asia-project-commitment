import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _scenario
from central_asia_project_commitment.backend import Backend

from _scenario import (
    CA, CA_MGR, LN, LN_MGR, FakeClock, build_scenario, hold_engagement,
)


class RecoveryTest(unittest.TestCase):
    def _terms(self, clock, due_days=5):
        return {
            "commitment_no": "C-REC",
            "obligations": ["交付"],
            "due_at": (clock._at + timedelta(days=due_days)).isoformat(),
        }

    def test_recovery_expires_holds_grants_and_sends_reminders_once(self):
        clock = FakeClock()
        backend = Backend(clock=clock)
        build_scenario(backend)
        hold_engagement(backend, expires_in_days=14)
        backend.convert_to_commitment("eng-1", contact_id=LN_MGR,
                                      terms=self._terms(clock, due_days=5),
                                      request_id="req-conv-rec")

        # 尽调授权 3 天后到期
        backend.open_dossier("dd-1", project_id="proj-1")
        backend.register_material(
            "dd-1", owner_party_id=LN, contact_id=LN_MGR, material_id="m-1",
            title="财报", language="zh", confidentiality="L3",
            original_text_ref="store://m-1/orig")
        backend.grant_access(
            "dd-1", granter_party_id=LN, granter_contact_id=LN_MGR, grant_id="g-1",
            material_id="m-1", grantee_party_id=CA, grantee_contact_id=CA_MGR,
            purpose="financial",
            valid_until=(clock._at + timedelta(days=3)).isoformat())

        # ---- 崩溃发生，10 天后恢复 ----
        clock.advance(days=10)
        report = backend.recover()
        # 承诺不会因暂留到期被处理（已转承诺），这里没有 held 组合
        self.assertEqual(report["expired_grants"], [{"dossier_id": "dd-1", "grant_id": "g-1"}])
        self.assertEqual(len(report["sent_reminders"]), 1)
        self.assertEqual(report["sent_reminders"][0]["reminder_id"], "eng-1-due")
        # outbox 可被通知通道取走
        notices = backend.drain_outbox()
        self.assertEqual(len(notices), 1)
        self.assertIn("responsible", notices[0])

        # 重跑恢复：不重复终止、不重复通知
        second = backend.recover()
        self.assertEqual(second["expired_grants"], [])
        self.assertEqual(second["sent_reminders"], [])
        self.assertEqual(backend.drain_outbox(), [])
        self.assertEqual(backend.dossier("dd-1").grants["g-1"].status, "revoked")

    def test_held_engagement_expires_and_releases_on_recovery(self):
        clock = FakeClock()
        backend = Backend(clock=clock)
        build_scenario(backend)
        hold_engagement(backend, expires_in_days=14)
        # 交接一部分土地，验证到期只释放未履行份额
        backend.record_handover("eng-1", kind="land", qty=10000.0, detail="部分交地",
                                contact_id=CA_MGR)
        clock.advance(days=20)
        report = backend.recover()
        self.assertEqual(len(report["expired_holds"]), 1)
        released = {p["kind"]: p["release"] for p in report["expired_holds"][0]["released"]}
        retained = {p["kind"]: p["qty"] for p in report["expired_holds"][0]["retained_fulfilled"]}
        self.assertAlmostEqual(released["land"], 40000.0)
        self.assertAlmostEqual(retained["land"], 10000.0)
        self.assertEqual(backend.engagement("eng-1").status, "expired")
        # 再跑一次：状态已非 held，不会重复释放
        second = backend.recover()
        self.assertEqual(second["expired_holds"], [])

    def test_recovery_after_process_restart_from_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            clock = FakeClock()
            backend = Backend(path, clock=clock)
            build_scenario(backend)
            hold_engagement(backend, expires_in_days=14)
            backend.convert_to_commitment("eng-1", contact_id=LN_MGR,
                                          terms=self._terms(clock, 2),
                                          request_id="req-conv-disk")
            clock.advance(days=3)

            # 新进程：事件日志重放 + 恢复处理继续执行
            restarted = Backend(path, clock=clock)
            report = restarted.recover()
            self.assertEqual(len(report["sent_reminders"]), 1)
            self.assertEqual(restarted.engagement("eng-1").status, "converted")


class ProjectInterfaceTest(unittest.TestCase):
    def test_project_view_states_owner_gaps_history_and_outcome(self):
        clock = FakeClock()
        backend = Backend(clock=clock)
        build_scenario(backend)

        # 一个因冲突无法暂留的候选（先占掉班列窗口）
        hold_engagement(backend, engagement_id="eng-ok")
        deadline = (clock._at + timedelta(days=7)).isoformat()
        backend.propose_match(
            "eng-blocked", project_id="proj-1", demand_intent_id="int-ln", supply_intent_id="int-ca",
            proposed_expires_at=deadline,
            responsible={"secretary": "ch-sec"}, request_id="req-blocked")

        view = backend.project_view("proj-1")
        self.assertEqual(view["secretary"], "ch-sec")
        self.assertEqual(len(view["commitments"]), 2)
        # eng-ok 已暂留；eng-blocked 候选带冲突
        statuses = {c["engagement_id"]: c["status"] for c in view["commitments"]}
        self.assertEqual(statuses["eng-ok"], "held")
        self.assertEqual(statuses["eng-blocked"], "proposed")
        blocked_conflicts = [c for c in view["resource_conflicts"]
                             if c["engagement_id"] == "eng-blocked"]
        self.assertTrue(any(c["kind"] == "train_slot" for c in blocked_conflicts))

        # 推进落地后，最终原因可见
        backend.convert_to_commitment(
            "eng-ok", contact_id=LN_MGR,
            terms={"commitment_no": "C-9", "obligations": ["x"],
                   "due_at": (clock._at + timedelta(days=30)).isoformat()},
            request_id="req-conv-ok")
        backend.land_engagement("eng-ok", reason="建成投产", contact_id=LN_MGR)
        view = backend.project_view("proj-1")
        self.assertEqual(view["final_outcome"]["engagement_id"], "eng-ok")
        self.assertEqual(view["final_outcome"]["reason"], "建成投产")
        # 历次承诺保留两条
        self.assertEqual(len(view["commitments"]), 2)
        landed = next(c for c in view["commitments"] if c["engagement_id"] == "eng-ok")
        self.assertEqual(landed["final_reason"], "建成投产")
        self.assertEqual(landed["commitment_no"], "C-9")


if __name__ == "__main__":
    unittest.main()
