import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_builds import external, mcp
from agent_builds.store import Store

ROOT = str(Path(__file__).resolve().parent.parent)


class ExternalWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state')
        self.store.update_config(dict(cpu_slots=2,max_running=2,max_heavy=2,memory_gb=4,
            min_free_gb=0,min_available_gb=0,quick_reserve_slots=0,quick_reserve_jobs=0,
            quick_reserve_memory_gb=0,nice=0))
        self.env = dict(os.environ,AGENT_BUILDS_STATE=str(self.store.root),PYTHONPATH=ROOT)
        self.env.pop('AGENT_BUILDS_JOB_ID',None)  # explicitly isolated fixture submissions
        self.children = []
        self.handles = []
        self.ready = self.root/'external-ready'

    def tearDown(self):
        with mock.patch.dict(os.environ,self.env,clear=True):
            for handle in self.handles:
                row=external.raw(self.store,handle)
                if row['state'] not in external.FINISHED:
                    child=external.cancel(self.store,handle)
                    if child is not None:
                        self.children.append(child)
            deadline=time.monotonic()+8
            while any(external.raw(self.store,h)['state'] not in external.FINISHED for h in self.handles) and time.monotonic()<deadline:
                time.sleep(.03)
        for child in self.children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        self.store.close()
        self.temp.cleanup()

    def spec(self,**changes):
        command=[sys.executable,'-c','import sys; from pathlib import Path; sys.exit(0 if Path({!r}).exists() else 75)'.format(str(self.ready))]
        result=dict(cwd=str(self.root),command=command,read_only_probe=True,external_ref='fixture:external-task',
            interval=.2,timeout=10,probe_timeout=2,max_probes=10,jobs=1,memory_gb=.25,track_source=False)
        result.update(changes)
        return result

    def create(self,spec=None,start=False):
        with mock.patch.dict(os.environ,self.env,clear=True):
            handle=external.create(self.store,spec or self.spec())
            self.handles.append(handle)
            if start:
                self.children.append(external.spawn(self.store,handle))
        return handle

    def observe(self,handle,states,timeout=8):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            row=external.get(self.store,handle)
            if row['state'] in states:
                return row
            time.sleep(.02)
        self.fail('Watch did not reach {}: {}'.format(states,external.get(self.store,handle)))

    def test_repeatable_read_only_contract_and_bounded_inputs_are_required(self):
        for changes in (dict(read_only_probe=False),dict(interval=True),dict(timeout=float('nan')),
                        dict(command='shell string'),dict(command=[]),dict(jobs=3),dict(memory_gb=5),
                        dict(external_ref='unsafe\x1b'),dict(unknown=True)):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                external.validate(self.spec(**changes),self.store.config())
        self.assertEqual(external.listing(self.store),[])

    def test_pending_probe_releases_capacity_for_an_entire_machine_budget(self):
        handle=self.create(self.spec(interval=3,timeout=15),start=True)
        waiting=self.observe(handle,{'waiting'})
        first=waiting['probes'][0]
        self.assertEqual((first['native_exit_code'],first['semantic_result']),(75,'pending'))
        self.assertEqual(self.store.counts(active=True)['running'],0)
        command=[sys.executable,'-m','agent_builds','run','--cwd',str(self.root),'--resource-key','independent-output',
                 '--lane','quick','--jobs','2','--memory-gb','.5','--',sys.executable,'-c','print("full budget admitted")']
        completed=subprocess.run(command,env=self.env,capture_output=True,timeout=8)
        self.assertEqual(completed.returncode,0,completed.stderr)
        normal=next(row for row in self.store.rows() if row['resource_key']=='independent-output')
        self.ready.touch()  # fixture actor changes external state; probes only read it
        result=self.observe(handle,{'succeeded'})
        self.assertEqual(result['id'],handle)
        self.assertEqual(result['probe_count'],2)
        self.assertEqual(result['exit_code'],0)
        self.assertGreater(result['external_idle_seconds'],1)
        self.assertLess(result['accounting']['local_reserved_seconds'],result['elapsed_seconds'])
        second_job=self.store.get(result['probes'][-1]['job_id'])
        self.assertLessEqual(normal['finished'],second_job['started'])
        self.assertGreaterEqual(normal['started'],self.store.get(first['job_id'])['finished'])
        self.assertTrue(result['accounting_complete'])

    def test_nonpending_probe_failure_is_not_repeated(self):
        handle=self.create(self.spec(command=[sys.executable,'-c','raise SystemExit(7)']),start=True)
        result=self.observe(handle,{'failed'})
        self.assertEqual(result['exit_code'],7)
        self.assertEqual(result['probe_count'],1)
        self.assertEqual(result['external_outcome'],'unknown')
        with self.assertRaises(ValueError):
            external.spawn(self.store,handle)

    def test_pending_probe_limit_stops_with_pending_native_result(self):
        handle=self.create(self.spec(max_probes=2),start=True)
        result=self.observe(handle,{'exhausted'})
        self.assertEqual((result['probe_count'],result['exit_code']),(2,75))
        self.assertEqual(result['external_outcome'],'pending_reported')
        self.assertEqual(self.store.counts(active=True)['running'],0)

    def test_deadline_stops_observation_without_reporting_external_failure(self):
        handle=self.create(self.spec(interval=5,timeout=.8),start=True)
        result=self.observe(handle,{'timed_out'})
        self.assertEqual(result['exit_code'],124)
        self.assertEqual(result['probe_count'],1)
        self.assertNotEqual(result['external_outcome'],'completion_reported')

    def test_probe_timeout_is_terminal_and_is_not_polled_again(self):
        handle=self.create(self.spec(command=[sys.executable,'-c','import time; time.sleep(5)'],probe_timeout=.15),start=True)
        result=self.observe(handle,{'cancelled','failed'})
        self.assertEqual(result['exit_code'],124)
        self.assertEqual(result['probe_count'],1)

    def test_idle_cancellation_creates_no_more_probes_and_preserves_native_pending_exit(self):
        handle=self.create(self.spec(interval=5),start=True)
        pending=self.observe(handle,{'waiting'})
        with mock.patch.dict(os.environ,self.env,clear=True):
            external.cancel(self.store,handle)
            external.cancel(self.store,handle)
        result=self.observe(handle,{'cancelled'})
        self.assertEqual(result['probe_count'],1)
        self.assertEqual(result['exit_code'],130)
        self.assertEqual(self.store.get(pending['probes'][0]['job_id'])['exit_code'],75)
        self.assertFalse(self.ready.exists())

    def test_running_probe_cancellation_preserves_unrelated_job(self):
        handle=self.create(self.spec(command=[sys.executable,'-c','import time; time.sleep(10)']),start=True)
        deadline=time.monotonic()+8
        job=None
        while time.monotonic()<deadline:
            row=external.get(self.store,handle)
            if row['current_job_id']:
                job=self.store.get(row['current_job_id'])
                if job['child_pid']:
                    break
            time.sleep(.02)
        self.assertTrue(job and job['child_pid'])
        other=self.store.submit(self.root,['true'],'local','quick',1,.25,'unrelated','',10)
        with mock.patch.dict(os.environ,self.env,clear=True):
            external.cancel(self.store,handle)
        result=self.observe(handle,{'cancelled'})
        self.assertEqual(self.store.get(job['id'])['exit_code'],130)
        self.assertFalse(self.store.get(other)['cancel_requested'])
        self.assertEqual(result['probe_count'],1)

    def test_controller_restart_observes_the_existing_running_probe_without_duplicate(self):
        command=[sys.executable,'-c','import time,sys; from pathlib import Path; time.sleep(1); sys.exit(0 if Path({!r}).exists() else 75)'.format(str(self.ready))]
        handle=self.create(self.spec(command=command),start=True)
        deadline=time.monotonic()+8
        job=None
        while time.monotonic()<deadline:
            current=external.get(self.store,handle)
            if current['current_job_id']:
                job=self.store.get(current['current_job_id'])
                if job['child_pid']:
                    break
            time.sleep(.02)
        self.assertTrue(job and job['child_pid'])
        first=self.children[0]
        first.kill()
        first.wait(timeout=5)
        self.ready.touch()
        with mock.patch.dict(os.environ,self.env,clear=True):
            self.children.append(external.spawn(self.store,handle))
        result=self.observe(handle,{'succeeded'})
        self.assertEqual(result['probe_count'],1)
        self.assertEqual(result['probes'][0]['job_id'],job['id'])
        self.assertEqual(len(self.store.rows()),1)

    def test_unknown_probe_outcome_stops_automatic_progression(self):
        handle=self.create()
        probe=external.prepare(self.store,handle)
        self.store.finish(probe['job_id'],'lost',None,'fixture unknown outcome')
        with mock.patch('subprocess.Popen',side_effect=AssertionError('must not retry')):
            external.step(self.store,handle)
        result=external.get(self.store,handle)
        self.assertEqual(result['state'],'uncertain')
        self.assertEqual(result['probe_count'],1)
        with self.assertRaises(ValueError):
            external.spawn(self.store,handle)

    def test_prepared_worker_token_can_attach_only_once(self):
        handle=self.create()
        probe=external.prepare(self.store,handle)
        self.store.db.execute('UPDATE external_probes SET launch_attempted=1 WHERE job_id=?',(probe['job_id'],))
        args,job=external.attach_job(self.store,probe['job_id'],probe['launch_token'])
        self.assertEqual(args.jobs,1)
        self.assertEqual(job,probe['job_id'])
        with self.assertRaises(ValueError):
            external.attach_job(self.store,job,probe['launch_token'])
        self.store.finish(job,'failed',7,'known unlaunched unit fixture')
        external.step(self.store,handle)

    def test_cancellation_before_launch_serializes_against_dispatch(self):
        handle=self.create()
        probe=external.prepare(self.store,handle)
        with mock.patch.dict(os.environ,self.env,clear=True):
            self.children.append(external.cancel(self.store,handle))
        with mock.patch('subprocess.Popen',side_effect=AssertionError('no new probe')):
            self.assertIsNone(external.dispatch(self.store,external.raw(self.store,handle),probe))
        result=self.observe(handle,{'cancelled'})
        job=self.store.get(probe['job_id'])
        self.assertIsNone(job['started'])
        self.assertIsNone(job['child_pid'])
        self.assertEqual(job['exit_code'],130)
        self.assertEqual(result['probe_count'],1)

    def test_uncertain_launch_attempt_is_not_reexecuted_on_resume(self):
        handle=self.create()
        probe=external.prepare(self.store,handle)
        self.store.db.execute('UPDATE external_probes SET launch_attempted=1 WHERE job_id=?',(probe['job_id'],))
        with mock.patch('subprocess.Popen',side_effect=AssertionError('must not retry')):
            self.assertIsNone(external.dispatch(self.store,external.raw(self.store,handle),probe))
        self.store.db.execute('UPDATE jobs SET owner_start=? WHERE id=?',('impossible',probe['job_id']))
        self.store.recover()
        external.step(self.store,handle)
        self.assertEqual(external.get(self.store,handle)['state'],'uncertain')

    def test_wait_timeout_and_missing_state_inspection_are_read_only(self):
        handle=self.create()
        before=external.raw(self.store,handle)
        reader=Store(self.store.root,read_only=True)
        try:
            result=external.wait(reader,handle,.01)
        finally:
            reader.close()
        self.assertEqual(result['exit_code'],124)
        self.assertFalse(result['complete'])
        self.assertEqual(external.raw(self.store,handle),before)
        missing=self.root/'missing-state'
        reader=Store(missing,read_only=True)
        try:
            self.assertEqual(external.listing(reader),[])
            with self.assertRaises(ValueError):
                external.get(reader,handle)
        finally:
            reader.close()
        self.assertFalse(missing.exists())

    def test_deadline_retains_a_live_unsettled_probe_reservation(self):
        handle=self.create(self.spec(timeout=.2))
        probe=external.prepare(self.store,handle)
        self.store.db.execute("UPDATE jobs SET state='running',started=? WHERE id=?",(time.time(),probe['job_id']))
        self.store.db.execute('UPDATE external_probes SET launch_attempted=1,attached=1 WHERE job_id=?',(probe['job_id'],))
        self.store.db.execute('UPDATE external_watches SET created=? WHERE id=?',(time.time()-1,handle))
        with mock.patch('agent_builds.runner.cancel_job',return_value=None):
            external.step(self.store,handle)
        result=external.get(self.store,handle)
        self.assertEqual(result['state'],'cancelling')
        self.assertEqual(self.store.get(probe['job_id'])['state'],'running')
        self.assertEqual(result['probes'][0]['reserved_cpu_slots'],1)
        # This was a known in-process fixture reservation, never a live child.
        self.store.finish(probe['job_id'],'cancelled',130,'fixture settled')
        external.step(self.store,handle)
        self.assertEqual(external.get(self.store,handle)['state'],'timed_out')

    def test_mcp_watch_start_is_idempotent_and_exposes_read_only_observation(self):
        args=dict(self.spec(),request_key='watch-mcp-fixture-001')
        # Check the durable MCP contract independently of process admission.
        # Native controller/probe completion is covered by the integration tests
        # above; a busy host must not impose an eight-second startup assertion here.
        with mock.patch.dict(os.environ,self.env,clear=True), \
                mock.patch('agent_builds.external.spawn',return_value=None) as spawn:
            handle=None
            try:
                first=mcp.call('buildr_watch_submit',args)
                handle=first['result']['watch_id']
                self.handles.append(handle)
                repeated=mcp.call('buildr_watch_submit',args)
                self.assertEqual(first['submission_state'],'submitted')
                self.assertEqual(repeated,first)
                spawn.assert_called_once_with(mock.ANY,handle)
                before=external.raw(self.store,handle)
                observed=mcp.call('buildr_watch',dict(watch_id=handle),read_only=True)
                self.assertEqual(observed['id'],handle)
                self.assertEqual(observed['state'],'queued')
                self.assertEqual(observed['probe_count'],0)
                self.assertEqual([handle],[r['id'] for r in mcp.call('buildr_watches',{},read_only=True)['watches']])
                with self.assertRaises(ValueError):
                    mcp.call('buildr_watch_cancel',dict(watch_id=handle),read_only=True)
                self.assertEqual(external.raw(self.store,handle),before)
                self.assertEqual(self.store.rows(),[])
                spawn.assert_called_once_with(mock.ANY,handle)
            finally:
                if handle is not None:
                    # Settle only this deliberately unlaunched fixture while the
                    # spawn mock is still active, even if a contract assertion fails.
                    external.cancel(self.store,handle)
                    external.step(self.store,handle)
            self.assertEqual(external.get(self.store,handle)['state'],'cancelled')

    def test_cli_receipt_and_wait_use_one_durable_watch_handle(self):
        self.ready.touch()
        args=self.spec()
        command=[sys.executable,'-m','agent_builds','watch','start','--request-key','watch-cli-fixture-001',
            '--cwd',str(self.root),'--external-ref','fixture:cli','--read-only-probe','--no-track-source',
            '--jobs','1','--memory-gb','.25','--']+args['command']
        result=subprocess.run(command,env=self.env,capture_output=True,timeout=8)
        self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads(result.stdout)
        handle=receipt['result']['watch_id']
        self.handles.append(handle)
        result=subprocess.run([sys.executable,'-m','agent_builds','watch','wait',handle,'--timeout','5','--json'],
                              env=self.env,capture_output=True,timeout=8)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['watch']['id'],handle)

    def test_nested_start_is_rejected_without_creating_a_watch(self):
        with mock.patch.dict(os.environ,{'AGENT_BUILDS_JOB_ID':'outer-job'}):
            with self.assertRaises(ValueError):
                external.create(self.store,self.spec())
        self.assertEqual(external.listing(self.store),[])

    def test_compact_dashboard_retains_watch_handle_and_idle_accounting(self):
        from agent_builds.tui import compact_lines
        handle=self.create()
        row=external.get(self.store,handle)
        result=compact_lines([],0,[],0,10,self.store.config(),20,24,[row],0)
        self.assertIn(handle,result)
        self.assertTrue(any('EXTERNAL' in line for line in result))
        self.assertTrue(any('idle' in line for line in result))
        self.assertIn('V views',result[-1])
        self.assertIn('j/k',result[-1])
        self.assertIn('q',result[-1])
        self.assertLessEqual(len(result[-1]),23)

    def test_empty_watch_view_cannot_cancel_or_mutate_an_unrelated_job(self):
        from concurrent.futures import Future
        import curses
        from agent_builds import tui

        def completed(loader,key):
            future=Future()
            try:
                future.set_result(loader(key))
            except Exception as error:
                future.set_exception(error)
            return future

        job=self.store.submit(self.root,['true'],'local','quick',1,.25,'foreign-output','',10)
        screen=mock.Mock()
        screen.getmaxyx.return_value=(32,100)
        screen.getch.side_effect=[ord('v')]*10+[ord(key) for key in 'crenpboa']+[10,ord('q')]
        with mock.patch.dict(os.environ,self.env,clear=True), \
                mock.patch('agent_builds.tui.submit_read',side_effect=completed), \
                mock.patch('agent_builds.tui.submit_log_read',side_effect=completed), \
                mock.patch('agent_builds.tui_reads.submit_read',side_effect=completed), \
                mock.patch('curses.curs_set'),mock.patch('curses.has_colors',return_value=False), \
                mock.patch('curses.color_pair',return_value=0), \
                mock.patch('agent_builds.tui.ProcessSampler') as sampler, \
                mock.patch('agent_builds.disk.inventory',return_value=dict(paths=[],note='fixture')), \
                mock.patch('agent_builds.tui.prompt',side_effect=AssertionError('unexpected mutation prompt')), \
                mock.patch('agent_builds.tui.cancel_job',side_effect=AssertionError('must not cancel unrelated job')):
            sampler.return_value.sample.return_value=[]
            tui.main(screen,self.root)
        self.assertTrue(any('EXTERNAL COMPLETION' in call.args[2] for call in screen.addnstr.call_args_list))
        self.assertFalse(self.store.get(job)['cancel_requested'])
        self.assertEqual(external.listing(self.store),[])
