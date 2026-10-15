import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_builds import experiments


class ExperimentTests(unittest.TestCase):
    def test_unqueued_driver_is_rejected_before_creating_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'proof.json'
            with mock.patch.dict(os.environ,{'AGENT_BUILDS_JOB_ID':''}):
                with self.assertRaisesRegex(ValueError,'shared queue'):
                    experiments.run(path,pairs=1,agents=2)
            self.assertEqual(list(Path(temp).iterdir()),[])

    def test_invalid_workloads_are_rejected_before_execution(self):
        for pairs,agents in ((0,2),(6,2),(1,1),(1,7),(True,2)):
            with self.assertRaises(ValueError):
                experiments.run('/tmp/unused-experiment.json',pairs=pairs,agents=agents)
        with self.assertRaises(ValueError):
            experiments.run('/tmp/unused-experiment.json',scenarios=['unknown'])

    def test_paired_arms_capture_real_outcomes_and_preserve_affinity(self):
        # The full test suite itself is globally queued. This fixture supplies
        # metadata for its bounded, isolated inner scheduler experiment.
        before=os.sched_getaffinity(0)
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'report.json'
            with mock.patch('agent_builds.experiments.parent_budget',return_value=dict(job_id='fixture',slots=2,memory_gb=2)):
                result=experiments.run(output,pairs=2,agents=2,cpus=2,scenarios=['shared_output'])
            self.assertEqual(os.sched_getaffinity(0),before)
            self.assertEqual(len(result['records']),4)
            self.assertEqual([r['mode'] for r in result['records']],['independent','queued','queued','independent'])
            self.assertFalse(result['autonomous_coding_productivity_measured'])
            self.assertEqual(output.stat().st_mode&0o777,0o600)
            self.assertEqual(json.loads(output.read_text())['schema'],'buildr-paired-experiment-v1')
            for record in result['records']:
                self.assertEqual(record['jobs'],2)
                self.assertGreater(record['wall_seconds'],0)
                self.assertEqual(record['verified_jobs'],sum(r['verified'] for r in record['results']))
                if record['mode']=='queued':
                    self.assertEqual(record['verified_jobs'],2)
                self.assertTrue(all(Path(r['log_path']).exists() for r in record['results']))

    def test_existing_output_is_not_overwritten_or_used_to_run_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'report.json'
            path.write_text('retain me')
            with mock.patch('agent_builds.experiments.parent_budget',return_value={}),mock.patch('subprocess.Popen',side_effect=AssertionError('must not execute')):
                with self.assertRaises(ValueError):
                    experiments.run(path,pairs=1,agents=2)
            self.assertEqual(path.read_text(),'retain me')

    def test_parent_budget_cannot_exceed_outer_reservation(self):
        reader=mock.Mock()
        reader.get.return_value=dict(state='running',slots=1,memory_gb=2)
        with mock.patch.dict(os.environ,{'AGENT_BUILDS_JOB_ID':'fixture'}),mock.patch('agent_builds.experiments.Store',return_value=reader):
            with self.assertRaisesRegex(ValueError,'outer reservation'):
                experiments.parent_budget(2)
        reader.close.assert_called_once()

    def test_shared_worker_checks_the_actual_output_and_keeps_own_result(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(experiments.worker('shared_output',temp,'one'),0)
            result=json.loads((Path(temp)/'result-one.json').read_text())
            self.assertTrue(result['verified'])
            self.assertEqual((Path(temp)/'shared-output.txt').read_text(),'one')

    def test_rotating_batch_comparison_keeps_native_results_and_one_batch_reservation(self):
        before=os.sched_getaffinity(0)
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'batch-proof.json'
            with mock.patch('agent_builds.experiments.parent_budget',return_value=dict(job_id='fixture',slots=2,memory_gb=2)):
                result=experiments.run(output,pairs=2,agents=2,cpus=2,scenarios=['python_verification'],include_batches=True)
            self.assertEqual(os.sched_getaffinity(0),before)
            self.assertEqual(len(result['records']),8)
            self.assertEqual([r['mode'] for r in result['records'][:4]],['independent','queued','serial_batch','parallel_batch'])
            self.assertEqual([r['mode'] for r in result['records'][4:]],['queued','serial_batch','parallel_batch','independent'])
            self.assertEqual(len(result['batch_comparisons']),4)
            self.assertTrue(all(r['verified_jobs']==2 for r in result['records']))
            for arm in result['records']:
                if arm['mode'] in ('serial_batch','parallel_batch'):
                    self.assertEqual(len({r['queue_job_id'] for r in arm['results']}),1)
                    self.assertTrue(all(r['exit_code']==0 and r['wrapper_exit_code']==0 for r in arm['results']))
                    self.assertEqual({r['slots_per_check'] for r in arm['results']},{1 if arm['mode']=='parallel_batch' else 2})
            self.assertFalse(result['autonomous_coding_productivity_measured'])

    def test_batch_comparison_requires_compatible_fixture(self):
        with self.assertRaises(ValueError):
            experiments.run('/tmp/not-created-batch.json',scenarios=['shared_output'],include_batches=True)
        with self.assertRaises(ValueError):
            experiments.run('/tmp/not-created-batch.json',include_batches='yes')
