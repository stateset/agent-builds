"""Lock-scan optimization must preserve the released scheduler's decisions."""

import copy
import importlib.util
import io
import json
from pathlib import Path
import random
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from agent_builds import locks, scheduler
from agent_builds.store import DEFAULTS


class LockScalingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="buildr-lock-reference-")
        archive = subprocess.check_output(
            ["git", "archive", "v0.24.2", "agent_builds"],
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(cls.temp.name)
        package = Path(cls.temp.name) / "agent_builds"
        spec = importlib.util.spec_from_file_location(
            "buildr_lock_reference_v0242",
            package / "__init__.py",
            submodule_search_locations=[str(package)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        from buildr_lock_reference_v0242 import (
            locks as old_locks,
            scheduler as old_scheduler,
        )

        cls.old_locks, cls.old_scheduler = old_locks, old_scheduler

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def row(self, name, resources, legacy=False, encoded=False, **changes):
        row = dict(
            id=name,
            resource_key=resources[0],
            lock_protocol=int(not legacy),
            resource_keys=json.dumps(resources) if encoded else resources,
            created=900,
            lane="quick",
            slots=1,
            memory_gb=0.25,
            scratch_gb=0,
            min_free_gb=0,
            cancel_requested=0,
        )
        row.update(changes)
        return row

    def test_mixed_lock_eligibility_preserves_legacy_primary_and_input_order(self):
        rows = [
            self.row("multi-blocked", ["checkout", "target"], encoded=True),
            self.row(
                "legacy-target", ["target"], legacy=True, resource_keys='["spoofed"]'
            ),
            self.row("single", ["target"]),
            self.row("independent", ["other", "spoofed"]),
        ]
        before = copy.deepcopy(rows)
        self.assertEqual(
            [row["id"] for row in locks.eligible(rows)],
            ["legacy-target", "single", "independent"],
        )
        self.assertEqual(locks.eligible(rows), self.old_locks.eligible(rows))
        self.assertEqual(rows, before)
        self.assertEqual(locks.eligible([]), [])

    def test_large_mixed_backlog_decodes_keys_linearly(self):
        legacy = [
            self.row("old-" + str(i), ["legacy-" + str(i)], legacy=True)
            for i in range(100)
        ]
        current = [
            self.row(
                "new-" + str(i), ["repo-" + str(i), "output-" + str(i)], encoded=True
            )
            for i in range(200)
        ]
        rows = legacy + current
        with mock.patch("agent_builds.locks.keys", wraps=locks.keys) as read:
            eligible = locks.eligible(rows)
        self.assertEqual(eligible, rows)
        self.assertLessEqual(read.call_count, 2 * len(rows))
        self.assertEqual(eligible, self.old_locks.eligible(rows))

    def test_busy_multi_lock_chain_keeps_fifo_on_every_resource(self):
        rows = [
            self.row("first", ["a", "b"], created=800),
            self.row("next", ["b", "c"], created=801),
            self.row("tail", ["c"], created=802),
            self.row("free", ["d"], created=803),
        ]
        running = [self.row("running", ["a"])]
        config = dict(
            DEFAULTS,
            cpu_slots=4,
            memory_gb=8,
            max_running=4,
            quick_reserve_slots=0,
            quick_reserve_jobs=0,
            quick_reserve_memory_gb=0,
            min_free_gb=0,
            min_available_gb=0,
        )
        selected, skipped = scheduler.choose(
            rows, running, config, 100, lambda row: 100, now=1000
        )
        self.assertEqual(selected["id"], "free")
        self.assertEqual(skipped, {})
        self.assertEqual(
            (selected, skipped),
            self.old_scheduler.choose(
                rows, running, config, 100, lambda row: 100, now=1000
            ),
        )

    def test_released_policy_matches_across_seeded_mixed_workloads(self):
        rng = random.Random(734291)
        for case in range(400):
            with self.subTest(case=case):
                rows = []
                for index in range(rng.randrange(41)):
                    resources = rng.sample(
                        ["key-" + str(i) for i in range(24)], rng.randrange(1, 5)
                    )
                    rows.append(
                        self.row(
                            str(index),
                            resources,
                            legacy=rng.random() < 0.3,
                            encoded=rng.random() < 0.5,
                            created=1000 - rng.randrange(241),
                            lane=rng.choice(("quick", "heavy")),
                            slots=rng.randrange(1, 5),
                            memory_gb=rng.choice((0.25, 1, 4, 8)),
                            scratch_gb=rng.choice((0, 0, 0.5)),
                            min_free_gb=rng.choice((0, 1)),
                            cancel_requested=int(rng.random() < 0.1),
                        )
                    )
                running = [
                    self.row(
                        "running-" + str(i),
                        ["key-" + str(rng.randrange(24))],
                        lane=rng.choice(("quick", "heavy")),
                        slots=rng.randrange(1, 4),
                        memory_gb=rng.choice((0.25, 1, 2)),
                        scratch_gb=rng.choice((0, 0.5)),
                    )
                    for i in range(rng.randrange(5))
                ]
                config = dict(
                    DEFAULTS,
                    cpu_slots=6,
                    max_running=4,
                    max_heavy=2,
                    memory_gb=8,
                    protected_quick=bool(case % 2),
                    quick_reserve_slots=2,
                    quick_reserve_jobs=1,
                    quick_reserve_memory_gb=2,
                    min_free_gb=0,
                    min_available_gb=1,
                )
                disk = {row["id"]: rng.choice((0, 0.5, 2, 100)) for row in rows}
                available = rng.choice((0, 1, 4, 16))
                before = copy.deepcopy((rows, running, config))
                expected_rows = self.old_locks.eligible(rows)
                actual_rows = locks.eligible(rows)
                self.assertEqual(
                    [row["id"] for row in actual_rows],
                    [row["id"] for row in expected_rows],
                )
                args = (running, config, available, lambda row: disk[row["id"]])
                expected = self.old_scheduler.choose(
                    expected_rows, *args, allow_backfill=bool(case % 3), now=1000
                )
                actual = scheduler.choose(
                    actual_rows, *args, allow_backfill=bool(case % 3), now=1000
                )
                self.assertEqual(
                    (actual[0]["id"] if actual[0] else None, actual[1]),
                    (expected[0]["id"] if expected[0] else None, expected[1]),
                )
                self.assertEqual((rows, running, config), before)
        self.assertEqual(scheduler.POLICY_VERSION, self.old_scheduler.POLICY_VERSION)


if __name__ == "__main__":
    unittest.main()
