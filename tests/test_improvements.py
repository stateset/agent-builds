import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_builds.disk import clean_logs, inventory
from agent_builds.profiles import load, save_worker
from agent_builds.remote import PROTOCOL, journal, reconcile, rpc, serve, submit
from agent_builds.store import Store, identity
from agent_builds.telemetry import JobSampler, statistics

ROOT = str(Path(__file__).resolve().parent.parent)


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state')
        self.store.update_config(dict(cpu_slots=6, min_free_gb=0, min_available_gb=0,
                                      quick_reserve_slots=2, aging_seconds=120, nice=0))
        self.env = dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root), PYTHONPATH=ROOT)
        self.env.pop('AGENT_BUILDS_JOB_ID', None)
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=8)
        # Remote submission fixtures have short-lived detached runners.
        deadline = time.monotonic() + 8
        remote_path = self.root / 'remote'
        if remote_path.exists():
            reader = Store(remote_path, read_only=True)
            try:
                while reader.rows(active=True) and time.monotonic() < deadline:
                    time.sleep(.03)
            finally:
                reader.close()
        self.store.close()
        self.temp.cleanup()

    def job(self, key, lane='heavy', slots=3, memory=1, scratch=0):
        return self.store.submit(self.root, ['true'], 'local', lane, slots, memory, key, '', 5, scratch_gb=scratch)

    def age(self, job, seconds=121):
        self.store.db.execute('UPDATE jobs SET created=? WHERE id=?', (time.time() - seconds, job))

    def cli(self, *args, timeout=12):
        return subprocess.run([sys.executable, '-m', 'agent_builds'] + list(args), env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)

    def test_quick_check_can_start_while_second_heavy_waits(self):
        first = self.job('a')
        self.assertTrue(self.store.claim(first))
        heavy = self.job('b')
        self.assertFalse(self.store.claim(heavy))
        self.assertIn('reserved for quick', self.store.get(heavy)['reason'])
        quick = self.job('c', 'quick', 2)
        self.assertTrue(self.store.claim(quick))
        self.assertFalse(self.store.claim(heavy))

    def test_aged_heavy_drains_capacity_and_cannot_be_starved_by_quick_work(self):
        first = self.job('a')
        self.assertTrue(self.store.claim(first))
        quick_running = self.job('b', 'quick', 2)
        self.assertTrue(self.store.claim(quick_running))
        heavy = self.job('c')
        self.age(heavy)
        quick = self.job('d', 'quick', 1)
        self.assertFalse(self.store.claim(quick))
        self.assertIn('aged job', self.store.get(quick)['reason'])
        self.assertFalse(self.store.claim(heavy))
        self.store.finish(quick_running, 'succeeded', 0)
        self.assertTrue(self.store.claim(heavy))

    def test_same_repo_fifo_survives_quick_priority(self):
        heavy = self.job('same', slots=2)
        quick = self.job('same', 'quick', 1)
        self.assertFalse(self.store.claim(quick))
        self.assertEqual(self.store.get(quick)['wait_context']['blocking_job']['id'], heavy)
        self.assertTrue(self.store.claim(heavy))

    def test_disk_blocked_aged_job_does_not_freeze_other_repositories(self):
        impossible = self.job('large', scratch=10**9)
        self.age(impossible)
        quick = self.job('small', 'quick', 1)
        self.assertTrue(self.store.claim(quick))
        self.assertFalse(self.store.claim(impossible))
        self.assertIn('disk', self.store.get(impossible)['reason'])

    def test_reduced_budget_does_not_make_old_oversized_job_block_everything(self):
        large = self.job('large', slots=6)
        self.age(large)
        self.store.update_config(dict(cpu_slots=3, quick_reserve_slots=1))
        quick = self.job('small', 'quick', 1)
        self.assertTrue(self.store.claim(quick))
        self.assertFalse(self.store.claim(large))

    def test_old_running_reservations_are_preserved_and_disable_reserve_can_restore_throughput(self):
        first = self.job('a')
        self.assertTrue(self.store.claim(first))
        second = self.job('b')
        self.store.update_config(dict(quick_reserve_slots=0))
        self.assertTrue(self.store.claim(second))
        self.store.update_config(dict(quick_reserve_slots=2))
        self.assertEqual(self.store.counts()['running'], 2)
        self.assertEqual(self.store.get(first)['slots'], 3)

    def test_quick_job_count_and_memory_capacity_are_reserved(self):
        self.store.update_config(dict(quick_reserve_slots=0, max_running=2, max_heavy=2,
                                      memory_gb=4, quick_reserve_memory_gb=1))
        first = self.job('a', slots=1)
        self.assertTrue(self.store.claim(first))
        second = self.job('b', slots=1)
        self.assertFalse(self.store.claim(second))
        self.assertIn('job capacity reserved', self.store.get(second)['reason'])
        quick = self.job('c', 'quick', 1)
        self.assertTrue(self.store.claim(quick))
        self.store.finish(first, 'succeeded', 0)
        self.store.finish(quick, 'succeeded', 0)
        self.store.update_config(dict(quick_reserve_jobs=0))
        self.store.db.execute('UPDATE jobs SET memory_gb=4 WHERE id=?', (second,))
        self.assertFalse(self.store.claim(second))
        self.assertIn('memory capacity reserved', self.store.get(second)['reason'])
        self.age(second)
        self.assertTrue(self.store.claim(second))

    def test_wait_reconnects_read_only_and_preserves_failure_exit_and_log_tail(self):
        from agent_builds.completion import wait
        first = self.job('first')
        second = self.job('second')
        self.store.finish(first, 'succeeded', 0)
        self.store.finish(second, 'failed', 7)
        self.store.log_path(second).write_text('earlier\nfinal line\n')
        with mock.patch('os.chmod', side_effect=AssertionError('read-only')):
            reader = Store(self.store.root, read_only=True)
            try:
                result = wait(reader, [first, second], tail_lines=1)
            finally:
                reader.close()
        self.assertEqual(result['exit_code'], 7)
        self.assertTrue(result['complete'])
        self.assertEqual(result['jobs'][1]['log_tail'], ['final line'])
        command = self.cli('wait', first, second, '--json', '--tail', '1')
        self.assertEqual(command.returncode, 7, command.stderr)
        self.assertEqual(json.loads(command.stdout)['jobs'][1]['exit_code'], 7)

    def test_wait_timeout_does_not_cancel_job_or_missing_state_create_files(self):
        from agent_builds.completion import wait
        job = self.job('a')
        reader = Store(self.store.root, read_only=True)
        try:
            result = wait(reader, [job], timeout=.02)
            self.assertTrue(result['timed_out'])
            self.assertEqual(result['exit_code'], 124)
            self.assertEqual(self.store.get(job)['state'], 'queued')
        finally:
            reader.close()
        missing = self.root / 'missing'
        reader = Store(missing, read_only=True)
        try:
            with self.assertRaises(ValueError):
                wait(reader, ['not-a-job'], timeout=.02)
            self.assertFalse(missing.exists())
        finally:
            reader.close()

    def test_wait_observes_completion_after_reattachment(self):
        command = subprocess.Popen([sys.executable, '-m', 'agent_builds', 'run', '--lane', 'quick', '--jobs', '1',
            '--cwd', str(self.root), '--', sys.executable, '-c', 'import time; time.sleep(.8); print("final")'],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(command)
        deadline = time.monotonic()+5
        while not self.store.rows() and time.monotonic()<deadline:
            time.sleep(.02)
        job = self.store.rows()[0]['id']
        result = self.cli('wait', job, '--json', '--timeout', '6', '--tail', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['jobs'][0]['log_tail'], ['final'])

    def test_scheduling_diagnostics_expose_aging_without_an_eta(self):
        job = self.job('aged')
        self.age(job)
        reader = Store(self.store.root, read_only=True)
        try:
            policy = reader.get(job)['scheduling']
            self.assertTrue(policy['age_priority'])
            self.assertFalse(policy['preemption'])
            self.assertNotIn('eta', policy)
        finally:
            reader.close()

    def test_config_cli_validates_updates_without_partial_write(self):
        good = self.cli('config', '--set', 'quick_reserve_slots=1', '--set', 'aging_seconds=45')
        self.assertEqual(good.returncode, 0, good.stderr)
        before = self.store.config_path.read_text()
        bad = self.cli('config', '--set', 'quick_reserve_slots=6')
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(self.store.config_path.read_text(), before)
        for invalid in ('memory_gb="text"', 'nice=false', 'aging_seconds=true', 'min_free_gb="text"'):
            bad = self.cli('config', '--set', invalid)
            self.assertEqual(bad.returncode, 2, bad.stderr)
            self.assertNotIn(b'Traceback', bad.stderr)
            self.assertEqual(self.store.config_path.read_text(), before)

    def test_zero_worker_budget_is_not_silently_replaced_by_default(self):
        result = self.cli('run', '--jobs', '0', '--cwd', str(self.root), '--', 'true')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.store.rows(), [])

    def test_sampler_measures_visible_tree_without_command_arguments(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; data=bytearray(8*1024*1024); time.sleep(.7)'])
        self.children.append(child)
        sampler = JobSampler(child.pid, identity(child.pid))
        for _ in range(4):
            sampler.sample()
            time.sleep(.05)
        usage = sampler.result()
        self.assertGreater(usage['sampled_peak_rss_gb'], .005)
        self.assertEqual(usage['samples'], 4)
        self.assertNotIn('command', usage)

    def test_job_execution_persists_measurements_and_history(self):
        result = self.cli('run', '--lane', 'quick', '--jobs', '1', '--memory-gb', '1', '--cwd', str(self.root),
                          '--', sys.executable, '-c', 'import time; data=bytearray(4*1024*1024); time.sleep(.65)')
        self.assertEqual(result.returncode, 0, result.stderr)
        job = self.store.rows()[0]
        self.assertTrue(job['usage']['complete'])
        self.assertGreater(job['usage']['sampled_peak_rss_gb'], 0)
        self.assertGreaterEqual(job['usage']['samples'], 2)
        report = statistics(self.store)
        self.assertEqual(report['groups'][0]['measured_jobs'], 1)
        self.assertIsNone(report['groups'][0]['suggested_memory_gb'])
        self.assertFalse(report['suggestions_applied'])

    def test_old_database_can_be_inspected_without_migration(self):
        old = self.root / 'old'
        old.mkdir()
        import sqlite3
        db = sqlite3.connect(str(old / 'queue.sqlite3'))
        db.execute('CREATE TABLE jobs (id TEXT, state TEXT, created REAL, started REAL, finished REAL, command TEXT, cwd TEXT, slots INTEGER, herdr TEXT, wait_context TEXT, owner_pid INTEGER, owner_start TEXT)')
        db.execute("INSERT INTO jobs VALUES ('old','succeeded',1,2,3,'[\"true\"]','/tmp',1,'{}','{}',1,'x')")
        db.commit()
        db.close()
        reader = Store(old, read_only=True)
        try:
            self.assertEqual(reader.get('old')['usage'], {})
            self.assertNotIn('usage', {r[1] for r in reader.db.execute('PRAGMA table_info(jobs)')})
        finally:
            reader.close()

    def test_cleanup_preview_and_apply_preserve_active_recent_and_symlink_logs(self):
        old = self.job('old')
        active = self.job('active')
        recent = self.job('recent')
        linked = self.job('linked')
        self.store.finish(old, 'succeeded', 0)
        self.store.finish(recent, 'succeeded', 0)
        self.store.finish(linked, 'failed', 1)
        stamp = time.time() - 10*86400
        for job in (old, active, recent, linked):
            self.store.log_path(job).write_text('retained content')
            if job != recent:
                os.utime(self.store.log_path(job), (stamp, stamp))
        for job in (old, linked):
            self.store.db.execute('UPDATE jobs SET finished=? WHERE id=?', (stamp, job))
        target = self.root / 'outside'
        target.write_text('keep me')
        self.store.log_path(linked).unlink()
        self.store.log_path(linked).symlink_to(target)
        preview = clean_logs(self.store)
        self.assertEqual([r['job_id'] for r in preview['files']], [old])
        self.assertTrue(self.store.log_path(old).exists())
        clean_logs(self.store, apply=True)
        self.assertFalse(self.store.log_path(old).exists())
        self.assertEqual(self.store.get(old)['state'], 'succeeded')
        for job in (active, recent, linked):
            self.assertTrue(self.store.log_path(job).exists())
        self.assertEqual(target.read_text(), 'keep me')

    def test_disk_inventory_counts_cache_and_never_follows_top_level_symlink(self):
        target = self.root / 'target'
        target.mkdir()
        (target / 'artifact').write_bytes(b'x'*8192)
        (self.root / 'node_modules').symlink_to(target, target_is_directory=True)
        report = inventory(self.store, self.root)
        entry = next(r for r in report['paths'] if r['kind'] == 'target')
        self.assertGreaterEqual(entry['bytes'], 8192)
        self.assertFalse(any(r['kind'] == 'node_modules' for r in report['paths']))
        self.assertTrue((target / 'artifact').exists())

    def test_disk_inventory_marks_active_cache_and_separates_scratch_estimate(self):
        target = self.root / 'target'
        target.mkdir()
        job = self.store.submit(self.root, ['true'], 'local', 'quick', 1, 1, str(target), '', 5, scratch_gb=2)
        self.assertTrue(self.store.claim(job))
        report = inventory(self.store, self.root)
        entry = next(r for r in report['paths'] if r['kind'] == 'target')
        self.assertEqual(entry['running_jobs'], [job])
        self.assertEqual(entry['cleanup_status'], 'active_or_queued')
        self.assertEqual(report['active_scratch_reservations'][0]['scratch_gb'], 2)

    def test_shortcut_is_explicit_and_preserves_literal_argv(self):
        marker = self.root / 'do-not-create'
        registered = self.cli('task', 'add', 'check', '--cwd', str(self.root), '--lane', 'quick', '--jobs', '1',
                              '--', sys.executable, '-c', 'import sys; print(sys.argv[1])', '$(touch {})'.format(marker))
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.assertEqual(self.store.rows(), [])
        run = self.cli('task', 'run', 'check', '--cwd', str(self.root))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(b'$(touch', run.stdout)
        self.assertFalse(marker.exists())
        self.assertEqual(self.store.rows()[0]['slots'], 1)
        self.assertEqual(self.store.rows()[0]['lane'], 'quick')

    def test_shortcut_retains_explicit_output_resource_key(self):
        key = str(self.root / 'independent-output')
        result = self.cli('task', 'add', 'isolated', '--cwd', str(self.root), '--resource-key', key, '--', 'true')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli('task', 'run', 'isolated', '--cwd', str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.rows()[0]['resource_key'], key)

    def test_stats_groups_by_lane_and_reservation_and_requires_three_successes_for_suggestions(self):
        for index in range(3):
            job = self.job('same', 'quick', 1)
            self.store.finish(job, 'succeeded', 0)
            self.store.record_usage(job, dict(samples=3, sampled_peak_rss_gb=.8, cpu_seconds=.2, complete=True))
        heavy = self.job('same', 'heavy', 2)
        self.store.finish(heavy, 'failed', 1)
        result = statistics(self.store)
        self.assertEqual(len(result['groups']), 2)
        quick = next(r for r in result['groups'] if r['lane'] == 'quick')
        self.assertEqual(quick['suggested_memory_gb'], 1.25)
        self.assertEqual(next(r for r in result['lanes'] if r['lane'] == 'quick')['jobs'], 3)

    def test_worker_registration_never_connects_and_rejects_ssh_options(self):
        with mock.patch('subprocess.run', side_effect=AssertionError('must not connect')):
            args = argparse.Namespace(name='build', host='dedicated-example', state_dir='/srv/build-queue', dedicated_build_host=True)
            save_worker(self.store, args)
            args.host = '-oProxyCommand=bad'
            with self.assertRaises(ValueError):
                save_worker(self.store, args)
            args.host = 'good'
            args.dedicated_build_host = False
            with self.assertRaises(ValueError):
                save_worker(self.store, args)
        self.assertEqual(load(self.store)['workers']['build']['host'], 'dedicated-example')

    def test_ssh_uses_fixed_command_and_json_stdin(self):
        worker = dict(host='dedicated-example', state_dir='/srv/queue')
        response = mock.Mock(returncode=0, stdout=json.dumps(dict(protocol=PROTOCOL, ok=True, result={'ok': 1})))
        with mock.patch('agent_builds.remote.subprocess.run', return_value=response) as run:
            self.assertEqual(rpc(worker, dict(action='get', job='literal;not-shell')), {'ok': 1})
        argv = run.call_args[0][0]
        self.assertEqual(argv[-1], 'python3 -m agent_builds.remote')
        for flag in ('ForwardAgent=no', 'ForwardX11=no', 'ClearAllForwardings=yes', 'PermitLocalCommand=no'):
            self.assertIn(flag, argv)
        self.assertNotIn('literal;not-shell', ' '.join(argv))
        self.assertEqual(json.loads(run.call_args[1]['input'])['job'], 'literal;not-shell')

    def test_remote_protocol_rejects_mismatch_without_creating_state(self):
        state = self.root / 'absent'
        with self.assertRaises(ValueError):
            serve(dict(protocol=99, state_dir=str(state), action='submit'))
        self.assertFalse(state.exists())

    def test_remote_disconnect_after_submit_is_reconciled_without_rerunning(self):
        state = str(self.root / 'remote')
        save_worker(self.store, argparse.Namespace(name='build', host='fake', state_dir=state, dedicated_build_host=True))
        args = argparse.Namespace(worker='build', cwd=str(self.root), command=[sys.executable, '-c', 'print("worker done")'],
            lane='quick', jobs=1, memory_gb=1, timeout=5, min_free_gb=0, scratch_gb=0, backend='local', image='')
        # Use actual server/runner execution in isolated local state, with no SSH.
        def disconnected(worker, request):
            with mock.patch.dict(os.environ, self.env):
                serve(dict(request, protocol=PROTOCOL, state_dir=state))
            raise ValueError('connection lost after submission')
        with mock.patch('agent_builds.remote.rpc', side_effect=disconnected):
            record = submit(self.store, args)
        self.assertEqual(record['state'], 'unknown')
        self.assertIsNone(record['job_id'])
        def local_rpc(worker, request):
            return serve(dict(request, protocol=PROTOCOL, state_dir=state))
        with mock.patch('agent_builds.remote.rpc', side_effect=local_rpc):
            found = reconcile(self.store, record['handle'])
        self.assertIsNotNone(found['job_id'])
        duplicate = serve(dict(protocol=PROTOCOL, action='submit', state_dir=state, request_id=record['request_id']))
        self.assertEqual(duplicate['state'], 'unknown')
        reader = Store(state, read_only=True)
        try:
            self.assertEqual(len(reader.rows()), 1)
            deadline = time.monotonic()+8
            while reader.get(found['job_id'])['state'] not in ('succeeded', 'failed', 'cancelled') and time.monotonic()<deadline:
                time.sleep(.03)
            self.assertEqual(reader.get(found['job_id'])['state'], 'succeeded')
            logs = serve(dict(protocol=PROTOCOL, action='logs', state_dir=state, job=found['job_id'], offset=0))
            import base64
            self.assertIn(b'worker done', base64.b64decode(logs['data']))
        finally:
            reader.close()


if __name__ == '__main__':
    unittest.main()
