"""System PSI remains bounded, honest, compatible and read-only."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_builds import diagnostics, host_pressure, metrics
from agent_builds.store import Store
from agent_builds.tui_inspect import HealthView


PSI = (
    "full avg10=61.97 avg60=30.65 avg300=21.95 total=153824194135\n"
    "some avg10=86.35 avg60=63.13 avg300=58.34 total=199079163494\n"
)


class HostPressureTests(unittest.TestCase):
    def test_rows_are_selected_by_name_and_cpu_full_is_undefined(self):
        io = host_pressure.parse_pressure(PSI, "io")
        self.assertEqual(io["full"]["avg10"], 61.97)
        self.assertEqual(io["some"]["avg300"], 58.34)
        self.assertEqual(io["some"]["total_us"], 199079163494)
        cpu = host_pressure.parse_pressure(PSI, "cpu")
        self.assertEqual(cpu["some"], io["some"])
        self.assertIsNone(cpu["full"])
        some = host_pressure.parse_pressure(PSI.splitlines()[1], "memory")
        self.assertIsNone(some["full"])
        self.assertEqual(some["some"], io["some"])

    def test_malformed_nonfinite_out_of_range_and_duplicate_rows_stay_unknown(self):
        for invalid in ("nan", "inf", "-1", "100.01", "oops"):
            with self.subTest(invalid=invalid):
                parsed = host_pressure.parse_pressure(
                    PSI.replace("86.35", invalid), "io"
                )
                self.assertIsNone(parsed["some"])
                self.assertIsNotNone(parsed["full"])
        for malformed in (
            "some avg10=1 avg60=2 avg300=3",
            "some avg10=1 avg60=2 avg300=3 total=-1",
            "some avg10=1 avg60=2 avg300=3 total=1.2",
            "some avg10=1 avg10=2 avg60=2 avg300=3 total=1",
            "some avg10=1 broken avg60=2 avg300=3 total=1",
            PSI + PSI.splitlines()[1] + "\n",
        ):
            with self.subTest(malformed=malformed):
                self.assertIsNone(host_pressure.parse_pressure(malformed, "io")["some"])
        zero = host_pressure.parse_pressure(
            "some avg10=0 avg60=0 avg300=0 total=0", "io"
        )
        self.assertEqual(zero["some"]["avg10"], 0)
        self.assertEqual(
            host_pressure.parse_pressure("", "io"), dict(some=None, full=None)
        )

    def test_reads_are_bounded_and_missing_or_oversized_files_remain_unknown(self):
        for data in (PSI, "x" * (host_pressure.PRESSURE_LIMIT + 1)):
            reader = mock.mock_open(read_data=data)
            with mock.patch.object(Path, "open", reader):
                sample = host_pressure.pressure_sample("io")
            reader().read.assert_called_once_with(host_pressure.PRESSURE_LIMIT + 1)
            self.assertEqual(sample["some"] is None, data != PSI)
        for error in (FileNotFoundError(), PermissionError(), UnicodeError()):
            with mock.patch.object(Path, "open", side_effect=error):
                self.assertEqual(
                    host_pressure.pressure_sample("io"), dict(some=None, full=None)
                )
        with self.assertRaises(ValueError):
            host_pressure.pressure_sample("../meminfo")

    def test_snapshot_reads_each_resource_once_and_keeps_legacy_averages(self):
        io = host_pressure.parse_pressure(PSI, "io")
        with mock.patch(
            "agent_builds.metrics.pressure_sample", return_value=io
        ) as sample:
            machine = metrics.snapshot("/tmp")
        self.assertEqual(
            sample.call_args_list, [mock.call(kind) for kind in ("cpu", "memory", "io")]
        )
        for kind in ("cpu", "memory", "io"):
            self.assertEqual(machine[kind + "_pressure"], 86.35)
            self.assertEqual(machine["psi"][kind], io)
        self.assertIn("I/O 86.3%/62.0%", host_pressure.pressure_summary(machine))

    def test_retained_legacy_snapshots_do_not_invent_full_or_long_term_readings(self):
        old = dict(cpu_pressure=0, memory_pressure=1, io_pressure=90)
        summary = host_pressure.pressure_summary(old)
        self.assertIn("I/O 90.0%/unknown", summary)
        self.assertIn("CPU 0.0%", summary)
        text = "\n".join(host_pressure.pressure_lines(old))
        self.assertIn("I/O some: 90.0% / unknown / unknown", text)
        self.assertIn("I/O full: unknown", text)
        self.assertNotIn("CPU full:", text)
        fresh = dict(old, psi={"io": dict(some=None, full=None)})
        self.assertIn("I/O unknown/unknown", host_pressure.pressure_summary(fresh))

    def test_diagnosis_and_narrow_health_keep_pressure_without_initializing_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "missing-state"
            store = Store(root, read_only=True)
            machine = dict(
                psi={
                    kind: host_pressure.parse_pressure(PSI, kind)
                    for kind in ("cpu", "memory", "io")
                }
            )
            try:
                with mock.patch(
                    "agent_builds.diagnostics.snapshot", return_value=machine
                ):
                    report = diagnostics.report(store)
            finally:
                store.close()
            self.assertFalse(root.exists())
            self.assertFalse(report["changes_applied"])
            text = "\n".join(diagnostics.lines(report))
            self.assertIn("I/O full: 62.0% / 30.6% / 21.9%", text)
            self.assertIn("not CPU utilization", text)
            for width in (19, 64, 99):
                view = HealthView()
                with mock.patch.object(
                    Path, "open", side_effect=AssertionError("render reread PSI")
                ):
                    view.page("global", report, width, 2)
                    wrapped = list(view.wrapped)
                    for unused in range(len(wrapped)):
                        page = view.page("global", report, width, 2)
                        self.assertTrue(all(len(line) <= width for line in page))
                        view.scroll(1)
                    view.page("global", report, width, 2)
                    self.assertEqual(view.wrapped, wrapped)
                self.assertIn("I/O full:", " ".join(wrapped))
