"""Layout storage never touches queue state or saves action context."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_builds.tui_preferences import Layout, load, path, save
import test_tui_browser as fixtures


class PreferenceFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        patch = mock.patch.dict(os.environ, XDG_CONFIG_HOME=str(self.base / "config"))
        patch.start()
        self.addCleanup(patch.stop)

    def test_missing_read_creates_no_directories(self):
        self.assertEqual(load(), (Layout(), ""))
        self.assertFalse(path().parent.exists())

    def test_round_trip_contains_only_version_and_two_display_flags(self):
        save(Layout(True, True))
        self.assertEqual(load(), (Layout(True, True), ""))
        self.assertEqual(
            json.loads(path().read_text()), dict(version=1, focused=True, table=True)
        )
        self.assertEqual(path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(path().parent.iterdir()), [path()])

    def test_invalid_oversized_and_future_data_keep_defaults(self):
        path().parent.mkdir(parents=True)
        for value in (
            "{",
            "[]",
            "x" * 4097,
            "[" * 1500 + "]" * 1500,
            '{"version":true}',
            '{"version":2}',
            '{"version":1,"focused":1}',
            '{"version":1,"table":"false"}',
        ):
            path().write_text(value)
            layout, warning = load()
            self.assertEqual(layout, Layout())
            self.assertIn("using defaults", warning)
            self.assertEqual(path().read_text(), value)

    def test_nonregular_file_is_rejected_without_reading_or_blocking(self):
        path().parent.mkdir(parents=True)
        os.mkfifo(str(path()))
        with mock.patch(
            "agent_builds.tui_preferences.os.fdopen",
            side_effect=AssertionError("Read a FIFO"),
        ):
            layout, warning = load()
        self.assertEqual(layout, Layout())
        self.assertTrue(warning)

    def test_failed_replace_preserves_previous_preferences_and_cleans_temporary(self):
        save(Layout(True, False))
        before = path().read_bytes()
        with mock.patch(
            "agent_builds.tui_preferences.os.replace", side_effect=OSError("read only")
        ):
            with self.assertRaises(OSError):
                save(Layout(False, True))
        self.assertEqual(path().read_bytes(), before)
        self.assertEqual(list(path().parent.iterdir()), [path()])

    def test_relative_xdg_directory_does_not_write_into_checkout(self):
        with mock.patch.dict(os.environ, XDG_CONFIG_HOME="relative"), mock.patch.object(
            Path, "home", return_value=self.base
        ):
            self.assertEqual(path(), self.base / ".config/buildr/tui.json")


class PreferenceDashboardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        patch = mock.patch.dict(
            os.environ, XDG_CONFIG_HOME=str(self.fixture.root / "user-config")
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_toggles_are_session_only_until_explicit_save_then_restore(self):
        self.fixture.dashboard([ord("z"), 9, ord("q")])
        self.assertFalse(path().exists())
        self.fixture.dashboard([ord("z"), 9, ord("Z"), ord("q")])
        self.assertEqual(load()[0], Layout(True, True))
        frames = self.fixture.dashboard([ord("q")])
        self.assertIn("[focus:", frames[-1][0])
        self.assertTrue(
            any("JOB" in text and "COMMAND" in text for text in frames[-1].values())
        )

    def test_save_failure_does_not_undo_session_layout_or_open_writer(self):
        with mock.patch(
            "agent_builds.tui.save_layout", side_effect=OSError("read only")
        ):
            frames = self.fixture.dashboard([ord("z"), ord("Z"), ord("q")])
        self.assertIn("[focus:", frames[-1][0])
        self.assertTrue(any("could not save" in text for text in frames[-1].values()))
        self.assertFalse(path().exists())

    def test_minimum_pane_does_not_write_preferences(self):
        self.fixture.dashboard([ord("Z"), ord("q")], (8, 20))
        self.assertFalse(path().exists())
