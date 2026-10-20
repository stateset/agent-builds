"""Plan search is local to accepted snapshots and never acts on hidden plans."""

import copy
import unittest
from concurrent.futures import Future
from unittest import mock

import test_tui_browser as fixtures
import test_tui_plan_stages as plan_fixtures
from agent_builds import tui
from agent_builds.store import Store
from agent_builds.tui_data import LazyStore
from agent_builds.tui_plan_filter import PlanFilter


class PlanFilterTests(unittest.TestCase):
    def test_literal_unicode_search_covers_recorded_fields(self):
        item = plan_fixtures.plan()
        item["name"] = "Straße [Beta]"
        item["stages"][0]["job"].update(
            command_display="cargo test --locked",
            reason="disk headroom",
            cwd="/work/repo",
        )
        report, view = [item], PlanFilter()
        for query in (
            "STRASSE",
            "[Beta]",
            item["id"],
            "lint",
            "a" * 12,
            "--locked",
            "disk headroom",
            "/work/repo",
            "failed",
        ):
            view.search(query)
            self.assertEqual(view.get(report), report, query)
        for query in (".*", "(?", "failed\nlint"):
            view.search(query)
            self.assertEqual(view.get(report), [], query)

    def test_idle_and_query_changes_reuse_index_until_snapshot_changes(self):
        report, view = [plan_fixtures.plan()], PlanFilter()
        self.assertIs(view.get(report), report)
        with mock.patch(
            "agent_builds.tui_plan_filter.searchable",
            side_effect=AssertionError("unfiltered indexing"),
        ):
            self.assertIs(view.get(report), report)
        view.search("lint")
        first = view.get(report)
        with mock.patch(
            "agent_builds.tui_plan_filter.searchable",
            side_effect=AssertionError("duplicate indexing"),
        ):
            self.assertIs(view.get(report), first)
            view.search("tests")
            self.assertEqual(view.get(report), report)
        changed = copy.deepcopy(report)
        changed[0]["stages"][1]["name"] = "new-stage"
        self.assertEqual(view.get(changed), [])
        view.search("")
        self.assertIs(view.get(changed), changed)

    def test_mismatched_job_evidence_never_creates_matches(self):
        item, view = plan_fixtures.plan(), PlanFilter()
        item["stages"][0]["job"].update(id="c" * 12, command_display="WRONG COMMAND")
        view.search("WRONG COMMAND")
        self.assertEqual(view.get([item]), [])

    def test_pending_counts_remain_unknown_and_stale_labels_keep_priority(self):
        view = PlanFilter()
        view.search("needle")
        self.assertNotIn("0/0", view.label("Loading plans", None))
        self.assertEqual(view.empty(None), ["Loading plans"])
        self.assertEqual(view.empty(None, "error"), ["Plans unavailable"])
        self.assertTrue(
            view.label("STALE PLANS", [], compact=True).startswith("STALE PLANS 0/0 /")
        )
        self.assertTrue(
            view.label("PLANS~ 0s", [], compact=True).startswith("PLANS~ 0/0 /")
        )
        self.assertEqual(view.empty([]), ["No matching plans", "/: blank clears"])

    def test_query_is_bounded_and_does_not_modify_snapshot(self):
        report, view = [plan_fixtures.plan()], PlanFilter()
        before = copy.deepcopy(report)
        view.search("x" * 1000)
        self.assertEqual(len(view.query), 256)
        view.get(report)
        self.assertEqual(report, before)


class PlanSearchDashboardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.reports = [
            plan_fixtures.plan("plan-alpha"),
            plan_fixtures.plan("plan-beta"),
        ]
        for index, plan in enumerate(self.reports):
            plan["name"] = ("Alpha", "Beta")[index]
            plan["stages"] = [
                dict(
                    name="check",
                    state="failed",
                    reason="",
                    job_id=self.fixture.jobs[2],
                    job=self.fixture.store.get(self.fixture.jobs[2]),
                )
            ]

    def dashboard(self, keys, size=(24, 100), **kwargs):
        config = self.fixture.store.config_path.read_bytes()
        before = list(
            map(tuple, self.fixture.store.db.execute("SELECT * FROM jobs ORDER BY id"))
        )
        with mock.patch.object(tui, "choose_view", return_value=7), mock.patch.object(
            tui, "read_tool", side_effect=lambda root, key: copy.deepcopy(self.reports)
        ), mock.patch.object(
            LazyStore, "writable", side_effect=AssertionError("queue mutation")
        ), mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("schema initialization")
        ):
            frames = self.fixture.dashboard(keys, size, **kwargs)
        self.assertEqual(config, self.fixture.store.config_path.read_bytes())
        self.assertEqual(
            before,
            list(
                map(
                    tuple,
                    self.fixture.store.db.execute("SELECT * FROM jobs ORDER BY id"),
                )
            ),
        )
        return frames

    def test_search_and_clear_preserve_matching_plan_and_stage(self):
        frames = self.dashboard(
            [ord("V"), ord("j"), 9, ord("/"), ord("/"), ord("q")], answers=["beta", ""]
        )
        for frame in frames[3:]:
            text = "\n".join(frame.values())
            self.assertIn("> check", text)
            self.assertIn("Enter " + self.fixture.jobs[2], text)
        self.assertIn("plan-beta", "\n".join(frames[-1].values()))
        self.assertTrue(
            any("1/2 loaded / beta" in "\n".join(f.values()) for f in frames)
        )

    def test_no_matches_are_explicit_and_cannot_act_on_hidden_plan(self):
        for size in ((24, 100), (8, 20)):
            frames = self.dashboard(
                [ord("V"), ord("/"), 10, ord("c"), ord("r"), ord("e"), ord("q")],
                size,
                answers=["absent"],
            )
            text = "\n".join(frames[-1].values())
            self.assertIn("No matching plans", text)
            self.assertIn("/: blank clears", text)
            self.assertNotIn("No plans yet", text)
            self.assertEqual(len(self.fixture.prompt_calls), 1)

    def test_cancelled_search_retains_query_and_matching_selection(self):
        frames = self.dashboard(
            [ord("V"), ord("/"), ord("/"), ord("q")], answers=["beta", None]
        )
        self.assertIn("plan-beta", "\n".join(frames[-1].values()))
        self.assertIn("1/2 loaded / beta", "\n".join(frames[-1].values()))
        self.assertEqual(self.fixture.prompt_calls[-1].args[2], "beta")

    def test_refresh_preserves_selected_match_when_newer_matches_arrive(self):
        now, pending = [100], Future()
        keys = iter([ord("V"), ord("/"), -1, -1, ord("q")])

        def key():
            value = next(keys)
            if value == -1 and now[0] == 100:
                newest = copy.deepcopy(self.reports[1])
                newest["id"] = "beta-newest"
                self.reports.insert(0, newest)
                now[0] = 102
            return value

        frames = self.dashboard(key, answers=["beta"], clock=lambda: now[0])
        self.assertIn("plan-beta", "\n".join(frames[-1].values()))
        self.assertIn("2/3 loaded / beta", "\n".join(frames[-1].values()))

    def test_plan_search_does_not_clear_job_browser_query(self):
        frames = self.dashboard(
            [ord("/"), ord("V"), ord("/"), ord("U"), ord("U"), ord("q")],
            answers=["absent-job", "beta"],
        )
        self.assertIn("No matching jobs", "\n".join(frames[-1].values()))
