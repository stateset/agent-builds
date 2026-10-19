"""Pressure context stays bounded and separates missing data from low pressure."""

import unittest

from agent_builds.study_pressure import StudyPressure


def machine(value, available=8):
    return dict(
        available_gb=available,
        psi={
            kind: {
                scope: dict(avg10=value, avg60=value / 2, avg300=value / 4)
                for scope in ("some", "full")
            }
            for kind in ("cpu", "memory", "io")
        },
    )


class StudyPressureTests(unittest.TestCase):
    def test_empty_report_does_not_invent_measurements_or_coverage(self):
        report = StudyPressure().report()
        self.assertEqual(report["samples"], 0)
        self.assertEqual(report["retained"], [])
        for field in (
            "min_available_gb",
            "last_observation",
            "observation_span_seconds",
            "max_sample_gap_seconds",
        ):
            self.assertIsNone(report[field])
        self.assertIsNone(report["max_avg10"]["io_pressure"])
        self.assertEqual(report["valid_readings"]["io"]["full"]["avg10"], 0)

    def test_full_and_long_term_maxima_count_all_observations(self):
        collector = StudyPressure()
        collector.observe(machine(20, 8), at=100, clock=0)
        collector.observe(machine(40, 4), at=101, clock=0.5)
        collector.observe(machine(10, 6), at=102, clock=1)
        report = collector.report()
        self.assertEqual(report["max_avg10"]["io_pressure"], 40)
        self.assertEqual(
            report["max_psi_percent"]["memory"]["full"],
            dict(avg10=40, avg60=20, avg300=10),
        )
        self.assertEqual(report["min_available_gb"], 4)
        self.assertEqual(report["valid_available_memory_readings"], 3)
        self.assertEqual(report["valid_readings"]["io"]["full"]["avg60"], 3)
        self.assertEqual(report["max_sample_gap_seconds"], 0.5)
        self.assertEqual(report["long_sample_gaps"], 0)
        self.assertIsNone(report["max_psi_percent"]["cpu"]["full"]["avg10"])
        self.assertIsNone(report["last_observation"]["psi"]["cpu"]["full"])
        self.assertEqual(report["valid_readings"]["cpu"]["full"]["avg10"], 0)

    def test_missing_nonfinite_invalid_and_legacy_values_keep_explicit_denominators(
        self,
    ):
        collector = StudyPressure()
        collector.observe({}, at=0, clock=0)
        for index, value in enumerate(
            (None, True, -1, float("nan"), float("inf"), 101)
        ):
            collector.observe(
                dict(
                    cpu_pressure=value,
                    memory_pressure=value,
                    io_pressure=value,
                    available_gb=value,
                ),
                at=index + 1,
                clock=index + 1,
            )
        collector.observe(
            dict(cpu_pressure=0, memory_pressure=1, io_pressure=2, available_gb=0),
            at=7,
            clock=7,
        )
        report = collector.report()
        self.assertEqual(report["samples"], 8)
        self.assertEqual(report["max_avg10"]["cpu_pressure"], 0)
        self.assertEqual(report["max_avg10"]["io_pressure"], 2)
        self.assertEqual(report["min_available_gb"], 0)
        # 101 GiB RAM is valid even though 101% PSI is invalid.
        self.assertEqual(report["valid_available_memory_readings"], 2)
        self.assertEqual(report["valid_readings"]["io"]["some"]["avg10"], 1)
        self.assertEqual(report["unavailable_readings"]["io"]["some"]["avg10"], 7)
        self.assertEqual(report["unavailable_readings"]["io"]["some"]["avg60"], 8)
        self.assertIsNone(report["max_psi_percent"]["io"]["full"]["avg10"])

    def test_long_run_keeps_first_samples_latest_observation_and_late_extrema(self):
        collector = StudyPressure()
        for index in range(1201):
            collector.observe(
                machine(90 if index == 1000 else 1, 2 if index == 1100 else 8),
                at=1000 + index,
                clock=index * 0.5,
            )
        report = collector.report()
        self.assertEqual(report["samples"], 1201)
        self.assertEqual(report["retained_samples"], 300)
        self.assertEqual(len(report["retained"]), 300)
        self.assertEqual(report["omitted_samples"], 901)
        self.assertEqual(report["retained"][0]["at"], 1000)
        self.assertEqual(report["retained"][-1]["at"], 1299)
        self.assertEqual(report["last_observation"]["at"], 2200)
        self.assertEqual(report["max_psi_percent"]["io"]["full"]["avg10"], 90)
        self.assertEqual(report["min_available_gb"], 2)
        self.assertEqual(report["valid_readings"]["io"]["full"]["avg300"], 1201)
        self.assertEqual(report["observation_span_seconds"], 600)

    def test_wall_clock_changes_do_not_hide_monotonic_observation_gaps(self):
        collector = StudyPressure()
        for at, clock in ((100, 0), (90, 0.5), (80, 1.5), (70, 4)):
            collector.observe({}, at=at, clock=clock)
        report = collector.report()
        self.assertEqual(report["first_observation_at"], 100)
        self.assertEqual(report["last_observation_at"], 70)
        self.assertEqual(report["observation_span_seconds"], 4)
        self.assertEqual(report["max_sample_gap_seconds"], 2.5)
        self.assertEqual(report["long_sample_gaps"], 1)
        self.assertEqual(report["gap_threshold_seconds"], 1)
        self.assertEqual(report["unavailable_readings"]["memory"]["full"]["avg10"], 4)

    def test_totals_remain_raw_and_invalid_values_do_not_become_healthy_zero(self):
        collector = StudyPressure()
        measured = machine(0, available=10**1000)
        measured["psi"]["io"]["some"]["total_us"] = 123456
        measured["psi"]["io"]["full"]["total_us"] = -1
        measured["psi"]["memory"]["full"]["total_us"] = True
        measured["psi"]["cpu"]["full"]["total_us"] = 0
        measured["psi"]["memory"]["some"]["avg10"] = 10**1000
        collector.observe(measured, at=1, clock=1)
        report = collector.report()
        raw = report["last_observation"]["psi"]
        self.assertEqual(raw["io"]["some"]["total_us"], 123456)
        self.assertIsNone(raw["io"]["full"]["total_us"])
        self.assertIsNone(raw["memory"]["full"]["total_us"])
        self.assertIsNone(raw["memory"]["some"]["avg10"])
        self.assertIsNone(raw["cpu"]["full"])
        self.assertIsNone(report["min_available_gb"])
        self.assertEqual(report["max_psi_percent"]["io"]["full"]["avg10"], 0)

    def test_reports_and_raw_observations_do_not_change_after_later_samples(self):
        collector = StudyPressure()
        measured = machine(10)
        collector.observe(measured, at=1, clock=1)
        first = collector.report()
        measured["psi"]["io"]["some"]["avg10"] = 99
        collector.observe(machine(90), at=2, clock=2)
        self.assertEqual(first["samples"], 1)
        self.assertEqual(first["max_psi_percent"]["io"]["some"]["avg10"], 10)
        self.assertEqual(first["valid_readings"]["io"]["some"]["avg10"], 1)
        self.assertEqual(len(first["retained"]), 1)
        self.assertEqual(first["retained"][0]["psi"]["io"]["some"]["avg10"], 10)
        first["retained"].clear()
        self.assertEqual(collector.report()["retained_samples"], 2)
