import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_builds.advice import lines, report
from agent_builds.store import Store

ROOT = str(Path(__file__).resolve().parent.parent)


class AdviceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state')
        self.store.update_config(dict(min_free_gb=0, min_available_gb=0, paused=True))
        self.now = time.time()
        self.env = dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root), PYTHONPATH=ROOT)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job(self, key='repo', slots=3, memory=6, lane='quick', backend='local', command=None):
        return self.store.submit(self.root, command or ['literal;never-execute'], backend, lane,
                                 slots, memory, key, 'fixture' if backend=='docker' else '', 10)

    def completed(self, duration=120, ago=5, code=0, measured=True, **kwargs):
        job = self.job(**kwargs)
        finish = self.now-ago
        self.store.db.execute('UPDATE jobs SET state=?,created=?,started=?,finished=?,exit_code=?,wait_history=? WHERE id=?',
            ('succeeded' if code==0 else 'failed', finish-duration-100, finish-duration, finish, code, '{}', job))
        if measured:
            self.store.record_usage(job, dict(source='local_process_tree', complete=True, samples=6,
                sampled_peak_rss_gb=.6, cpu_seconds=duration*.3))
        return job

    def codes(self, result=None):
        return {f['code'] for f in (result or report(self.store))['findings']}

    def test_matching_successes_support_explicit_trials_without_actions(self):
        ids = [self.completed() for _ in range(3)]
        before = self.store.db.execute('SELECT * FROM jobs ORDER BY id').fetchall()
        config = self.store.config_path.read_bytes()
        with mock.patch('subprocess.Popen', side_effect=AssertionError('must not execute')):
            result = report(self.store)
        self.assertFalse(result['changes_applied'])
        self.assertTrue({'long_quick_command','memory_trial','low_average_cpu'} <= self.codes(result))
        for f in result['findings']:
            if f['suggested_trial']:
                self.assertEqual(set(f['evidence']['job_ids']), set(ids))
                self.assertIn('literal;never-execute', f['evidence']['command_display'])
        self.assertEqual(before, self.store.db.execute('SELECT * FROM jobs ORDER BY id').fetchall())
        self.assertEqual(config, self.store.config_path.read_bytes())

    def test_insufficient_successes_and_failures_do_not_support_trials(self):
        for _ in range(2):
            self.completed()
        for _ in range(4):
            self.completed(code=7)
        self.assertFalse({'long_quick_command','memory_trial','low_average_cpu'} & self.codes())

    def test_different_commands_reservations_outputs_and_backends_are_not_pooled(self):
        self.completed()
        self.completed(command=['different'])
        self.completed(slots=2)
        self.completed(memory=4)
        self.completed(key='different-output')
        self.completed(backend='docker')
        self.assertFalse({'long_quick_command','memory_trial','low_average_cpu'} & self.codes())

    def test_duration_findings_do_not_require_resource_measurements(self):
        for _ in range(3):
            self.completed(measured=False)
        codes = self.codes()
        self.assertIn('long_quick_command', codes)
        self.assertFalse({'memory_trial','low_average_cpu'} & codes)

    def test_incomplete_short_and_unsampled_measurements_do_not_support_trials(self):
        for mode in ('incomplete','short','unsampled','unknown_source'):
            for _ in range(3):
                job = self.completed(command=[mode], duration=.5 if mode=='short' else 120)
                usage = self.store.get(job)['usage']
                if mode=='incomplete':
                    usage['complete'] = False
                if mode=='unsampled':
                    usage['samples'] = 0
                if mode=='unknown_source':
                    usage['source'] = 'unknown'
                self.store.record_usage(job, usage)
        self.assertFalse({'memory_trial','low_average_cpu'} & self.codes())

    def test_peak_memory_and_high_cpu_prevent_unsupported_downsize_trials(self):
        for _ in range(3):
            job = self.completed(lane='heavy')
            self.store.record_usage(job, dict(source='local_process_tree', complete=True, samples=6,
                sampled_peak_rss_gb=5.8, cpu_seconds=300))
        self.assertFalse({'long_quick_command','memory_trial','low_average_cpu'} & self.codes())

    def test_history_window_repo_filter_limit_and_exact_counts(self):
        for _ in range(4):
            self.completed()
        self.completed(ago=25*3600)
        result = report(self.store, limit=2)
        self.assertEqual(result['history_matching'], 4)
        self.assertEqual(result['history_sampled'], 2)
        self.assertTrue(result['history_truncated'])
        self.assertFalse({'long_quick_command','memory_trial','low_average_cpu'} & self.codes(result))
        self.assertEqual(report(self.store, repo=self.root/'other')['history_sampled'], 0)
        self.assertEqual(report(self.store, hours=48)['history_matching'], 5)

    def test_active_reservations_and_fresh_blockers_are_reported_with_handles(self):
        job = self.job()
        self.store.db.execute('UPDATE jobs SET created=? WHERE id=?', (self.now-90, job))
        self.assertFalse(self.store.claim(job))
        result = report(self.store)
        finding = next(f for f in result['findings'] if f['code']=='current_wait_paused')
        self.assertEqual(finding['evidence']['job_ids'], [job])
        self.assertIn('not time attributed', finding['evidence']['note'])
        self.assertEqual(result['active_counts']['queued'], 1)

    def test_stale_samples_are_not_claimed_current_or_dead(self):
        job = self.job()
        self.store.db.execute('UPDATE jobs SET created=?,reason=?,wait_context=? WHERE id=?',
            (self.now-90, 'CPU slot budget', json.dumps({'measured_at':self.now-30}), job))
        result = report(self.store)
        self.assertIn('stale_wait_observation', self.codes(result))
        self.assertNotIn('current_wait_cpu_capacity', self.codes(result))
        self.assertEqual(self.store.get(job)['state'], 'queued')

    def test_budget_reduction_identifies_impossible_job_without_cancelling(self):
        job = self.job(slots=6)
        self.store.update_config(dict(cpu_slots=4,quick_reserve_slots=1))
        result = report(self.store)
        finding = next(f for f in result['findings'] if f['code']=='job_exceeds_budget')
        self.assertEqual(finding['evidence']['job_id'], job)
        self.assertEqual(self.store.get(job)['cancel_requested'], 0)

    def test_limited_coverage_is_not_mistaken_for_a_scheduler_cause(self):
        self.completed()
        result = report(self.store)
        self.assertIn('limited_wait_coverage', self.codes(result))
        self.assertEqual(result['wait_breakdown']['coverage'], 0)
        self.assertEqual(result['wait_breakdown']['seconds'], {'unobserved':100})

    def test_active_sample_limit_is_disclosed(self):
        for index in range(4):
            self.job(key=str(index))
        result = report(self.store,limit=2)
        self.assertTrue(result['active_truncated'])
        self.assertEqual(result['active_sampled'],2)
        self.assertEqual(result['active_counts']['queued'],4)

    def test_invalid_limits_do_not_write_or_silently_fall_back(self):
        for hours in (0, -1, float('nan'), float('inf'), True, 8761):
            with self.assertRaises(ValueError):
                report(self.store,hours=hours)
        for limit in (0,-1,2001,True,1.5):
            with self.assertRaises(ValueError):
                report(self.store,limit=limit)

    def test_empty_cli_inspection_does_not_create_state(self):
        missing=self.root/'absent'
        result=subprocess.run([sys.executable,'-m','agent_builds','advise','--json'],
            env=dict(self.env,AGENT_BUILDS_STATE=str(missing)),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8)
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(result.stdout)
        self.assertEqual(data['findings'],[])
        self.assertFalse(data['changes_applied'])
        self.assertFalse(missing.exists())
        self.assertIn('No findings', '\n'.join(lines(data)))

    def test_cli_text_and_json_are_read_only_and_support_filters(self):
        for _ in range(3):
            self.completed()
        before=self.store.db.total_changes
        reader=Store(self.store.root,read_only=True)
        try:
            with mock.patch('os.chmod',side_effect=AssertionError('no writes')):
                self.assertIn('memory_trial',self.codes(report(reader)))
        finally:
            reader.close()
        self.assertEqual(self.store.db.total_changes,before)
        for extra in (['--json'],[]):
            result=subprocess.run([sys.executable,'-m','agent_builds','advise','--repo',str(self.root),
                '--hours','24','--limit','10']+extra,env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn(b'long_quick_command',result.stdout)
