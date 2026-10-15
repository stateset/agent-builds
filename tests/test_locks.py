import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_builds import external, mcp, plans
from agent_builds.locks import normalize
from agent_builds.plan_draft import draft
from agent_builds.scheduler import choose, safe_backfill
from agent_builds.store import Store, identity

ROOT = Path(__file__).resolve().parent.parent


class LockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Exercise the actual previous Store/scheduler, loaded before migration.
        cls.old_temp = tempfile.TemporaryDirectory(prefix='buildr-legacy-locks-')
        archive = subprocess.check_output(['git', 'archive', 'v0.13.0', 'agent_builds'], cwd=str(ROOT))
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(cls.old_temp.name)
        package = Path(cls.old_temp.name)/'agent_builds'
        spec = importlib.util.spec_from_file_location('buildr_legacy_v013', package/'__init__.py',
            submodule_search_locations=[str(package)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        from buildr_legacy_v013.store import Store as OldStore
        cls.OldStore = OldStore

    @classmethod
    def tearDownClass(cls):
        cls.old_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = self.OldStore(self.root/'state')
        self.store = Store(self.root/'state')
        self.store.update_config(dict(min_free_gb=0, min_available_gb=0, nice=0,
            quick_reserve_slots=0, quick_reserve_jobs=0, quick_reserve_memory_gb=0))
        self.env = dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root), PYTHONPATH=str(ROOT))
        self.env.pop('AGENT_BUILDS_JOB_ID', None)  # only this private integration fixture

    def tearDown(self):
        self.old.close()
        self.store.close()
        self.temp.cleanup()

    def job(self, primary='checkout', extra=None, old=False, backend='local'):
        store = self.old if old else self.store
        kwargs = {} if old else dict(locks=extra or [])
        return store.submit(self.root, ['true'], backend, 'quick', 1, .25, primary,
            'fixture:image' if backend=='docker' else '', 30, **kwargs)

    def test_validation_deduplicates_without_reinterpreting_keys(self):
        self.assertEqual(normalize('checkout', ['output', 'checkout', 'output']), ['checkout', 'output'])
        for value in ('output', [None], [''], ['\0'], ['x'*4097], ['x']*16):
            with self.assertRaises(ValueError):
                normalize('checkout', value)

    def test_all_keys_acquired_together_and_disjoint_work_can_run(self):
        first = self.job(extra=['target'])
        conflict = self.job('other-checkout', ['target'])
        independent = self.job('independent')
        self.assertEqual(self.store.get(first)['state'], 'queued')
        self.assertEqual(self.store.counts(active=True)['queued'], 3)
        self.assertTrue(self.store.claim(first))
        self.assertFalse(self.store.claim(conflict))
        self.assertEqual(self.store.get(conflict)['wait_context']['conflicting_resource_keys'], ['target'])
        self.assertTrue(self.store.claim(independent))
        self.store.finish(first, 'succeeded', 0)
        self.assertTrue(self.store.claim(conflict))

    def test_existing_legacy_queued_worker_drains_even_when_it_is_younger(self):
        multi = self.job(extra=['target'])
        legacy = self.job('target', old=True)
        self.assertFalse(self.store.claim(multi))
        self.assertIn('legacy worker must drain', self.store.get(multi)['reason'])
        # The old scheduler ignores held rows and can make progress.
        self.assertTrue(self.old.claim(legacy))
        self.assertFalse(self.store.claim(multi))
        self.old.finish(legacy, 'succeeded', 0)
        self.assertTrue(self.store.claim(multi))

    def test_new_runner_can_drain_a_legacy_queued_row(self):
        multi = self.job(extra=['target'])
        legacy = self.job('target', old=True)
        self.assertTrue(self.store.claim(legacy))
        self.assertFalse(self.store.claim(multi))

    def test_future_legacy_extra_key_insert_rejected_before_job_creation(self):
        multi = self.job(extra=['target'])
        self.assertTrue(self.store.claim(multi))
        before = sum(self.store.counts().values())
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'installed Buildr runner'):
            self.job('target', old=True)
        self.assertEqual(sum(self.store.counts().values()), before)
        # The old scheduler already understands the primary key and budgets.
        primary = self.job('checkout', old=True)
        self.assertFalse(self.old.claim(primary))
        independent = self.job('independent', old=True)
        self.assertTrue(self.old.claim(independent))
        self.store.finish(multi, 'succeeded', 0)
        self.assertIsInstance(self.job('target', old=True), str)

    def test_old_scheduler_cannot_choose_held_work_and_block_legacy_drain(self):
        multi = self.job(extra=['target'])
        legacy = self.job('target', old=True)
        self.assertEqual(self.old.get(multi)['state'], 'held')
        self.assertTrue(self.old.claim(legacy))

    def test_fifo_on_every_key_and_reverse_lock_order_have_no_partial_reservations(self):
        first = self.job('checkout', ['target'])
        reverse = self.job('target', ['checkout'])
        self.assertFalse(self.store.claim(reverse))
        self.assertEqual(self.store.reservation_snapshot()['jobs'], [])
        self.assertTrue(self.store.claim(first))
        self.assertFalse(self.store.claim(reverse))
        self.store.finish(first, 'succeeded', 0)
        self.assertTrue(self.store.claim(reverse))

    def test_busy_earlier_multi_lock_job_retains_fifo_on_its_other_keys(self):
        running = self.job('checkout')
        self.assertTrue(self.store.claim(running))
        multi = self.job('checkout', ['target'])
        later = self.job('target')
        self.assertFalse(self.store.claim(later))
        self.store.finish(running, 'succeeded', 0)
        self.assertTrue(self.store.claim(multi))

    def test_concurrent_claims_on_shared_extra_key_admit_exactly_one(self):
        jobs = [self.job(str(i), ['shared']) for i in range(2)]
        results = []
        gate = threading.Barrier(2)
        def claim(job):
            store = Store(self.store.root)
            try:
                gate.wait(timeout=5)
                results.append(store.claim(job))
            finally:
                store.close()
        workers = [threading.Thread(target=claim, args=(job,)) for job in jobs]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(results), [False, True])

    def test_legacy_insert_racing_multi_admission_cannot_execute_both(self):
        multi = self.job(extra=['target'])
        gate = threading.Barrier(2)
        result = {}
        def admit():
            store = Store(self.store.root)
            try:
                gate.wait(timeout=5)
                result['admitted'] = store.claim(multi)
            finally:
                store.close()
        def insert():
            old = self.OldStore(self.store.root)
            try:
                gate.wait(timeout=5)
                try:
                    result['legacy'] = old.submit(self.root, ['true'], 'local', 'quick', 1, .25, 'target', '', 10)
                except sqlite3.IntegrityError:
                    result['rejected'] = True
            finally:
                old.close()
        workers = [threading.Thread(target=admit), threading.Thread(target=insert)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
        if result['admitted']:
            self.assertTrue(result['rejected'])
            self.assertNotIn('legacy', result)
        else:
            self.assertIn('legacy', result)
            self.assertTrue(self.old.claim(result['legacy']))
            self.assertFalse(self.store.claim(multi))

    def test_cancel_held_preserves_nonreservation_until_runner_settles(self):
        job = self.job(extra=['target'])
        self.store.cancel(job)
        self.assertEqual(self.store.get(job)['storage_state'], 'held')
        self.assertFalse(self.store.claim(job))
        self.assertEqual(self.store.reservation_snapshot()['jobs'], [])
        self.store.db.execute('UPDATE jobs SET owner_start=? WHERE id=?', ('gone', job))
        self.store.recover()
        self.assertEqual(self.store.get(job)['state'], 'cancelled')

    def test_orphan_live_child_keeps_all_locks_and_unknown_docker_keeps_reservation(self):
        for backend in ('local', 'docker'):
            job = self.job(backend, [backend+'-output'], backend=backend)
            self.assertTrue(self.store.claim(job))
            self.store.db.execute('UPDATE jobs SET owner_start=?,child_pid=?,child_start=? WHERE id=?',
                ('gone', os.getpid(), identity(os.getpid()), job))
            self.store.recover()
            self.assertEqual(self.store.get(job)['state'], 'running')
            with self.assertRaises(sqlite3.IntegrityError):
                self.job(backend+'-output', old=True)

    def test_held_docker_never_started_does_not_gain_a_false_reservation(self):
        job = self.job(extra=['target'], backend='docker')
        self.store.db.execute('UPDATE jobs SET owner_start=? WHERE id=?', ('gone', job))
        self.store.recover()
        self.assertEqual(self.store.get(job)['state'], 'lost')
        self.assertEqual(self.store.reservation_snapshot()['jobs'], [])

    def test_backfill_rejects_any_key_shared_with_aged_head(self):
        head = self.store.get(self.job('head', ['shared']))
        candidate = self.store.get(self.job('quick', ['shared']))
        self.assertFalse(safe_backfill(head, candidate, [], self.store.config(), time.time()))

    def test_cli_detach_receipt_locks_and_native_exit(self):
        result = subprocess.run([sys.executable, '-m', 'agent_builds', 'run', '--detach', '--events-json',
            '--cwd', str(self.root), '--lane', 'quick', '--jobs', '1', '--memory-gb', '.25',
            '--resource-key', 'checkout', '--lock', 'output-a', '--lock', 'output-b', '--',
            sys.executable, '-c', 'raise SystemExit(7)'], env=self.env, capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stderr)
        result = subprocess.run([sys.executable, '-m', 'agent_builds', 'wait', receipt['job_id'], '--timeout', '8'],
            env=self.env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.store.get(receipt['job_id'])['resource_keys'], ['checkout', 'output-a', 'output-b'])

    def test_detached_worker_imports_installed_package_even_from_a_shadow_checkout(self):
        shadow = self.root/'foreign-checkout'
        (shadow/'agent_builds').mkdir(parents=True)
        (shadow/'agent_builds'/'__init__.py').write_text('raise RuntimeError("wrong Buildr package")\n')
        result = subprocess.run([sys.executable, str(ROOT/'buildr'), 'run', '--detach', '--events-json',
            '--cwd', '.', '--lane', 'quick', '--jobs', '1', '--memory-gb', '.25', '--',
            sys.executable, '-c', 'import os; print(os.getcwd()); raise SystemExit(7)'],
            cwd=str(shadow), env=dict(self.env, AGENT_BUILDS_STATE='../state'), capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        job = json.loads(result.stderr)['job_id']
        result = subprocess.run([sys.executable, str(ROOT/'buildr'), 'wait', job, '--timeout', '8'],
            env=self.env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn(str(shadow), self.store.log_path(job).read_text())

    def test_plan_watch_and_draft_keep_additional_locks(self):
        definition = plans.validate(dict(stages=[dict(name='check', command=['true'], resource_key='repo', locks=['output'])]),
            self.root, self.store.config())
        with mock.patch.dict(os.environ, self.env, clear=True):
            handle = plans.create(self.store, definition)
        plan = plans.get(self.store, handle)
        job, token = plans.prepare(self.store, plan, plan['stages'][0])
        self.assertEqual(self.store.get(job)['resource_keys'], ['repo', 'output'])
        args, attached = plans.attach_job(self.store, job, self.store.db.execute('SELECT launch_token FROM plan_stages WHERE job_id=?', (job,)).fetchone()[0])
        self.assertEqual(attached, job)
        self.assertEqual(draft(self.store, [job], self.root)['manifest']['stages'][0]['locks'], ['output'])
        spec = external.validate(dict(cwd=str(self.root), external_ref='fixture', read_only_probe=True,
            command=['true'], resource_key='watch', locks=['output'], track_source=False), self.store.config())
        with mock.patch.dict(os.environ, self.env, clear=True):
            watch = external.create(self.store, spec)
        probe = external.prepare(self.store, watch)
        self.assertEqual(self.store.get(probe['job_id'])['resource_keys'], ['watch', 'output'])
        self.assertEqual(external.get(self.store, watch)['probes'][0]['state'], 'queued')

    def test_saved_task_and_advisor_preserve_locks_and_count_held_as_queued(self):
        from agent_builds.__main__ import parser
        from agent_builds import advice, profiles
        args = parser().parse_args(['task', 'add', 'check', '--cwd', str(self.root),
            '--resource-key', 'checkout', '--lock', 'target'])
        args.command = ['true']
        profiles.save_task(self.store, args)
        args = parser().parse_args(['task', 'run', 'check', '--cwd', str(self.root)])
        self.assertEqual(profiles.task_args(self.store, args).locks, ['target'])
        job = self.job(extra=['target'])
        report = advice.report(self.store, policy='current')
        self.assertEqual(report['active_counts']['queued'], 1)
        self.assertNotIn('held', report['active_counts'])

    def test_read_only_old_schema_does_not_migrate_and_only_declares_primary(self):
        path = self.root/'old-only'
        old = self.OldStore(path)
        try:
            job = old.submit(self.root, ['true'], 'local', 'quick', 1, .25, 'legacy', '', 10)
            reader = Store(path, read_only=True)
            try:
                self.assertEqual(reader.get(job)['resource_keys'], ['legacy'])
                columns = [row[1] for row in old.db.execute('PRAGMA table_info(jobs)')]
                self.assertNotIn('lock_protocol', columns)
            finally:
                reader.close()
        finally:
            old.close()

    def test_mcp_schema_and_invalid_lock_sets_never_reserve_submission_key(self):
        arguments = dict(request_key='invalid-lock-request', cwd=str(self.root), command=['true'], locks=[' '])
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(ValueError):
                mcp.call('buildr_submit', arguments)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM mcp_requests').fetchone()[0], 0)
