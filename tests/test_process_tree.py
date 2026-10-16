import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_builds import process_tree, telemetry


def stat_line(pid, parent=1, group=100, start=1000, ticks=30, pages=8, comm=b"worker"):
    fields = [b"0"] * 22
    fields[0] = b"S"
    for index, value in (
        (1, parent),
        (2, group),
        (11, ticks),
        (19, start),
        (21, pages),
    ):
        fields[index] = str(value).encode("ascii")
    return str(pid).encode("ascii") + b" (" + comm + b") " + b" ".join(fields)


class ProcessReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def process(self, pid, **kwargs):
        path = self.root / str(pid)
        path.mkdir()
        (path / "stat").write_bytes(stat_line(pid, **kwargs))
        return path / "stat"

    def test_counters_and_identity_match_linux_stat_fields(self):
        self.process(100, parent=99, ticks=70, pages=12)
        self.assertEqual(
            process_tree.read_processes(self.root), [(100, "1000", 99, 100, 70, 12)]
        )

    def test_process_names_may_contain_parentheses_newlines_and_invalid_utf8(self):
        self.process(100, comm=b"odd) (\xff\nname)")
        self.assertEqual(process_tree.read_processes(self.root)[0][:2], (100, "1000"))

    def test_foreign_processes_are_not_opened(self):
        self.process(100)
        with mock.patch.object(process_tree.os, "getuid", return_value=os.getuid() + 1):
            with mock.patch(
                "builtins.open", side_effect=AssertionError("foreign stat read")
            ):
                self.assertEqual(process_tree.read_processes(self.root), [])

    def test_missing_root_missing_process_and_invalid_metadata_are_skipped(self):
        (self.root / "self").mkdir()
        (self.root / "101").mkdir()
        self.process(102).write_bytes(b"invalid")
        self.process(103).write_bytes(b"103 (short) S 1")
        self.process(104).write_bytes(stat_line(104, start="invalid"))
        self.process(105).write_bytes(stat_line(105, ticks="invalid"))
        rows = process_tree.read_processes(self.root)
        self.assertEqual(rows, [])
        self.assertEqual(process_tree.read_processes(self.root / "missing"), [])

    def test_oversized_metadata_is_not_parsed_and_negative_rss_is_clamped(self):
        self.process(100).write_bytes(
            stat_line(100) + b" " * process_tree.MAX_STAT_BYTES
        )
        self.process(101, pages=-1)
        self.assertEqual(
            process_tree.read_processes(self.root), [(101, "1000", 1, 100, 30, 0)]
        )

    def test_unreadable_process_does_not_discard_other_observations(self):
        self.process(100)
        self.process(101)
        opened = open

        def read(path, *args, **kwargs):
            if str(path).endswith("/100/stat"):
                raise PermissionError("process changed credentials")
            return opened(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=read):
            self.assertEqual(
                [row[0] for row in process_tree.read_processes(self.root)], [101]
            )


class ProcessMembershipTests(unittest.TestCase):
    def test_descendants_can_change_groups_and_unrelated_processes_stay_out(self):
        rows = [
            (1, "a", 0, 1, 1, 1),
            (2, "b", 1, 22, 1, 1),
            (3, "c", 2, 33, 1, 1),
            (4, "d", 0, 4, 1, 1),
        ]
        self.assertEqual(
            process_tree.select_tree(rows, 1, "a", {(1, "a")}),
            {(1, "a"), (2, "b"), (3, "c")},
        )

    def test_reparented_group_member_requires_original_leader(self):
        member = (2, "b", 99, 1, 1, 1)
        self.assertEqual(process_tree.select_tree([member], 1, "a", {(1, "a")}), set())
        self.assertEqual(
            process_tree.select_tree(
                [(1, "a", 0, 1, 1, 1), member], 1, "a", {(1, "a")}
            ),
            {(1, "a"), (2, "b")},
        )

    def test_reused_leader_pid_does_not_authorize_group_members(self):
        rows = [(1, "new", 0, 1, 1, 1), (2, "b", 99, 1, 1, 1)]
        self.assertEqual(process_tree.select_tree(rows, 1, "old", {(1, "old")}), set())

    def test_known_children_survive_reparenting_but_reused_child_pids_do_not(self):
        rows = [
            (2, "b", 99, 22, 1, 1),
            (3, "c", 2, 33, 1, 1),
            (4, "new", 99, 44, 1, 1),
            (5, "d", 4, 55, 1, 1),
        ]
        self.assertEqual(
            process_tree.select_tree(rows, 1, "a", {(1, "a"), (2, "b"), (4, "old")}),
            {(2, "b"), (3, "c")},
        )

    def test_deep_out_of_order_tree_and_cycles_terminate_without_recursion(self):
        rows = [(pid, str(pid), pid - 1, pid, 1, 1) for pid in reversed(range(1, 1201))]
        rows.extend(
            [(1201, "cycle", 1200, 1201, 1, 1), (1200, "1200", 1201, 1200, 1, 1)]
        )
        selected = process_tree.select_tree(rows, 1, "1", {(1, "1")})
        self.assertEqual(len(selected), 1201)

    def test_sampler_keeps_counters_monotonic_and_never_adopts_reused_ids(self):
        sampler = telemetry.JobSampler(100, "a")
        snapshots = [
            [(100, "a", 0, 100, 20, 10), (101, "b", 100, 101, 30, 12)],
            [(101, "b", 99, 101, 25, 8)],
            [(100, "new", 0, 100, 9999, 9999), (101, "new", 99, 101, 9999, 9999)],
        ]
        with mock.patch.object(telemetry, "read_processes", side_effect=snapshots):
            for _ in snapshots:
                result = sampler.sample()
        self.assertEqual(sampler.known, {(100, "a"), (101, "b")})
        self.assertEqual(sampler.counters, {(100, "a"): 20, (101, "b"): 30})
        self.assertEqual(sampler.peak, 22 * sampler.pages)
        self.assertEqual(result["samples"], 3)
        self.assertEqual(result["cpu_seconds"], round(50 / sampler.ticks, 4))


if __name__ == "__main__":
    unittest.main()
