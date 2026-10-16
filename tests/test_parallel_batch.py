"""Parallel checks share one reservation and retain independent native outcomes."""
import json
import importlib.util
import io
import subprocess
import tarfile
import tempfile
import os
from pathlib import Path
import signal
import sys
import time
import unittest

from agent_builds import batch
from agent_builds.runner import cancel_job
import test_mcp
import test_plans


class ParallelBatchTests(unittest.TestCase):
    setUp = test_plans.PlanTests.setUp
    tearDown = test_plans.PlanTests.tearDown
    git = test_plans.PlanTests.git
    cli = test_plans.PlanTests.cli

    def check(self, name, code):
        return dict(name=name,command=[sys.executable,'-c',code])

    def launch(self, checks, parallel=2, keep=True, jobs=2, timeout=8):
        manifest = self.root/'checks.json'
        manifest.write_text(json.dumps(dict(checks=checks,max_parallel=parallel,keep_going=keep)))
        result = self.cli('batch','run',str(manifest),'--cwd',str(self.repo),'--jobs',str(jobs),
            '--memory-gb','.5','--timeout',str(timeout),'--detach','--events-json')
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stderr)['job_id']

    def finish(self, handle):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            row=self.store.get(handle)
            if row['state'] in ('succeeded','failed','cancelled','lost'):
                return row
            time.sleep(.025)
        self.fail('Batch did not settle: '+str(row))

    def ready(self, handle, count=2):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            row=self.store.get(handle)
            if sum(c['state']=='running' for c in row['batch'].get('checks',[]))==count:
                return row
            time.sleep(.02)
        self.fail('Batch did not launch expected checks')

    def test_independent_checks_overlap_under_one_reservation_with_divided_worker_hints(self):
        code='import os,time; print(os.environ["CARGO_BUILD_JOBS"],os.environ["RUST_TEST_THREADS"]); time.sleep(.25)'
        handle=self.launch([self.check(str(i),code) for i in range(4)])
        row=self.finish(handle)
        self.assertEqual(row['exit_code'],0)
        self.assertEqual(len(self.store.rows()),1)
        checks=row['batch']['checks']
        self.assertLess(max(c['started'] for c in checks[:2]),min(c['finished'] for c in checks[:2]))
        self.assertGreaterEqual(checks[2]['started'],min(c['finished'] for c in checks[:2]))
        events=sorted([(c['started'],1) for c in checks]+[(c['finished'],-1) for c in checks])
        active=0
        for unused,delta in events:
            active+=delta
            self.assertLessEqual(active,2)
        self.assertEqual(row['batch']['slots_per_check'],1)
        for check in checks:
            self.assertEqual(check['worker_budget']['environment'],dict(CARGO_BUILD_JOBS='1',RUST_TEST_THREADS='1'))
        log=Path(row['log_path']).read_text()
        self.assertEqual(log.count('1 1'),4)
        self.assertIn('[buildr check: 0]',log)

    def test_aggregate_exit_is_first_failure_in_manifest_order_despite_completion_order(self):
        row=self.finish(self.launch([self.check('first','import time; time.sleep(.25); raise SystemExit(7)'),
            self.check('second','raise SystemExit(9)'),self.check('last','print("still runs")')]))
        self.assertEqual(row['exit_code'],7)
        self.assertEqual([c['exit_code'] for c in row['batch']['checks']],[7,9,0])
        self.assertLess(row['batch']['checks'][1]['finished'],row['batch']['checks'][0]['finished'])

    def test_fail_fast_skips_future_checks_and_preserves_already_running_sibling(self):
        marker=self.repo/'out'
        marker.mkdir()
        row=self.finish(self.launch([self.check('fail','import time; time.sleep(.12); raise SystemExit(7)'),
            self.check('running','import time; from pathlib import Path; time.sleep(.3); Path("out/finished").touch()'),
            self.check('never','from pathlib import Path; Path("out/never").touch()')],keep=False))
        self.assertEqual([c['state'] for c in row['batch']['checks']],['failed','succeeded','skipped'])
        self.assertTrue((marker/'finished').exists())
        self.assertFalse((marker/'never').exists())
        self.assertEqual(row['exit_code'],7)

    def test_missing_executable_stops_new_launches_without_inventing_native_success(self):
        row=self.finish(self.launch([dict(name='missing',command=['/buildr-missing-executable']),
            self.check('never','raise SystemExit(99)')],keep=False))
        self.assertEqual(row['exit_code'],127)
        self.assertEqual([c['exit_code'] for c in row['batch']['checks']],[127,None])

    def test_timeout_terminates_both_checks_and_future_work(self):
        (self.repo/'out').mkdir()
        code='import time; from pathlib import Path; time.sleep(.8); Path("out/escaped").touch()'
        row=self.finish(self.launch([self.check('one',code),self.check('two',code),self.check('later',code)],timeout=.15))
        self.assertEqual(row['exit_code'],124)
        time.sleep(.9)
        self.assertFalse((self.repo/'out'/'escaped').exists())
        self.assertNotEqual(row['batch']['checks'][2]['state'],'succeeded')

    def test_slow_telemetry_cannot_delay_parallel_timeout(self):
        (self.repo/'out').mkdir()
        marker=self.repo/'out'/'escaped-slow-sampler'
        child_code='import time; from pathlib import Path; time.sleep(.8); Path({!r}).touch()'.format(str(marker))
        manifest=self.root/'slow-checks.json'
        manifest.write_text(json.dumps(dict(max_parallel=2,checks=[self.check('one',child_code),
            self.check('two',child_code),self.check('later',child_code)])))
        wrapper = (
            'import sys,time; from agent_builds import telemetry; '
            'original=telemetry.JobSampler.sample; '
            'telemetry.JobSampler.sample=lambda self: (time.sleep(1.4),original(self))[1]; '
            'from agent_builds.__main__ import main; sys.exit(main())')
        result=subprocess.run([sys.executable,'-c',wrapper,'batch','run',str(manifest),
            '--cwd',str(self.repo),'--jobs','2','--memory-gb','.5','--timeout','.15'],
            env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
        self.assertEqual(result.returncode,124,result.stderr)
        self.assertFalse(marker.exists(), 'Slow telemetry delayed process-group timeout')
        row=self.store.rows()[0]
        self.assertEqual(row['exit_code'],124)
        self.assertNotEqual(row['batch']['checks'][2]['state'],'succeeded')

    def test_post_reap_telemetry_error_cannot_signal_reused_process_group(self):
        marker=self.root/'late-group-signal'
        wrapper = """import os,signal,sys
from pathlib import Path
from agent_builds import telemetry
original_sample=telemetry.JobSampler.result
original_signal=os.killpg
marker=Path(sys.argv.pop(1))
def result(self, finished=False):
    if finished:
        raise RuntimeError('deliberate post-reap telemetry failure')
    return original_sample(self, finished)
def send(pid, signum):
    if signum==signal.SIGTERM:
        marker.touch()
    return original_signal(pid, signum)
telemetry.JobSampler.result=result
os.killpg=send
from agent_builds.__main__ import main
sys.exit(main())
"""
        result=subprocess.run([sys.executable,'-c',wrapper,str(marker),'run',
            '--cwd',str(self.repo),'--lane','quick','--jobs','1','--memory-gb','.25',
            '--timeout','8','--','true'],env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=12)
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertFalse(marker.exists(), 'Cleanup signalled a process group after reaping its leader')
        self.assertEqual(self.store.rows()[0]['exit_code'],1)

    def test_cancel_terminates_all_running_siblings(self):
        handle=self.launch([self.check('one','import time; time.sleep(10)'),self.check('two','import time; time.sleep(10)'),
            self.check('later','raise SystemExit(99)')],timeout=15)
        self.ready(handle)
        cancel_job(self.store,handle)
        row=self.finish(handle)
        self.assertEqual(row['exit_code'],130)
        self.assertNotEqual(row['batch']['checks'][2]['state'],'succeeded')
        self.assertFalse(any(c['exit_code']==99 for c in row['batch']['checks']))

    def test_worker_crash_keeps_both_outcomes_unknown_and_reaps_group_before_release(self):
        (self.repo/'out').mkdir()
        code='import time; from pathlib import Path; time.sleep(.8); Path("out/escaped").touch()'
        handle=self.launch([self.check('one',code),self.check('two',code),self.check('later',code)])
        row=self.ready(handle)
        os.kill(row['child_pid'],signal.SIGKILL)  # isolated fixture worker only
        row=self.finish(handle)
        self.assertEqual(row['exit_code'],137)
        self.assertEqual([c['state'] for c in row['batch']['checks']],['unknown','unknown','skipped'])
        time.sleep(.9)
        self.assertFalse((self.repo/'out'/'escaped').exists())
        self.assertEqual(len(self.store.rows()),1)

    def test_output_larger_than_pipes_is_drained_from_both_checks(self):
        row=self.finish(self.launch([self.check('one','print("a"*200000)'),self.check('two','print("b"*200000)')]))
        self.assertEqual(row['exit_code'],0)
        log=Path(row['log_path']).read_text()
        self.assertIn('[buildr check: one]',log)
        self.assertIn('[buildr check: two]',log)
        self.assertIn('a'*1000,log)
        self.assertIn('b'*1000,log)

    def test_invalid_parallelism_and_budget_reject_before_submission(self):
        for value in (0,17,True,1.5,'2'):
            with self.assertRaises(ValueError):
                batch.validate(dict(checks=[self.check('one','pass')],max_parallel=value))
        manifest=self.root/'invalid.json'
        manifest.write_text(json.dumps(dict(checks=[self.check('one','pass')],max_parallel=2)))
        result=self.cli('batch','run',str(manifest),'--cwd',str(self.repo),'--jobs','1')
        self.assertEqual(result.returncode,2)
        self.assertEqual(self.store.rows(),[])

    def test_actual_previous_worker_rejects_parallel_manifest_and_accepts_default_encoding(self):
        source=Path(__file__).resolve().parent.parent
        archive=subprocess.check_output(['git','archive','v0.17.0','agent_builds'],cwd=str(source))
        with tempfile.TemporaryDirectory() as directory:
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                bundle.extractall(directory)
            package=Path(directory)/'agent_builds'
            alias='buildr_previous_batch_017'
            spec=importlib.util.spec_from_file_location(alias,package/'__init__.py',submodule_search_locations=[str(package)])
            module=importlib.util.module_from_spec(spec)
            sys.modules[alias]=module
            spec.loader.exec_module(module)
            old=__import__(alias+'.batch',fromlist=['validate'])
            value=dict(checks=[self.check('one','pass')])
            self.assertEqual(old.validate(value),batch.validate(value))
            with self.assertRaises(ValueError):
                old.validate(dict(value,max_parallel=2))

    def test_default_sequential_encoding_and_execution_remain_compatible(self):
        value=batch.validate(dict(checks=[self.check('one','pass')]))
        self.assertNotIn('max_parallel',value)
        row=self.finish(self.launch([self.check('one','import time; time.sleep(.1)'),self.check('two','pass')],parallel=1))
        checks=row['batch']['checks']
        self.assertGreaterEqual(checks[1]['started'],checks[0]['finished'])
        self.assertEqual(row['batch']['slots_per_check'],2)


class ParallelMCPTests(unittest.TestCase):
    setUp=test_mcp.MCPTests.setUp
    tearDown=test_mcp.MCPTests.tearDown
    client=test_mcp.MCPTests.client
    send=test_mcp.MCPTests.send
    receive=test_mcp.MCPTests.receive
    request=test_mcp.MCPTests.request
    invoke=test_mcp.MCPTests.invoke

    def test_parallel_batch_reconnect_is_one_job_and_bad_budget_does_not_bind_key(self):
        client=self.client()
        manifest=dict(max_parallel=2,checks=[dict(name=str(i),command=[sys.executable,'-c','import time; time.sleep(.1)']) for i in range(3)])
        args=dict(request_key='parallel-batch-fixture',cwd=str(self.root),jobs=1,memory_gb=.5,manifest=manifest)
        rejected=self.invoke(client,'buildr_batch_submit',args)['result']
        self.assertTrue(rejected['isError'])
        self.assertEqual(len(self.store.rows()),0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM mcp_requests').fetchone()[0],0)
        args['jobs']=2
        first=self.invoke(client,'buildr_batch_submit',args)['result']['structuredContent']
        client.stdin.close()
        client.wait(timeout=5)
        client=self.client()
        repeat=self.invoke(client,'buildr_batch_submit',args)['result']['structuredContent']
        handle=first['result']['job_id']
        self.assertEqual(repeat['result']['job_id'],handle)
        result=self.invoke(client,'buildr_wait',dict(job_ids=[handle],timeout=5))['result']['structuredContent']
        self.assertEqual(result['exit_code'],0)
        self.assertEqual(self.store.get(handle)['batch']['max_parallel'],2)
        self.assertEqual(len(self.store.rows()),1)
