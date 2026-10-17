"""Explicit linked-log lookups never retry, overlap, or steal later focus."""

from concurrent.futures import Future
import unittest

from agent_builds.tui_links import LogLinks


def row(handle="target"):
    return dict(id=handle, state="failed", cwd="/checkout", log_path="/logs/" + handle)


class ControlledReads:
    def __init__(self):
        self.calls = []

    def __call__(self, loader, key):
        if any(not future.done() for unused, unused_key, future in self.calls):
            raise AssertionError("overlapping linked-job reads")
        future = Future()
        self.calls.append((loader, key, future))
        return future


class LogLinkTests(unittest.TestCase):
    def setUp(self):
        self.submit = ControlledReads()
        self.loaded = []

        def loader(key):
            self.loaded.append(key)
            return row(key[1])

        self.links = LogLinks(loader, submit=self.submit)
        self.origin = (7, "plan")

    def finish(self, index=-1, value=None, error=None):
        loader, key, future = self.submit.calls[index]
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(loader(key) if value is None else value)

    def test_explicit_request_returns_one_row_and_never_refreshes_it(self):
        self.links.open("browser", "target", self.origin, 0)
        self.assertEqual(self.links.handle, "target")
        self.assertTrue(self.links.status.startswith("Opening log target"))
        self.assertIsNone(self.links.poll(self.origin, True, 1))
        self.assertEqual(len(self.submit.calls), 1)
        self.finish()
        self.assertEqual(self.links.poll(self.origin, True, 2), row())
        self.assertEqual(self.loaded, [("browser", "target")])
        self.assertEqual(self.submit.calls[0][1], (1, "browser", "target"))
        self.assertEqual(self.links.status, "")
        self.assertIsNone(self.links.error)
        self.assertIsNone(self.links.poll(self.origin, True, 1000))
        self.assertEqual(len(self.submit.calls), 1)

    def test_blocker_lookup_accepts_its_resolved_target_id(self):
        self.links.open("blocker", "source", (0, "source"), 0)
        self.finish(value=row("resolved-blocker"))
        self.assertEqual(
            self.links.poll((0, "source"), True, 1), row("resolved-blocker")
        )

    def test_error_is_retained_without_retry_until_an_explicit_request(self):
        self.links.open("browser", "target", self.origin, 0)
        self.finish(error=OSError("fixture unavailable"))
        self.assertIsNone(self.links.poll(self.origin, True, 1))
        self.assertEqual(self.links.error, "fixture unavailable")
        self.assertEqual(self.links.status, "Log unavailable: fixture unavailable")
        for now in (2, 100, 10000):
            self.assertIsNone(self.links.poll(self.origin, True, now))
            self.assertEqual(len(self.submit.calls), 1)
            self.assertEqual(self.links.status, "Log unavailable: fixture unavailable")
        self.links.open("browser", "target", self.origin, 10001)
        self.assertEqual(self.submit.calls[-1][1], (2, "browser", "target"))
        self.assertIsNone(self.links.error)
        self.finish()
        self.assertEqual(self.links.poll(self.origin, True, 10002), row())

    def test_startup_failure_is_visible_and_never_retried_implicitly(self):
        calls = []

        def unavailable(loader, key):
            calls.append(key)
            raise RuntimeError("fixture thread unavailable")

        links = LogLinks(lambda key: row(), submit=unavailable)
        links.open("browser", "target", self.origin, 0)
        self.assertIsNone(links.poll(self.origin, True, 1))
        self.assertEqual(links.status, "Log unavailable: fixture thread unavailable")
        self.assertIsNone(links.poll(self.origin, True, 10000))
        self.assertEqual(len(calls), 1)

    def test_hidden_or_help_overlay_defers_handoff_without_resubmission(self):
        self.links.open("browser", "target", self.origin, 0)
        self.finish()
        self.assertIsNone(self.links.poll(self.origin, False, 1))
        self.assertIsNone(self.links.poll(self.origin, False, 100))
        self.assertTrue(self.links.status.startswith("Opening log target"))
        self.assertEqual(len(self.submit.calls), 1)
        self.assertEqual(self.links.poll(self.origin, True, 101), row())

    def test_changed_origin_discards_finished_and_pending_results(self):
        for finished in (False, True):
            with self.subTest(finished=finished):
                self.links.open("browser", "target", self.origin, 0)
                if finished:
                    self.finish()
                self.assertIsNone(self.links.poll((7, "other-plan"), True, 1))
                self.assertEqual(self.links.status, "")
                if not finished:
                    self.finish()
                self.assertIsNone(self.links.poll(self.origin, True, 2))
                self.assertEqual(self.links.status, "")

    def test_cancel_does_not_cancel_future_wait_or_allow_overlapping_worker(self):
        self.links.open("browser", "first", self.origin, 0)
        old = self.submit.calls[0][2]
        self.links.cancel()
        self.assertFalse(old.cancelled())
        self.assertFalse(old.done())
        self.assertEqual(self.links.status, "")
        self.links.open("browser", "second", self.origin, 1)
        self.assertEqual(len(self.submit.calls), 1)
        self.finish(value=row("first"))
        self.assertIsNone(self.links.poll(self.origin, True, 2))
        self.assertEqual(len(self.submit.calls), 2)
        self.assertEqual(self.submit.calls[-1][1], (2, "browser", "second"))
        self.finish(value=row("second"))
        self.assertEqual(self.links.poll(self.origin, True, 3), row("second"))

    def test_aba_requests_discard_obsolete_data_and_errors(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                submit = ControlledReads()
                links = LogLinks(lambda key: row(key[1]), submit=submit)
                links.open("browser", "A", (7, "first"), 0)
                links.open("browser", "B", (7, "second"), 1)
                links.open("browser", "A", (7, "first"), 2)
                self.assertEqual(len(submit.calls), 1)
                old = submit.calls[0][2]
                if failed:
                    old.set_exception(OSError("obsolete A error"))
                else:
                    old.set_result(row("A"))
                self.assertIsNone(links.poll((7, "first"), True, 3))
                self.assertEqual(len(submit.calls), 2)
                self.assertEqual(submit.calls[-1][1], (3, "browser", "A"))
                self.assertIsNone(links.error)
                submit.calls[-1][2].set_result(row("A"))
                self.assertEqual(links.poll((7, "first"), True, 4), row("A"))

    def test_repeated_same_target_has_a_new_intent_serial(self):
        self.links.open("browser", "target", self.origin, 0)
        self.links.open("browser", "target", self.origin, 1)
        self.finish()
        self.assertIsNone(self.links.poll(self.origin, True, 2))
        self.assertEqual(self.submit.calls[-1][1], (2, "browser", "target"))
        self.finish()
        self.assertEqual(self.links.poll(self.origin, True, 3), row())

    def test_malformed_and_foreign_replies_are_visible_errors(self):
        invalid = [None, [], "target", {}, row("wrong")]
        for field in ("id", "state", "cwd", "log_path"):
            invalid.extend((dict(row(), **{field: None}), dict(row(), **{field: ""})))
        for reply in invalid:
            with self.subTest(reply=reply):
                self.links.open("browser", "target", self.origin, 0)
                self.submit.calls[-1][2].set_result(reply)
                self.assertIsNone(self.links.poll(self.origin, True, 1))
                self.assertEqual(
                    self.links.status, "Log unavailable: invalid linked-job reply"
                )

    def test_cancel_clears_error_notice_and_rejects_invalid_requests(self):
        self.links.open("browser", "target", self.origin, 0)
        self.finish(error=ValueError("unknown target"))
        self.links.poll(self.origin, True, 1)
        self.links.cancel()
        self.assertEqual(self.links.status, "")
        self.assertIsNone(self.links.error)
        self.assertIsNone(self.links.handle)
        for kind, handle in (("unknown", "target"), ("browser", ""), ("browser", None)):
            with self.subTest(kind=kind, handle=handle):
                with self.assertRaises(ValueError):
                    self.links.open(kind, handle, self.origin, 2)
        self.assertEqual(len(self.submit.calls), 1)


if __name__ == "__main__":
    unittest.main()
