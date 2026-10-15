import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_builds import waits
from agent_builds import __version__
from agent_builds.store import Store
from agent_builds.telemetry import statistics
from agent_builds.tui import compact_lines

ROOT = str(Path(__file__).resolve().parent.parent)


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state')
        self.store.update_config(dict(cpu_slots=3, quick_reserve_slots=0, min_free_gb=0,
                                      min_available_gb=0, paused=True, nice=0))
        self.env = dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root), PYTHONPATH=ROOT)
        self.env.pop('AGENT_BUILDS_JOB_ID', None)
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=8)
        self.store.close()
        self.temp.cleanup()

    def job(self, key='repo', slots=1):
        return self.store.submit(self.root, ['true'], 'local', 'quick', slots, 1, key, '', 10)

    def at(self, stamp, function, *args):
        with mock.patch('time.time', return_value=stamp):
            return function(*args)

    def test_pause_repository_and_capacity_waits_survive_admission_and_completion(self):
        first = self.at(99, self.job)
        second = self.at(100, self.job)
        self.assertFalse(self.at(101, self.store.claim, second))
        self.assertFalse(self.at(103, self.store.claim, second))
        self.store.update_config({'paused': False})
        self.assertFalse(self.at(104, self.store.claim, second))
        self.assertTrue(self.at(104, self.store.claim, first))
        self.assertFalse(self.at(106, self.store.claim, second))
        self.at(107, self.store.finish, first, 'succeeded', 0)
        self.assertTrue(self.at(108, self.store.claim, second))
        self.at(115, self.store.finish, second, 'succeeded', 0)
        row = self.store.get(second)
        report = row['wait_breakdown']
        self.assertEqual(report['total_seconds'], 8)
        self.assertEqual(report['seconds'], {'unobserved': 1, 'paused': 3, 'repository_lock': 4})
        self.assertEqual(report['observed_seconds'], 7)
        self.assertEqual(report['transition_count'], 2)
        self.assertEqual(report['transitions'][-1]['blocking_job_id'], first)
        self.assertEqual(row['exit_code'], 0)

    def test_capacity_and_priority_are_distinct_from_repository_locks(self):
        self.store.update_config({'paused': False})
        first = self.job('a', 3)
        self.assertTrue(self.store.claim(first))
        second = self.job('b')
        self.assertFalse(self.store.claim(second))
        self.assertEqual(self.store.get(second)['wait_breakdown']['current_category'], 'cpu_capacity')
        third = self.job('c')
        self.store.db.execute('UPDATE jobs SET created=? WHERE id=?', (time.time()-121, second))
        self.assertFalse(self.store.claim(third))
        self.assertEqual(self.store.get(third)['wait_breakdown']['current_category'], 'priority')
        self.assertEqual(self.store.get(third)['wait_breakdown']['current_blocker'], second)

    def test_gap_and_orphan_recovery_do_not_invent_continuous_observation(self):
        job = self.at(100, self.job)
        self.assertFalse(self.at(101, self.store.claim, job))
        self.assertFalse(self.at(103, self.store.claim, job))
        self.store.db.execute('UPDATE jobs SET owner_pid=0,owner_start=NULL WHERE id=?', (job,))
        self.at(200, self.store.recover)
        row = self.store.get(job)
        self.assertEqual(row['state'], 'lost')
        self.assertEqual(row['wait_breakdown']['seconds'], {'unobserved': 98, 'paused': 2})
        self.assertEqual(sum(row['wait_breakdown']['seconds'].values()), row['wait_seconds'])

    def test_queued_cancellation_preserves_observed_wait_and_exit_code(self):
        job = self.at(100, self.job)
        self.at(101, self.store.claim, job)
        self.at(103, self.store.cancel, job)
        self.at(104, self.store.finish, job, 'cancelled', 130, 'cancelled while queued')
        row = self.store.get(job)
        self.assertEqual(row['wait_breakdown']['seconds'], {'unobserved': 1, 'paused': 3})
        self.assertEqual(row['exit_code'], 130)

    def test_read_only_projection_is_bounded_and_does_not_write(self):
        job = self.at(100, self.job)
        self.at(101, self.store.claim, job)
        before = self.store.db.execute('SELECT wait_history FROM jobs WHERE id=?', (job,)).fetchone()[0]
        reader = Store(self.store.root, read_only=True)
        try:
            recent = self.at(103, reader.get, job)['wait_breakdown']
            stale = self.at(120, reader.get, job)['wait_breakdown']
            self.assertEqual(recent['seconds']['paused'], 2)
            self.assertEqual(stale['observed_seconds'], 0)
            self.assertEqual(stale['seconds']['unobserved'], 20)
            self.assertEqual(reader.db.execute('SELECT wait_history FROM jobs WHERE id=?', (job,)).fetchone()[0], before)
        finally:
            reader.close()

    def test_legacy_history_is_unknown_without_migration_on_inspection(self):
        job = self.at(100, self.job)
        self.store.db.execute("UPDATE jobs SET wait_history='{}',state='succeeded',started=110,finished=120 WHERE id=?", (job,))
        # Simulate the complete pre-v0.8 jobs schema, not just empty history.
        self.store.db.execute('ALTER TABLE jobs RENAME TO legacy_jobs')
        columns = [r[1] for r in self.store.db.execute('PRAGMA table_info(legacy_jobs)') if r[1] != 'wait_history']
        names = ','.join(columns)
        self.store.db.execute('CREATE TABLE jobs AS SELECT '+names+' FROM legacy_jobs')
        reader = Store(self.store.root, read_only=True)
        try:
            report = reader.get(job)['wait_breakdown']
            self.assertFalse(report['recorded'])
            self.assertEqual(report['seconds'], {'unobserved': 10})
            self.assertNotIn('wait_history', [r[1] for r in reader.db.execute('PRAGMA table_info(jobs)')])
        finally:
            reader.close()

    def test_transition_history_is_bounded_but_totals_are_not_truncated(self):
        history = waits.begin(100)
        for index in range(200):
            history = waits.advance(history, 101+index, 'repository_lock', str(index))
        report = waits.describe(history, 100, 301)
        self.assertEqual(len(report['transitions']), 64)
        self.assertEqual(report['transition_count'], 200)
        self.assertEqual(report['seconds']['repository_lock'], 200)
        self.assertEqual(report['total_seconds'], 201)
        self.assertLess(len(json.dumps(history)), 8000)

    def test_clock_regression_never_creates_negative_or_overcounted_wait(self):
        history = waits.advance(waits.begin(100), 101, 'paused')
        history = waits.advance(history, 99, 'repository_lock', 'job')
        report = waits.describe(history, 100, 98)
        self.assertEqual(report['total_seconds'], 0)
        self.assertEqual(report['observed_seconds'], 0)
        self.assertTrue(all(value >= 0 for value in report['seconds'].values()))

    def test_heartbeat_coalesces_measurements_but_not_blocker_changes(self):
        updates = waits.WaitUpdates()
        self.assertTrue(updates.due('disk below 10.0 GiB requirement', {}, 0))
        for second in range(1, 60):
            self.assertFalse(updates.due('disk below 11.0 GiB requirement', {'disk_free_gb': second}, second))
        self.assertTrue(updates.due('disk below 11.0 GiB requirement', {}, 60))
        self.assertTrue(updates.due('aged job waiting for capacity [one]', {'blocking_job': {'id': 'one'}}, 61))
        self.assertTrue(updates.due('aged job waiting for capacity [two]', {'blocking_job': {'id': 'two'}}, 62))
        self.assertTrue(updates.due('CPU slot budget', {}, 63))

    def test_cli_records_paused_wait_then_finishes_with_native_exit(self):
        child = subprocess.Popen([sys.executable, '-m', 'agent_builds', 'run', '--events-json', '--jobs', '1',
            '--cwd', str(self.root), '--', sys.executable, '-c', 'raise SystemExit(7)'], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(child)
        deadline = time.monotonic()+6
        while time.monotonic()<deadline:
            rows = self.store.rows()
            if rows and rows[0]['wait_breakdown'].get('current_category') == 'paused':
                break
            time.sleep(.02)
        else:
            self.fail('runner did not observe paused queue')
        time.sleep(.35)
        self.store.update_config({'paused': False})
        _, stderr = child.communicate(timeout=8)
        self.assertEqual(child.returncode, 7, stderr)
        row = self.store.rows()[0]
        self.assertGreater(row['wait_breakdown']['seconds']['paused'], .2)
        self.assertEqual(row['exit_code'], 7)
        events = [json.loads(line) for line in stderr.splitlines()]
        waiting = [event for event in events if event['state']=='waiting']
        self.assertEqual(len(waiting), 1)
        self.assertIn('wait_breakdown', waiting[0])
        result = subprocess.run([sys.executable, '-m', 'agent_builds', 'show', row['id']], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=6)
        self.assertIn(b'Wait breakdown:', result.stdout)

    def test_statistics_separates_legacy_wait_and_honors_repo_filter(self):
        first = self.at(100, self.job, 'a')
        self.at(101, self.store.claim, first)
        self.at(104, self.store.finish, first, 'cancelled', 130)
        second = self.at(100, self.job, 'b')
        self.store.db.execute("UPDATE jobs SET wait_history='{}',state='succeeded',started=110,finished=111 WHERE id=?", (second,))
        report = statistics(self.store)['wait_breakdown']
        self.assertEqual(report['seconds'], {'unobserved': 11, 'paused': 3})
        self.assertEqual(report['recorded_jobs'], 1)
        self.assertEqual(report['jobs'], 2)
        self.assertEqual(report['coverage'], 3/14)
        filtered = statistics(self.store, repo=self.root / 'absent')['wait_breakdown']
        self.assertEqual(filtered['jobs'], 0)

    def test_compact_view_keeps_handle_status_and_navigation_in_narrow_pane(self):
        job = self.job()
        row = self.store.get(job)
        lines = compact_lines([row], 0, [], 0, 0, self.store.config(), 40, 21)
        self.assertIn(job, lines)
        self.assertIn('queued', lines)
        self.assertIn(__version__, lines[0])
        self.assertEqual(lines[-1], 'j/k move v views q')
        self.assertLessEqual(len(lines), 40)
        plan = dict(id='plan-handle', state='running', name='Check', stages=[dict(name='test', state='queued')], reason='')
        lines = compact_lines([], 0, [plan], 0, 7, self.store.config(), 20, 21)
        self.assertIn('test queued', lines)


if __name__ == '__main__':
    unittest.main()
