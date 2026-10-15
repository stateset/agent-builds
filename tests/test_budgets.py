import unittest

from agent_builds.budgets import worker_budget


class BudgetTests(unittest.TestCase):
    def test_queue_defaults_and_explicit_rust_test_threads_are_separate(self):
        argv = ['cargo', 'test', '--workspace', '--', '--test-threads=8']
        before = list(argv)
        result = worker_budget(argv, 3)
        self.assertEqual(result['environment'], {'CARGO_BUILD_JOBS': '3', 'RUST_TEST_THREADS': '3'})
        self.assertEqual(result['cargo_build']['configured_value'], 3)
        self.assertEqual(result['rust_tests']['configured_value'], 8)
        self.assertTrue(result['rust_tests']['exceeds_reservation'])
        self.assertFalse(result['runtime_concurrency_measured'])
        self.assertEqual(argv, before)

    def test_cargo_jobs_forms_and_harness_boundary(self):
        for flags in [['-j4'], ['-j', '4'], ['--jobs=4'], ['--jobs', '4'], ['-j=4']]:
            result = worker_budget(['cargo', '+stable', 'test'] + flags + ['--', '--test-threads', '2', '--jobs=99'], 3)
            self.assertEqual(result['cargo_build']['configured_value'], 4)
            self.assertEqual(result['rust_tests']['configured_value'], 2)

    def test_shell_payload_is_not_treated_as_observed_cargo_flags(self):
        result = worker_budget(['bash', '-lc', 'cargo test -j8 -- --test-threads=16'], 3)
        self.assertFalse(result['direct_cargo_command'])
        self.assertEqual(result['cargo_build']['source'], 'queue_environment')
        self.assertEqual(result['rust_tests']['source'], 'queue_environment')

    def test_non_numeric_worker_option_is_retained_without_inventing_a_count(self):
        result = worker_budget(['cargo', 'build', '--jobs=default'], 3)
        self.assertEqual(result['cargo_build']['configured_value'], 'default')
        self.assertFalse(result['cargo_build']['exceeds_reservation'])

    def test_literal_harness_arguments_after_separator_are_not_flags(self):
        result = worker_budget(['cargo', 'test', '--', '--', '--test-threads=99'], 3)
        self.assertEqual(result['rust_tests']['configured_value'], 3)
