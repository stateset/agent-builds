"""Reviewed profiles, bounded CPU packing, workflow reviews and real plan studies."""
from argparse import Namespace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from agent_builds import advice, batch, plans, profiles, task_review, workbench, workflow, workflow_study
from agent_builds.source import capture
from agent_builds.store import Store
from agent_builds.tui import compact_lines
import test_mcp
import test_parallel_batch
import test_plans


class WeightedTests(unittest.TestCase):
    setUp=test_plans.PlanTests.setUp
    tearDown=test_plans.PlanTests.tearDown
    git=test_plans.PlanTests.git
    cli=test_plans.PlanTests.cli
    launch=test_parallel_batch.ParallelBatchTests.launch
    finish=test_parallel_batch.ParallelBatchTests.finish
    ready=test_parallel_batch.ParallelBatchTests.ready

    def check(self,name,jobs,seconds=.2,exit_code=0):
        return dict(name=name,jobs=jobs,command=[sys.executable,'-c',
            'import os,time; assert os.environ["CARGO_BUILD_JOBS"]=={0!r}; assert os.environ["RUST_TEST_THREADS"]=={0!r}; time.sleep({1}); raise SystemExit({2})'.format(str(jobs),seconds,exit_code)])

    def test_individual_budgets_pack_free_cpu_without_exceeding_outer_reservation(self):
        row=self.finish(self.launch([self.check('two',2,.35),self.check('full',3,.05),
            self.check('small-a',1,.2),self.check('small-b',1,.1)],parallel=3,jobs=3))
        self.assertEqual(row['exit_code'],0)
        self.assertEqual(row['batch']['cpu_allocation'],'explicit')
        self.assertIsNone(row['batch']['slots_per_check'])
        checks=row['batch']['checks']
        self.assertLess(checks[2]['started'],checks[0]['finished'])
        self.assertGreaterEqual(checks[1]['started'],max(checks[i]['finished'] for i in (0,2,3)))
        active=0
        for unused,delta in sorted([(c['started'],c['cpu_slots']) for c in checks]+[(c['finished'],-c['cpu_slots']) for c in checks]):
            active+=delta
            self.assertLessEqual(active,3)
        self.assertEqual(len(self.store.rows()),1)

    def test_weighted_fail_fast_keeps_native_result_and_does_not_start_pending_full_check(self):
        row=self.finish(self.launch([self.check('fail',2,.1,7),self.check('full',3,.1,9),
            self.check('sibling',1,.3)],parallel=3,jobs=3,keep=False))
        self.assertEqual(row['exit_code'],7)
        self.assertEqual([c['state'] for c in row['batch']['checks']],['failed','skipped','succeeded'])

    def test_explicit_weights_validate_before_submission_and_keep_legacy_encoding(self):
        for value in (0,True,1.5,65):
            with self.assertRaises(ValueError):
                batch.validate(dict(checks=[dict(name='bad',command=['true'],jobs=value)]))
        value=batch.validate(dict(max_parallel=2,checks=[self.check('two',2),dict(name='default',command=['true'])]))
        self.assertEqual(batch.allocations(value,3),[2,1])
        self.assertEqual(batch.minimum_slots(value),2)
        with self.assertRaises(ValueError):
            batch.worker_slots(value,1)
        manifest=self.root/'invalid.json'
        manifest.write_text(json.dumps(dict(checks=[self.check('large',4)])))
        result=self.cli('batch','run',str(manifest),'--cwd',str(self.repo),'--jobs','3')
        self.assertEqual(result.returncode,2)
        self.assertEqual(self.store.rows(),[])
        legacy=batch.validate(dict(checks=[dict(name='legacy',command=['true'])]))
        self.assertEqual(legacy,dict(version=1,checks=[dict(name='legacy',command=['true'])],keep_going=True))

    def test_advisor_never_suggests_less_cpu_than_an_explicit_check_requires(self):
        value=batch.validate(dict(max_parallel=2,checks=[self.check('full',3),self.check('small',1)]))
        row=dict(batch=dict(complete=True),argv=batch.command(value),slots=3)
        self.assertEqual(advice.minimum_cpu_slots(row),3)
        from agent_builds.preflight import report
        result=report(self.store,dict(cwd=str(self.repo),jobs=3,compare_jobs=[2],manifest=value,memory_gb=.5))
        self.assertEqual([case['submission_acceptable'] for case in result['cases']],[True,False])
        self.assertEqual(result['cases'][0]['batch']['check_slots'],dict(full=3,small=1))

    def test_weighted_timeout_and_cancel_keep_outer_cleanup_boundary(self):
        from agent_builds.runner import cancel_job
        for cancelled in (False,True):
            handle=self.launch([self.check('two',2,5),self.check('one',1,5),self.check('pending',3,5)],
                parallel=3,jobs=3,timeout=8 if cancelled else .2)
            if cancelled:
                self.ready(handle)
                cancel_job(self.store,handle)
            row=self.finish(handle)
            self.assertEqual(row['exit_code'],130 if cancelled else 124)
            self.assertTrue(all(c['state']!='succeeded' for c in row['batch']['checks']))


class GrowthTests(unittest.TestCase):
    setUp=test_plans.PlanTests.setUp
    tearDown=test_plans.PlanTests.tearDown
    git=test_plans.PlanTests.git
    cli=test_plans.PlanTests.cli
    finish=test_plans.PlanTests.finish

    def seed(self,duration=2,code=None,slots=2,memory=2):
        command=code or [sys.executable,'-c','import time; time.sleep(1.2); print("profile-fixture")']
        handle=self.store.submit(self.repo,command,'local','quick',slots,memory,str(self.repo),'',8,
                                 source=dict(submission=capture(self.repo)))
        end=time.time()-.1
        self.store.db.execute("UPDATE jobs SET state='succeeded',created=?,started=?,finished=?,exit_code=0 WHERE id=?",
            (end-duration-.1,end-duration,end,handle))
        self.store.record_usage(handle,dict(source='local_process_tree',complete=True,samples=4,sampled_peak_rss_gb=.2,cpu_seconds=.1))
        return handle

    def proposal(self):
        handles=[self.seed() for unused in range(3)]
        value=task_review.propose(self.store,handles[0],'memory_trial','reviewed-fixture')
        filename=self.root/'proposal.json'
        plans.write_export(filename,value)
        return filename,value

    def test_review_trial_promote_and_source_guarded_reuse_are_end_to_end(self):
        filename,proposal=self.proposal()
        self.assertEqual(proposal['candidate']['memory_gb'],.5)
        handle=task_review.trial(self.store,filename,emit=False,processes=self.children)
        self.finish(handle)
        result=task_review.review(self.store,filename,handle)
        self.assertTrue(result['eligible'],result['issues'])
        task_review.promote(self.store,filename,handle)
        key=profiles.task_key(self.repo,'reviewed-fixture')
        self.assertNotIn(key,profiles.load(self.store).get('tasks',{}))
        self.assertIn(key,profiles.load(self.store)['reviewed_tasks'])
        result=self.cli('task','run','reviewed-fixture','--cwd',str(self.repo),'--detach')
        self.assertEqual(result.returncode,0,result.stderr)
        saved=json.loads(result.stdout)
        self.assertTrue(saved['reviewed_task'])
        self.finish(saved['plan_id'])
        before=len(plans.listing(self.store))
        (self.repo/'source.txt').write_text('changed after profile review')
        result=self.cli('task','run','reviewed-fixture','--cwd',str(self.repo),'--detach')
        self.assertEqual(result.returncode,2)
        self.assertEqual(len(plans.listing(self.store)),before)

    def test_proposals_do_not_execute_or_initialize_missing_state(self):
        filename,proposal=self.proposal()
        marker=self.root/'must-not-run'
        proposal['candidate']['command']=[sys.executable,'-c','from pathlib import Path; Path({!r}).touch()'.format(str(marker))]
        filename.write_text(json.dumps(proposal))
        with self.assertRaisesRegex(ValueError,'only CPU, RAM or lane'):
            task_review.read(filename)
        self.assertFalse(marker.exists())
        before=self.store.db.total_changes
        with self.assertRaises(ValueError):
            task_review.propose(self.store,'missing','not-a-kind','profile')
        self.assertEqual(self.store.db.total_changes,before)
        missing=self.root/'missing-state'
        reader=Store(missing,read_only=True)
        try:
            with self.assertRaises(ValueError):
                task_review.propose(reader,'missing','memory_trial','profile')
        finally:
            reader.close()
        self.assertFalse(missing.exists())

    def test_source_drift_blocks_trial_before_any_plan_is_created(self):
        filename,unused=self.proposal()
        (self.repo/'source.txt').write_text('new source')
        with self.assertRaisesRegex(ValueError,'changed since proposal'):
            task_review.trial(self.store,filename,emit=False)
        self.assertEqual(plans.listing(self.store),[])

    def test_editing_baseline_cannot_remove_the_memory_measurement_gate(self):
        filename,value=self.proposal()
        value['baseline']['memory_gb']=.25
        filename.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'baseline differs'):
            task_review.trial(self.store,filename,emit=False)
        self.assertEqual(plans.listing(self.store),[])

    def test_reduction_requires_complete_measurements_and_entire_successful_plan(self):
        filename,proposal=self.proposal()
        definition=task_review.definition(self.store,filename,proposal)
        handle=plans.create(self.store,definition)
        observed=capture(self.repo,definition['input_files'])
        for index,stage in enumerate(definition['stages']):
            job=self.store.submit(self.repo,stage['command'],'local','quick',stage['jobs'],stage['memory_gb'],str(self.repo),'',8,
                source=dict(submission=observed,start=observed,finish=observed,changed_since_submission=False,changed_during_run=False))
            self.store.db.execute("UPDATE jobs SET state='succeeded',started=?,finished=?,exit_code=0 WHERE id=?",(time.time()-2,time.time(),job))
            self.store.record_usage(job,dict(source='local_process_tree',complete=True,samples=1,sampled_peak_rss_gb=.1,cpu_seconds=.1))
            self.store.db.execute("UPDATE plan_stages SET state='succeeded',job_id=? WHERE plan_id=? AND ordinal=?",(job,handle,index))
        self.store.db.execute("UPDATE plans SET state='succeeded',finished=? WHERE id=?",(time.time(),handle))
        result=task_review.review(self.store,filename,handle)
        self.assertFalse(result['eligible'])
        with self.assertRaisesRegex(ValueError,'RAM reduction'):
            task_review.promote(self.store,filename,handle)
        self.assertFalse((self.store.root/'profiles.json').exists())
        self.store.db.execute("UPDATE jobs SET state='lost',exit_code=NULL WHERE id=?",(job,))
        self.assertFalse(task_review.review(self.store,filename,handle)['eligible'])

    def test_repeated_long_quick_work_gets_reviewed_lane_change_without_command_edits(self):
        command=[sys.executable,'-c','print("literal; not split")']
        for unused in range(3):
            self.seed(duration=100,code=command,slots=1,memory=1)
        manifest=dict(stages=[dict(name='prerequisite',command=['true'],lane='quick',jobs=1,memory_gb=.25,timeout=10),
            dict(name='long',command=command,lane='quick',jobs=1,memory_gb=1,timeout=300)])
        before=json.dumps(manifest,sort_keys=True)
        result=workflow.report(self.store,manifest,self.repo)
        self.assertEqual(result['stages'][0]['classification'],'bounded_short')
        self.assertEqual(result['manifest']['stages'][1]['lane'],'heavy')
        self.assertEqual(result['manifest']['stages'][1]['command'],command)
        self.assertEqual(json.dumps(manifest,sort_keys=True),before)
        self.assertFalse(result['commands_submitted'])

    def test_lane_review_retains_budget_when_protected_heavy_pool_cannot_fit_it(self):
        command=['literal-long-command']
        for unused in range(3):
            self.seed(duration=100,code=command,slots=3,memory=2)
        self.store.update_config(dict(quick_reserve_slots=1,protected_quick=True))
        result=workflow.report(self.store,dict(stages=[dict(name='long',command=command,lane='quick',jobs=3,memory_gb=2)]),self.repo)
        self.assertEqual(result['changes_proposed'],0)
        self.assertIn('protected heavy pool',' '.join(result['stages'][0]['warnings']))

    def test_workbench_and_compact_view_join_evidence_without_mutation_or_execution(self):
        handles=[self.seed() for unused in range(3)]
        before=self.store.db.total_changes
        result=workbench.report(self.store,handles[0])
        self.assertTrue(result['candidates'])
        self.assertEqual(result['preflight']['comparisons']['cases'],4)
        self.assertEqual(self.store.db.total_changes,before)
        lines=compact_lines(self.store.rows(),0,[],0,12,self.store.config(),12,40,workbench=result)
        self.assertTrue(any('WORKBENCH' in line for line in lines))
        self.assertLessEqual(len(lines),12)
        self.assertIn('read only','\n'.join(workbench.lines(result)))

    def study_manifests(self):
        stages=[dict(name=name,command=[sys.executable,'-c','import time; time.sleep(.15)'],
            after=[],jobs=1,memory_gb=1,lane='quick',timeout=8,resource_key='independent-'+name) for name in ('one','two')]
        baseline=dict(max_parallel=1,stages=stages)
        candidate=json.loads(json.dumps(baseline))
        candidate['max_parallel']=2
        for stage in candidate['stages']:
            stage['memory_gb']=.5
        paths=[self.root/'baseline.json',self.root/'candidate.json']
        for path,value in zip(paths,(baseline,candidate)):
            path.write_text(json.dumps(value))
        return paths

    def test_study_runs_counterbalanced_real_queued_plans_and_retains_native_results(self):
        baseline,candidate=self.study_manifests()
        config=self.store.config_path.read_bytes()
        output=self.root/'study.json'
        result=workflow_study.run(self.store,baseline,candidate,self.repo,output,pairs=2,timeout=30)
        self.assertEqual([r['mode'] for r in result['records']],['baseline','candidate','candidate','baseline'])
        self.assertEqual(len(plans.listing(self.store)),4)
        self.assertTrue(all(r['verified_stages']==2 for r in result['records']))
        self.assertTrue(all(r['comparable'] for r in result['comparisons']))
        self.assertEqual(config,self.store.config_path.read_bytes())
        self.assertFalse(result['autonomous_coding_productivity_measured'])
        self.assertEqual(json.loads(output.with_name(output.name+'.progress.json').read_text())['status'],'completed')
        self.store.update_config(dict(memory_gb=.25))
        comparison=workflow_study.compare(self.store,result['records'][0]['plan_id'],result['records'][1]['plan_id'])
        self.assertTrue(comparison['comparable'])
        with self.assertRaisesRegex(ValueError,'distinct plan handles'):
            workflow_study.compare(self.store,result['records'][0]['plan_id'],result['records'][0]['plan_id'])

    def test_study_rejects_changed_commands_before_any_submission(self):
        baseline,candidate=self.study_manifests()
        value=json.loads(candidate.read_text())
        value['stages'][0]['command']=['different-acceptance']
        candidate.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'identical commands'):
            workflow_study.run(self.store,baseline,candidate,self.repo,self.root/'study.json',pairs=1)
        self.assertEqual(plans.listing(self.store),[])

    def test_interrupted_startup_keeps_handle_and_does_not_retry_or_cancel_it(self):
        baseline,candidate=self.study_manifests()
        output=self.root/'study.json'
        with mock.patch('agent_builds.workflow_study.plans.spawn',side_effect=RuntimeError('uncertain startup')) as spawn:
            with self.assertRaisesRegex(RuntimeError,'uncertain startup'):
                workflow_study.run(self.store,baseline,candidate,self.repo,output,pairs=1)
            spawn.assert_called_once()
        progress=json.loads(output.with_name(output.name+'.progress.json').read_text())
        self.assertEqual(len(progress['arms']),1)
        handle=progress['arms'][0]['plan_id']
        self.assertEqual(plans.get(self.store,handle)['state'],'queued')
        self.assertFalse(output.exists())

    def test_source_expectation_rejects_plan_before_creation(self):
        source=capture(self.repo)
        (self.repo/'source.txt').write_text('changed')
        definition=plans.validate(dict(stages=[dict(name='one',command=['true'],jobs=1,memory_gb=.25)]),self.repo,self.store.config())
        with self.assertRaisesRegex(ValueError,'no plan was submitted'):
            plans.create(self.store,definition,expected_source=source)
        self.assertEqual(plans.listing(self.store),[])

    def test_changed_proposal_while_queued_prevents_execution_and_promotion(self):
        filename,unused=self.proposal()
        self.store.update_config(dict(paused=True))
        handle=task_review.trial(self.store,filename,emit=False,processes=self.children)
        filename.write_text(filename.read_text()+'\n')
        self.store.update_config(dict(paused=False))
        result=self.finish(handle)
        self.assertEqual(result['state'],'failed')
        self.assertFalse(task_review.review(self.store,filename,handle)['eligible'])

    def test_actual_old_task_reader_cannot_bypass_reviewed_profile_source_gate(self):
        import importlib.util
        values=dict(cwd=str(self.repo),name='reviewed-only',command=['true'])
        profiles.update(self.store,'reviewed_tasks',profiles.task_key(self.repo,'reviewed-only'),values)
        source=subprocess.check_output(['git','show','v0.20.0:agent_builds/profiles.py'],cwd=str(Path(__file__).resolve().parent.parent))
        filename=self.root/'old_profiles.py'
        filename.write_bytes(source)
        spec=importlib.util.spec_from_file_location('actual_old_profile_reader_020',filename)
        old=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        with self.assertRaisesRegex(ValueError,'Unknown shortcut'):
            old.task_args(self.store,Namespace(cwd=str(self.repo),name='reviewed-only',detach=True,events_json=True))

    def test_native_failures_are_reported_for_both_arms_without_a_speedup_claim(self):
        baseline,candidate=self.study_manifests()
        for filename in (baseline,candidate):
            value=json.loads(filename.read_text())
            for stage in value['stages']:
                stage['command']=[sys.executable,'-c','raise SystemExit(7)']
            filename.write_text(json.dumps(value))
        result=workflow_study.run(self.store,baseline,candidate,self.repo,self.root/'failed-study.json',pairs=1,timeout=20)
        self.assertTrue(all(arm['failed_stages']==2 for arm in result['records']))
        self.assertFalse(result['comparisons'][0]['comparable'])
        self.assertIsNone(result['comparisons'][0]['verified_throughput_ratio'])

    def test_source_drift_between_arms_stops_future_submissions(self):
        baseline,candidate=self.study_manifests()
        actual=workflow_study.capture
        calls=[0]
        def observed(*args,**kwargs):
            calls[0]+=1
            if calls[0]==3:
                (self.repo/'source.txt').write_text('changed between arms')
            return actual(*args,**kwargs)
        output=self.root/'drift-study.json'
        with mock.patch('agent_builds.workflow_study.capture',side_effect=observed):
            with self.assertRaisesRegex(ValueError,'remaining arms were not submitted'):
                workflow_study.run(self.store,baseline,candidate,self.repo,output,pairs=1,timeout=20)
        progress=json.loads(output.with_name(output.name+'.progress.json').read_text())
        self.assertEqual(len(progress['arms']),1)
        self.assertEqual(len(plans.listing(self.store)),1)

    def test_real_tui_opens_workbench_and_exits_in_an_isolated_pseudoterminal(self):
        import fcntl
        import pty
        import selectors
        import struct
        import termios
        self.seed()
        master,slave=pty.openpty()
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,100,0,0))
        env=dict(self.env,TERM='xterm-256color')
        child=subprocess.Popen([sys.executable,'-m','agent_builds','tui','--cwd',str(self.repo)],
            stdin=slave,stdout=slave,stderr=slave,env=env,start_new_session=True)
        self.children.append(child)
        os.close(slave)
        selector=selectors.DefaultSelector()
        selector.register(master,selectors.EVENT_READ)
        output=b''
        try:
            sent=False
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                for unused,events in selector.select(.1):
                    output+=os.read(master,65536)
                if not sent and b'BUILDR' in output:
                    os.write(master,b'r')
                    sent=True
                if b'WORKBENCH' in output and b'Future-run' in output:
                    break
            self.assertIn(b'Future-run',output[-200000:])
            os.write(master,b'q')
            self.assertEqual(child.wait(timeout=3),0)
        finally:
            selector.close()
            os.close(master)


class WorkbenchMCPTests(unittest.TestCase):
    setUp=test_mcp.MCPTests.setUp
    tearDown=test_mcp.MCPTests.tearDown
    client=test_mcp.MCPTests.client
    send=test_mcp.MCPTests.send
    receive=test_mcp.MCPTests.receive
    request=test_mcp.MCPTests.request
    invoke=test_mcp.MCPTests.invoke

    def test_workbench_is_available_in_read_only_server_and_keeps_the_queue_unchanged(self):
        job=self.store.submit(self.root,['true'],'local','quick',1,.25,'fixture','',10)
        client=self.client(read_only=True)
        before=self.store.db.total_changes
        response=self.invoke(client,'buildr_workbench',dict(job_id=job))['result']
        self.assertFalse(response['isError'],response)
        self.assertEqual(response['structuredContent']['job_id'],job)
        self.assertFalse(response['structuredContent']['changes_applied'])
        self.assertEqual(before,self.store.db.total_changes)
