"""Preflight reads a bounded snapshot and never submits or reserves native work."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from agent_builds import mcp, preflight
from agent_builds.__main__ import main
from agent_builds.store import Store
import test_mcp

ROOT=Path(__file__).resolve().parent.parent


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'state')
        self.store.update_config(dict(cpu_slots=3,max_running=3,max_heavy=2,memory_gb=4,
            min_free_gb=0,min_available_gb=0,quick_reserve_slots=0,quick_reserve_jobs=0,
            quick_reserve_memory_gb=0,nice=0))
        self.machine=mock.patch('agent_builds.preflight.snapshot',return_value=dict(available_gb=32,free_gb=32))
        self.disk=mock.patch('agent_builds.preflight.shutil.disk_usage',return_value=type('Disk',(),dict(free=32*1024**3))())
        self.machine.start()
        self.disk.start()

    def tearDown(self):
        self.disk.stop()
        self.machine.stop()
        self.store.close()
        self.temp.cleanup()

    def request(self,**values):
        return dict(dict(cwd=str(self.root),lane='quick',jobs=1,memory_gb=.25,resource_key='candidate'),**values)

    def job(self,key='busy',slots=1,lane='quick',memory=.25,**extra):
        return self.store.submit(self.root,['true'],'local',lane,slots,memory,key,'',30,**extra)

    def test_projection_matches_real_claim_when_capacity_fits_and_when_it_does_not(self):
        busy=self.job(slots=2)
        self.assertTrue(self.store.claim(busy))
        result=preflight.report(self.store,self.request(jobs=2,compare_jobs=[1]))
        self.assertEqual([c['fits_now'] for c in result['cases']],[False,True])
        self.assertIn('global CPU slots',result['cases'][0]['blocking_facts'])
        one=self.job('candidate',slots=1)
        self.assertTrue(self.store.claim(one))
        two=self.job('other-candidate',slots=2)
        self.assertFalse(self.store.claim(two))
        self.store.finish(one,'succeeded',0)
        self.store.finish(busy,'succeeded',0)
        self.assertTrue(self.store.claim(two))

    def test_aged_head_draining_agrees_with_real_claim(self):
        busy=self.job('busy',slots=2,lane='heavy')
        self.assertTrue(self.store.claim(busy))
        head=self.job('head',slots=3,lane='heavy')
        self.store.db.execute('UPDATE jobs SET created=? WHERE id=?',(time.time()-200,head))
        result=preflight.report(self.store,self.request())
        self.assertFalse(result['cases'][0]['fits_now'])
        self.assertEqual(result['cases'][0]['selected'],head)
        actual=self.job('candidate')
        self.assertFalse(self.store.claim(actual))

    def test_safe_backfill_projection_agrees_with_real_claim(self):
        busy=self.job('busy',slots=2,lane='heavy')
        self.assertTrue(self.store.claim(busy))
        head=self.job('head',slots=2,lane='heavy')
        self.store.db.execute('UPDATE jobs SET created=? WHERE id=?',(time.time()-200,head))
        result=preflight.report(self.store,self.request())
        self.assertTrue(result['cases'][0]['fits_now'])
        actual=self.job('candidate')
        self.assertTrue(self.store.claim(actual))

    def test_submission_and_command_are_never_created_or_executed(self):
        marker=self.root/'must-not-exist'
        before=self.store.db.total_changes
        result=preflight.report(self.store,self.request(command=[sys.executable,'-c',
            'from pathlib import Path; Path({!r}).touch()'.format(str(marker))]))
        self.assertTrue(result['cases'][0]['fits_now'])
        self.assertEqual(self.store.db.total_changes,before)
        self.assertEqual(self.store.rows(),[])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM mcp_requests').fetchone()[0],0)
        self.assertFalse(marker.exists())
        self.assertFalse(result['commands_submitted'])

    def test_fifo_multiple_locks_and_running_locks_match_actual_claim(self):
        first=self.job('older',locks=['shared'])
        result=preflight.report(self.store,self.request(locks=['shared']))
        self.assertFalse(result['cases'][0]['fits_now'])
        self.assertEqual(result['cases'][0]['lock_blockers'][0]['job_id'],first)
        self.assertTrue(self.store.claim(first))
        result=preflight.report(self.store,self.request(locks=['shared']))
        self.assertIn('running lock',result['cases'][0]['blocking_facts'])
        later=self.job('candidate',locks=['shared'])
        self.assertFalse(self.store.claim(later))

    def test_legacy_multi_lock_drain_is_visible_before_submission(self):
        first=self.job('legacy')
        self.store.db.execute('UPDATE jobs SET lock_protocol=0 WHERE id=?',(first,))
        result=preflight.report(self.store,self.request(locks=['legacy']))
        self.assertEqual(result['cases'][0]['reason'],'legacy lock')
        self.assertFalse(result['cases'][0]['fits_now'])

    def test_pending_policy_is_unknown_and_does_not_activate_it(self):
        legacy=self.job()
        self.store.db.execute('UPDATE jobs SET reserve_protocol=0 WHERE id=?',(legacy,))
        self.store.update_config(dict(protected_quick=True))
        before=self.store.db.total_changes
        result=preflight.report(self.store,self.request(resource_key='independent'))
        self.assertTrue(result['policy_pending'])
        self.assertIsNone(result['cases'][0]['fits_now'])
        self.assertEqual(result['cases'][0]['reason'],'policy activation pending')
        self.assertEqual(self.store.db.total_changes,before)
        self.assertFalse(self.store.config()['protected_quick'])

    def test_protected_pool_and_invalid_future_request_are_distinct(self):
        self.store.update_config(dict(quick_reserve_slots=1,quick_reserve_jobs=1,protected_quick=True))
        busy=self.job(lane='heavy',slots=1)
        self.assertTrue(self.store.claim(busy))
        result=preflight.report(self.store,self.request(lane='heavy',jobs=2,compare_jobs=[1,3]))
        self.assertEqual([c['submission_acceptable'] for c in result['cases']],[True,True,False])
        self.assertEqual([c['fits_now'] for c in result['cases']],[False,True,False])
        self.assertIn('heavy pool CPU slots',result['cases'][0]['blocking_facts'])

    def test_disk_and_ram_deficits_include_global_cancelling_reservations(self):
        busy=self.job(scratch_gb=2)
        self.store.db.execute("UPDATE jobs SET state='cancelling' WHERE id=?",(busy,))
        self.store.update_config(dict(min_free_gb=10,min_available_gb=2))
        usage=type('Disk',(),dict(free=11*1024**3))()
        with mock.patch('agent_builds.preflight.shutil.disk_usage',return_value=usage), \
                mock.patch('agent_builds.preflight.snapshot',return_value=dict(available_gb=2.1)):
            result=preflight.report(self.store,self.request(memory_gb=1,scratch_gb=1))
        case=result['cases'][0]
        self.assertEqual(case['disk']['required_gb'],13)
        self.assertEqual(case['disk']['deficit_gb'],2)
        self.assertAlmostEqual(case['ram']['deficit_gb'],.9)
        self.assertIn('disk headroom',case['blocking_facts'])
        self.assertIn('available RAM headroom',case['blocking_facts'])
        self.assertFalse(case['fits_now'])

    def test_pause_ordering_and_cpu_options_are_explicit(self):
        head=self.job('other')
        result=preflight.report(self.store,self.request(command=['cargo','test','-j8']))
        case=result['cases'][0]
        self.assertEqual(case['selected'],head)
        self.assertFalse(case['fits_now'])
        self.assertTrue(case['worker_budget']['cargo_build']['exceeds_reservation'])
        self.assertIn('requests 8 workers',case['worker_warnings'][0])
        self.assertTrue(any('Worker warning' in line for line in preflight.lines(result)))
        self.store.update_config(dict(paused=True))
        self.assertEqual(preflight.report(self.store,self.request())['cases'][0]['reason'],'queue paused')

    def test_batch_options_and_compared_budgets_preserve_parallel_minimum(self):
        manifest=dict(max_parallel=2,checks=[dict(name=str(i),command=['true']) for i in range(3)])
        result=preflight.report(self.store,self.request(jobs=2,compare_jobs=[1,2,3],manifest=manifest))
        self.assertEqual([c['jobs'] for c in result['cases']],[2,1,3])
        self.assertEqual([c['submission_acceptable'] for c in result['cases']],[True,False,True])
        self.assertEqual(result['cases'][0]['batch']['slots_per_check'],1)
        self.assertIsNone(result['cases'][1]['batch']['slots_per_check'])
        self.assertFalse(result['cases'][1]['fits_now'])

    def test_batch_worker_flags_are_checked_against_divided_hint(self):
        manifest=dict(max_parallel=2,checks=[dict(name='cargo',command=['cargo','test','-j2','--','--test-threads=3']),
                                           dict(name='safe',command=['git','diff','--check'])])
        result=preflight.report(self.store,self.request(jobs=2,manifest=manifest))
        self.assertEqual(len(result['cases'][0]['worker_warnings']),2)
        self.assertTrue(all('1 CPU worker slots' in warning for warning in result['cases'][0]['worker_warnings']))
        self.assertEqual(manifest['checks'][0]['command'][2],'-j2')

    def test_unknown_host_data_and_large_snapshots_never_claim_fit(self):
        with mock.patch('agent_builds.preflight.shutil.disk_usage',side_effect=PermissionError):
            result=preflight.report(self.store,self.request())
        self.assertIsNone(result['cases'][0]['fits_now'])
        self.assertIsNone(result['cases'][0]['disk']['available_gb'])
        for index in range(4):
            self.job(str(index))
        with mock.patch('agent_builds.preflight.MAX_ACTIVE',2):
            result=preflight.report(self.store,self.request())
        self.assertFalse(result['snapshot_complete'])
        self.assertEqual(result['active_matching'],4)
        self.assertEqual(result['active_sampled'],2)
        self.assertIsNone(result['cases'][0]['fits_now'])
        self.assertIsNone(result['cases'][0]['reservation_arithmetic'])

    def test_missing_state_does_not_initialize_and_uses_existing_parent_for_disk_probe(self):
        missing=self.root/'absent'/'queue'
        reader=Store(missing,read_only=True)
        try:
            result=preflight.report(reader,self.request())
            self.assertFalse(result['initialized'])
            self.assertFalse(missing.parent.exists())
            self.assertEqual(result['state_probe_path'],str(self.root))
            self.assertTrue(result['cases'][0]['fits_now'])
        finally:
            reader.close()

    def test_projection_does_not_load_large_evidence_or_emit_source_contents(self):
        handle=self.job()
        self.store.db.execute('UPDATE jobs SET source=? WHERE id=?',(json.dumps(dict(secret='x'*1000000)),handle))
        statements=[]
        self.store.db.set_trace_callback(statements.append)
        try:
            result=preflight.report(self.store,self.request())
        finally:
            self.store.db.set_trace_callback(None)
        self.assertLess(len(json.dumps(result)),12000)
        self.assertFalse(any('SELECT *' in query.upper() for query in statements))
        self.assertNotIn('secret',json.dumps(result))

    def test_cli_and_batch_cli_are_read_only_even_inside_an_existing_job(self):
        manifest=self.root/'checks.json'
        manifest.write_text(json.dumps(dict(max_parallel=2,checks=[dict(name='one',command=['true'])])))
        env=dict(AGENT_BUILDS_STATE=str(self.store.root),AGENT_BUILDS_JOB_ID='outer-test-job')
        for argv in (['preflight','--cwd',str(self.root),'--lane','quick','--jobs','1','--json'],
                     ['batch','preflight',str(manifest),'--cwd',str(self.root),'--jobs','2','--compare-jobs','1','--json']):
            with mock.patch.dict(os.environ,env),mock.patch('sys.argv',['buildr']+argv),mock.patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(main(),0)
            self.assertEqual(json.loads(output.getvalue())['schema'],'buildr-preflight-v1')
        self.assertEqual(self.store.rows(),[])

    def test_invalid_shapes_do_not_write(self):
        for values in (dict(jobs=True),dict(jobs=0),dict(memory_gb=float('nan')),dict(compare_jobs=[0]),
                       dict(compare_jobs=[1]*9),dict(command=['']),dict(command=['true'],manifest=dict(checks=[])),
                       dict(locks=['']),dict(cwd=str(self.root/'missing'))):
            before=self.store.db.total_changes
            with self.assertRaises(ValueError):
                preflight.report(self.store,self.request(**values))
            self.assertEqual(self.store.db.total_changes,before)

    def test_old_schema_inspection_preserves_columns_and_triggers(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=subprocess.check_output(['git','archive','v0.15.0','agent_builds'],cwd=str(ROOT))
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                bundle.extractall(directory)
            path=Path(directory)/'agent_builds'
            alias='buildr_preflight_old_015'
            spec=importlib.util.spec_from_file_location(alias,path/'__init__.py',submodule_search_locations=[str(path)])
            package=importlib.util.module_from_spec(spec)
            sys.modules[alias]=package
            spec.loader.exec_module(package)
            old_class=__import__(alias+'.store',fromlist=['Store']).Store
            old=old_class(self.root/'old-state')
            old.submit(self.root,['true'],'local','quick',1,.25,'old','',30)
            before=[tuple(r) for r in old.db.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]
            reader=Store(old.root,read_only=True)
            try:
                result=preflight.report(reader,self.request())
                after=[tuple(r) for r in reader.db.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]
                self.assertEqual(before,after)
                self.assertEqual(result['active_matching'],1)
                self.assertFalse(result['protocol_compatible'])
                self.assertIsNone(result['cases'][0]['fits_now'])
                self.assertEqual(result['cases'][0]['reason'],'policy upgrade pending')
                self.assertNotIn('required_protocol',{r[1] for r in reader.db.execute('PRAGMA table_info(admission_policy)')})
            finally:
                reader.close()
                old.close()


class PreflightMCPTests(unittest.TestCase):
    setUp=test_mcp.MCPTests.setUp
    tearDown=test_mcp.MCPTests.tearDown
    client=test_mcp.MCPTests.client
    send=test_mcp.MCPTests.send
    receive=test_mcp.MCPTests.receive
    request=test_mcp.MCPTests.request
    invoke=test_mcp.MCPTests.invoke

    def test_read_only_server_preflight_keeps_missing_state_and_returns_batch_comparisons(self):
        missing=self.root/'missing'
        client=self.client(read_only=True,env=dict(self.env,AGENT_BUILDS_STATE=str(missing)))
        result=self.invoke(client,'buildr_preflight',dict(cwd=str(self.root),jobs=2,compare_jobs=[1],
            manifest=dict(max_parallel=2,checks=[dict(name='one',command=['true'])])))['result']
        self.assertFalse(result['isError'],result)
        report=result['structuredContent']
        self.assertEqual([c['submission_acceptable'] for c in report['cases']],[True,False])
        self.assertFalse(report['initialized'])
        self.assertFalse(missing.exists())
        self.assertFalse(report['commands_submitted'])
