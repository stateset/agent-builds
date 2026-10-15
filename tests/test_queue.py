import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from collections import namedtuple
from unittest import mock

from agent_builds.runner import command_for
from agent_builds.store import Store, identity


ROOT = str(Path(__file__).resolve().parent.parent)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state')
        self.store.config_path.write_text(json.dumps(dict(cpu_slots=3, memory_gb=8, min_free_gb=0, min_available_gb=0, nice=0, quick_reserve_slots=0)))
        self.env = dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root), PYTHONPATH=ROOT)
        self.env.pop('AGENT_BUILDS_JOB_ID', None)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.communicate()
        self.store.close()
        self.temp.cleanup()

    def submit(self, key='repo', slots=2, lane='heavy'):
        return self.store.submit(self.root, ['true'], 'local', lane, slots, 2, key, '', 5)

    def run_job(self, source, cwd=None, extra=None):
        args = [sys.executable, '-m', 'agent_builds', 'run', '--cwd', str(cwd or self.root),
                '--jobs', '2', '--memory-gb', '2'] + (extra or [])
        args += ['--', sys.executable, '-c', source]
        process = subprocess.Popen(args, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        return process

    def wait_state(self, state, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.store.rows()
            match = [row for row in rows if row['state'] == state]
            if match:
                return match[0]
            time.sleep(.03)
        self.fail('Did not observe state {}: {}'.format(state, self.store.rows()))

    def test_independent_connections_cannot_exceed_budget(self):
        first = self.submit('first')
        second = self.submit('second')
        other = Store(self.store.root)
        try:
            self.assertTrue(self.store.claim(first))
            self.assertFalse(other.claim(second))
            self.assertEqual(other.get(second)['reason'], 'CPU slot budget')
            self.store.finish(first, 'succeeded', 0)
            self.assertTrue(other.claim(second))
        finally:
            other.close()

    def test_shared_repo_serializes_even_when_slots_available(self):
        first = self.submit(slots=1)
        second = self.submit(slots=1)
        self.assertTrue(self.store.claim(first))
        self.assertFalse(self.store.claim(second))
        row = self.store.get(second)
        self.assertIn('repository already building', row['reason'])
        self.assertEqual(row['wait_context']['blocking_job']['id'], first)

    def test_crashed_owner_does_not_retry(self):
        job = self.submit()
        self.store.db.execute('UPDATE jobs SET owner_start=? WHERE id=?', ('impossible', job))
        self.store.recover()
        self.assertEqual(self.store.get(job)['state'], 'lost')
        self.assertFalse(self.store.claim(job))

    def test_orphan_live_child_keeps_reservation(self):
        job = self.submit()
        self.assertTrue(self.store.claim(job))
        self.store.db.execute('UPDATE jobs SET owner_start=?, child_pid=?, child_start=? WHERE id=?',
                              ('impossible', os.getpid(), identity(os.getpid()), job))
        self.store.recover()
        self.assertEqual(self.store.get(job)['state'], 'running')

    def test_docker_orphan_needs_explicit_reconciliation(self):
        job = self.store.submit(self.root, ['true'], 'docker', 'heavy', 2, 2, 'docker', 'alpine:3', 5)
        self.assertTrue(self.store.claim(job))
        self.store.db.execute('UPDATE jobs SET owner_start=? WHERE id=?', ('impossible', job))
        self.store.recover()
        self.assertEqual(self.store.get(job)['state'], 'running')
        self.assertIn('Docker', self.store.get(job)['reason'])

    def test_disk_watermark_blocks_new_work(self):
        self.store.config_path.write_text(json.dumps(dict(min_free_gb=10**9)))
        job = self.submit()
        self.assertFalse(self.store.claim(job))
        self.assertIn('disk', self.store.get(job)['reason'])

    def test_real_processes_serialize_across_repos_and_preserve_logs(self):
        a = self.root / 'a'
        b = self.root / 'b'
        a.mkdir()
        b.mkdir()
        first = self.run_job('import time; print("first", flush=True); time.sleep(.6)', a)
        row = self.wait_state('running')
        second = self.run_job('print("second")', b)
        self.assertEqual(first.wait(timeout=8), 0)
        self.assertEqual(second.wait(timeout=8), 0)
        rows = self.store.rows()
        self.assertEqual(len(rows), 2)
        older, newer = sorted(rows, key=lambda r: r['created'])
        self.assertGreaterEqual(newer['started'], older['finished'])
        self.assertIn('first', self.store.log_path(row['id']).read_text())

    def test_command_exit_code_is_preserved(self):
        child = self.run_job('import sys; print("failure output"); sys.exit(7)')
        self.assertEqual(child.wait(timeout=8), 7)
        row = self.store.rows()[0]
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['exit_code'], 7)

    def test_running_cancellation_stops_command(self):
        child = self.run_job('import time; print("ready", flush=True); time.sleep(60)')
        row = self.wait_state('running')
        # Ensure the command has been launched, not merely claimed.
        deadline = time.monotonic() + 5
        while not self.store.get(row['id'])['child_pid'] and time.monotonic() < deadline:
            time.sleep(.02)
        self.store.cancel(row['id'])
        self.assertEqual(child.wait(timeout=8), 130)
        self.assertEqual(self.store.get(row['id'])['state'], 'cancelled')

    def test_cancel_kills_descendant_that_ignores_sigterm(self):
        marker = self.root / 'descendant.pid'
        descendant = ('import os,signal,time; from pathlib import Path; '
                      'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                      'Path({!r}).write_text(str(os.getpid())); time.sleep(60)').format(str(marker))
        source = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{!r}]); time.sleep(60)'.format(descendant)
        child = self.run_job(source)
        row = self.wait_state('running')
        deadline = time.monotonic() + 15
        while (not marker.exists() or not marker.read_text()) and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertTrue(marker.exists())
        descendant_pid = int(marker.read_text())
        self.store.cancel(row['id'])
        self.assertEqual(child.wait(timeout=9), 130)
        deadline = time.monotonic() + 2
        while identity(descendant_pid) and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertIsNone(identity(descendant_pid))

    def test_timeout_is_enforced(self):
        child = self.run_job('import time; time.sleep(30)', extra=['--timeout', '.2'])
        self.assertEqual(child.wait(timeout=8), 124)
        self.assertEqual(self.store.rows()[0]['reason'], 'timeout')

    def test_timeout_survives_closed_output_pipe(self):
        child = self.run_job('import os,time; os.close(1); os.close(2); time.sleep(30)', extra=['--timeout', '.2'])
        self.assertEqual(child.wait(timeout=8), 124)

    def test_available_memory_headroom_blocks_start(self):
        job = self.submit()
        with mock.patch('agent_builds.store.snapshot', return_value={'available_gb': .5}):
            self.assertFalse(self.store.claim(job))
        self.assertEqual(self.store.get(job)['reason'], 'available RAM headroom')

    def test_queued_cancel_does_not_start_command(self):
        first = self.submit('first', slots=3)
        self.assertTrue(self.store.claim(first))
        marker = self.root / 'not-started'
        child = self.run_job('from pathlib import Path; Path({!r}).touch()'.format(str(marker)))
        row = self.wait_state('queued')
        self.store.cancel(row['id'])
        self.assertEqual(child.wait(timeout=8), 130)
        self.assertFalse(marker.exists())

    def test_docker_daemon_error_cannot_confirm_stopped(self):
        from agent_builds.runner import docker_stopped
        with mock.patch('agent_builds.runner.subprocess.run', return_value=mock.Mock(returncode=1, stdout='')):
            self.assertFalse(docker_stopped({'id': 'abc'}))

    def test_pause_blocks_new_starts_without_changing_running_job(self):
        first = self.submit('first', slots=1)
        self.assertTrue(self.store.claim(first))
        self.store.update_config({'paused': True})
        second = self.submit('second', slots=1)
        self.assertFalse(self.store.claim(second))
        self.assertEqual(self.store.get(first)['state'], 'running')
        self.assertEqual(self.store.get(second)['reason'], 'queue paused')
        self.store.update_config({'paused': False})
        self.assertTrue(self.store.claim(second))

    def test_invalid_budget_update_preserves_valid_configuration(self):
        before = self.store.config_path.read_text()
        with self.assertRaises(ValueError):
            self.store.update_config({'cpu_slots': 0})
        self.assertEqual(self.store.config_path.read_text(), before)

    def test_budget_reduction_waits_for_existing_reservations(self):
        first = self.submit('first', slots=2)
        self.assertTrue(self.store.claim(first))
        second = self.submit('second', slots=1)
        self.store.update_config({'cpu_slots': 2})
        self.assertFalse(self.store.claim(second))
        self.store.finish(first, 'succeeded', 0)
        self.assertTrue(self.store.claim(second))

    def test_process_sampler_reports_own_process_without_arguments(self):
        from agent_builds.metrics import ProcessSampler
        sampler = ProcessSampler()
        rows = sampler.sample()
        row = next(row for row in rows if row['pid'] == os.getpid())
        self.assertGreater(row['rss_mb'], 0)
        self.assertNotIn('command', row)
        self.assertIsNone(row['cpu'])
        time.sleep(.02)
        row = next(row for row in sampler.sample() if row['pid'] == os.getpid())
        self.assertIsNotNone(row['cpu'])

    def test_read_only_status_does_not_initialize_or_chmod(self):
        job = self.submit()
        with mock.patch('agent_builds.store.os.chmod', side_effect=AssertionError('chmod called')), \
                mock.patch('pathlib.Path.mkdir', side_effect=AssertionError('mkdir called')):
            reader = Store(self.store.root, read_only=True)
            try:
                self.assertEqual(reader.get(job)['argv'], ['true'])
                self.assertIsNone(reader.get(job)['owner_alive'])
                with self.assertRaises(Exception):
                    reader.db.execute("UPDATE jobs SET state='failed'")
            finally:
                reader.close()

    def test_read_only_missing_state_creates_nothing(self):
        missing = self.root / 'does-not-exist'
        reader = Store(missing, read_only=True)
        try:
            self.assertEqual(reader.rows(), [])
            self.assertFalse(missing.exists())
            self.assertIsNone(reader.db)
        finally:
            reader.close()

    def test_per_job_disk_requirement_and_active_scratch_reservations(self):
        self.store.update_config({'min_free_gb': 10})
        usage = namedtuple('usage', 'total used free')(32 * 1024**3, 18 * 1024**3, 14 * 1024**3)
        first = self.store.submit(self.root, ['true'], 'local', 'quick', 1, 1, 'first', '', 5, scratch_gb=3)
        second = self.store.submit(self.root, ['true'], 'local', 'quick', 1, 1, 'second', '', 5, scratch_gb=2)
        with mock.patch('agent_builds.store.shutil.disk_usage', return_value=usage):
            self.assertTrue(self.store.claim(first))
            self.assertFalse(self.store.claim(second))
            context = self.store.get(second)['wait_context']
            self.assertEqual(context['required_free_gb'], 15)
            self.assertEqual(context['disk_free_gb'], 14)
            self.store.finish(first, 'succeeded', 0)
            self.assertTrue(self.store.claim(second))
        third = self.store.submit(self.root, ['true'], 'local', 'quick', 1, 1, 'third', '', 5, min_free_gb=20)
        with mock.patch('agent_builds.store.shutil.disk_usage', return_value=usage):
            self.assertFalse(self.store.claim(third))
            self.assertGreaterEqual(self.store.get(third)['wait_context']['required_free_gb'], 20)

    def test_lifecycle_json_does_not_mix_with_command_stdout(self):
        child = self.run_job('print("only command output")', extra=['--events-json'])
        output, errors = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, errors)
        self.assertEqual(output.strip(), b'only command output')
        events = [json.loads(line) for line in errors.decode().splitlines()]
        self.assertEqual([event['state'] for event in events], ['queued', 'running', 'succeeded'])
        self.assertEqual(events[-1]['exit_code'], 0)
        self.assertEqual(events[0]['job_id'], events[-1]['job_id'])

    def test_nested_wrapper_fails_without_creating_second_job(self):
        source = 'import subprocess,sys; sys.exit(subprocess.call([sys.executable,"-m","agent_builds","run","--","true"]))'
        child = self.run_job(source)
        output, errors = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 2)
        self.assertIn(b'Already inside queued job', output)
        self.assertEqual(len(self.store.rows()), 1)

    def test_detached_job_outlives_submitting_process(self):
        child = self.run_job('import time; time.sleep(.5); print("detached complete")', extra=['--detach'])
        output, errors = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, errors)
        self.assertIn(b'submitted', output)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = self.store.rows()
            if rows and rows[0]['state'] == 'succeeded':
                self.assertIn('detached complete', self.store.log_path(rows[0]['id']).read_text())
                return
            time.sleep(.05)
        self.fail('Detached runner did not complete')

    def test_status_summary_is_compact_and_has_counts(self):
        self.submit()
        result = subprocess.run([sys.executable, '-m', 'agent_builds', 'status', '--summary', '--json'],
                                 env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['counts']['queued'], 1)
        self.assertEqual(payload['counts']['running'], 0)
        self.assertEqual(sum(payload['counts'].values()), 1)
        self.assertEqual(payload['count_scope'], {'active_only': False, 'repository': None, 'limited_by_result_limit': False})
        self.assertNotIn('jobs', payload)
        self.assertTrue(payload['initialized'])

    def test_active_repo_filter_excludes_other_repos_and_finished_jobs(self):
        first = self.submit('first')
        self.store.finish(first, 'succeeded', 0)
        second = self.submit('second')
        elsewhere = self.root / 'another'
        elsewhere.mkdir()
        self.store.submit(elsewhere, ['true'], 'local', 'quick', 1, 1, str(elsewhere), '', 5)
        rows = self.store.rows(active=True, repo=self.root)
        self.assertEqual([row['id'] for row in rows], [second])
        counts = self.store.counts(active=True, repo=self.root)
        self.assertEqual(counts['queued'], 1)
        self.assertEqual(sum(counts.values()), 1)

    def test_running_job_is_visible_before_newer_queued_jobs_with_limit(self):
        running = self.submit('running', slots=1)
        self.assertTrue(self.store.claim(running))
        first = self.submit('first', slots=1)
        second = self.submit('second', slots=1)
        self.assertEqual(self.store.rows(limit=1, active=True)[0]['id'], running)
        self.assertEqual([row['id'] for row in self.store.rows(active=True)], [running, first, second])

    def test_cpu_wait_names_contributing_jobs_and_refreshes_observation(self):
        first = self.submit('first', slots=2)
        self.assertTrue(self.store.claim(first))
        second = self.submit('second', slots=2)
        self.assertFalse(self.store.claim(second))
        context = self.store.get(second)['wait_context']
        self.assertEqual(context['requested_cpu_slots'], 2)
        self.assertEqual(context['contributing_jobs'][0]['id'], first)
        self.assertEqual(context['contributing_jobs'][0]['slots'], 2)
        self.assertEqual(context['current_used_cpu_slots'], 2)
        self.store.finish(first, 'succeeded', 0)
        refreshed = self.store.get(second)['wait_context']
        self.assertEqual(refreshed['contributing_jobs'], [])
        self.assertEqual(refreshed['current_used_cpu_slots'], 0)
        self.assertEqual(refreshed['used_cpu_slots'], 2)  # Original admission snapshot retained.

    def test_memory_wait_explains_reservation_plus_headroom(self):
        self.store.update_config({'min_available_gb': 4})
        job = self.submit()
        with mock.patch('agent_builds.store.snapshot', return_value={'available_gb': 5}):
            self.assertFalse(self.store.claim(job))
        context = self.store.get(job)['wait_context']
        self.assertEqual(context['job_memory_gb'], 2)
        self.assertEqual(context['memory_headroom_gb'], 4)
        self.assertEqual(context['required_memory_gb'], 6)
        self.assertEqual(context['available_memory_gb'], 5)

    def test_detached_submission_keeps_herdr_origin(self):
        self.env.update(HERDR_PANE_ID='w1:p-test', HERDR_TAB_ID='w1:t-test', HERDR_WORKSPACE_ID='w1')
        child = self.run_job('print("origin captured")', extra=['--detach'])
        output, errors = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, errors)
        row = self.store.rows()[0]
        self.assertEqual(row['origin'], {'pane_id': 'w1:p-test', 'tab_id': 'w1:t-test', 'workspace_id': 'w1'})
        deadline = time.monotonic() + 8
        while self.store.get(row['id'])['state'] not in ('succeeded', 'failed') and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertEqual(self.store.get(row['id'])['state'], 'succeeded')

    def test_follow_a_live_log_until_final_output(self):
        release = self.root / 'release-log-producer'
        source = ('import time; from pathlib import Path; print("first line",flush=True); '
                  'deadline=time.monotonic()+10\n'
                  'while not Path({!r}).exists() and time.monotonic()<deadline: time.sleep(.02)\n'
                  'print("final line",flush=True)').format(str(release))
        child = self.run_job(source)
        row = self.wait_state('running')
        reader = subprocess.Popen([sys.executable, '-m', 'agent_builds', 'logs', '--follow', row['id']],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(reader)
        first = reader.stdout.readline()
        self.assertTrue(first)
        release.touch()
        output, errors = reader.communicate(timeout=8)
        self.assertEqual(reader.returncode, 0, errors)
        self.assertIn(b'final line', first + output)
        self.assertEqual(child.wait(timeout=8), 0)

    def test_empty_filtered_counts_have_stable_zero_keys(self):
        self.submit()
        counts = self.store.counts(active=True, repo=self.root / 'empty')
        self.assertEqual(set(counts), {'queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled', 'lost'})
        self.assertTrue(all(value == 0 for value in counts.values()))

    def test_queued_blocker_reports_wait_time_separately_from_runtime(self):
        first = self.submit()
        self.store.db.execute('UPDATE jobs SET created=? WHERE id=?', (time.time() - 200, first))
        second = self.submit()
        self.assertFalse(self.store.claim(second))
        blocker = self.store.get(second)['wait_context']['blocking_job']
        self.assertEqual(blocker['id'], first)
        self.assertGreaterEqual(blocker['wait_seconds'], 200)
        self.assertEqual(blocker['elapsed_seconds'], 0)

    def test_status_counts_and_rows_share_a_read_only_snapshot(self):
        from agent_builds.__main__ import main
        first = self.submit(slots=1)
        self.assertTrue(self.store.claim(first))
        original = Store.counts
        def counts_with_concurrent_finish(reader, *args, **kwargs):
            result = original(reader, *args, **kwargs)
            self.store.finish(first, 'succeeded', 0)
            return result
        with mock.patch.dict(os.environ, self.env), \
                mock.patch('sys.argv', ['agent-builds', 'status', '--active', '--json']), \
                mock.patch.object(Store, 'counts', counts_with_concurrent_finish), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['counts']['running'], 1)
        self.assertEqual(payload['jobs'][0]['state'], 'running')
        self.assertEqual(self.store.get(first)['state'], 'succeeded')

    def test_literal_arguments_are_not_shell_expanded(self):
        marker = self.root / 'SHOULD_NOT_EXIST'
        child = subprocess.run([sys.executable, '-m', 'agent_builds', 'run', '--jobs', '1', '--memory-gb', '.25',
                    '--cwd', str(self.root), '--', sys.executable, '-c',
                    'import sys; print(sys.argv[1])', '$(touch {})'.format(marker)],
                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertFalse(marker.exists())
        self.assertIn(b'$(touch', child.stdout)

    def test_container_resource_flags_and_no_credential_forwarding(self):
        job = self.store.submit(self.root, ['cargo', 'test'], 'docker', 'heavy', 2, 3, 'docker', 'rust:1.88', 5)
        command = command_for(self.store.get(job), self.store.config())
        self.assertEqual(command[command.index('--cpus') + 1], '2')
        self.assertEqual(command[command.index('--memory') + 1], '3.0g')
        self.assertNotIn('/var/run/docker.sock', ' '.join(command))
        self.assertNotIn('--privileged', command)
        self.assertEqual(command[-3:], ['rust:1.88', 'cargo', 'test'])


if __name__ == '__main__':
    unittest.main()
