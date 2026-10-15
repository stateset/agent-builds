import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_builds import plans
from agent_builds.source import capture, changed, digest_file
from agent_builds.store import Store, alive

ROOT = str(Path(__file__).resolve().parent.parent)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.store = Store(self.root/'state')
        self.store.update_config(dict(cpu_slots=3, memory_gb=4, min_free_gb=0, min_available_gb=0,
            quick_reserve_slots=0, quick_reserve_jobs=0, quick_reserve_memory_gb=0, nice=0))
        self.env = dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root), PYTHONPATH=ROOT)
        self.env.pop('AGENT_BUILDS_JOB_ID', None)
        self.environment = mock.patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.children = []
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        (self.repo/'.gitignore').write_text('out/\n')
        (self.repo/'source.txt').write_text('original source\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial source')

    def tearDown(self):
        try:
            for plan in plans.listing(self.store):
                if plan['state'] not in plans.PLAN_TERMINAL:
                    plans.cancel(self.store, plan['id'])
            deadline = time.monotonic()+10
            while plans.listing(self.store, active=True) and time.monotonic()<deadline:
                time.sleep(.05)
            for child in self.children:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=6)
        finally:
            self.environment.stop()
            self.store.close()
            self.temp.cleanup()

    def git(self, *arguments):
        return subprocess.check_output(['git','-C',str(self.repo)]+list(arguments), stderr=subprocess.DEVNULL)

    def stage(self, name, code='print("ok")', **extra):
        return dict(name=name, command=[sys.executable, '-c', code], jobs=1, memory_gb=.25, timeout=8, **extra)

    def create(self, stages, **extra):
        definition = plans.validate(dict(name='fixture', stages=stages, **extra), self.repo, self.store.config())
        return plans.create(self.store, definition)

    def launch(self, stages, **extra):
        handle = self.create(stages, **extra)
        self.children.append(plans.spawn(self.store, handle))
        return handle

    def finish(self, handle, timeout=15):
        reader = Store(self.store.root, read_only=True)
        try:
            result = plans.wait(reader, handle, timeout)
        finally:
            reader.close()
        if not result['complete']:
            log = self.store.root/'logs'/('plan-'+handle+'.log')
            self.fail('Plan timed out: {}\n{}'.format(result['plan'], log.read_text()[-4000:] if log.exists() else 'no controller log'))
        return result['plan']

    def observe(self, handle, predicate, timeout=8):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            current = plans.get(self.store, handle)
            if predicate(current):
                return current
            time.sleep(.03)
        self.fail('Plan did not reach expected state: '+str(plans.get(self.store, handle)))

    def cli(self, *arguments):
        return subprocess.run([sys.executable, '-m', 'agent_builds']+list(arguments), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)

    def test_successful_chain_keeps_individual_jobs_source_artifacts_and_logs(self):
        handle = self.launch([
            self.stage('test', 'from pathlib import Path; Path("out").mkdir(); Path("out/result.txt").write_text("evidence"); print("test done")', artifacts=['out/result.txt']),
            self.stage('lint'), self.stage('build')])
        plan = self.finish(handle)
        self.assertEqual(plan['state'], 'succeeded')
        self.assertEqual(len(self.store.rows()), 3)
        first, second, third = plan['stages']
        self.assertGreaterEqual(second['job']['started'], first['job']['finished'])
        self.assertGreaterEqual(third['job']['started'], second['job']['finished'])
        artifact = first['evidence']['artifacts'][0]
        self.assertEqual(artifact['sha256'], hashlib.sha256(b'evidence').hexdigest())
        self.assertEqual(first['evidence']['log']['status'], 'captured')
        self.assertEqual(first['job']['source']['submission']['status'], 'captured')
        self.assertFalse(first['job']['source']['changed_since_submission'])
        self.assertFalse(first['job']['source']['changed_during_run'])
        self.assertEqual(plan['exit_code'], 0)

    def test_failure_skips_dependents_but_runs_independent_stages(self):
        handle = self.launch([self.stage('test', 'import sys; sys.exit(7)'),
            self.stage('build', 'from pathlib import Path; Path("must-not-exist").touch()'),
            self.stage('audit', after=[])])
        plan = self.finish(handle)
        self.assertEqual([s['state'] for s in plan['stages']], ['failed','skipped','succeeded'])
        self.assertIsNone(plan['stages'][1]['job_id'])
        self.assertFalse((self.repo/'must-not-exist').exists())
        self.assertEqual(plan['exit_code'], 7)
        result = self.cli('plan','wait',handle,'--json')
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertTrue(json.loads(result.stdout)['complete'])

    def test_missing_required_artifact_fails_stage_without_rewriting_command_exit(self):
        handle = self.launch([self.stage('test', artifacts=['out/missing']),self.stage('build')])
        plan = self.finish(handle)
        self.assertEqual(plan['stages'][0]['state'], 'failed')
        self.assertEqual(plan['stages'][0]['job']['exit_code'], 0)
        self.assertEqual(plan['stages'][0]['evidence']['artifacts'][0]['status'], 'missing')
        self.assertEqual(plan['stages'][1]['state'], 'skipped')
        self.assertEqual(plan['exit_code'], 1)

    def test_independent_stages_can_run_in_parallel_with_explicit_resource_keys(self):
        handle = self.launch([self.stage('a','import time; time.sleep(.8)',resource_key='output-a'),
            self.stage('b','import time; time.sleep(.8)',resource_key='output-b',after=[])],max_parallel=2)
        plan = self.finish(handle)
        first, second = [s['job'] for s in plan['stages']]
        self.assertLess(max(first['started'],second['started']), min(first['finished'],second['finished']))
        self.assertEqual(plan['state'], 'succeeded')

    def test_controller_does_not_reserve_capacity_while_stage_waits(self):
        self.store.update_config(dict(paused=True))
        handle = self.launch([self.stage('test'),self.stage('build')])
        self.observe(handle, lambda p: p['stages'][0]['job'] is not None)
        self.assertEqual(self.store.counts()['running'], 0)
        self.assertEqual(self.store.counts()['queued'], 1)
        self.assertIsNone(plans.get(self.store, handle)['stages'][1]['job'])
        self.store.update_config(dict(paused=False))
        self.assertEqual(self.finish(handle)['state'],'succeeded')

    def test_failed_prerequisite_skips_dependents_even_when_admission_is_blocked(self):
        handle=self.create([self.stage('prerequisite'),self.stage('dependent'),self.stage('last')])
        current=plans.get(self.store,handle)
        job,token=plans.prepare(self.store,current,current['stages'][0])
        self.store.finish(job,'failed',7)
        self.store.update_config(dict(paused=True,min_free_gb=100000))
        self.children.append(plans.spawn(self.store,handle))
        result=self.finish(handle)
        self.assertEqual([stage['state'] for stage in result['stages']],['failed','skipped','skipped'])
        self.assertEqual(result['exit_code'],7)
        self.assertIsNone(result['stages'][1]['job_id'])
        self.assertIsNone(result['stages'][2]['job_id'])
        self.assertEqual(len(self.store.rows()),1)
        from agent_builds import __version__
        self.assertEqual(result['runner_version'],__version__)

    def test_plan_cancellation_cancels_pending_and_active_without_affecting_other_jobs(self):
        self.store.update_config(dict(paused=True))
        unrelated = self.store.submit(self.repo, ['true'], 'local','quick',1,.25,'unrelated','',5)
        handle = self.launch([self.stage('test'),self.stage('build')])
        self.observe(handle, lambda p: p['stages'][0]['job'] is not None)
        plans.cancel(self.store, handle)
        plan = self.finish(handle)
        self.assertEqual(plan['state'],'cancelled')
        self.assertEqual(plan['exit_code'],130)
        self.assertEqual(plan['stages'][1]['state'],'cancelled')
        self.assertIsNone(plan['stages'][1]['job'])
        self.assertEqual(self.store.get(unrelated)['state'],'queued')
        self.store.finish(unrelated,'cancelled',130)

    def test_running_plan_cancellation_stops_its_command_and_preserves_unrelated_reservation(self):
        unrelated = self.store.submit(self.repo, ['true'], 'local','quick',1,.25,'unrelated','',5)
        self.assertTrue(self.store.claim(unrelated))
        handle = self.launch([self.stage('test','from pathlib import Path; import time; Path("out").mkdir(); Path("out/ready").touch(); time.sleep(30)'),self.stage('build')])
        self.observe(handle,lambda p: (self.repo/'out'/'ready').exists())
        plans.cancel(self.store,handle)
        current = self.finish(handle)
        self.assertEqual(current['state'],'cancelled')
        self.assertEqual(current['stages'][0]['job']['state'],'cancelled')
        self.assertIsNone(current['stages'][1]['job_id'])
        self.assertEqual(self.store.get(unrelated)['state'],'running')
        self.store.finish(unrelated,'succeeded',0)

    def test_read_only_wait_timeout_does_not_cancel_plan(self):
        self.store.update_config(dict(paused=True))
        handle = self.launch([self.stage('test')])
        reader = Store(self.store.root, read_only=True)
        try:
            result = plans.wait(reader, handle, .02)
            self.assertTrue(result['timed_out'])
            self.assertEqual(result['exit_code'],124)
        finally:
            reader.close()
        self.assertFalse(plans.get(self.store,handle)['cancel_requested'])
        self.store.update_config(dict(paused=False))
        self.finish(handle)

    def test_source_changes_while_queued_are_reported_and_strict_policy_prevents_launch(self):
        self.store.update_config(dict(paused=True))
        handle = self.launch([self.stage('test','from pathlib import Path; Path("out").mkdir()')],source_policy='require_unchanged')
        self.observe(handle, lambda p: p['stages'][0]['job'] is not None)
        (self.repo/'source.txt').write_text('changed during queue wait')
        self.store.update_config(dict(paused=False))
        plan = self.finish(handle)
        job = plan['stages'][0]['job']
        self.assertEqual(plan['state'],'failed')
        self.assertTrue(job['source']['changed_since_submission'])
        self.assertFalse(job['source']['command_started'])
        self.assertFalse((self.repo/'out').exists())
        self.assertEqual(job['exit_code'],2)

    def test_report_policy_keeps_command_result_when_source_changes(self):
        handle = self.launch([self.stage('test','from pathlib import Path; Path("source.txt").write_text("changed by command")')])
        plan = self.finish(handle)
        self.assertEqual(plan['state'],'succeeded')
        self.assertTrue(plan['stages'][0]['job']['source']['changed_during_run'])

    def test_strict_policy_fails_verification_for_changes_during_execution_without_rewriting_exit(self):
        handle = self.launch([self.stage('test','from pathlib import Path; Path("source.txt").write_text("changed")'),self.stage('build')],source_policy='require_unchanged')
        current = self.finish(handle)
        self.assertEqual(current['state'],'failed')
        self.assertEqual(current['stages'][0]['job']['exit_code'],0)
        self.assertTrue(current['stages'][0]['job']['source']['changed_during_run'])
        self.assertEqual(current['stages'][1]['state'],'skipped')

    def test_controller_resume_observes_existing_worker_without_duplicate_execution(self):
        marker = self.repo/'out'/'counter'
        code = 'from pathlib import Path; import time; p=Path("out"); p.mkdir(); (p/"counter").write_text("once"); time.sleep(1.5)'
        handle = self.launch([self.stage('test',code),self.stage('lint')])
        original = self.children[-1]
        self.observe(handle,lambda p: marker.exists())
        original.kill()
        original.wait(timeout=5)
        self.children.append(plans.spawn(self.store,handle))
        plan = self.finish(handle)
        self.assertEqual(plan['state'],'succeeded')
        self.assertEqual(marker.read_text(),'once')
        self.assertEqual(len(self.store.rows()),2)

    def test_crash_after_preparation_does_not_redispatch_unknown_stage(self):
        handle = self.create([self.stage('test'),self.stage('build')])
        plan = plans.get(self.store,handle)
        job, token = plans.prepare(self.store,plan,plan['stages'][0])
        self.store.db.execute("UPDATE jobs SET owner_start='impossible' WHERE id=?",(job,))
        self.children.append(plans.spawn(self.store,handle))
        result = self.finish(handle)
        self.assertEqual(result['stages'][0]['state'],'lost')
        self.assertEqual(result['stages'][1]['state'],'skipped')
        self.assertEqual(len(self.store.rows()),1)
        with self.assertRaises(ValueError):
            plans.attach_job(self.store,job,token)

    def test_prepared_stage_can_only_attach_once(self):
        handle = self.create([self.stage('test')])
        plan = plans.get(self.store,handle)
        job, token = plans.prepare(self.store,plan,plan['stages'][0])
        plans.attach_job(self.store,job,token)
        with self.assertRaises(ValueError):
            plans.attach_job(self.store,job,token)
        self.store.finish(job,'succeeded',0)
        self.children.append(plans.spawn(self.store,handle))
        self.finish(handle)

    def test_stage_link_failure_rolls_back_job_creation(self):
        import sqlite3
        handle = self.create([self.stage('test')])
        current = plans.get(self.store,handle)
        self.store.db.execute("CREATE TRIGGER reject_link BEFORE UPDATE ON plan_stages WHEN NEW.job_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'fixture link failure'); END")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                plans.prepare(self.store,current,current['stages'][0])
            self.assertEqual(self.store.rows(),[])
            self.assertIsNone(plans.get(self.store,handle)['stages'][0]['job_id'])
        finally:
            self.store.db.execute('DROP TRIGGER reject_link')

    def test_known_worker_launch_failure_is_recorded_without_retry(self):
        handle = self.create([self.stage('test'),self.stage('build')])
        with mock.patch('agent_builds.plans.subprocess.Popen',side_effect=OSError('fixture cannot launch')):
            plans.control(handle)
        current = plans.get(self.store,handle)
        self.assertEqual([s['state'] for s in current['stages']],['failed','skipped'])
        self.assertEqual(len(self.store.rows()),1)
        self.assertEqual(current['stages'][0]['job']['exit_code'],2)

    def test_two_controllers_cannot_dispatch_duplicate_stage_jobs(self):
        self.store.update_config(dict(paused=True))
        handle = self.launch([self.stage('test')])
        self.observe(handle,lambda p: p['controller_alive'])
        with self.assertRaises(ValueError):
            plans.spawn(self.store,handle)
        self.store.update_config(dict(paused=False))
        self.finish(handle)
        self.assertEqual(len(self.store.rows()),1)

    def test_export_is_read_only_private_and_preserves_recorded_hash_after_artifact_changes(self):
        handle = self.launch([self.stage('test','from pathlib import Path; Path("out").mkdir(); Path("out/item").write_text("original")',artifacts=['out/item'])])
        plan = self.finish(handle)
        expected = plan['stages'][0]['evidence']['artifacts'][0]['sha256']
        (self.repo/'out'/'item').write_text('later changed')
        with mock.patch('os.chmod',side_effect=AssertionError('inspection must be read-only')):
            reader = Store(self.store.root,read_only=True)
            try:
                report = plans.export(reader,handle)
            finally:
                reader.close()
        self.assertEqual(report['plan']['stages'][0]['evidence']['artifacts'][0]['sha256'],expected)
        path = self.root/'evidence.json'
        plans.write_export(path,report)
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError):
            plans.write_export(path,{'overwrite':True})
        self.assertEqual(json.loads(path.read_text())['schema'],'agent-builds-evidence-v1')

    def test_manifest_validation_rejects_cycles_unknown_deps_commands_and_escaping_paths(self):
        invalid = [dict(stages=[self.stage('a',after=['b']),self.stage('b',after=['a'])]),
            dict(stages=[self.stage('a',after=['missing'])]),dict(stages=[dict(name='a',command='rm everything')]),
            dict(stages=[self.stage('a',artifacts=['../outside'])]),dict(stages=[self.stage('a',cwd='..')])]
        for definition in invalid:
            with self.subTest(definition=definition), self.assertRaises(ValueError):
                plans.validate(definition,self.repo,self.store.config())

    def test_inspection_on_missing_state_does_not_create_or_migrate(self):
        missing = self.root/'missing'
        with mock.patch('os.chmod',side_effect=AssertionError('read-only')), mock.patch('pathlib.Path.mkdir',side_effect=AssertionError('read-only')):
            reader = Store(missing,read_only=True)
            try:
                self.assertEqual(plans.listing(reader),[])
                with self.assertRaises(ValueError):
                    plans.get(reader,'unknown')
            finally:
                reader.close()
        self.assertFalse(missing.exists())

    def test_source_fingerprints_include_nonignored_untracked_files_without_contents(self):
        first = capture(self.repo)
        secret = self.repo/'untracked.txt'
        secret.write_text('never-store-this-content')
        second = capture(self.repo)
        self.assertTrue(changed(first,second))
        self.assertEqual(second['untracked_files'],1)
        self.assertNotIn('never-store-this-content',json.dumps(second))
        (self.repo/'out').mkdir()
        (self.repo/'out'/'ignored').write_text('ignored output')
        self.assertFalse(changed(second,capture(self.repo)))

    def test_unavailable_identity_and_oversized_artifacts_are_explicit(self):
        self.assertEqual(capture(self.root)['status'],'unavailable')
        self.assertIsNone(changed({},capture(self.root)))
        path = self.root/'big'
        path.write_bytes(b'12345')
        evidence = digest_file(path,limit=4)
        self.assertEqual(evidence['status'],'size_limit')
        self.assertIsNone(evidence['sha256'])

    def test_statistics_compare_measured_and_reserved_cpu_without_changing_budgets(self):
        from agent_builds.telemetry import statistics
        job = self.store.submit(self.repo,['true'],'local','quick',1,.25,'fixture','',5)
        self.store.db.execute("UPDATE jobs SET state='succeeded',started=100,finished=102 WHERE id=?",(job,))
        self.store.record_usage(job,dict(samples=2,complete=True,cpu_seconds=1,sampled_peak_rss_gb=.1))
        before = self.store.config()
        result = statistics(self.store)
        self.assertEqual(result['groups'][0]['observed_cpu_seconds'],1)
        self.assertEqual(result['groups'][0]['reserved_cpu_seconds'],2)
        self.assertEqual(result['groups'][0]['observed_to_reserved_cpu_ratio'],.5)
        from agent_builds.scheduler import POLICY_VERSION
        self.assertEqual(result['policies'][0]['policy_version'],POLICY_VERSION)
        self.assertEqual(self.store.config(),before)

    def test_cli_validate_run_show_logs_and_evidence_export(self):
        path = self.root/'plan.json'
        path.write_text(json.dumps(dict(stages=[self.stage('test','print("stage output")')])))
        result = self.cli('plan','validate',str(path),'--cwd',str(self.repo))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(plans.listing(self.store),[])
        result = self.cli('plan','run',str(path),'--cwd',str(self.repo))
        self.assertEqual(result.returncode,0,result.stderr)
        handle = json.loads(result.stdout)['plan_id']
        self.finish(handle)
        result = self.cli('plan','show',handle,'--json')
        self.assertEqual(json.loads(result.stdout)['state'],'succeeded')
        result = self.cli('plan','logs',handle,'test')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(b'stage output',result.stdout)
        result = self.cli('plan','export',handle)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['schema'],'agent-builds-evidence-v1')


    def test_always_cleanup_runs_after_failure_and_skip_preserving_original_exit(self):
        handle=self.launch([self.stage('tests','print("failed check"); raise SystemExit(7)'),
            self.stage('build','from pathlib import Path; Path("must-not-run").touch()'),
            self.stage('cleanup','from pathlib import Path; Path("out").mkdir(); Path("out/cleaned").touch()',when='always')])
        plan=self.finish(handle)
        self.assertEqual([s['state'] for s in plan['stages']],['failed','skipped','succeeded'])
        self.assertTrue((self.repo/'out/cleaned').exists())
        self.assertFalse((self.repo/'must-not-run').exists())
        self.assertEqual(plan['exit_code'],7)

    def test_global_cancel_does_not_dispatch_always_cleanup(self):
        self.store.update_config({'paused':True})
        handle=self.launch([self.stage('tests'),self.stage('cleanup','from pathlib import Path; Path("must-not-run").touch()',when='always')])
        self.observe(handle,lambda p:p['stages'][0]['job'] is not None)
        plans.cancel(self.store,handle)
        plan=self.finish(handle)
        self.assertEqual(plan['state'],'cancelled')
        self.assertIsNone(plan['stages'][1]['job_id'])
        self.assertFalse((self.repo/'must-not-run').exists())

    def test_cleanup_waits_for_all_prerequisites_and_retains_verification_failure(self):
        handle=self.launch([self.stage('artifact','print("command succeeded")',artifacts=['missing.json']),
            self.stage('cleanup','print("cleanup ran")',when='always')])
        plan=self.finish(handle)
        self.assertEqual(plan['exit_code'],1)
        self.assertEqual(plan['stages'][0]['job']['exit_code'],0)
        self.assertEqual(plan['stages'][1]['state'],'succeeded')
        self.assertGreaterEqual(plan['stages'][1]['job']['started'],plan['stages'][0]['job']['finished'])

    def test_invalid_stage_condition_is_rejected(self):
        with self.assertRaises(ValueError):
            self.create([self.stage('bad',when='sometimes')])

    def test_compact_summary_preserves_verification_failure_and_failure_tail(self):
        from agent_builds.plan_output import summary
        handle=self.launch([self.stage('report','print("first line"); print("last line")',artifacts=['missing.json'])])
        plan=self.finish(handle)
        result=summary(plan,1)
        self.assertEqual(result['exit_code'],1)
        self.assertEqual(result['stages'][0]['command_exit_code'],0)
        self.assertTrue(result['verification_failed_after_command_success'])
        self.assertEqual(result['stages'][0]['log_tail'],['last line'])
        self.assertEqual(result['stages'][0]['artifact_issues'][0]['status'],'missing')
        self.assertLess(len(json.dumps(result)),len(json.dumps(plan)))
        self.assertNotIn('definition',result)

    def test_summary_wait_timeout_is_read_only_and_keeps_exit_124(self):
        handle=self.create([self.stage('waiting')])
        before=plans.get(self.store,handle)
        result=self.cli('plan','wait',handle,'--summary','--json','--timeout','.03','--tail','2')
        self.assertEqual(result.returncode,124,result.stderr)
        payload=json.loads(result.stdout)
        self.assertTrue(payload['timed_out'])
        self.assertEqual(payload['plan']['schema'],'agent-builds-plan-summary-v1')
        self.assertIsNone(payload['plan']['exit_code'])
        self.assertEqual(plans.get(self.store,handle)['state'],before['state'])

    def test_summary_cli_show_list_and_full_report_compatibility(self):
        handle=self.launch([self.stage('tests')])
        self.finish(handle)
        shown=self.cli('plan','show',handle,'--summary','--json')
        self.assertEqual(json.loads(shown.stdout)['schema'],'agent-builds-plan-summary-v1')
        full=json.loads(self.cli('plan','show',handle,'--json').stdout)
        self.assertIn('definition',full)
        listed=json.loads(self.cli('plan','list','--summary','--json','--limit','1').stdout)
        self.assertEqual(listed[0]['id'],handle)
        self.assertEqual(self.cli('plan','show',handle,'--tail','1').returncode,2)
        self.assertEqual(self.cli('plan','show',handle,'--summary','--tail','201').returncode,2)

    def test_draft_keeps_literal_commands_and_observes_harness_only_now(self):
        from agent_builds.plan_draft import draft
        handle=self.launch([self.stage('tests','print("literal $value")')])
        plan=self.finish(handle)
        job=plan['stages'][0]['job_id']
        script=self.root/'harness.sh'
        script.write_text('echo original\n')
        before=len(self.store.rows())
        result=draft(self.store,[job,job],self.repo,[str(script)])
        self.assertFalse(result['commands_submitted'])
        self.assertEqual(len(result['manifest']['stages']),1)
        self.assertEqual(result['manifest']['stages'][0]['command'],plan['stages'][0]['job']['argv'])
        self.assertEqual(result['input_observations'][0]['sha256'],hashlib.sha256(script.read_bytes()).hexdigest())
        self.assertEqual(result['input_observations'][0]['observation_scope'],'draft_creation_only')
        self.assertEqual(len(self.store.rows()),before)
        script.write_text('echo changed\n')
        self.assertNotEqual(draft(self.store,[job],self.repo,[str(script)])['input_observations'][0]['sha256'],result['input_observations'][0]['sha256'])

    def test_draft_cli_writes_exclusive_runnable_manifest_without_reexecuting(self):
        handle=self.launch([self.stage('tests')])
        plan=self.finish(handle)
        job=plan['stages'][0]['job_id']
        output=self.root/'draft.json'
        before=len(self.store.rows())
        result=self.cli('plan','draft',job,'--cwd',str(self.repo),'--output',str(output))
        self.assertEqual(result.returncode,0,result.stderr)
        plans.read_manifest(output,self.repo,self.store.config())
        self.assertEqual(output.stat().st_mode&0o777,0o600)
        self.assertEqual(self.cli('plan','draft',job,'--cwd',str(self.repo),'--output',str(output)).returncode,2)
        self.assertEqual(len(self.store.rows()),before)

    def test_draft_rejects_jobs_outside_selected_checkout(self):
        from agent_builds.plan_draft import draft
        job=self.store.submit(self.root,['true'],'local','quick',1,.25,'elsewhere','',5)
        with self.assertRaises(ValueError):
            draft(self.store,[job],self.repo)

    def test_prepared_worker_records_its_loaded_version(self):
        from agent_builds import __version__
        handle=self.launch([self.stage('tests')])
        plan=self.finish(handle)
        self.assertEqual(plan['stages'][0]['job']['runner_version'],__version__)


if __name__ == '__main__':
    unittest.main()
