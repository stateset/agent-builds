import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from agent_builds import diagnostics, protection, mcp, announce
from agent_builds.__main__ import main
from agent_builds.store import Store
from agent_builds.tui import compact_lines


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'state')
        self.store.update_config(dict(cpu_slots=6,max_running=3,memory_gb=16,min_free_gb=10,min_available_gb=0,paused=True))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job(self,key='repo',lane='quick',scratch=0):
        return self.store.submit(self.root,['true'],'local',lane,1,1,key,'',30,scratch_gb=scratch)

    def cli(self,*args):
        with mock.patch.dict(os.environ,AGENT_BUILDS_STATE=str(self.store.root)), \
                mock.patch('sys.argv',['buildr']+list(args)), \
                mock.patch('sys.stdout',new_callable=io.StringIO) as out:
            code=main()
        return code,out.getvalue()

    def test_disk_deficit_includes_global_scratch_despite_repo_filter(self):
        running=self.job('other',scratch=2)
        self.store.db.execute("UPDATE jobs SET state='running',started=? WHERE id=?",(time.time(),running))
        waiting=self.job('here',scratch=1)
        usage=type('Usage',(),{'free':int(10.25*1024**3)})()
        with mock.patch('agent_builds.diagnostics.shutil.disk_usage',return_value=usage):
            report=diagnostics.report(self.store,waiting,repo='/different-filter')
        row=report['jobs'][0]
        self.assertEqual(row['disk']['required_gb'],13)
        self.assertEqual(row['disk']['deficit_gb'],2.75)
        self.assertEqual(row['disk']['contributing_jobs'],[dict(job_id=running,scratch_gb=2)])
        self.assertEqual(report['global_reservations']['scratch_gb'],2)
        self.assertIn('--repo',row['inspect_cache_command'])
        self.assertFalse(report['changes_applied'])
        self.assertIn('deficit 2.750', '\n'.join(diagnostics.lines(report)))

    def test_missing_filesystem_is_unknown_not_zero_or_created(self):
        handle=self.job()
        missing=self.root/'deleted-checkout'
        self.store.db.execute('UPDATE jobs SET cwd=? WHERE id=?',(str(missing),handle))
        report=diagnostics.report(self.store,handle)
        self.assertIsNone(report['jobs'][0]['disk']['deficit_gb'])
        self.assertFalse(missing.exists())

    def test_chain_follows_fifo_to_disk_and_labels_stale_links(self):
        head=self.job('a')
        tail=self.job('b')
        now=time.time()
        self.store.db.execute('UPDATE jobs SET reason=?,wait_context=? WHERE id=?',
            ('disk below 10.0 GiB requirement',json.dumps(dict(measured_at=now)),head))
        self.store.db.execute('UPDATE jobs SET reason=?,wait_context=? WHERE id=?',
            ('earlier job in this repository ['+head+']',json.dumps(dict(measured_at=now-60,blocking_job=dict(id=head))),tail))
        result=diagnostics.report(self.store,tail)['jobs'][0]
        self.assertTrue(result['observation']['stale'])
        self.assertEqual([r['id'] for r in result['blocker_chain']['jobs']],[tail,head])
        self.assertEqual(result['blocker_chain']['end'],'observed_head')
        self.assertEqual(result['blocker_chain']['jobs'][-1]['observation']['category'],'disk')
        self.store.finish(head,'failed',7)
        self.assertEqual(diagnostics.report(self.store,tail)['jobs'][0]['blocker_chain']['end'],'settled_since_observation')

    def test_bad_blocker_links_are_bounded_and_not_recovered(self):
        first=self.job('a')
        second=self.job('b')
        for here,other in ((first,second),(second,first)):
            self.store.db.execute('UPDATE jobs SET wait_context=? WHERE id=?',
                (json.dumps(dict(measured_at=time.time(),blocking_job=dict(id=other))),here))
        self.assertEqual(diagnostics.report(self.store,first)['jobs'][0]['blocker_chain']['end'],'cycle')
        self.store.db.execute('DELETE FROM jobs WHERE id=?',(second,))
        self.assertEqual(diagnostics.report(self.store,first)['jobs'][0]['blocker_chain']['end'],'missing_blocker')
        self.assertEqual(self.store.get(first)['state'],'queued')

    def test_protection_explains_legacy_jobs_controllers_and_unknown_versions(self):
        job=self.job()
        self.store.db.execute("UPDATE jobs SET reserve_protocol=0,runner_version='0.14.0' WHERE id=?",(job,))
        self.store.db.execute("INSERT INTO plans (id,name,cwd,definition,created,state) VALUES ('oldplan','old',?,'{}',?,'running')",(str(self.root),time.time()))
        self.store.update_config(dict(protected_quick=True))
        policy=diagnostics.policy_summary(self.store)
        self.assertTrue(policy['pending'])
        self.assertEqual(policy['blocking_job_details'][0]['runner_version'],'0.14.0')
        self.assertIn('legacy runner',policy['blocking_job_details'][0]['conditions'][0])
        self.assertEqual(policy['blocking_controllers'][0]['runner_version'],'unrecorded')
        self.assertIn('idle intervals',policy['blocking_controllers'][0]['condition'])
        self.assertIsNotNone(policy['requested_at'])
        before=policy['requested_at']
        self.store.update_config(dict(nice=9))
        self.assertEqual(protection.status(self.store)['requested_at'],before)
        self.store.finish(job,'succeeded',0)
        self.store.db.execute("UPDATE plans SET state='succeeded' WHERE id='oldplan'")
        # Inspection must not perform the activation itself.
        self.assertTrue(diagnostics.report(self.store)['protection']['pending'])
        self.store.update_config(dict(paused=False,min_free_gb=0))
        state=protection.status(self.store)
        self.assertTrue(state['effective'])
        self.assertGreaterEqual(state['effective_at'],state['requested_at'])

    def test_pre_timestamp_schema_inspection_does_not_migrate_or_invent_request_time(self):
        self.store.db.execute('ALTER TABLE admission_policy RENAME TO admission_old')
        self.store.db.execute('CREATE TABLE admission_policy(id INTEGER PRIMARY KEY,requested INTEGER,effective INTEGER)')
        self.store.db.execute('INSERT INTO admission_policy VALUES (1,1,0)')
        reader=Store(self.store.root,read_only=True)
        try:
            with mock.patch.object(reader,'recover',side_effect=AssertionError('inspection recovered jobs')):
                policy=diagnostics.report(reader)['protection']
            self.assertIsNone(policy['requested_at'])
            self.assertIsNone(policy['pending_seconds'])
            self.assertEqual(len(list(reader.db.execute('PRAGMA table_info(admission_policy)'))),3)
        finally:
            reader.close()

    def test_missing_state_cli_mcp_and_announcement_preview_are_read_only(self):
        missing=self.root/'never-created'
        with mock.patch.dict(os.environ,AGENT_BUILDS_STATE=str(missing),HERDR_ENV='1'), \
                mock.patch.object(announce,'preview',return_value=[]), \
                mock.patch.object(Store,'recover',side_effect=AssertionError('recovery attempted')):
            for argv in (['buildr','diagnose','--json'],['buildr','status','--json'],['buildr','announce']):
                with mock.patch('sys.argv',argv),mock.patch('sys.stdout',new_callable=io.StringIO):
                    self.assertEqual(main(),0)
            self.assertFalse(mcp.call('buildr_diagnose',{},read_only=True)['initialized'])
        self.assertFalse(missing.exists())

    def test_status_default_is_small_active_summary_with_explicit_detailed_history(self):
        active=self.job('active')
        completed=self.job('done')
        huge=json.dumps(dict(large='x'*100000))
        self.store.db.execute('UPDATE jobs SET source=?',(huge,))
        self.store.finish(completed,'failed',7)
        code,output=self.cli('status','--json')
        result=json.loads(output)
        self.assertEqual(code,0)
        self.assertLess(len(output),12000)
        self.assertEqual([j['id'] for j in result['jobs']],[active])
        self.assertNotIn('source',result['jobs'][0])
        self.assertIn('protection',result)
        _,output=self.cli('status','--history','--details','--json')
        result=json.loads(output)
        self.assertEqual([j['id'] for j in result['jobs']],[completed])
        self.assertEqual(result['jobs'][0]['source']['large'],'x'*100000)
        self.assertEqual(result['jobs'][0]['exit_code'],7)

    def test_history_cursor_handles_timestamp_ties_and_new_completions(self):
        handles=[self.job(str(i)) for i in range(5)]
        for handle in handles:
            self.store.finish(handle,'succeeded',0)
        self.store.db.execute('UPDATE jobs SET created=100')
        first=json.loads(self.cli('status','--history','--limit','2','--json')[1])
        self.assertEqual([r['id'] for r in first['jobs']],sorted(handles,reverse=True)[:2])
        newer=self.job('new')
        self.store.finish(newer,'succeeded',0)
        second=json.loads(self.cli('status','--history','--limit','3','--before',first['next_cursor'],'--json')[1])
        self.assertEqual([r['id'] for r in second['jobs']],sorted(handles,reverse=True)[2:])
        self.assertIsNone(second['next_cursor'])

    def test_diagnostics_report_truncation_and_compact_health_monitor(self):
        self.job('a')
        self.job('b')
        result=diagnostics.report(self.store,limit=1)
        self.assertTrue(result['jobs_truncated'])
        lines=compact_lines([],0,[],0,11,self.store.config(),14,40,health=result)
        self.assertLessEqual(len(lines),14)
        self.assertTrue(any('HEALTH' in line for line in lines))
        self.assertLessEqual(len(compact_lines([],0,[],0,11,self.store.config(),14,20,health=result)[-1]),19)
        scrolled=compact_lines([],0,[],0,11,self.store.config(),14,40,health=result,health_offset=5)
        self.assertNotEqual(lines[4:-1],scrolled[4:-1])
        with self.assertRaises(ValueError):
            diagnostics.report(self.store,limit=0)

    def test_latency_separates_short_quick_checks_from_long_checks_and_heavy_work(self):
        from agent_builds import latency
        now=time.time()
        for index,(lane,wait,runtime,exit_code) in enumerate((('quick',5,2,0),('quick',40,20,7),('quick',100,31,0),('heavy',200,2,0))):
            job=self.job(str(index),lane=lane)
            self.store.db.execute('UPDATE jobs SET created=?,started=?,finished=?,state=?,exit_code=? WHERE id=?',
                (now-runtime-wait,now-runtime,now,'succeeded' if not exit_code else 'failed',exit_code,job))
        result=latency.report(self.store)
        quick=next(c for c in result['cohorts'] if c['lane']=='quick')
        heavy=next(c for c in result['cohorts'] if c['lane']=='heavy')
        self.assertEqual(quick['short_quick_checks']['jobs'],2)
        self.assertEqual(quick['short_quick_checks']['wait_p95_seconds'],40)
        self.assertEqual(quick['short_quick_checks']['target_met_fraction'],.5)
        self.assertEqual(quick['short_quick_checks']['native_failures'],1)
        self.assertEqual(quick['wait_max_seconds'],100)
        self.assertEqual(heavy['short_quick_checks']['jobs'],0)
        self.assertIsNone(heavy['short_quick_checks']['wait_p95_seconds'])


if __name__=='__main__':
    unittest.main()

    def test_capacity_explains_protected_cpu_and_memory_despite_free_global_slots(self):
        config=dict(self.store.config(),protected_quick=True,max_running=4,quick_reserve_slots=2,
                    quick_reserve_jobs=2,quick_reserve_memory_gb=4)
        row=dict(lane='heavy',slots=2,memory_gb=8,created=0)
        running=[dict(lane='heavy',slots=3,memory_gb=8)]
        report=diagnostics.capacity(row,running,config,time.time())
        self.assertEqual(report['blocking_dimensions'],['heavy pool CPU slots','heavy pool memory GiB'])
        dimensions={d['name']:d for d in report['dimensions']}
        self.assertEqual(dimensions['global CPU slots']['remaining'],3)
        self.assertEqual(dimensions['heavy pool CPU slots']['remaining'],1)
        self.assertEqual(dimensions['heavy pool CPU slots']['deficit'],1)
        self.assertEqual(dimensions['heavy pool memory GiB']['deficit'],4)
        self.assertTrue(report['fits_alone'])

    def test_capacity_marks_never_fitting_requests_and_aged_soft_pool_behavior(self):
        config=dict(self.store.config(),protected_quick=True,quick_reserve_slots=2)
        row=dict(lane='heavy',slots=5,memory_gb=1,created=0)
        report=diagnostics.capacity(row,[],config,time.time())
        self.assertFalse(report['fits_alone'])
        self.assertEqual(report['blocking_dimensions'],['heavy pool CPU slots'])
        config['protected_quick']=False
        report=diagnostics.capacity(row,[],config,time.time())
        self.assertTrue(report['fits_alone'])
        self.assertEqual(report['blocking_dimensions'],[])
        row['created']=time.time()
        self.assertFalse(diagnostics.capacity(row,[],config,time.time())['fits_alone'])

    def test_quick_capacity_includes_global_reservations_without_heavy_pool_limits(self):
        config=dict(self.store.config(),protected_quick=True,max_running=2)
        row=dict(lane='quick',slots=1,memory_gb=2,created=0)
        running=[dict(lane='heavy',slots=3,memory_gb=8),dict(lane='quick',slots=3,memory_gb=8)]
        report=diagnostics.capacity(row,running,config,time.time())
        self.assertEqual(set(report['blocking_dimensions']),{'global CPU slots','global memory GiB','global job count'})
        self.assertEqual(len(report['dimensions']),3)

    def test_capacity_report_is_read_only_and_counts_cancelling_reservations(self):
        busy=self.job('busy')
        self.store.db.execute("UPDATE jobs SET state='cancelling' WHERE id=?",(busy,))
        waiting=self.job('waiting')
        before=self.store.db.total_changes
        result=diagnostics.report(self.store,waiting,repo='/other-repo')
        self.assertEqual(self.store.db.total_changes,before)
        dimensions={d['name']:d for d in result['jobs'][0]['capacity']['dimensions']}
        self.assertEqual(dimensions['global job count']['used'],1)
        self.assertEqual(dimensions['global CPU slots']['used'],1)
        self.assertFalse(result['changes_applied'])
