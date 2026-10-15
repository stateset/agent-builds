import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_builds.scheduler import capacity_reason, choose, safe_backfill
from agent_builds.simulation import compare, fixture, history
from agent_builds.store import DEFAULTS, Store

ROOT = str(Path(__file__).resolve().parent.parent)


def row(name, lane='heavy', slots=1, memory=1, created=0, duration=10):
    return dict(id=name,lane=lane,slots=slots,memory_gb=memory,created=created,duration=duration,
                resource_key=name,cancel_requested=0,min_free_gb=0,scratch_gb=0)


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.config=dict(DEFAULTS,quick_reserve_slots=0,quick_reserve_jobs=0,quick_reserve_memory_gb=0)
        self.running=[row('a'),row('b')]
        self.head=row('head')
        self.quick=row('check','quick',slots=2,created=130,duration=1)

    def select(self,queued,running=None,available=100,disk=100):
        with mock.patch('agent_builds.scheduler.time.time',return_value=130):
            return choose(queued,self.running if running is None else running,self.config,available,lambda r:disk)

    def test_safe_spare_capacity_starts_quick_without_preempting_aged_head(self):
        selected,skipped=self.select([self.head,self.quick])
        self.assertEqual(selected['id'],'check')
        self.assertEqual(skipped['head'],'heavy job budget')
        # Whichever existing heavy finishes, the aged head remains eligible.
        for remaining in ([],[self.running[0]],[self.running[1]]):
            self.assertEqual(capacity_reason(self.head,remaining+[self.quick],self.config,130),'')

    def test_cpu_memory_and_job_count_release_cases_reject_unsafe_backfill(self):
        examples=[([row('a',slots=3),row('b','quick',slots=2)],row('head',slots=3),row('q','quick')),
                  ([row('a',memory=8),row('b',memory=6)],row('head',memory=8),row('q','quick',memory=3))]
        for running,head,quick in examples:
            self.assertFalse(safe_backfill(head,quick,running,self.config,130))
        config=dict(self.config,max_running=2)
        self.assertFalse(safe_backfill(self.head,self.quick,self.running,config,130))

    def test_busy_checkout_earlier_sibling_and_cancelled_work_are_not_backfilled(self):
        self.quick['resource_key']='a'
        self.assertEqual(self.select([self.head,self.quick])[0]['id'],'head')
        self.quick['resource_key']='check'
        sibling=row('sibling',slots=1,created=10)
        sibling['resource_key']='check'
        self.assertEqual(self.select([self.head,sibling,self.quick])[0]['id'],'head')
        self.quick['cancel_requested']=1
        self.assertEqual(self.select([self.head,self.quick])[0]['id'],'head')

    def test_disk_scratch_and_ram_headroom_remain_conservative(self):
        self.quick['scratch_gb']=.1
        self.assertEqual(self.select([self.head,self.quick])[0]['id'],'head')
        self.quick['scratch_gb']=0
        self.quick['min_free_gb']=1000
        self.assertEqual(self.select([self.head,self.quick])[0]['id'],'head')
        self.quick['min_free_gb']=0
        self.assertEqual(self.select([self.head,self.quick],available=5)[0]['id'],'head')

    def test_protected_heavy_pool_does_not_freeze_scratch_quick_when_global_limits_also_block(self):
        config=dict(DEFAULTS,protected_quick=True)
        running=[row('a',slots=2),row('b',slots=2)]
        head=row('head',slots=3)
        quick=dict(row('check','quick',created=129),scratch_gb=.1)
        selected,skipped=choose([head,quick],running,config,100,lambda r:100,now=130)
        self.assertEqual(selected['id'],'check')
        self.assertEqual(skipped['head'],'CPU slot budget')
        # Legacy soft reserves still retain their conservative scratch rule.
        legacy=choose([head,quick],running,dict(config,protected_quick=False),100,lambda r:100,now=130)[0]
        self.assertEqual(legacy['id'],'head')
        # Memory can mask the protected-pool blocker in the same way as CPU.
        running=[row('a',slots=2,memory=6),row('b',slots=2,memory=6)]
        selected,skipped=choose([dict(head,slots=1,memory_gb=6),quick],running,config,100,lambda r:100,now=130)
        self.assertEqual(selected['id'],'check')
        self.assertEqual(skipped['head'],'memory reservation budget')

    def test_protected_pool_still_enforces_disk_ram_locks_and_global_limits(self):
        config=dict(DEFAULTS,protected_quick=True)
        running=[row('a',slots=2),row('b',slots=2)]
        head=row('head',slots=3)
        quick=dict(row('check','quick',created=129),scratch_gb=.1)
        for candidate,available,disk in ((quick,100,0),(quick,0,100),
                (dict(quick,slots=3),100,100),(dict(quick,resource_key='a'),100,100),
                (dict(quick,resource_key='head'),100,100)):
            selected,_=choose([head,candidate],running,config,available,lambda r:disk,now=130)
            self.assertIsNone(selected)
        selected,_=choose([head,quick],running+[row('q','quick')],config,100,lambda r:100,now=130)
        self.assertIsNone(selected)

    def test_protected_aged_head_drains_quick_work_when_heavy_pool_alone_would_fit(self):
        config=dict(DEFAULTS,protected_quick=True)
        head=row('head',slots=3)
        running=[row('q1','quick',slots=2),row('q2','quick',slots=2)]
        quick=dict(row('new','quick',created=129),scratch_gb=.1)
        selected,_=choose([head,quick],running,config,100,lambda r:100,now=130)
        self.assertEqual(selected['id'],'head')
        # Once a quick job settles, the aged heavy gets priority over new work.
        selected,_=choose([head,quick],running[:1],config,100,lambda r:100,now=130)
        self.assertEqual(selected['id'],'head')
        self.assertFalse(capacity_reason(head,running[:1],config,130))

    def test_real_protected_queue_admits_scratch_quick_and_then_aged_heavy(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'state')
            other=Store(store.root)
            try:
                store.update_config(dict(protected_quick=True,min_free_gb=0,min_available_gb=0,nice=0))
                a=store.submit(temp,['true'],'local','heavy',2,1,'a','',10)
                b=store.submit(temp,['true'],'local','heavy',2,1,'b','',10)
                self.assertTrue(store.claim(a))
                self.assertTrue(store.claim(b))
                head=store.submit(temp,['true'],'local','heavy',3,1,'head','',10)
                quick=store.submit(temp,['true'],'local','quick',1,1,'quick','',10,scratch_gb=.1)
                store.db.execute('UPDATE jobs SET created=? WHERE id=?',(time.time()-121,head))
                self.assertFalse(store.claim(head))
                self.assertTrue(other.claim(quick))
                self.assertEqual(store.counts()['running'],3)
                store.finish(a,'succeeded',0)
                self.assertFalse(store.claim(head))
                store.finish(b,'succeeded',0)
                self.assertTrue(store.claim(head))
                self.assertEqual(other.get(quick)['state'],'running')
            finally:
                other.close()
                store.close()

    def test_aged_head_keeps_priority_when_it_can_start(self):
        self.assertEqual(self.select([self.head,self.quick],running=[row('a')])[0]['id'],'head')

    def test_large_configurations_conservatively_drain(self):
        self.assertFalse(safe_backfill(self.head,self.quick,[row(str(i)) for i in range(9)],dict(self.config,max_running=20),130))

    def test_oldest_compatible_quick_head_is_selected(self):
        later=dict(self.quick,id='later',resource_key='later',created=130.1)
        # Arrival is enforced by Store; queued rows have all already arrived.
        self.assertEqual(self.select([self.head,later,self.quick])[0]['id'],'check')

    def test_exhaustive_completion_subsets_for_randomized_reservations(self):
        rng=random.Random(941)
        accepted=0
        for case in range(200):
            running=[row(str(i),rng.choice(['heavy','quick']),rng.randint(1,2),rng.randint(1,3)) for i in range(rng.randint(1,3))]
            head=row('head',slots=rng.randint(1,3),memory=rng.randint(1,5))
            candidate=row('quick','quick',slots=rng.randint(1,2),memory=rng.randint(1,3))
            if not safe_backfill(head,candidate,running,self.config,130):
                continue
            accepted+=1
            # Independently verify the reservation inequalities after every subset.
            for mask in range(1<<len(running)):
                remaining=[r for i,r in enumerate(running) if mask&(1<<i)]
                before=remaining+[head]
                eligible=(len(before)<=self.config['max_running'] and sum(r['slots'] for r in before)<=6
                          and sum(r['memory_gb'] for r in before)<=16 and sum(r['lane']=='heavy' for r in before)<=2)
                if eligible:
                    after=before+[candidate]
                    self.assertLessEqual(len(after),3)
                    self.assertLessEqual(sum(r['slots'] for r in after),6)
                    self.assertLessEqual(sum(r['memory_gb'] for r in after),16)
                    self.assertLessEqual(sum(r['lane']=='heavy' for r in after),2)
        self.assertGreater(accepted,0)

    def test_fixture_improves_modeled_quick_latency_without_delaying_head(self):
        result=compare(*fixture())
        before={r['id']:r for r in result['baseline']['admissions']}
        after={r['id']:r for r in result['candidate']['admissions']}
        self.assertEqual(before['short-check']['wait_seconds'],50)
        self.assertEqual(after['short-check']['wait_seconds'],0)
        self.assertEqual(before['aged-head']['started'],after['aged-head']['started'])
        self.assertEqual(result['baseline']['makespan_seconds'],result['candidate']['makespan_seconds'])

    def test_finite_seeded_workloads_complete_without_starvation(self):
        for seed in range(20):
            rng=random.Random(seed)
            workload=[row(str(i),rng.choice(['quick','heavy']),rng.randint(1,3),rng.randint(1,4),
                          created=i*rng.uniform(.5,3),duration=rng.uniform(.1,60)) for i in range(30)]
            result=compare(workload,self.config)
            for name in ('baseline','candidate'):
                self.assertEqual(result[name]['completed'],30,(seed,name))
                self.assertEqual(result[name]['unadmitted_job_ids'],[])

    def test_simulation_reports_unadmittable_jobs_instead_of_looping_or_lying(self):
        result=compare([row('oversized',slots=10)],self.config)
        self.assertEqual(result['candidate']['completed'],0)
        self.assertEqual(result['candidate']['unadmitted_job_ids'],['oversized'])

    def test_history_and_fixture_inspection_never_create_state_or_execute_argv(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            reader=Store(root/'missing',read_only=True)
            try:
                with mock.patch('subprocess.Popen',side_effect=AssertionError('no execution')):
                    result=history(reader)
                self.assertEqual(result['jobs'],0)
                self.assertFalse((root/'missing').exists())
            finally:
                reader.close()
            env=dict(os.environ,AGENT_BUILDS_STATE=str(root/'absent'),PYTHONPATH=ROOT)
            cli=subprocess.run([sys.executable,'-m','agent_builds','simulate','--fixture','--json'],env=env,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
            self.assertEqual(cli.returncode,0,cli.stderr)
            self.assertFalse(json.loads(cli.stdout)['changes_applied'])
            self.assertFalse((root/'absent').exists())

    def test_history_replay_is_bounded_and_does_not_invoke_saved_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'state')
            try:
                stamp=time.time()-100
                for i in range(3):
                    job=store.submit(temp,['do-not-execute'], 'local','quick',1,1,str(i),'',10)
                    store.db.execute("UPDATE jobs SET state='succeeded',created=?,started=?,finished=?,exit_code=0 WHERE id=?",
                                     (stamp+i,stamp+i+1,stamp+i+2,job))
                with mock.patch('subprocess.Popen',side_effect=AssertionError('no execution')):
                    result=history(store,limit=2)
                self.assertEqual(result['history_matching'],3)
                self.assertTrue(result['history_truncated'])
                self.assertEqual(result['jobs'],2)
                self.assertFalse(result['changes_applied'])
            finally:
                store.close()

    def test_real_queue_admission_enforces_head_safety_with_independent_connections(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'state')
            other=Store(store.root)
            try:
                store.update_config(dict(self.config,min_free_gb=0,min_available_gb=0))
                jobs=[]
                for key,lane,slots in [('a','heavy',1),('b','heavy',1),('head','heavy',1),('quick','quick',2)]:
                    jobs.append(store.submit(temp,['true'],'local',lane,slots,1,key,'',10))
                    if len(jobs)<=2:
                        self.assertTrue(store.claim(jobs[-1]))
                a,b,head,quick=jobs
                store.db.execute('UPDATE jobs SET created=? WHERE id=?',(time.time()-121,head))
                self.assertFalse(store.claim(head))
                self.assertTrue(other.claim(quick))
                store.finish(a,'succeeded',0)
                self.assertTrue(store.claim(head))
                self.assertEqual(store.counts()['running'],3)
                self.assertEqual(other.get(quick)['state'],'running')
            finally:
                other.close()
                store.close()
