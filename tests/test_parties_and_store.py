import tempfile
import unittest
from pathlib import Path

import _scenario
from central_asia_project_commitment.backend import Backend
from central_asia_project_commitment.errors import IdempotencyReplay, VersionConflict

from _scenario import (
    AGENCY, CA, CA_ADMIN, CA_MGR, CHAMBER, CH_SEC, LN, LN_ADMIN, LN_MGR, FakeClock, build_scenario
)


class PartyVersioningTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.backend = Backend(clock=self.clock)
        build_scenario(self.backend)

    def test_profile_keeps_all_versions(self):
        party = self.backend.party(LN)
        self.assertEqual(party.latest_profile(), {"credit": "A"})
        self.backend.update_party_profile(LN, contact_id=LN_ADMIN,
                                          profile={"credit": "AA", "registered_capital": 1.2e8},
                                          reason="辽洽会后补充验资")
        self.clock.advance(days=2)
        self.backend.update_party_profile(LN, contact_id=LN_ADMIN,
                                          profile={"credit": "AAA"}, reason="年度复评")
        party = self.backend.party(LN)
        self.assertEqual(len(party.profile_versions), 3)
        self.assertEqual(party.profile_at_version(1), {"credit": "A"})
        self.assertEqual(party.latest_profile()["credit"], "AAA")

    def test_non_admin_cannot_change_profile(self):
        from central_asia_project_commitment.errors import AuthorizationError
        with self.assertRaises(AuthorizationError):
            self.backend.update_party_profile(LN, contact_id=LN_MGR, profile={}, reason="越权")

    def test_departure_revokes_only_pending_access_and_keeps_history(self):
        from datetime import timedelta
        # 建案卷、授权、完成一笔、再离任：只终止未完成授权
        self.backend.open_dossier("dd-1", project_id="proj-1")
        self.backend.register_material(
            "dd-1", owner_party_id=LN, contact_id=LN_MGR, material_id="m-1",
            title="营业执照", language="zh", confidentiality="L2",
            original_text_ref="store://m-1/original")
        self.clock.advance(days=1)
        future = (self.clock._at + timedelta(days=30)).isoformat()
        self.backend.grant_access(
            "dd-1", granter_party_id=LN, granter_contact_id=LN_MGR, grant_id="g-done",
            material_id="m-1", grantee_party_id=CA, grantee_contact_id=CA_MGR,
            purpose="qualification", valid_until=future)
        # g-done 先完成
        self.backend.use_material("dd-1", grant_id="g-done", contact_id=CA_MGR,
                                  purpose="qualification", note="已核验")
        self.backend.complete_grant("dd-1", grant_id="g-done", contact_id=CA_MGR)
        # g-open 仍生效
        self.backend.grant_access(
            "dd-1", granter_party_id=LN, granter_contact_id=LN_MGR, grant_id="g-open",
            material_id="m-1", grantee_party_id=CA, grantee_contact_id=CA_MGR,
            purpose="financial", valid_until=future)
        result = self.backend.contact_departs(CA, admin_contact_id=CA_ADMIN, contact_id=CA_MGR)
        revoked = {item["grant_id"] for item in result["revoked_active_grants"]}
        self.assertIn("g-open", revoked)
        self.assertNotIn("g-done", revoked)
        dossier = self.backend.dossier("dd-1")
        self.assertEqual(dossier.grants["g-done"].status, "completed")
        # 已完成访问记录保留
        self.assertEqual(len(dossier.grants["g-done"].accesses), 1)
        self.assertEqual(dossier.grants["g-open"].status, "revoked")

    def test_four_party_kinds_present(self):
        for pid in (LN, CA, CHAMBER, AGENCY):
            self.assertIsNotNone(self.backend.party(pid).party_id)


class EventStoreRecoveryTest(unittest.TestCase):
    def test_reload_jsonl_rebuilds_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            clock = FakeClock()
            backend = Backend(path, clock=clock)
            build_scenario(backend)
            from _scenario import hold_engagement
            hold_engagement(backend)
            land_before = backend.resource("land-1").occupied

            backend2 = Backend(path, clock=clock)
            self.assertEqual(backend2.resource("land-1").occupied, land_before)
            self.assertEqual(backend2.engagement("eng-1").status, "held")

    def test_optimistic_concurrency_serializes_contention(self):
        backend = Backend()
        build_scenario(backend)
        r1 = backend.resource("train-1")
        r2 = backend.resource("train-1")
        r1.allocate(ref_id="a", qty=10, at=self.ts())
        backend.repo.save([r1], at=self.ts())
        # r2 基于旧版本，同一班列窗口的并发占用必须失败
        r2.allocate(ref_id="b", qty=10, at=self.ts())
        with self.assertRaises(VersionConflict):
            backend.repo.save([r2], at=self.ts())

    def test_idempotent_request_does_not_create_second_commitment(self):
        backend = Backend(clock=FakeClock())
        build_scenario(backend)
        from _scenario import hold_engagement
        hold_engagement(backend)
        terms = {"commitment_no": "C-001", "obligations": ["土建", "供粮"], "due_at": "2027-01-01T00:00:00+00:00"}
        first = backend.convert_to_commitment("eng-1", contact_id=LN_MGR, terms=terms, request_id="req-1")
        self.assertEqual(first["commitment_no"], "C-001")
        with self.assertRaises(IdempotencyReplay) as ctx:
            backend.convert_to_commitment("eng-1", contact_id=LN_MGR, terms=terms, request_id="req-1")
        self.assertEqual(ctx.exception.result["commitment_no"], "C-001")
        # 事件流中只有一次转换
        types = [e["type"] for e in backend.store.stream_events("eng-eng-1")]
        self.assertEqual(types.count("EngagementConverted"), 1)

    @staticmethod
    def ts():
        return "2026-09-25T00:00:00+00:00"


if __name__ == "__main__":
    unittest.main()
