import unittest
from datetime import timedelta

import _scenario
from central_asia_project_commitment.backend import Backend
from central_asia_project_commitment.errors import (
    AuthorizationError, IdempotencyReplay, RuleViolation,
)

from _scenario import (
    CA, CA_ADMIN, CA_MGR, CHAMBER, CH_SEC, LN, LN_ADMIN, LN_MGR, FakeClock,
    build_scenario, hold_engagement,
)


def terms(clock, *, commitment_no="C-001", due_days=100):
    return {
        "commitment_no": commitment_no,
        "obligations": ["厂区土建", "年供小麦 5000 吨", "班列回运"],
        "due_at": (clock._at + timedelta(days=due_days)).isoformat(),
    }


class CommitmentLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.backend = Backend(clock=self.clock)
        build_scenario(self.backend)
        hold_engagement(self.backend)
        self.backend.convert_to_commitment(
            "eng-1", contact_id=LN_MGR, terms=terms(self.clock), request_id="req-conv")

    def test_handover_and_fees_are_immutable_facts(self):
        self.clock.advance(days=10)
        self.backend.record_handover("eng-1", kind="land", qty=20000.0, detail="一期用地交付",
                                     contact_id=CA_MGR)
        self.backend.record_fee("eng-1", amount=150000.0, currency="CNY",
                                purpose="土地平整费", contact_id=CA_MGR)
        eng = self.backend.engagement("eng-1")
        self.assertEqual(eng.handovers[0]["detail"], "一期用地交付")
        self.assertEqual(eng.fees[0]["purpose"], "土地平整费")

    def test_change_cannot_cut_below_fulfilled_share(self):
        self.backend.record_handover("eng-1", kind="land", qty=20000.0, detail="一期用地",
                                     contact_id=CA_MGR)
        # 50000 -> 40000 合法（释放 10000 未履行）
        result = self.backend.change_quantities(
            "eng-1", changes={"land": 40000.0}, reason="设计调整",
            contact_id=LN_MGR, request_id="req-change-1")
        self.assertAlmostEqual(result["deltas"]["land"]["reduce"], 10000.0)
        self.assertAlmostEqual(self.backend.resource("land-1").occupied, 40000.0)
        # 40000 -> 10000 非法（低于已交接 20000）
        with self.assertRaises(RuleViolation):
            self.backend.change_quantities(
                "eng-1", changes={"land": 10000.0}, reason="试图抹掉交接",
                contact_id=LN_MGR, request_id="req-change-2")

    def test_increase_then_resource_capacity_rejects_whole_change_atomically(self):
        # 班列已占 35/40，增加 10 必失败；组合与库存都不得留下半笔变更
        with self.assertRaises(RuleViolation):
            self.backend.change_quantities(
                "eng-1", changes={"train_slot": 45.0}, reason="加舱",
                contact_id=LN_MGR, request_id="req-change-train")
        eng = self.backend.engagement("eng-1")
        self.assertAlmostEqual(eng._line("train_slot").qty, 35.0)
        self.assertAlmostEqual(self.backend.resource("train-1").occupied, 35.0)

    def test_change_request_replay_has_no_second_effect(self):
        self.backend.change_quantities(
            "eng-1", changes={"warehouse": 1000.0}, reason="缩容",
            contact_id=LN_MGR, request_id="req-replay")
        with self.assertRaises(IdempotencyReplay):
            self.backend.change_quantities(
                "eng-1", changes={"warehouse": 500.0}, reason="缩容",
                contact_id=LN_MGR, request_id="req-replay")
        self.assertAlmostEqual(self.backend.resource("wh-1").occupied, 1000.0)

    def test_extend_only_moves_deadline_forward(self):
        old_due = self.backend.engagement("eng-1").terms["due_at"]
        new_due = (self.clock._at + timedelta(days=200)).isoformat()
        self.backend.extend_engagement("eng-1", new_expires_at=new_due, reason="土建延期",
                                       contact_id=LN_MGR, request_id="req-ext-1")
        self.assertEqual(self.backend.engagement("eng-1").terms["due_at"], new_due)
        with self.assertRaises(RuleViolation):
            self.backend.extend_engagement("eng-1", new_expires_at=old_due, reason="倒退",
                                           contact_id=LN_MGR, request_id="req-ext-2")

    def test_terminate_releases_only_unfulfilled_and_retains_facts(self):
        self.backend.record_handover("eng-1", kind="land", qty=20000.0, detail="一期用地",
                                     contact_id=CA_MGR)
        self.backend.record_handover("eng-1", kind="train_slot", qty=10.0, detail="首列已发",
                                     contact_id=CA_MGR)
        self.backend.record_fee("eng-1", amount=80000.0, currency="USD",
                                purpose="班列定金", contact_id=CA_MGR)
        result = self.backend.terminate_engagement(
            "eng-1", reason="哈方政策调整，项目终止", contact_id=LN_MGR, request_id="req-term")
        released = {p["kind"]: p["release"] for p in result["released"]}
        retained = {p["kind"]: p["qty"] for p in result["retained_fulfilled"]}
        self.assertAlmostEqual(released["land"], 30000.0)
        self.assertAlmostEqual(retained["land"], 20000.0)
        self.assertAlmostEqual(released["train_slot"], 25.0)
        self.assertAlmostEqual(retained["train_slot"], 10.0)
        # 库存：土地占用只剩已交接份额
        self.assertAlmostEqual(self.backend.resource("land-1").occupied, 20000.0)
        self.assertAlmostEqual(self.backend.resource("train-1").occupied, 10.0)
        # 事实留存
        eng = self.backend.engagement("eng-1")
        self.assertEqual(eng.status, "terminated")
        self.assertEqual(eng.final_reason, "哈方政策调整，项目终止")
        self.assertEqual(len(eng.fees), 1)
        self.assertEqual(len(eng.handovers), 2)
        self.assertEqual(result["retained_fees"], 1)

    def test_land_records_final_reason(self):
        self.clock.advance(days=120)
        result = self.backend.land_engagement("eng-1", reason="验收合格，园区投产",
                                              contact_id=LN_MGR)
        self.assertEqual(result["status"], "landed")
        self.assertEqual(self.backend.engagement("eng-1").final_reason, "验收合格，园区投产")


class DiligenceTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.backend = Backend(clock=self.clock)
        build_scenario(self.backend)
        hold_engagement(self.backend)
        self.backend.open_dossier("dd-1", project_id="proj-1")
        self.backend.register_material(
            "dd-1", owner_party_id=LN, contact_id=LN_MGR, material_id="m-1",
            title="土地权属证明", language="zh", confidentiality="L3",
            original_text_ref="store://m-1/original")
        self.future = (self.clock._at + timedelta(days=30)).isoformat()
        self.backend.grant_access(
            "dd-1", granter_party_id=LN, granter_contact_id=LN_MGR, grant_id="g-1",
            material_id="m-1", grantee_party_id=CA, grantee_contact_id=CA_MGR,
            purpose="site_verification", valid_until=self.future)

    def test_use_must_match_granted_purpose(self):
        self.backend.use_material("dd-1", grant_id="g-1", contact_id=CA_MGR,
                                  purpose="site_verification", note="现场核对")
        with self.assertRaises(AuthorizationError):
            self.backend.use_material("dd-1", grant_id="g-1", contact_id=CA_MGR,
                                      purpose="financial")

    def test_translation_correction_keeps_original_and_old_versions(self):
        self.backend.submit_translation(
            "dd-1", material_id="m-1", translator_contact_ref=CH_SEC,
            language="ru", text_ref="store://m-1/ru-v1")
        self.backend.correct_translation(
            "dd-1", material_id="m-1", corrector_contact_ref=CH_SEC,
            text_ref="store://m-1/ru-v2", note="地名拼写更正")
        versions = self.backend.dossier("dd-1").materials["m-1"].translations
        kinds = [(v["kind"], v["translation_version"]) for v in versions]
        self.assertEqual(kinds, [("original", 0), ("translation", 1), ("correction", 2)])
        self.assertEqual(versions[0]["text_ref"], "store://m-1/original")
        self.assertEqual(versions[1]["text_ref"], "store://m-1/ru-v1")
        self.assertEqual(versions[2]["note"], "地名拼写更正")


class ApprovalTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.backend = Backend(clock=self.clock)
        build_scenario(self.backend)
        hold_engagement(self.backend)

    def test_manual_adjustment_requires_independent_reviewer(self):
        self.backend.open_approval(
            "apr-1", kind="manual_adjustment", engagement_id="eng-1",
            command="change_quantities", payload={"changes": {"warehouse": 1500.0}, "reason": "人工调减"},
            requested_by=LN_MGR, requested_by_party=LN, required_parties=[CHAMBER])
        with self.assertRaises(AuthorizationError):
            self.backend.endorse_approval(
                "apr-1", contact_ref=LN_MGR, party_id=CHAMBER, approve=True)
        decision = self.backend.endorse_approval(
            "apr-1", contact_ref=CH_SEC, party_id=CHAMBER, approve=True)
        self.assertEqual(decision["decision"], "approved")
        result = self.backend.execute_approved(
            "apr-1", contact_id=CH_SEC, request_id="req-apr-exec")
        self.assertEqual(result["decision"], "executed")
        self.assertAlmostEqual(self.backend.resource("wh-1").occupied, 1500.0)
        # 审批案不可二次执行
        with self.assertRaises(RuleViolation):
            self.backend.execute_approved("apr-1", contact_id=CH_SEC, request_id="req-apr-exec2")

    def test_cross_party_approval_single_outcome_and_replay(self):
        self.backend.convert_to_commitment(
            "eng-1", contact_id=LN_MGR, terms=terms(self.clock), request_id="req-conv2")
        self.backend.open_approval(
            "apr-2", kind="cross_party", engagement_id="eng-1",
            command="terminate_engagement", payload={"reason": "双方同意终止"},
            requested_by=LN_MGR, requested_by_party=LN, required_parties=[LN, CA])
        self.backend.endorse_approval("apr-2", contact_ref=LN_MGR, party_id=LN, approve=True,
                                      zone="Asia/Shanghai")
        pending = self.backend.approval("apr-2")
        self.assertEqual(pending.decision, "pending")
        self.backend.endorse_approval("apr-2", contact_ref=CA_MGR, party_id=CA, approve=True,
                                      zone="Asia/Almaty")
        first = self.backend.execute_approved("apr-2", contact_id=CH_SEC, request_id="req-term-x")
        self.assertEqual(first["decision"], "executed")
        self.assertEqual(self.backend.engagement("eng-1").status, "terminated")
        # 同一请求重放不产生第二份效果
        with self.assertRaises(IdempotencyReplay):
            self.backend.execute_approved("apr-2", contact_id=CH_SEC, request_id="req-term-x")

    def test_rejection_closes_case(self):
        self.backend.open_approval(
            "apr-3", kind="cross_party", engagement_id="eng-1",
            command="terminate_engagement", payload={"reason": "试拒"},
            requested_by=LN_MGR, requested_by_party=LN, required_parties=[LN, CA])
        decision = self.backend.endorse_approval(
            "apr-3", contact_ref=CA_MGR, party_id=CA, approve=False)
        self.assertEqual(decision["decision"], "rejected")
        with self.assertRaises(RuleViolation):
            self.backend.execute_approved("apr-3", contact_id=CH_SEC, request_id="req-nope")


if __name__ == "__main__":
    unittest.main()
