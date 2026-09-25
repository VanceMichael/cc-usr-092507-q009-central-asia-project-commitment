"""跨进程争用与恢复测试：两个"时区节点"同时提交同一业务请求。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

from tests.test_liaoning_scenario import World


class CrossProcessTest(unittest.TestCase):
    def test_concurrent_confirmation_yields_single_hold(self):
        world = World()
        world.seed_parties()
        world.seed_pools()
        world.seed_intents()
        b = world.backend
        b.propose_commitment(
            "cmt-x", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
            world.bundle(), ttl_seconds=3600, request_key="prop-x",
        )
        b.confirm_commitment("cmt-x", "ln-equip", "c-ln", request_key="conf-a")
        store_path = str(world.path)

        script = textwrap.dedent(f"""
            import json
            import sys
            sys.path.insert(0, {str(SRC)!r})
            from central_asia_project_commitment import Backend, EventStore, SystemClock
            try:
                r = Backend(EventStore({store_path!r}), SystemClock()).confirm_commitment(
                    "cmt-x", "kz-agro", "c-kz", request_key="conf-b-shared")
                print(json.dumps(r))
            except Exception as exc:
                print(json.dumps({{"error": type(exc).__name__}}))
                sys.exit(0)
        """)
        procs = [
            subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
            for _ in range(2)
        ]
        for p in procs:
            self.assertEqual(p.returncode, 0, p.stderr)
        outputs = [json.loads(p.stdout.strip().splitlines()[-1]) for p in procs]
        # 一个首次成功 held，一个重放，结果引用一致
        statuses = sorted(o.get("status") for o in outputs)
        self.assertEqual(statuses, ["held", "held"])
        replayed = sorted(bool(o.get("replayed")) for o in outputs)
        self.assertEqual(replayed, [False, True])

        # 只有一次暂留、一份占用
        b2 = world.fresh_backend()
        held_events = [
            e for e in b2.store.read_all() if e.type == "ResourceBundleHeld"
        ]
        self.assertEqual(len(held_events), 1)
        self.assertAlmostEqual(b2._ledger().occupied("train-kz"), 1)
        self.assertAlmostEqual(b2._ledger().occupied("land-sy"), 200)
        world.close()

    def test_simultaneous_two_party_confirmation_holds_once(self):
        # 双方在不同时区同一刻分别确认（不同请求键），最终只产生一次暂留
        world = World()
        world.seed_parties()
        world.seed_pools()
        world.seed_intents()
        b = world.backend
        b.propose_commitment(
            "cmt-s", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
            world.bundle(), ttl_seconds=3600, request_key="prop-s",
        )
        store_path = str(world.path)

        script = textwrap.dedent("""
            import json, sys
            party, contact, key = sys.argv[1], sys.argv[2], sys.argv[3]
            sys.path.insert(0, %r)
            from central_asia_project_commitment import Backend, EventStore, SystemClock
            backend = Backend(EventStore(%r), SystemClock())
            try:
                r = backend.confirm_commitment("cmt-s", party, contact, request_key=key)
                print(json.dumps(r))
            except Exception as exc:
                print(json.dumps({"error": type(exc).__name__, "msg": str(exc)[:120]}))
        """ % (str(SRC), store_path))
        procs = [
            subprocess.Popen([sys.executable, "-c", script, "ln-equip", "c-ln", "key-ln"],
                             stdout=subprocess.PIPE, text=True),
            subprocess.Popen([sys.executable, "-c", script, "kz-agro", "c-kz", "key-kz"],
                             stdout=subprocess.PIPE, text=True),
        ]
        results = []
        for p in procs:
            out, _ = p.communicate()
            results.append(json.loads(out.strip().splitlines()[-1]))
        statuses = sorted(r.get("status", "") for r in results)
        self.assertIn("held", statuses)  # 恰有一方推进到暂留
        self.assertEqual(statuses.count("held"), 1)
        # 另一方不是重复暂留：可能是 awaiting（先到）或冲突拒绝
        self.assertTrue(
            "awaiting_counterpart" in statuses or any("error" in r for r in results),
            results,
        )
        b2 = world.fresh_backend()
        held_events = [e for e in b2.store.read_all() if e.type == "ResourceBundleHeld"]
        self.assertEqual(len(held_events), 1)
        cmt = b2._commitment("cmt-s")
        self.assertEqual(cmt.status, "held")
        self.assertEqual(len(cmt.confirmations), 2)
        world.close()


if __name__ == "__main__":
    unittest.main()
