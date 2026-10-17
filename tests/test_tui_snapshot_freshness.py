"""Cached tool snapshots keep their age and uncertainty visible without writes."""

from concurrent.futures import Future
import copy
import curses
import unittest
from unittest import mock

import test_tui_browser as fixtures

from agent_builds import tui
from agent_builds.store import Store
from agent_builds.tui_data import LazyStore
from agent_builds.tui_logs import LogView, TailCache


VIEWS = (
    (1, "processes", "processes", 1),
    (7, "plans", ("plans", None), 1),
    (8, "advisor", ("advisor", None), 2),
    (9, "groups", ("groups", None), 1),
    (10, "watches", ("watches", None), 1),
)


def completed(loader, key):
    future = Future()
    try:
        future.set_result(loader(key))
    except Exception as error:
        future.set_exception(error)
    return future


def resolved(value):
    future = Future()
    future.set_result(copy.deepcopy(value))
    return future


def body(frame, view, size):
    # Exclude independently fresh host/global queue snapshots. These tests
    # require the selected tool's own status, rather than another view's age.
    first = 14 if view == 1 and size[1] >= 65 else 6 if size[1] >= 65 else 4
    return "\n".join(
        value for row, value in frame.items() if first <= row < size[0] - 1
    )


class SnapshotFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.store, self.jobs = (
            self.fixture.root,
            self.fixture.store,
            self.fixture.jobs,
        )
        self.plan_id = "plan00000001"
        self.watch_id = "watch0000001"
        self.group_id = "group0000001"
        self.payloads = {
            "processes": [
                dict(
                    pid=12345,
                    cpu=10,
                    rss_mb=20,
                    name="CACHED-PROCESS",
                    cwd=str(self.root),
                )
            ],
            "plans": [
                dict(
                    id=self.plan_id,
                    state="failed",
                    name="cached-plan",
                    elapsed_seconds=0,
                    controller_alive=None,
                    reason="",
                    stages=[],
                )
            ],
            "advisor": dict(lines=["CACHED ADVISOR"]),
            "groups": [
                dict(id=self.group_id, state="failed", name="cached-group", jobs=[])
            ],
            "watches": [self.watch(self.watch_id)],
        }
        self.markers = {
            "processes": "CACHED-PROCESS",
            "plans": self.plan_id,
            "advisor": "CACHED ADVISOR",
            "groups": self.group_id,
            "watches": self.watch_id,
        }

    def watch(self, handle):
        return dict(
            id=handle,
            state="waiting",
            complete=False,
            controller_alive=None,
            definition=dict(external_ref="fixture external task"),
            probe_count=1,
            external_idle_seconds=0,
            reason="read-only observation",
            probes=[dict(job_id=self.jobs[2])],
        )

    def dashboard(self, keys, size, **kwargs):
        before = list(
            map(tuple, self.store.db.execute("SELECT * FROM jobs ORDER BY id"))
        )
        config = self.store.config_path.read_bytes()
        with mock.patch.object(
            LazyStore, "writable", side_effect=AssertionError("queue mutation")
        ), mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("queue initialized")
        ), mock.patch.object(
            Store, "recover", side_effect=AssertionError("recovery from inspection")
        ), mock.patch.object(
            TailCache, "read", side_effect=AssertionError("synchronous inline log read")
        ), mock.patch.object(
            LogView, "poll", side_effect=AssertionError("synchronous focused log read")
        ), mock.patch.object(
            tui.subprocess,
            "Popen",
            side_effect=AssertionError("native command launched"),
        ), mock.patch.object(
            tui,
            "read_tool",
            side_effect=lambda root, key: copy.deepcopy(self.payloads[key[0]]),
        ), mock.patch(
            "agent_builds.external.lines",
            side_effect=lambda row: [row["id"], row["state"], row["reason"]],
        ), mock.patch(
            "agent_builds.advice.lines", side_effect=lambda report: report["lines"]
        ):
            frames = self.fixture.dashboard(keys, size, **kwargs)
        self.assertEqual(self.store.config_path.read_bytes(), config)
        self.assertEqual(
            list(map(tuple, self.store.db.execute("SELECT * FROM jobs ORDER BY id"))),
            before,
        )
        self.assertEqual(self.fixture.prompt_calls, [])
        return frames

    def test_cached_tool_and_process_snapshots_show_age_refreshing_and_strict_stale_boundary(
        self,
    ):
        size = (30, 120)
        for view, name, target, interval in VIEWS:
            now, calls, pending, ticks = [100], [], Future(), [0]
            keys = iter([ord("V"), -1, -1, -1, -1, ord("q")])

            def submit(loader, key):
                if key != target:
                    return completed(loader, key)
                calls.append(now[0])
                return resolved(self.payloads[name]) if len(calls) == 1 else pending

            def key():
                value = next(keys)
                if value == -1:
                    index = ticks[0]
                    if index == 0:
                        now[0] = 100 + interval
                    elif index == 1:
                        now[0] = 100 + 2 * interval
                    elif index == 2:
                        now[0] = 100 + 2 * interval + 0.1
                    else:
                        pending.set_result(copy.deepcopy(self.payloads[name]))
                        now[0] = 100 + 3 * interval
                    ticks[0] += 1
                return value

            with self.subTest(view=name), mock.patch.object(
                tui, "choose_view", return_value=view
            ):
                frames = self.dashboard(
                    key, size, clock=lambda: now[0], background_submit=submit
                )
            first, refreshing, boundary, stale, fresh = [
                body(frame, view, size) for frame in frames[1:6]
            ]
            self.assertIn("Snapshot 0s ago", first)
            self.assertIn("Snapshot {}s ago; refreshing".format(interval), refreshing)
            self.assertNotIn("STALE", refreshing)
            self.assertIn("Snapshot {}s ago; refreshing".format(2 * interval), boundary)
            self.assertNotIn("STALE", boundary)
            self.assertIn("STALE Snapshot", stale)
            self.assertIn("refreshing", stale)
            self.assertIn("Snapshot 0s ago", fresh)
            self.assertNotIn("STALE", fresh)
            self.assertNotIn("refreshing", fresh)
            for frame in frames[1:]:
                self.assertIn(self.markers[name], body(frame, view, size))
            self.assertEqual(calls, [100, 100 + interval])

    def test_minimum_panes_keep_selected_handle_and_snapshot_freshness_visible(self):
        size = (8, 20)
        for view, name, target, unused in (VIEWS[1], VIEWS[4]):
            now, calls, pending, ticks = [100], [], Future(), [0]
            keys = iter([ord("V"), -1, -1, ord("q")])

            def submit(loader, key):
                if key != target:
                    return completed(loader, key)
                calls.append(now[0])
                return resolved(self.payloads[name]) if len(calls) == 1 else pending

            def key():
                value = next(keys)
                if value == -1:
                    now[0] = 101 if ticks[0] == 0 else 105
                    ticks[0] += 1
                return value

            with self.subTest(view=name), mock.patch.object(
                tui, "choose_view", return_value=view
            ):
                frames = self.dashboard(
                    key, size, clock=lambda: now[0], background_submit=submit
                )
            label = "PLANS" if name == "plans" else "EXTERNAL"
            self.assertTrue(frames[1][4].startswith(label))
            self.assertIn("0s", frames[1][4])
            self.assertIn("1s~", frames[2][4])
            self.assertNotIn("STALE", frames[2][4])
            self.assertIn("STALE " + label, frames[3][4])
            self.assertIn("5s~", frames[3][4])
            for frame in frames[1:]:
                self.assertEqual(frame[5], self.markers[name])
                self.assertEqual(frame[6], self.payloads[name][0]["state"])
                self.assertIn("q", frame[7])
            self.assertEqual(calls, [100, 101])
            self.assertFalse(pending.done())

    def test_empty_success_is_a_cached_snapshot_when_refresh_stalls(self):
        variants = [
            (view, name, target, interval, (30, 120))
            for view, name, target, interval in VIEWS
        ]
        variants += [
            (view, name, target, interval, (8, 20))
            for view, name, target, interval in (VIEWS[1], VIEWS[4])
        ]
        for view, name, target, interval, size in variants:
            now, calls, pending = [100], [], Future()
            payload = dict(lines=[]) if name == "advisor" else []
            keys = iter([ord("V"), -1, ord("q")])

            def submit(loader, key):
                if key != target:
                    return completed(loader, key)
                calls.append(now[0])
                return resolved(payload) if len(calls) == 1 else pending

            def key():
                value = next(keys)
                if value == -1:
                    now[0] = 100 + 3 * interval
                return value

            with self.subTest(view=name, size=size), mock.patch.object(
                tui, "choose_view", return_value=view
            ):
                frames = self.dashboard(
                    key, size, clock=lambda: now[0], background_submit=submit
                )
            initial, stale = body(frames[1], view, size), body(frames[-1], view, size)
            self.assertNotIn("Loading", initial)
            self.assertNotIn("loading", initial)
            self.assertIn("STALE", stale)
            self.assertNotIn("Loading", stale)
            if size[1] >= 65:
                self.assertIn("Snapshot 0s ago", initial)
                self.assertIn("refreshing", stale)
            else:
                self.assertIn("0s", frames[1][4])
                self.assertIn("{}s~".format(3 * interval), frames[-1][4])
            self.assertEqual(calls, [100, 100 + 3 * interval])
            self.assertFalse(pending.done())

    def test_initial_pending_and_failure_are_visible_and_retry_only_when_due(self):
        variants = [
            (view, name, target, interval, (30, 120))
            for view, name, target, interval in VIEWS
        ]
        variants += [
            (view, name, target, interval, (8, 20))
            for view, name, target, interval in (VIEWS[1], VIEWS[4])
        ]
        for view, name, target, interval, size in variants:
            for failure in (False, True):
                now, calls, pending, ticks = [100], [], Future(), [0]
                keys = iter([ord("V"), -1, -1, ord("q")])

                def submit(loader, key):
                    if key != target:
                        return completed(loader, key)
                    calls.append(now[0])
                    if len(calls) > 1:
                        return resolved(self.payloads[name])
                    if failure:
                        pending.set_exception(OSError("fixture snapshot blocked"))
                    return pending

                def key():
                    value = next(keys)
                    if value == -1:
                        if ticks[0] == 0:
                            now[0] = 100 + interval / 2
                        else:
                            now[0] = 100 + interval
                            if not failure:
                                pending.set_result(copy.deepcopy(self.payloads[name]))
                        ticks[0] += 1
                    return value

                with self.subTest(
                    view=name, size=size, failure=failure
                ), mock.patch.object(tui, "choose_view", return_value=view):
                    frames = self.dashboard(
                        key, size, clock=lambda: now[0], background_submit=submit
                    )
                for frame in frames[1:3]:
                    value = body(frame, view, size)
                    if size[1] >= 65:
                        self.assertIn(
                            name.capitalize() + " unavailable"
                            if failure
                            else "Loading " + name,
                            value,
                        )
                        if failure:
                            self.assertIn("fixture snapshot blocked", value)
                    else:
                        self.assertIn("error" if failure else "loading", frame[4])
                value = body(frames[-1], view, size)
                self.assertNotIn("snapshot blocked", value)
                self.assertNotIn("loading", value.lower())
                self.assertNotIn("unavailable", value.lower())
                self.assertIn(self.markers[name], value)
                self.assertEqual(calls, [100, 100 + interval] if failure else [100])

    def test_refresh_failure_retains_last_data_and_fresh_completion_clears_stale(self):
        variants = [
            (view, name, target, interval, (30, 120))
            for view, name, target, interval in VIEWS
        ]
        variants += [
            (view, name, target, interval, (8, 20))
            for view, name, target, interval in (VIEWS[1], VIEWS[4])
        ]
        for view, name, target, interval, size in variants:
            now, calls, pending, ticks = [100], [], Future(), [0]
            keys = iter([ord("V"), -1, -1, -1, -1, ord("q")])

            def submit(loader, key):
                if key != target:
                    return completed(loader, key)
                calls.append(now[0])
                if len(calls) == 1:
                    return resolved(self.payloads[name])
                if len(calls) == 2:
                    failed = Future()
                    failed.set_exception(OSError("fixture refresh failed"))
                    return failed
                return pending

            def key():
                value = next(keys)
                if value == -1:
                    index = ticks[0]
                    now[0] = 100 + interval * (1, 1.5, 2, 3)[index]
                    if index == 3:
                        pending.set_result(copy.deepcopy(self.payloads[name]))
                    ticks[0] += 1
                return value

            with self.subTest(view=name, size=size), mock.patch.object(
                tui, "choose_view", return_value=view
            ):
                frames = self.dashboard(
                    key, size, clock=lambda: now[0], background_submit=submit
                )
            for frame in frames[2:5]:
                value = body(frame, view, size)
                self.assertIn("stale", value.lower())
                if size[1] >= 65:
                    self.assertIn("fixture refresh failed", value)
                else:
                    self.assertEqual(frame[5], self.markers[name])
                    self.assertEqual(frame[6], self.payloads[name][0]["state"])
                self.assertIn(self.markers[name], value)
            value = body(frames[-1], view, size)
            self.assertIn("Snapshot 0s ago" if size[1] >= 65 else "0s", value)
            self.assertNotIn("stale", value.lower())
            self.assertNotIn("fixture refresh failed", value)
            self.assertIn(self.markers[name], value)
            self.assertEqual(calls, [100, 100 + interval, 100 + 2 * interval])

    def test_help_suspends_refresh_without_losing_selected_tool(self):
        self.payloads["watches"].append(self.watch("watch0000002"))
        for size in ((30, 120), (8, 20)):
            now, calls, pending, ticks = [100], [], Future(), [0]
            keys = iter(
                [ord("V"), ord("j"), -1, ord("?"), -1, -1, ord("q"), -1, ord("q")]
            )

            def submit(loader, key):
                if key != ("watches", None):
                    return completed(loader, key)
                calls.append(now[0])
                return (
                    pending if len(calls) == 2 else resolved(self.payloads["watches"])
                )

            def key():
                value = next(keys)
                if value == -1:
                    if ticks[0] == 0:
                        now[0] = 101
                    elif ticks[0] in (1, 2):
                        self.assertEqual(len(calls), 2)
                        now[0] += 1
                        if ticks[0] == 2:
                            pending.set_exception(OSError("obsolete help refresh"))
                    ticks[0] += 1
                return value

            with self.subTest(size=size), mock.patch.object(
                tui, "choose_view", return_value=10
            ):
                frames = self.dashboard(
                    key, size, clock=lambda: now[0], background_submit=submit
                )
            self.assertIn("watch0000002", body(frames[2], 10, size))
            for frame in frames[4:7]:
                self.assertIn("BUILDR HELP", frame[0])
            self.assertIn("watch0000002", body(frames[-1], 10, size))
            self.assertEqual(len(calls), 3)
            self.assertTrue(
                all(
                    "obsolete help refresh" not in "\n".join(frame.values())
                    for frame in frames
                )
            )

    def test_hidden_tool_discards_late_results_without_overlapping_workers(self):
        for failed in (False, True):
            now, calls, futures, ticks = [100], [], [], [0]
            size = (30, 120)
            keys = iter([ord("V"), -1, ord("V"), -1, -1, ord("q")])

            def submit(loader, key):
                if key not in (("plans", None), ("watches", None)):
                    return completed(loader, key)
                calls.append(key)
                if len(calls) == 1:
                    return resolved(self.payloads["plans"])
                future = Future()
                futures.append(future)
                return future

            def key():
                value = next(keys)
                if value == -1:
                    if ticks[0] == 0:
                        now[0] = 101
                    elif ticks[0] == 1:
                        self.assertEqual(len(calls), 2)
                        if failed:
                            futures[0].set_exception(OSError("obsolete plan refresh"))
                        else:
                            futures[0].set_result(copy.deepcopy(self.payloads["plans"]))
                    else:
                        self.assertEqual(len(calls), 3)
                        futures[1].set_result(copy.deepcopy(self.payloads["watches"]))
                    ticks[0] += 1
                return value

            with self.subTest(failed=failed), mock.patch.object(
                tui, "choose_view", side_effect=[7, 10]
            ):
                frames = self.dashboard(
                    key, size, clock=lambda: now[0], background_submit=submit
                )
            self.assertEqual(
                calls, [("plans", None), ("plans", None), ("watches", None)]
            )
            self.assertIn(self.watch_id, body(frames[-1], 10, size))
            for frame in frames[3:]:
                self.assertNotIn(self.plan_id, body(frame, 10, size))
                self.assertNotIn("obsolete plan refresh", body(frame, 10, size))

    def test_full_only_views_suspend_refresh_below_minimum_size(self):
        for view, name, target, interval in (VIEWS[0], VIEWS[2], VIEWS[3]):
            now, calls, pending, ticks, size = [100], [], Future(), [0], [(30, 120)]
            keys = iter(
                [
                    ord("V"),
                    -1,
                    curses.KEY_RESIZE,
                    -1,
                    -1,
                    curses.KEY_RESIZE,
                    -1,
                    ord("q"),
                ]
            )

            def submit(loader, key):
                if key != target:
                    return completed(loader, key)
                calls.append(now[0])
                return pending if len(calls) == 2 else resolved(self.payloads[name])

            def key():
                value = next(keys)
                if value == curses.KEY_RESIZE:
                    size[0] = (8, 20) if size[0][1] >= 65 else (30, 120)
                elif value == -1:
                    if ticks[0] == 0:
                        now[0] = 100 + interval
                    elif ticks[0] in (1, 2):
                        self.assertEqual(size[0], (8, 20))
                        self.assertEqual(len(calls), 2)
                        now[0] += 10
                        if ticks[0] == 2:
                            pending.set_result(copy.deepcopy(self.payloads[name]))
                    ticks[0] += 1
                return value

            with self.subTest(view=name), mock.patch.object(
                tui, "choose_view", return_value=view
            ):
                frames = self.dashboard(
                    key, lambda: size[0], clock=lambda: now[0], background_submit=submit
                )
            for frame in frames[3:6]:
                self.assertIn("Resize to 65x15", "\n".join(frame.values()))
                self.assertNotIn(self.markers[name], "\n".join(frame.values()))
            self.assertIn(self.markers[name], body(frames[-1], view, (30, 120)))
            self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
