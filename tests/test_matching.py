import unittest

import _scenario
from central_asia_project_commitment.backend import Backend
from central_asia_project_commitment.errors import RuleViolation

from _scenario import (
    CA, CA_MGR, EXPERT, LAND, LN, LN_MGR, TRAIN, WAREHOUSE, FakeClock,
    build_scenario, demand_items, hold_engagement, supply_items,
)


class MatchingTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.backend = Backend(clock=self.clock)
        build_scenario(self.backend)

    def test_compatible_match_lists_satisfied_and_four_bundle_lines(self):
        deadline = self.clock._at
        from datetime import timedelta
        deadline = (deadline + timedelta(days=14)).isoformat()
        result = self.backend.propose_match(
            "eng-x", project_id="proj-1", demand_intent_id="int-ln", supply_intent_id="int-ca",
            proposed_expires_at=deadline, responsible={"secretary": "ch-sec"},
            request_id="req-x")
        self.assertTrue(result["compatible"], result)
        self.assertGreaterEqual(len(result["satisfied"]), 8)
        kinds = {line["kind"] for line in self.backend.engagement("eng-x").match_snapshot["lines"]}
        self.assertEqual(kinds, {"land", "warehouse", "train_slot", "expert_support"})
        # 单位归一：5 公顷 = 50000 m2；1 人月 = 21.75 人日
        by_kind = {l["kind"]: l["qty"] for l in self.backend.engagement("eng-x").match_snapshot["lines"]}
        self.assertAlmostEqual(by_kind["land"], 50000.0)
        self.assertAlmostEqual(by_kind["expert_support"], 21.75)

    def test_gap_when_supply_lacks_certification(self):
        items = [i for i in supply_items() if i["code"] != "s-wheat"]
        items.append({"code": "s-wheat2", "dimension": "agriculture", "role": "supply",
                      "confidentiality": "L3",
                      "attributes": {"product": "wheat", "certifications": []},
                      "quantity": {"value": 10000, "unit": "ton"}})
        self.backend.revise_intent("int-ca", contact_id=CA_MGR, items=items, reason="撤换麦源")
        from datetime import timedelta
        result = self.backend.propose_match(
            "eng-g", project_id="proj-1", demand_intent_id="int-ln", supply_intent_id="int-ca",
            proposed_expires_at=(self.clock._at + timedelta(days=7)).isoformat(),
            responsible={}, request_id="req-g")
        self.assertFalse(result["compatible"])
        gap_dims = {g.get("reason", "") for g in result["gaps"]}
        self.assertTrue(any("gacc" in r for r in gap_dims), result["gaps"])

    def test_conflict_when_train_window_already_held(self):
        # 先占 35 TEU，只剩 5，第二次 10 TEU 的候选必须列出资源冲突而非暂留
        hold_engagement(self.backend, engagement_id="eng-first")
        from datetime import timedelta
        result = self.backend.propose_match(
            "eng-second", project_id="proj-1", demand_intent_id="int-ln", supply_intent_id="int-ca",
            proposed_expires_at=(self.clock._at + timedelta(days=7)).isoformat(),
            responsible={}, request_id="req-second")
        self.assertFalse(result["compatible"])
        conflict = next(c for c in result["conflicts"] if c["kind"] == "train_slot")
        self.assertEqual(conflict["resource_id"], TRAIN)
        self.assertAlmostEqual(conflict["requested"], 35.0)
        self.assertAlmostEqual(conflict["available"], 5.0)
        self.assertTrue(any(o["ref_id"] == "eng-first" for o in conflict["occupants"]))

    def test_cannot_hold_when_conflicts_remain(self):
        hold_engagement(self.backend, engagement_id="eng-first")
        from datetime import timedelta
        self.backend.propose_match(
            "eng-second", project_id="proj-1", demand_intent_id="int-ln", supply_intent_id="int-ca",
            proposed_expires_at=(self.clock._at + timedelta(days=7)).isoformat(),
            responsible={}, request_id="req-second")
        self.backend.confirm_engagement("eng-second", party_id=LN, contact_id=LN_MGR)
        with self.assertRaises(RuleViolation):
            self.backend.confirm_engagement("eng-second", party_id=CA, contact_id=CA_MGR)

    def test_hold_atomically_occupies_four_resources(self):
        hold_engagement(self.backend)
        self.assertAlmostEqual(self.backend.resource(LAND).occupied, 50000.0)
        self.assertAlmostEqual(self.backend.resource(WAREHOUSE).occupied, 2000.0)
        self.assertAlmostEqual(self.backend.resource(TRAIN).occupied, 35.0)
        self.assertAlmostEqual(self.backend.resource(EXPERT).occupied, 21.75)
        eng = self.backend.engagement("eng-1")
        self.assertEqual(eng.status, "held")
        self.assertEqual(len(eng.confirmations), 2)

    def test_cross_timezone_confirmations_recorded_with_zones(self):
        from datetime import timedelta
        deadline = (self.clock._at + timedelta(days=14)).isoformat()
        self.backend.propose_match(
            "eng-1", project_id="proj-1", demand_intent_id="int-ln", supply_intent_id="int-ca",
            proposed_expires_at=deadline, responsible={}, request_id="req-propose-eng-1")
        self.backend.confirm_engagement("eng-1", party_id=LN, contact_id=LN_MGR, zone="Asia/Shanghai")
        events = self.backend.store.stream_events("eng-eng-1")
        zones = {e["payload"].get("zone") for e in events if e["type"] == "PartyConfirmed"}
        self.backend.confirm_engagement("eng-1", party_id=CA, contact_id=CA_MGR, zone="Asia/Almaty")
        events = self.backend.store.stream_events("eng-eng-1")
        confirmed = [e for e in events if e["type"] == "PartyConfirmed"]
        zones = [e["payload"]["zone"] for e in confirmed]
        self.assertIn("Asia/Shanghai", zones)
        self.assertIn("Asia/Almaty", zones)


if __name__ == "__main__":
    unittest.main()
