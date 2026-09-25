"""辽洽会综合场景端到端测试。

场景：辽洽会结束后秘书处同时手握本地化生产、陆港共建、农产品加工意向，
验证主体版本、需求供给匹配、组合原子暂留、双方确认、资源争用、
幂等重放、发起/复核分离、尽调授权、恢复续跑与项目接口。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from central_asia_project_commitment import Backend, Contact, EventStore, FixedClock
from central_asia_project_commitment.clock import to_iso
from central_asia_project_commitment.errors import (
    IdempotencyReplayed,
    PolicyViolation,
    ResourceConflict,
    StateConflict,
    VersionConflict,
)

T0 = "2026-09-25T01:00:00+00:00"  # 沈阳 09:00 / 阿拉木图 06:00


def _interval(clock: FixedClock, days: int = 30):
    start = to_iso(clock.current())
    end = to_iso(clock.current() + timedelta(days=days))
    return start, end


class World:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.clock = FixedClock(__import__("central_asia_project_commitment.clock", fromlist=["parse"]).parse(T0))
        self.backend = Backend(EventStore(self.path), self.clock)

    def close(self):
        self.tmp.cleanup()

    def fresh_backend(self):
        # 模拟系统恢复：同一事件日志、全新进程
        return Backend(EventStore(self.path), self.clock)

    def seed_parties(self):
        b = self.backend
        b.register_party("ln-equip", "liaoning_enterprise", "辽宁辽丰装备有限公司",
                         "Asia/Shanghai", {"capital": "5000万", "biz": "农产品加工装备"},
                         country="CN")
        b.register_party("kz-agro", "central_asia_enterprise", "阿拉木图金色麦田公司",
                         "Asia/Almaty", {"capital": "20亿坚戈", "biz": "小麦种植"},
                         country="KZ")
        b.register_party("uz-log", "central_asia_enterprise", "塔什干丝路陆港公司",
                         "Asia/Tashkent", {"biz": "陆港与班列运营"}, country="UZ")
        b.register_party("chamber", "chamber", "辽宁省中亚经贸商协会",
                         "Asia/Shanghai", {"role": "秘书处"})
        b.register_party("local-sy", "local_agency", "沈阳市项目服务局",
                         "Asia/Shanghai", {"role": "属地服务"})
        b.add_contact("ln-equip", Contact("c-ln", "王磊", "海外投资部长", "wang@ln.example"))
        b.add_contact("kz-agro", Contact("c-kz", "Aibek", "商务总监", "aibek@kz.example"))
        b.add_contact("uz-log", Contact("c-uz", "Dilnoza", "班列经理", "d@uz.example"))
        b.add_contact("chamber", Contact("c-sec", "赵秘书", "秘书处专员"))
        b.add_contact("local-sy", Contact("c-local", "陈工", "属地协调员"))
        b.add_contact("chamber", Contact("c-sec2", "钱秘书", "秘书处复核专员"))

    def seed_pools(self):
        b = self.backend
        b.register_pool("land-sy", "land", "沈阳综合保税区工业用地", "亩", 500,
                        region="辽宁/沈阳", attrs={"zoning": "bonded"})
        b.register_pool("wh-sy", "warehouse", "沈阳综保区冷库与干仓", "平方米", 2000,
                        region="辽宁/沈阳")
        b.register_pool("train-kz", "train_window", "中欧班列沈阳—阿拉木图周窗口",
                        "列/周", 3, corridor="沈阳-阿拉木图")
        b.register_pool("expert-agro", "expert", "农产品精深加工专家支持", "人天", 10)
        b.register_pool("land-tash", "land", "塔什干州产业园用地", "亩", 800,
                        region="乌兹别克斯坦/塔什干")

    def bundle(self, land=200, wh=800, train=1, expert=5, days=30):
        start, end = _interval(self.clock, days)
        return [
            {"pool_id": "land-sy", "qty": land, "start": start, "end": end},
            {"pool_id": "wh-sy", "qty": wh, "start": start, "end": end},
            {"pool_id": "train-kz", "qty": train, "start": start, "end": end},
            {"pool_id": "expert-agro", "qty": expert, "start": start, "end": end},
        ]

    def seed_intents(self):
        b = self.backend
        i1_demands = [
            {"dimension": "agriculture", "crop": "小麦", "annual_tons": 5000, "grade": "一级"},
            {"dimension": "logistics", "corridors": ["沈阳-阿拉木图"],
             "train_slots_per_week": 1, "warehouse_sqm": 800, "dry_port_required": False},
            {"dimension": "currency", "currency": "CNY", "amount_min": 1_000_000,
             "amount_max": 50_000_000, "tax": "含税"},
            {"dimension": "confidentiality", "scope": ["financial", "technical"],
             "nda_required": True, "embargo_days": 365},
        ]
        i1_offers = [
            {"dimension": "investment_stage", "stage": "construction"},
            {"dimension": "processing", "product": "面粉", "capacity_tons_year": 6000,
             "certifications": ["ISO22000"]},
            {"dimension": "local_condition", "region": "辽宁/沈阳", "area_mu": 300,
             "zoning": ["bonded"], "utilities": ["水", "电", "蒸汽"]},
            {"dimension": "currency", "currency": "CNY", "amount_min": 2_000_000,
             "amount_max": 30_000_000, "tax": "含税"},
            {"dimension": "confidentiality", "scope": ["financial", "technical", "commercial"],
             "nda_accepted": True, "embargo_days": 730},
        ]
        b.file_intent("i-ln", "ln-equip", "c-ln", "localized_production",
                      "辽丰—金色麦田面粉本地化生产", i1_demands, i1_offers,
                      request_key="req-intent-ln")

        i2_demands = [
            {"dimension": "investment_stage", "stage": "signed"},
            {"dimension": "processing", "product": "面粉", "capacity_tons_year": 6000,
             "certifications": ["ISO22000"]},
            {"dimension": "local_condition", "region": "辽宁/沈阳", "area_mu": 200,
             "zoning": ["bonded"], "utilities": ["水", "电"]},
            {"dimension": "currency", "currency": "CNY", "amount_min": 1_000_000,
             "amount_max": 40_000_000, "tax": "含税"},
            {"dimension": "confidentiality", "scope": ["financial", "technical"],
             "nda_required": True, "embargo_days": 365},
        ]
        i2_offers = [
            {"dimension": "agriculture", "crop": "小麦", "annual_tons": 8000, "grade": "一级"},
            {"dimension": "logistics", "corridors": ["沈阳-阿拉木图"],
             "train_slots_per_week": 2, "warehouse_sqm": 1000, "dry_port_available": True},
            {"dimension": "investment_stage", "stage": "signed"},
            {"dimension": "local_condition", "region": "辽宁/沈阳", "area_mu": 300,
             "zoning": ["bonded"], "utilities": ["水", "电", "蒸汽", "燃气"]},
            {"dimension": "currency", "currency": "CNY", "amount_min": 2_000_000,
             "amount_max": 20_000_000, "tax": "含税"},
            {"dimension": "confidentiality", "scope": ["financial", "technical", "commercial"],
             "nda_accepted": True, "embargo_days": 730},
        ]
        b.file_intent("i-kz", "kz-agro", "c-kz", "agro_processing",
                      "哈方优质小麦原料与加工合资", i2_demands, i2_offers,
                      request_key="req-intent-kz")


class LiaoningScenarioTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.seed_parties()
        self.world.seed_pools()
        self.world.seed_intents()

    def tearDown(self):
        self.world.close()

    def test_party_versions_and_contacts(self):
        b = self.world.backend
        # 主体版本不可变、可引用历史版本
        ln = b._party("ln-equip")
        self.assertEqual(ln.latest_version_no, 1)
        b.new_party_version("ln-equip", {"capital": "8000万", "biz": "装备+加工"}, "增资扩产")
        ln = b._party("ln-equip")
        self.assertEqual(ln.latest_version_no, 2)
        self.assertEqual(ln.profile_at(1)["capital"], "5000万")  # 原文版本保留

        # 离任联系人不能再发起动作
        b.contact_departed("ln-equip", "c-ln")
        with self.assertRaises(PolicyViolation):
            b.file_intent(
                "i-x", "ln-equip", "c-ln", "other", "x",
                [{"dimension": "currency", "currency": "CNY"}],
                [{"dimension": "currency", "currency": "CNY"}],
            )

    def test_match_lists_satisfied_gaps_and_conflicts(self):
        b = self.world.backend
        match = b.match("i-ln", "i-kz", self.world.bundle())
        self.assertTrue(match["clause_match"])
        self.assertTrue(match["ready_to_hold"])
        self.assertIn("agriculture", match["matched_dimensions"])
        self.assertIn("logistics", match["matched_dimensions"])
        self.assertEqual(match["resource_conflicts"], [])

        # 缺口：班列窗口需求超过供给时匹配呈现 gap
        over_demands = [{
            "dimension": "logistics", "corridors": ["沈阳-阿拉木图"],
            "train_slots_per_week": 5, "warehouse_sqm": 800,
        }]
        weak_offers = [{
            "dimension": "logistics", "corridors": ["沈阳-阿拉木图"],
            "train_slots_per_week": 0, "warehouse_sqm": 2000,
        }]
        b.file_intent("i-kz2", "kz-agro", "c-kz", "dry_port", "超额班列需求",
                      over_demands, weak_offers)
        match2 = b.match("i-ln", "i-kz2")
        self.assertFalse(match2["clause_match"])
        self.assertTrue(any("班列窗口缺口" in g["gap"] for g in match2["gaps"]))

        # 资源冲突：暂留窗口超过池容量
        conflict_match = b.match("i-ln", "i-kz", self.world.bundle(train=4))
        self.assertFalse(conflict_match["resource_available"])
        self.assertEqual(conflict_match["resource_conflicts"][0]["pool_id"], "train-kz")

    def test_double_confirmation_holds_bundle_with_deadline(self):
        b = self.world.backend
        b.open_project("prj-1", "面粉合资项目", "c-sec")
        b.propose_commitment(
            "cmt-1", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
            self.world.bundle(), ttl_seconds=7 * 24 * 3600, project_id="prj-1",
            request_key="req-cmt-1",
        )
        # 单方确认尚不暂留
        r = b.confirm_commitment("cmt-1", "ln-equip", "c-ln", request_key="req-conf-a")
        self.assertEqual(r["status"], "awaiting_counterpart")
        self.assertEqual(b._ledger().occupied("train-kz"), 0)

        # 非授权联系人不能替对方确认
        with self.assertRaises(PolicyViolation):
            b.confirm_commitment("cmt-1", "kz-agro", "c-uz")

        r = b.confirm_commitment("cmt-1", "kz-agro", "c-kz", request_key="req-conf-b")
        self.assertEqual(r["status"], "held")
        cmt = b._commitment("cmt-1")
        self.assertEqual(cmt.status, "held")
        self.assertTrue(cmt.deadline)
        # 组合原子四类资源同步占用
        ledger = b._ledger()
        self.assertAlmostEqual(ledger.occupied("land-sy"), 200)
        self.assertAlmostEqual(ledger.occupied("wh-sy"), 800)
        self.assertAlmostEqual(ledger.occupied("train-kz"), 1)
        self.assertAlmostEqual(ledger.occupied("expert-agro"), 5)

    def test_replay_same_request_does_not_create_second_commitment(self):
        b = self.world.backend
        payload_bundle = self.world.bundle()
        b.propose_commitment(
            "cmt-r", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
            payload_bundle, ttl_seconds=3600, request_key="idem-cmt",
        )
        # 同键同载荷重放：返回首次结果，不新增事件
        again = b.propose_commitment(
            "cmt-r", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
            payload_bundle, ttl_seconds=3600, request_key="idem-cmt",
        )
        self.assertTrue(again["replayed"])
        streams = {e.stream for e in b.store.read_all() if e.stream == "commitment:cmt-r"}
        self.assertEqual(len(streams), 1)
        # 同键不同载荷：拒绝
        with self.assertRaises(IdempotencyReplayed):
            b.propose_commitment(
                "cmt-r2", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
                self.world.bundle(train=2), ttl_seconds=3600, request_key="idem-cmt",
            )

    def test_cross_timezone_contention_has_single_outcome(self):
        b = self.world.backend
        # 第二个承诺先登记（容量尚足，可提议），需要 3 列班列
        b.propose_commitment(
            "cmt-compete", "i-ln", "i-kz", "kz-agro", "c-kz", "c-ln",
            self.world.bundle(train=3), ttl_seconds=3600, request_key="req-compete",
        )
        # cmt-1 先确认，占用班列 1
        b.propose_commitment(
            "cmt-1", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
            self.world.bundle(train=1), ttl_seconds=3600, request_key="req-cmt-1",
        )
        b.confirm_commitment("cmt-1", "ln-equip", "c-ln", request_key="k1")
        b.confirm_commitment("cmt-1", "kz-agro", "c-kz", request_key="k2")
        # 竞争承诺首次确认（提议方）不暂留；双方齐备时锁内重检失败
        b.confirm_commitment("cmt-compete", "kz-agro", "c-kz", request_key="k3a")
        with self.assertRaises(ResourceConflict) as ctx:
            b.confirm_commitment("cmt-compete", "ln-equip", "c-ln", request_key="k3")
        self.assertIn("train-kz", str(ctx.exception.conflicts))
        self.assertNotIn("cmt-compete", b._ledger().allocations)

        # 版本过期写入被拒：依据的版本落后于当前版本
        stale = b._commitment("cmt-1")
        with self.assertRaises(VersionConflict):
            b.store.append_many(
                [("commitment:cmt-1", stale.version - 1,
                  [("CommitmentNoteAdded", {"note": "过期版本写入"})])],
                b.now(),
            )

    def test_bundle_is_atomic_when_one_pool_short(self):
        b = self.world.backend
        # 专家池总共 10 人天：一个承诺暂留 8 个后，另一个要 5 个，整组失败
        b.propose_commitment(
            "cmt-big", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
            self.world.bundle(expert=8), ttl_seconds=3600, request_key="big",
        )
        b.confirm_commitment("cmt-big", "ln-equip", "c-ln")
        b.confirm_commitment("cmt-big", "kz-agro", "c-kz")
        with self.assertRaises(PolicyViolation):
            b.propose_commitment(
                "cmt-small", "i-ln", "i-kz", "kz-agro", "c-kz", "c-ln",
                self.world.bundle(expert=5), ttl_seconds=3600,
            )

    def test_handover_fees_retained_through_resize_and_terminate(self):
        b = self.world.backend
        b.propose_commitment(
            "cmt-1", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
            self.world.bundle(land=200), ttl_seconds=30 * 86400,
            request_key="req-cmt-1",
        )
        b.confirm_commitment("cmt-1", "ln-equip", "c-ln", request_key="k1")
        b.confirm_commitment("cmt-1", "kz-agro", "c-kz", request_key="k2")

        # 已交接土地 100 亩，费用 20 万
        b.register_handover("cmt-1", "land-sy", 100, "HO-001", 200_000,
                            request_key="ho-1")
        cmt = b._commitment("cmt-1")
        self.assertAlmostEqual(cmt.fees_total, 200_000)

        # 发起/复核分离：发起人本人不能复核，申请人本人也不行
        b.request_adjustment("cmt-1", "adj-1", "resize", "c-sec",
                             {"bundle": [
                                 {"pool_id": "land-sy", "qty": 100},
                                 {"pool_id": "wh-sy", "qty": 800},
                                 {"pool_id": "train-kz", "qty": 1},
                                 {"pool_id": "expert-agro", "qty": 5},
                             ]})
        with self.assertRaisesRegex(PolicyViolation, "不能复核自己"):
            b.approve_adjustment("cmt-1", "adj-1", "c-sec")
        with self.assertRaisesRegex(PolicyViolation, "参与发起"):
            b.approve_adjustment("cmt-1", "adj-1", "c-ln")

        # 独立复核人（未参与发起）通过：土地仅释放未履行的 100 亩
        b.approve_adjustment("cmt-1", "adj-1", "c-local", "按实际用地调减",
                             request_key="approve-1")
        ledger = b._ledger()
        line = next(l for l in ledger.allocations["cmt-1"] if l.pool_id == "land-sy")
        self.assertAlmostEqual(line.qty_held, 200)
        self.assertAlmostEqual(line.qty_fulfilled, 100)
        self.assertAlmostEqual(line.qty_released, 100)
        self.assertAlmostEqual(line.occupied, 100)  # 已交接继续占用

        # 不能改量到低于已履行份额（申请阶段即拒绝）
        with self.assertRaises(PolicyViolation):
            b.request_adjustment("cmt-1", "adj-below", "resize", "c-sec",
                                 {"bundle": [
                                     {"pool_id": "land-sy", "qty": 50},
                                     {"pool_id": "wh-sy", "qty": 800},
                                     {"pool_id": "train-kz", "qty": 1},
                                     {"pool_id": "expert-agro", "qty": 5},
                                 ]})

        # 终止：剩余未履行份额释放，已交接与费用留存
        b.request_adjustment("cmt-1", "adj-term", "terminate", "c-sec2",
                             {"reason": "对方投资计划变更"})
        b.approve_adjustment("cmt-1", "adj-term", "c-local", "复核同意终止",
                             request_key="approve-term")
        cmt = b._commitment("cmt-1")
        self.assertEqual(cmt.status, "terminated")
        self.assertEqual(cmt.final_reason, "对方投资计划变更")
        self.assertAlmostEqual(cmt.fees_total, 200_000)
        self.assertEqual(cmt.handovers[0]["handover_ref"], "HO-001")
        ledger = b._ledger()
        self.assertEqual(ledger.handovers["cmt-1"][0]["handover_ref"], "HO-001")
        for line in ledger.allocations["cmt-1"]:
            self.assertLessEqual(line.occupied, line.qty_fulfilled + 1e-9)

    def test_extend_adjustment_rechecks_contention(self):
        b = self.world.backend
        start, _ = _interval(self.world.clock, 30)
        b.propose_commitment(
            "cmt-1", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
            self.world.bundle(train=2), ttl_seconds=86400, request_key="c1",
        )
        b.confirm_commitment("cmt-1", "ln-equip", "c-ln")
        b.confirm_commitment("cmt-1", "kz-agro", "c-kz")
        # 延期到第 60 天，与第二承诺的占用区间重叠且班列容量不够
        later = to_iso(self.world.clock.current() + timedelta(days=60))
        b.request_adjustment("cmt-1", "ext-1", "extend", "c-sec",
                             {"new_deadline": later, "new_end": later})
        # 先在重叠区间放入另一个占用 2 列的承诺
        self.world.clock.advance(timedelta(days=31))
        # 直接登记一个竞争池占用：构造第二组意向承诺过重，这里用资源占用模拟
        b.register_pool("wh2", "warehouse", "二期仓", "平方米", 5000)
        b.register_pool("land2", "land", "二期地", "亩", 1000)
        b.register_pool("expert2", "expert", "二期专家", "人天", 100)
        # 用第二个承诺占用后段班列窗口
        i3d = [{"dimension": "currency", "currency": "CNY"}]
        i3o = [{"dimension": "currency", "currency": "CNY"}]
        b.file_intent("i3", "uz-log", "c-uz", "dry_port", "陆港二期", i3d, i3o)
        b.file_intent("i4", "kz-agro", "c-kz", "other", "陆港二期对接", i3d, i3o)
        s2, e2 = _interval(self.world.clock, 30)
        b.propose_commitment(
            "cmt-2", "i3", "i4", "uz-log", "c-uz", "c-kz",
            [
                {"pool_id": "land2", "qty": 10, "start": s2, "end": e2},
                {"pool_id": "wh2", "qty": 100, "start": s2, "end": e2},
                {"pool_id": "train-kz", "qty": 2, "start": s2, "end": e2},
                {"pool_id": "expert2", "qty": 2, "start": s2, "end": e2},
            ],
            ttl_seconds=86400, request_key="c2",
        )
        b.confirm_commitment("cmt-2", "uz-log", "c-uz")
        b.confirm_commitment("cmt-2", "kz-agro", "c-kz")
        with self.assertRaises(ResourceConflict):
            b.approve_adjustment("cmt-1", "ext-1", "c-local")

    def test_due_diligence_purpose_translation_and_departure(self):
        b = self.world.backend
        b.open_dd_case("dd-1", "prj-1", "ln-equip")
        b.add_dd_material("dd-1", "ru", "Устав компании（公司章程原文）", "doc-ru-1")
        # 译文更正：保留原文版本
        b.add_dd_material("dd-1", "zh", "公司章程（译文更正版）", "doc-zh-2",
                          corrects_version=1, note="初版术语误译，更正为认缴制表述")
        case = b._dd("dd-1")
        self.assertEqual(len(case.materials), 2)
        self.assertEqual(case.materials[0].ref, "doc-ru-1")  # 原文仍在
        self.assertEqual(case.materials[1].corrects_version, 1)

        b.grant_dd_access("dd-1", "g-1", "c-kz", "合资可行性核验",
                          ["doc-ru-1", "doc-zh-2"], timedelta(days=7))
        # 用途不符或超材料范围被拒
        with self.assertRaises(PolicyViolation):
            b.request_dd_visit("dd-1", "g-1", "v-1", "媒体公开", "doc-ru-1")
        with self.assertRaises(PolicyViolation):
            b.request_dd_visit("dd-1", "g-1", "v-1", "合资可行性核验", "doc-other")
        b.request_dd_visit("dd-1", "g-1", "v-1", "合资可行性核验", "doc-ru-1",
                           request_key="visit-1")
        b.complete_dd_visit("dd-1", "v-1")
        # 第二个尚未完成的访问
        b.request_dd_visit("dd-1", "g-1", "v-2", "合资可行性核验", "doc-zh-2")

        # 联系人离任：只终止未完成访问，已完成记录留存
        b.contact_departed("kz-agro", "c-kz")
        case = b._dd("dd-1")
        grant = case.grants["g-1"]
        self.assertEqual(grant.status, "terminated")
        visits = {v["visit_id"]: v["status"] for v in grant.visits}
        self.assertEqual(visits["v-1"], "done")       # 已完成保留
        self.assertEqual(visits["v-2"], "terminated")  # 未完成终止
        # 离任后不能再用该授权访问
        with self.assertRaises(PolicyViolation):
            b.request_dd_visit("dd-1", "g-1", "v-3", "合资可行性核验", "doc-ru-1")

    def test_recovery_resumes_expiry_todos_and_reminders(self):
        b = self.world.backend
        b.propose_commitment(
            "cmt-ttl", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
            self.world.bundle(), ttl_seconds=86400, request_key="ttl",
        )
        b.confirm_commitment("cmt-ttl", "ln-equip", "c-ln")
        b.confirm_commitment("cmt-ttl", "kz-agro", "c-kz")
        b.register_handover("cmt-ttl", "land-sy", 50, "HO-x", 5000)

        # 重启：新进程从事件日志重建，状态完整
        b2 = self.world.fresh_backend()
        self.assertAlmostEqual(b2._ledger().occupied("land-sy"), 200)

        # 临期提醒
        self.world.clock.advance(timedelta(hours=23))
        run1 = b2.run_due_jobs()
        self.assertIn("deadline:cmt-ttl", run1["reminders"][0])
        # 重复运行不重复发提醒
        run2 = b2.run_due_jobs()
        self.assertEqual(run2["reminders"], [])

        # 过截止时间：到期释放未履行份额，交接与费用留存
        self.world.clock.advance(timedelta(hours=3))
        run3 = b2.run_due_jobs()
        self.assertIn("cmt-ttl", run3["expired_commitments"])
        cmt = b2._commitment("cmt-ttl")
        self.assertEqual(cmt.status, "expired")
        self.assertEqual(cmt.handovers[0]["handover_ref"], "HO-x")
        ledger = b2._ledger()
        line = next(l for l in ledger.allocations["cmt-ttl"] if l.pool_id == "land-sy")
        self.assertAlmostEqual(line.occupied, 50)  # 只剩已交接
        self.assertAlmostEqual(line.qty_released, 150)
        # 再跑一次幂等
        run4 = self.world.fresh_backend().run_due_jobs()
        self.assertEqual(run4["expired_commitments"], [])

    def test_project_view_states_responsible_gaps_history_reason(self):
        b = self.world.backend
        b.open_project("prj-1", "中哈面粉合资", "c-sec")
        b.propose_commitment(
            "cmt-1", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
            self.world.bundle(), ttl_seconds=30 * 86400, project_id="prj-1",
            request_key="cmt1",
        )
        b.confirm_commitment("cmt-1", "ln-equip", "c-ln")
        view_pending = b.project_view("prj-1")
        self.assertEqual(view_pending["current_responsible"], "c-kz")
        self.assertEqual(view_pending["active_commitment_count"], 1)

        b.confirm_commitment("cmt-1", "kz-agro", "c-kz")
        b.register_handover("cmt-1", "land-sy", 120, "HO-9", 120_000)
        view = b.project_view("prj-1")
        self.assertEqual(view["current_responsible"], "c-ln")
        gap_pools = {g["pool_id"] for g in view["resource_gaps"]}
        self.assertEqual(gap_pools, {"land-sy", "wh-sy", "train-kz", "expert-agro"})
        land_gap = next(g for g in view["resource_gaps"] if g["pool_id"] == "land-sy")
        self.assertAlmostEqual(land_gap["unfulfilled_qty"], 80)
        self.assertEqual(len(view["commitment_history"]), 1)

        # 终止后视图说明最终原因
        b.request_adjustment("cmt-1", "adj-t", "terminate", "c-sec2",
                             {"reason": "原料价格未达成一致"})
        b.approve_adjustment("cmt-1", "adj-t", "c-local")
        view_end = b.project_view("prj-1")
        self.assertEqual(view_end["active_commitment_count"], 0)
        self.assertEqual(view_end["final_outcomes"][0]["reason"], "原料价格未达成一致")
        self.assertEqual(view_end["current_responsible"], "c-sec")
        # 历次承诺仍可追溯
        self.assertEqual(view_end["commitment_history"][0]["handovers_recorded"], 1)


if __name__ == "__main__":
    unittest.main()
