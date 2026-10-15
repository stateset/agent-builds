import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_builds import batch, latency, mcp, protection
from agent_builds.scheduler import POLICY_VERSION, capacity_reason, choose
from agent_builds.store import DEFAULTS, Store

ROOT=Path(__file__).resolve().parent.parent


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'state')
        self.store.update_config(dict(cpu_slots=2,max_running=3,memory_gb=4,min_free_gb=0,min_available_gb=0,
            quick_reserve_slots=1,quick_reserve_jobs=1,quick_reserve_memory_gb=1,nice=0,aging_seconds=.1))
        self.env=dict(os.environ,AGENT_BUILDS_STATE=str(self.store.root),PYTHONPATH=str(ROOT))
        self.env.pop('AGENT_BUILDS_JOB_ID',None)  # private fixture only

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job(self,key,lane='heavy',slots=1,memory=.25):
        return self.store.submit(self.root,['true'],'local',lane,slots,memory,key,'',10)

    def age(self,job):
        self.store.db.execute('UPDATE jobs SET created=? WHERE id=?',(time.time()-1,job))

    def test_protected_reserve_cannot_be_borrowed_by_aged_heavy_work(self):
        self.store.update_config(dict(protected_quick=True))
        first,second,quick=self.job('a'),self.job('b'),self.job('c','quick')
        for job in (first,second):
            self.age(job)
        self.assertTrue(self.store.claim(first))
        self.assertFalse(self.store.claim(second))
        self.assertTrue(self.store.claim(quick))
        self.store.finish(first,'succeeded',0)
        self.assertTrue(self.store.claim(second))

    def test_soft_reserve_keeps_legacy_aging_behavior_until_enabled(self):
        a,b=self.job('a'),self.job('b')
        self.age(a);self.age(b)
        self.assertTrue(self.store.claim(a))
        self.assertTrue(self.store.claim(b))

    def test_finite_protected_workloads_preserve_heavy_progress(self):
        import random
        from agent_builds.simulation import replay
        config=dict(self.store.config(),protected_quick=True)
        for seed in range(5):
            rng=random.Random(seed)
            workload=[dict(id=str(i),created=i*.1,lane=rng.choice(['quick','heavy']),slots=1,memory_gb=.25,
                resource_key=str(i),cancel_requested=0,min_free_gb=0,scratch_gb=0,duration=rng.uniform(.1,2)) for i in range(20)]
            result=replay(workload,config,True)
            self.assertEqual(result['completed'],20)
            self.assertEqual(result['unadmitted_job_ids'],[])

    def test_admission_snapshot_retains_effective_mode_after_config_changes(self):
        self.store.update_config(dict(protected_quick=True))
        job=self.job('quick','quick')
        self.assertTrue(self.store.claim(job))
        self.store.update_config(dict(protected_quick=False))
        self.assertTrue(self.store.get(job)['admission']['protected_quick'])

    def test_request_drains_legacy_queued_work_without_killing_or_retrying_it(self):
        old=self.job('old')
        self.store.db.execute("UPDATE jobs SET policy_version='quick-aging-locks-v3',reserve_protocol=0 WHERE id=?",(old,))
        self.store.update_config(dict(protected_quick=True))
        self.assertFalse(self.store.config()['protected_quick'])
        self.assertIn(old,protection.status(self.store)['blocking_jobs'])
        self.assertTrue(self.store.claim(old))
        self.store.finish(old,'succeeded',0)
        new=self.job('new')
        self.assertTrue(self.store.config()['protected_quick'])
        self.assertTrue(self.store.claim(new))

    def test_existing_heavy_reservations_and_large_jobs_drain_before_activation(self):
        large=self.job('large',slots=2)
        self.age(large)
        self.assertTrue(self.store.claim(large))
        self.store.update_config(dict(protected_quick=True))
        self.assertFalse(self.store.config()['protected_quick'])
        self.assertIn(large,protection.status(self.store)['blocking_jobs'])
        self.store.finish(large,'succeeded',0)
        self.job('next','quick')
        self.assertTrue(self.store.config()['protected_quick'])

    def test_idle_legacy_controller_cannot_be_stranded_by_policy_activation(self):
        from agent_builds import plans, external
        with mock.patch.dict(os.environ,self.env,clear=True):
            plan=plans.create(self.store,plans.validate(dict(stages=[dict(name='check',command=['true'])]),self.root,self.store.config()))
            watch=external.create(self.store,dict(cwd=str(self.root),command=['true'],read_only_probe=True,external_ref='fixture',track_source=False))
        self.store.db.execute('UPDATE plans SET reserve_protocol=0 WHERE id=?',(plan,))
        self.store.db.execute('UPDATE external_watches SET reserve_protocol=0 WHERE id=?',(watch,))
        self.store.update_config(dict(protected_quick=True))
        self.assertFalse(self.store.config()['protected_quick'])
        self.assertEqual(len(protection.status(self.store)['blocking_controllers']),2)
        self.store.db.execute("UPDATE plans SET state='succeeded' WHERE id=?",(plan,))
        self.store.db.execute("UPDATE external_watches SET state='succeeded' WHERE id=?",(watch,))
        self.job('new','quick')
        self.assertTrue(self.store.config()['protected_quick'])

    def test_new_large_heavy_job_is_rejected_before_creation(self):
        self.store.update_config(dict(protected_quick=True))
        with self.assertRaisesRegex(ValueError,'protected quick reserves'):
            self.job('large',slots=2)
        self.assertEqual(sum(self.store.counts().values()),0)

    def test_plan_validation_rejects_oversized_heavy_before_controller_creation(self):
        from agent_builds.plans import validate
        self.store.update_config(dict(protected_quick=True))
        with self.assertRaisesRegex(ValueError,'protected quick reserves'):
            validate(dict(stages=[dict(name='large',command=['true'],lane='heavy',jobs=2)]),self.root,self.store.config())

    def test_future_old_insert_fails_before_it_can_borrow_protected_capacity(self):
        self.store.update_config(dict(protected_quick=True))
        with self.assertRaisesRegex(sqlite3.IntegrityError,'installed Buildr runner'):
            self.store.db.execute("""INSERT INTO jobs (id,state,created,cwd,command,backend,lane,slots,memory_gb,resource_key,timeout)
                VALUES ('old','queued',0,?,'["true"]','local','heavy',1,1,'old',10)""",(str(self.root),))
        self.assertEqual(sum(self.store.counts().values()),0)

    def test_protected_budget_edit_cannot_invalidate_active_heavy_reservations(self):
        self.store.update_config(dict(protected_quick=True))
        active=self.job('a',memory=2)
        self.assertTrue(self.store.claim(active))
        before=self.store.config_path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.update_config(dict(quick_reserve_memory_gb=3))
        self.assertEqual(self.store.config_path.read_bytes(),before)

    def test_read_only_does_not_activate_pending_policy_or_initialize_missing_state(self):
        old=self.job('old')
        self.store.db.execute("UPDATE jobs SET policy_version='legacy',reserve_protocol=0 WHERE id=?",(old,))
        self.store.update_config(dict(protected_quick=True))
        self.store.finish(old,'succeeded',0)
        reader=Store(self.store.root,read_only=True)
        try:
            self.assertTrue(protection.status(reader)['pending'])
            self.assertFalse(reader.config()['protected_quick'])
        finally:
            reader.close()
        absent=Store(self.root/'absent',read_only=True)
        try:
            self.assertEqual(latency.report(absent)['cohorts'],[])
            self.assertFalse((self.root/'absent').exists())
        finally:
            absent.close()

    def launch_batch(self,checks,keep=True,timeout=8):
        manifest=self.root/'batch.json'
        manifest.write_text(json.dumps(dict(checks=checks,keep_going=keep)))
        result=subprocess.run([sys.executable,str(ROOT/'buildr'),'batch','run',str(manifest),'--cwd',str(self.root),
            '--timeout',str(timeout),'--detach','--events-json'],env=self.env,capture_output=True,timeout=8)
        self.assertEqual(result.returncode,0,result.stderr)
        handle=json.loads(result.stderr)['job_id']
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            row=self.store.get(handle)
            if row['state'] in ('succeeded','failed','cancelled','lost'):
                return row
            time.sleep(.03)
        self.fail('Batch did not settle')

    def check(self,name,code):
        return dict(name=name,command=[sys.executable,'-c',code])

    def test_batch_one_reservation_preserves_each_native_result_and_continues(self):
        row=self.launch_batch([self.check('one','print("one")'),self.check('failed','raise SystemExit(7)'),self.check('last','print("last")')])
        self.assertEqual(row['exit_code'],7)
        self.assertEqual([check['exit_code'] for check in row['batch']['checks']],[0,7,0])
        self.assertEqual(sum(self.store.counts().values()),1)
        self.assertTrue(row['batch']['complete'])

    def test_batch_fail_fast_skips_remaining_commands(self):
        marker=self.root/'must-not-exist'
        row=self.launch_batch([self.check('failed','raise SystemExit(9)'),self.check('skip','from pathlib import Path; Path({!r}).touch()'.format(str(marker)))],keep=False)
        self.assertEqual(row['exit_code'],9)
        self.assertEqual(row['batch']['checks'][1]['state'],'skipped')
        self.assertFalse(marker.exists())

    def test_batch_timeout_kills_process_group_and_prevents_later_checks(self):
        marker=self.root/'never'
        row=self.launch_batch([self.check('slow','import time; time.sleep(5)'),self.check('skip','from pathlib import Path; Path({!r}).touch()'.format(str(marker)))],timeout=.2)
        self.assertEqual(row['exit_code'],124)
        self.assertFalse(marker.exists())

    def test_batch_missing_executable_is_independent_failure(self):
        row=self.launch_batch([dict(name='missing',command=['/does-not-exist-buildr']),self.check('next','print("ok")')])
        self.assertEqual(row['exit_code'],127)
        self.assertEqual(row['batch']['checks'][1]['exit_code'],0)

    def test_crashed_wrapper_cannot_leave_children_running_after_reservation_release(self):
        marker=self.root/'escaped-child'
        child_code='import time; from pathlib import Path; time.sleep(.7); Path({!r}).touch()'.format(str(marker))
        wrapper='import os,signal,subprocess,sys; subprocess.Popen([sys.executable,"-c",{!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); os.kill(os.getpid(),signal.SIGKILL)'.format(child_code)
        result=subprocess.run([sys.executable,str(ROOT/'buildr'),'run','--cwd',str(self.root),'--lane','quick',
            '--jobs','1','--memory-gb','.25','--',sys.executable,'-c',wrapper],env=self.env,capture_output=True,timeout=8)
        self.assertEqual(result.returncode,137,result.stderr)
        time.sleep(.9)
        self.assertFalse(marker.exists())

    def test_unknown_partial_result_is_not_promoted_to_success_or_retried(self):
        row=dict(state='lost',batch=dict(complete=False,checks=[dict(name='a',state='running',exit_code=None),dict(name='b',state='pending',exit_code=None)]))
        result=batch.summary(row)
        self.assertEqual([check['state'] for check in result['checks']],['unknown','skipped'])
        self.assertEqual(row['batch']['checks'][0]['state'],'running')

    def test_invalid_batch_and_nested_direct_worker_are_rejected(self):
        for value in ({'checks':[]},{'checks':[self.check('a','pass')]*2},{'checks':[dict(name='a',command=[''])]}):
            with self.assertRaises(ValueError):
                batch.validate(value)
        with mock.patch.dict(os.environ,self.env,clear=True):
            with self.assertRaises(ValueError):
                batch.run(json.dumps(dict(checks=[self.check('a','pass')])))

    def test_latency_separates_active_censored_waits_and_completed_cohorts(self):
        a=self.job('a','quick')
        stamp=time.time()-100
        self.store.db.execute("UPDATE jobs SET state='succeeded',created=?,started=?,finished=?,exit_code=0 WHERE id=?",(stamp,stamp+40,stamp+41,a))
        b=self.job('b','quick')
        self.store.db.execute('UPDATE jobs SET created=? WHERE id=?',(stamp,b))
        report=latency.report(self.store,target=30)
        self.assertEqual(report['cohorts'][0]['within_target'],0)
        self.assertEqual(report['cohorts'][0]['wait_p95_seconds'],40)
        self.assertEqual(report['overdue_queued'][0]['job_id'],b)
        self.assertFalse(report['changes_applied'])
