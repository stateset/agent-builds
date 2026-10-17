"""Submission UI distinguishes a launch attempt from a verified queue receipt."""

from collections import namedtuple
import curses
from pathlib import Path
import unittest
from unittest import mock

import test_tui_browser as fixtures

from agent_builds.tui_submission_view import SubmissionView


Status = namedtuple(
    "Status",
    "key label phase kind handle detail receipt_path transcript_path",
)


def frame_text(frame):
    return "\n".join(frame.values())


class FakeTracker:
    """Only explicitly delivered receipt observations change an attempt's state."""

    def __init__(self, root):
        self.root = Path(root)
        self.starts = []
        self.polls = []
        self.current = {}
        self.updates = []

    @property
    def statuses(self):
        return tuple(self.current.values())

    @property
    def processes(self):
        return ()

    def start(self, argv, label, now):
        key = "attempt-{:02}".format(len(self.starts) + 1)
        self.starts.append((tuple(argv), label, now, key))
        self.current[key] = Status(
            key,
            label,
            "starting",
            None,
            None,
            "Waiting for a verified receipt",
            self.root / ("receipt-" + key + ".json"),
            self.root / "logs" / (key + ".log"),
        )
        return key

    def get(self, key):
        return self.current[key]

    def deliver(self, key, phase, kind=None, handle=None, detail=""):
        status = self.current[key]._replace(
            phase=phase, kind=kind, handle=handle, detail=detail
        )
        self.updates.append(status)
        return status

    def poll(self, now):
        self.polls.append(now)
        updates, self.updates = self.updates, []
        for status in updates:
            self.current[status.key] = status
        return updates


class TuiSubmissionFlowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.store = self.fixture.root, self.fixture.store
        self.tracker = FakeTracker(self.store.root)
        self.before = list(
            map(tuple, self.store.db.execute("SELECT * FROM jobs ORDER BY id"))
        )
        self.config_before = self.store.config_path.read_bytes()
        self.addCleanup(self.assert_no_real_submission)

    def assert_no_real_submission(self):
        self.assertEqual(
            list(map(tuple, self.store.db.execute("SELECT * FROM jobs ORDER BY id"))),
            self.before,
        )
        self.assertEqual(self.store.config_path.read_bytes(), self.config_before)

    def answers(self, action, command="printf '%s' 'value with spaces'"):
        if action == ord("n"):
            return [str(self.root), command, "quick", "local"]
        return [str(self.root), "smoke"]

    def dashboard(self, keys, dimensions=(24, 200), **kwargs):
        task = {"name": "smoke"}
        with mock.patch(
            "agent_builds.tui.SubmissionTracker", return_value=self.tracker
        ), mock.patch(
            "agent_builds.tui.subprocess.Popen",
            side_effect=AssertionError("untracked helper launched"),
        ), mock.patch(
            "agent_builds.profiles.repository", return_value=str(self.root)
        ), mock.patch(
            "agent_builds.profiles.tasks",
            return_value={str(self.root) + ":smoke": task},
        ):
            return self.fixture.dashboard(keys, dimensions, **kwargs)

    def test_new_job_and_saved_task_remain_starting_through_help_resize_and_quit(self):
        for action in (ord("n"), ord("t")):
            self.tracker = FakeTracker(self.store.root)
            size = [(24, 200)]
            keys = iter(
                (
                    action,
                    -1,
                    ord("?"),
                    ord("p"),
                    ord("q"),
                    curses.KEY_RESIZE,
                    -1,
                    ord("q"),
                )
            )

            def key():
                value = next(keys)
                if value == curses.KEY_RESIZE:
                    size[0] = (14, 64)
                return value

            with self.subTest(action=action):
                frames = self.dashboard(
                    key, dimensions=lambda: size[0], answers=self.answers(action)
                )
                self.assertEqual(len(self.tracker.starts), 1)
                self.assertEqual(self.tracker.statuses[0].phase, "starting")
                for index in (1, 2, 5, 6, 7):
                    text = frame_text(frames[index])
                    self.assertIn("Starting", text)
                    self.assertNotIn("Submitted", text)
                    self.assertNotIn("Created plan", text)
                self.assertIn("BUILDR HELP", frame_text(frames[3]))
                self.assertIn("BUILDR HELP", frame_text(frames[4]))
                self.assertGreaterEqual(len(self.tracker.polls), len(frames))
                self.assertEqual(
                    len(self.fixture.prompt_calls), len(self.answers(action))
                )

    def test_only_confirmed_receipts_show_the_exact_job_or_plan_handle(self):
        for action, kind in ((ord("n"), "job"), (ord("t"), "job"), (ord("t"), "plan")):
            self.tracker = FakeTracker(self.store.root)
            handle = "a1b2c3d4e5f6"
            keys = iter((action, -1, -1, ord("q")))
            delivered = [False]

            def key():
                value = next(keys)
                if value == -1 and not delivered[0]:
                    delivered[0] = True
                    self.tracker.deliver(
                        self.tracker.starts[0][3],
                        "submitted",
                        kind,
                        handle,
                        "Receipt and database identity verified",
                    )
                return value

            with self.subTest(action=action, kind=kind):
                frames = self.dashboard(key, answers=self.answers(action))
                self.assertIn("Starting", frame_text(frames[1]))
                self.assertNotIn(handle, frame_text(frames[1]))
                for frame in frames[2:]:
                    text = frame_text(frame)
                    self.assertIn(handle, text)
                    self.assertIn(
                        "Submitted job" if kind == "job" else "Created plan", text
                    )
                self.assertEqual(len(self.tracker.starts), 1)
                argv = self.tracker.starts[0][0]
                if action == ord("n"):
                    self.assertEqual(argv[-3:], ("printf", "%s", "value with spaces"))
                else:
                    self.assertIn("task", argv)
                    self.assertIn("smoke", argv)

    def test_unconfirmed_attempt_retains_diagnostics_without_an_automatic_retry(self):
        for action, detail in (
            (ord("n"), "Helper exited before a verified receipt; inspect before retry"),
            (
                ord("t"),
                "Receipt observation timed out; outcome unknown; inspect before retry",
            ),
        ):
            self.tracker = FakeTracker(self.store.root)
            keys = iter((action, -1, -1, ord("U"), -1, ord("q")))
            delivered = [False]

            def key():
                value = next(keys)
                if value == -1 and not delivered[0]:
                    delivered[0] = True
                    self.tracker.deliver(
                        self.tracker.starts[0][3], "unconfirmed", detail=detail
                    )
                return value

            with self.subTest(action=action):
                frames = self.dashboard(
                    key, dimensions=(40, 240), answers=self.answers(action)
                )
                self.assertIn("Starting", frame_text(frames[1]))
                self.assertIn("unconfirmed", frame_text(frames[2]).lower())
                text = frame_text(frames[-1])
                self.assertIn(detail, text)
                self.assertIn(str(self.tracker.statuses[0].receipt_path), text)
                self.assertIn(str(self.tracker.statuses[0].transcript_path), text)
                self.assertEqual(len(self.tracker.starts), 1)
                self.assertEqual(self.tracker.statuses[0].phase, "unconfirmed")

    def test_two_updates_remain_in_submissions_view_and_action_keys_do_not_resubmit(
        self,
    ):
        keys = iter(
            (
                ord("n"),
                ord("n"),
                -1,
                ord("U"),
                ord("p"),
                ord("c"),
                ord("n"),
                ord("t"),
                ord("q"),
            )
        )
        delivered = [False]

        def key():
            value = next(keys)
            if value == -1 and not delivered[0]:
                delivered[0] = True
                self.tracker.deliver(
                    self.tracker.starts[0][3],
                    "submitted",
                    "job",
                    "111111111111",
                    "First receipt verified",
                )
                self.tracker.deliver(
                    self.tracker.starts[1][3],
                    "submitted",
                    "plan",
                    "222222222222",
                    "Second receipt verified",
                )
            return value

        frames = self.dashboard(
            key,
            dimensions=(40, 240),
            answers=self.answers(ord("n"), "printf first")
            + self.answers(ord("n"), "printf second"),
        )
        for frame in frames[4:]:
            text = frame_text(frame)
            self.assertIn("111111111111", text)
            self.assertIn("222222222222", text)
            self.assertIn("First receipt verified", text)
            self.assertIn("Second receipt verified", text)
        self.assertEqual(len(self.tracker.starts), 2)
        self.assertEqual(len(self.fixture.prompt_calls), 8)

    def test_verified_receipt_with_later_helper_error_is_not_claimed_as_native_success(
        self,
    ):
        keys = iter((ord("n"), -1, ord("U"), ord("q")))
        detail = (
            "Receipt verified, but helper exited 2; native command outcome unconfirmed"
        )

        def key():
            value = next(keys)
            if value == -1:
                self.tracker.deliver(
                    self.tracker.starts[0][3],
                    "submitted",
                    "job",
                    "333333333333",
                    detail,
                )
            return value

        frames = self.dashboard(
            key, dimensions=(40, 240), answers=self.answers(ord("n"))
        )
        self.assertIn("Submitted job 333333333333", frame_text(frames[2]))
        text = frame_text(frames[-1])
        self.assertIn("333333333333", text)
        self.assertIn(detail, text)
        self.assertNotIn("Job succeeded", text)
        self.assertEqual(len(self.tracker.starts), 1)

    def test_cancelled_or_invalid_prompts_do_not_start_an_attempt(self):
        for action, answers in (
            (ord("n"), [None]),
            (ord("n"), [str(self.root), None]),
            (ord("n"), self.answers(ord("n"), "'unterminated")),
            (ord("t"), [None]),
            (ord("t"), [str(self.root), None]),
        ):
            self.tracker = FakeTracker(self.store.root)
            with self.subTest(action=action, answers=answers):
                self.dashboard([action, ord("q")], answers=answers)
                self.assertEqual(self.tracker.starts, [])
                self.assertEqual(self.tracker.statuses, ())

    def test_smallest_jobs_pane_keeps_job_identity_with_submission_notice(self):
        for phase, kind, handle, notice in (
            ("starting", None, None, "Starting | U"),
            ("unconfirmed", None, None, "Unconfirmed | U"),
            ("submitted", "job", "444444444444", "Job 444444444444 U"),
        ):
            self.tracker = FakeTracker(self.store.root)
            key = self.tracker.start(["run", "--", "true"], "private attempt", 100)
            self.tracker.deliver(key, phase, kind, handle)
            with self.subTest(phase=phase):
                frames = self.dashboard([9, ord("q")], dimensions=(8, 20))
                for frame in frames:
                    self.assertTrue(frame[5].startswith(self.fixture.jobs[0]))
                    self.assertEqual(frame[6], notice)
                    self.assertLessEqual(max(map(len, frame.values())), 19)

    def test_submission_view_pages_all_outcomes_and_wrapped_paths_at_twenty_by_eight(
        self,
    ):
        private_root = Path("/tmp/a-long-private-submission-state-directory")
        tracker = FakeTracker(private_root)
        first = tracker.start(["run", "--", "true"], "first command", 100)
        second = tracker.start(
            ["task", "run", "reviewed"], "\x1b[31mreviewed task\x1b[0m", 100
        )
        tracker.deliver(
            first, "submitted", "job", "555555555555", "First receipt verified"
        )
        tracker.deliver(
            second, "submitted", "plan", "666666666666", "Second receipt verified"
        )
        tracker.poll(100)
        report = tracker.statuses
        view = SubmissionView()
        with mock.patch.object(
            Path,
            "stat",
            side_effect=AssertionError("submission presentation touched disk"),
        ):
            initial = view.page(None, report, 19, 5)
            rendered = list(initial)
            while view.offset + view.height < len(view.wrapped):
                previous = view.offset
                view.scroll(1, pages=True)
                page = view.page(None, report, 19, 5)
                delta = view.offset - previous
                self.assertGreater(delta, 0)
                rendered.extend(page[-delta:])
            self.assertEqual(rendered, view.wrapped)
            self.assertTrue(all(len(line) <= 19 for line in rendered))
            text = "".join(rendered)
            self.assertNotIn("\x1b", text)
            self.assertLess(
                text.index("Created plan 666666666666"),
                text.index("Submitted job 555555555555"),
            )
            for status in report:
                self.assertIn(status.detail, text)
                self.assertIn(str(status.receipt_path), text)
                self.assertIn(str(status.transcript_path), text)
            view.home()
            self.assertEqual(view.page(None, report, 19, 5), initial)
            view.end()
            self.assertEqual(view.page(None, report, 19, 5), rendered[-5:])
            self.assertEqual(view.page(None, report, 63, 5), view.wrapped[-5:])
