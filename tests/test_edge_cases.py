import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import _scenario
from central_asia_project_commitment.backend import Backend
from central_asia_project_commitment.catalog import FxTable, Money
from central_asia_project_commitment.errors import (
    IdempotencyReplay, RuleViolation, VersionConflict,
)

from _scenario import (
    CA, CA_MGR, CHAMBER, CH_SEC, LN, LN_MGR, FakeClock,
    build_scenario, hold_engagement,
)


class AtomicityEdgeTest(unittest.TestCase):
    def test_resource_failure_rolls_back_confirmation_entirely(self):
        clock = FakeClock()
        backend = Backend(clock=clock)
        build_scenario(backend)
        deadline = (clock._at + timedelta(days=14)).isoformat()
        backend.propose_match(
            "eng-1", project_id="proj-1", demand_intent_id="int-ln", supply_intent_id="int-ca",
            proposed_expires_at=deadline, responsible={}, request_id="req-p")
        backend.confirm_engagement("eng-1", party_id=LN, contact_id=LN_MGR)

        # 第二方确认落库前，班列窗口被另一个组合抢走 10 TEU（只剩 5 < 需求 35）
        train = backend.resource("train-1")
        train.allocate(ref_id="racer", qty=10.0, at=clock.iso)
        backend.repo.save([train], at=clock.iso)

        # 第二方确认时四类资源无法全部占住：整体拒绝，确认事件也不落库
        with self.assertRaises(RuleViolation):
            backend.confirm_engagement("eng-1", party_id=CA, contact_id=CA_MGR)
        types = [e["type"] for e in backend.store.stream_events("eng-eng-1")]
        self.assertEqual(types, ["EngagementProposed", "PartyConfirmed"])
        self.assertEqual(backend.engagement("eng-1").status, "proposed")
        # 竞得方的占用不受影响
        self.assertAlmostEqual(backend.resource("train-1").occupied, 10.0)

    def test_stale_version_after_approval_gives_single_outcome(self):
        clock = FakeClock()
        backend = Backend(clock=clock)
        build_scenario(backend)
        hold_engagement(backend)
        backend.open_approval(
            "apr-1", kind="cross_party", engagement_id="eng-1",
            command="terminate_engagement", payload={"reason": "拟终止"},
            requested_by=LN_MGR, requested_by_party=LN, required_parties=[LN, CA])
        backend.endorse_approval("apr-1", contact_ref=LN_MGR, party_id=LN, approve=True)
        backend.endorse_approval("apr-1", contact_ref=CA_MGR, party_id=CA, approve=True)

        # 审批依据版本之后，组合流被另一笔已发生费用推进
        backend.record_fee("eng-1", amount=1000.0, currency="CNY",
                           purpose="审批期间发生的规费", contact_id=CA_MGR)

        result = backend.execute_approved("apr-1", contact_id=CH_SEC, request_id="req-exec")
        self.assertEqual(result["decision"], "stale")
        # 唯一结果：组合未终止；需要重新发起审批
        self.assertEqual(backend.engagement("eng-1").status, "held")
        self.assertEqual(backend.approval("apr-1").decision, "stale")
        with self.assertRaises(RuleViolation):
            backend.execute_approved("apr-1", contact_id=CH_SEC, request_id="req-exec2")

    def test_proposal_replay_does_not_open_second_engagement(self):
        clock = FakeClock()
        backend = Backend(clock=clock)
        build_scenario(backend)
        kwargs = dict(
            project_id="proj-1", demand_intent_id="int-ln", supply_intent_id="int-ca",
            proposed_expires_at=(clock._at + timedelta(days=7)).isoformat(),
            responsible={}, request_id="req-dup")
        first = backend.propose_match("eng-dup", **kwargs)
        self.assertTrue(first["compatible"])
        with self.assertRaises(IdempotencyReplay) as ctx:
            backend.propose_match("eng-dup", **kwargs)
        self.assertEqual(ctx.exception.result["engagement_id"], "eng-dup")
        self.assertEqual(len(backend.store.stream_events("eng-eng-dup")), 1)


class PersistenceEdgeTest(unittest.TestCase):
    def test_truncated_tail_line_is_ignored_on_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            clock = FakeClock()
            backend = Backend(path, clock=clock)
            build_scenario(backend)
            hold_engagement(backend)
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"seq": 999, "streams": {"broken": {"ev')  # 崩溃半行
            restarted = Backend(path, clock=clock)
            self.assertEqual(restarted.engagement("eng-1").status, "held")
            self.assertAlmostEqual(restarted.resource("land-1").occupied, 50000.0)


class FxTest(unittest.TestCase):
    def test_conversion_uses_pinned_fx_version(self):
        fx = FxTable(reporting_currency="CNY")
        v1 = fx.publish({"USD": 7.1, "EUR": 7.8})
        money = fx.convert(Money(100.0, "USD"), "CNY", fx_version=v1)
        self.assertAlmostEqual(money.amount, 710.0)
        # 汇率后续变动，旧版本报价不变
        fx.publish({"USD": 7.3})
        money = fx.convert(Money(100.0, "USD"), "CNY", fx_version=v1)
        self.assertAlmostEqual(money.amount, 710.0)
        with self.assertRaises(RuleViolation):
            fx.publish({"X": -1.0})

    def test_convertible_currency_matches_by_snapshot_fx(self):
        clock = FakeClock()
        backend = Backend(clock=clock)
        build_scenario(backend)
        # 供给侧把 USD 移出接受名单，EUR 列入可折算；需求侧改要 EUR
        supply = [dict(i) for i in __import__("_scenario").supply_items()]
        for item in supply:
            if item["code"] == "s-fx":
                item["attributes"] = {"accepted_currencies": ["CNY"], "convertible_currencies": ["EUR", "USD"]}
        backend.revise_intent("int-ca", contact_id=CA_MGR, items=supply, reason="币种口径调整")
        demand = [dict(i) for i in __import__("_scenario").demand_items()]
        for item in demand:
            if item["code"] == "d-fx":
                item["attributes"] = {"currency": "EUR"}
        backend.revise_intent("int-ln", contact_id=LN_MGR, items=demand, reason="以欧元计价")
        result = backend.propose_match(
            "eng-fx", project_id="proj-1", demand_intent_id="int-ln", supply_intent_id="int-ca",
            proposed_expires_at=(clock._at + timedelta(days=7)).isoformat(),
            responsible={}, request_id="req-fx")
        self.assertTrue(result["compatible"], result["gaps"])
        fx_satisfied = [s for s in result["satisfied"] if s["dimension"] == "currency"]
        self.assertTrue(any("折算" in s["detail"] for s in fx_satisfied))


if __name__ == "__main__":
    unittest.main()
