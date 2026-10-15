"""Declared helper inputs are evidence, with strict plan checks and safe upgrades."""
import hashlib
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
import time
import unittest
from unittest import mock

from agent_builds import plans, source
from agent_builds.store import Store
import test_mcp
import test_plans

ROOT = Path(__file__).resolve().parent.parent


class InputPlanTests(unittest.TestCase):
    setUp = test_plans.PlanTests.setUp
    tearDown = test_plans.PlanTests.tearDown
    git = test_plans.PlanTests.git
    stage = test_plans.PlanTests.stage
    create = test_plans.PlanTests.create
    launch = test_plans.PlanTests.launch
    finish = test_plans.PlanTests.finish
    observe = test_plans.PlanTests.observe
    cli = test_plans.PlanTests.cli

    def test_strict_external_helper_changed_while_queued_never_executes(self):
        helper = self.root/'helper.sh'
        helper.write_text('original helper')
        self.store.update_config(dict(paused=True))
        handle = self.launch([self.stage('verify', 'from pathlib import Path; Path("out").mkdir()'),
                              self.stage('dependent')], input_files=['../helper.sh'], source_policy='require_unchanged')
        self.observe(handle, lambda p: p['stages'][0]['job'] is not None)
        helper.write_text('changed helper')
        self.store.update_config(dict(paused=False))
        plan = self.finish(handle)
        job = plan['stages'][0]['job']
        self.assertEqual(plan['state'], 'failed')
        self.assertEqual(job['exit_code'], 2)
        self.assertTrue(job['source']['changed_since_submission'])
        self.assertFalse(job['source']['command_started'])
        self.assertEqual(plan['stages'][1]['state'], 'skipped')
        self.assertFalse((self.repo/'out').exists())
        self.assertEqual(job['source']['submission']['inputs']['files'][0]['sha256'], hashlib.sha256(b'original helper').hexdigest())

    def test_strict_external_helper_changed_during_run_preserves_native_success(self):
        helper = self.root/'helper.sh'
        helper.write_text('original')
        handle = self.launch([self.stage('verify', 'from pathlib import Path; Path({!r}).write_text("changed")'.format(str(helper))),
                              self.stage('dependent')], input_files=[str(helper)], source_policy='require_unchanged')
        plan = self.finish(handle)
        self.assertEqual(plan['state'], 'failed')
        self.assertEqual(plan['exit_code'], 1)
        self.assertEqual(plan['stages'][0]['job']['exit_code'], 0)
        self.assertTrue(plan['stages'][0]['job']['source']['changed_during_run'])
        self.assertEqual(plan['stages'][1]['state'], 'skipped')

    def test_strict_missing_input_never_executes(self):
        handle = self.launch([self.stage('verify')], input_files=['../missing.sh'], source_policy='require_unchanged')
        job = self.finish(handle)['stages'][0]['job']
        self.assertEqual(job['exit_code'], 2)
        self.assertIsNone(job['source']['changed_since_submission'])
        self.assertFalse(job['source']['command_started'])

    def test_unchanged_inputs_apply_to_all_stages_including_subdirectory(self):
        helper = self.root/'helper.sh'
        helper.write_text('stable')
        (self.repo/'out').mkdir()
        handle = self.launch([self.stage('first'), self.stage('subdir', cwd='out')],
                             input_files=['../helper.sh'], source_policy='require_unchanged')
        plan = self.finish(handle)
        self.assertEqual(plan['state'], 'succeeded')
        self.assertTrue(all(s['job']['source']['changed_since_submission'] is False for s in plan['stages']))

    def test_detached_relative_input_is_anchored_to_submitted_cwd_and_report_only(self):
        helper = self.root/'helper.sh'
        helper.write_text('initial')
        self.store.update_config(dict(paused=True))
        result = self.cli('run', '--cwd', str(self.repo), '--jobs', '1', '--memory-gb', '.25',
                          '--input-file', '../helper.sh', '--detach', '--events-json', '--', sys.executable, '-c', 'raise SystemExit(7)')
        self.assertEqual(result.returncode, 0, result.stderr)
        handle = json.loads(result.stderr)['job_id']
        helper.write_text('changed while queued')
        self.store.update_config(dict(paused=False))
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            job = self.store.get(handle)
            if job['state'] in ('succeeded', 'failed', 'lost'):
                break
            time.sleep(.03)
        self.assertEqual(job['exit_code'], 7)
        self.assertTrue(job['source']['changed_since_submission'])
        self.assertEqual(job['source']['submission']['inputs']['files'][0]['path'], str(helper))
        self.assertEqual(job['input_protocol'], 1)

    def test_saved_task_and_batch_carry_declared_inputs(self):
        helper = self.root/'helper.sh'
        helper.write_text('stable')
        result = self.cli('task', 'add', 'verify-input', '--cwd', str(self.repo), '--jobs', '1', '--memory-gb', '.25',
                          '--input-file', '../helper.sh', '--', sys.executable, '-c', 'print("ok")')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli('task', 'run', 'verify-input', '--cwd', str(self.repo), '--events-json')
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.root/'batch.json'
        manifest.write_text(json.dumps(dict(checks=[dict(name='one', command=[sys.executable, '-c', 'print("ok")'])])))
        result = self.cli('batch', 'run', str(manifest), '--cwd', str(self.repo), '--input-file', '../helper.sh', '--events-json')
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.store.rows()
        self.assertEqual(len(rows), 2)
        for job in rows:
            self.assertEqual(job['source']['finish']['inputs']['files'][0]['path'], str(helper))
            self.assertFalse(job['source']['changed_during_run'])


class InputCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.helper = self.root/'helper'
        self.helper.write_bytes(b'private contents')
        self.repo = dict(status='captured', repository='/fixture', head='abc', tracked_diff_sha256='a', untracked_sha256='b')
        self.patch = mock.patch.object(source, 'capture_repository', side_effect=lambda cwd: dict(self.repo))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def capture(self, *paths):
        return source.capture(self.root, list(paths) or [str(self.helper)])

    def test_hashes_only_no_contents_and_mode_drift(self):
        first = self.capture()
        evidence = first['inputs']['files'][0]
        self.assertEqual(evidence['sha256'], hashlib.sha256(b'private contents').hexdigest())
        self.assertNotIn('private contents', json.dumps(first))
        self.assertIs(source.changed(first, self.capture()), False)
        self.helper.chmod(0o700 if evidence['mode'] != 0o700 else 0o600)
        self.assertIs(source.changed(first, self.capture()), True)
        self.helper.write_bytes(b'new contents')
        self.assertIs(source.changed(first, self.capture()), True)

    def test_missing_symlink_directory_and_fifo_are_incomplete_without_blocking(self):
        missing = self.root/'missing'
        symlink = self.root/'symlink'
        symlink.symlink_to(self.helper)
        fifo = self.root/'fifo'
        os.mkfifo(str(fifo))
        result = self.capture(str(missing), str(symlink), str(self.root), str(fifo))
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual({f['status'] for f in result['inputs']['files']}, {'missing', 'not_regular_file'})
        self.assertIsNone(source.changed(result, self.capture(str(missing), str(symlink), str(self.root), str(fifo))))
        self.assertEqual(result['repository_status'], 'captured')

    def test_partial_capture_keeps_definite_drift_but_never_claims_unchanged(self):
        paths = [str(self.helper), str(self.root/'missing')]
        first = source.capture(self.root, paths)
        self.assertIsNone(source.changed(first, source.capture(self.root, paths)))
        self.helper.write_bytes(b'changed')
        self.assertIs(source.changed(first, source.capture(self.root, paths)), True)
        self.assertIs(source.changed(first, self.repo), True)
        self.repo['head'] = 'different'
        self.assertIs(source.changed(first, source.capture(self.root, paths)), True)

    def test_normalization_is_literal_bounded_and_does_not_follow_leaf_symlink(self):
        link = self.root/'link'
        link.symlink_to(self.helper)
        self.assertEqual(source.normalize_inputs(self.root, ['./link', 'link']), [str(link)])
        for value in ('file', None, [''] , ['\0'], [False], ['x'*4097], ['x']*17):
            with self.assertRaises(ValueError):
                source.normalize_inputs(self.root, value)
        self.assertEqual(source.capture(self.root), self.repo)

    def test_file_size_total_size_and_time_are_bounded(self):
        large = self.root/'large'
        with large.open('wb') as output:
            output.truncate(8*1024**2+1)
        self.assertEqual(self.capture(str(large))['inputs']['files'][0]['status'], 'size_limit')
        paths = []
        for index in range(3):
            path = self.root/str(index)
            with path.open('wb') as output:
                output.truncate(8*1024**2)
            paths.append(str(path))
        result = self.capture(*paths)
        self.assertEqual([f['status'] for f in result['inputs']['files']], ['captured', 'captured', 'size_limit'])
        with mock.patch.object(source.time, 'monotonic', side_effect=[0, 4]):
            result = self.capture()
        self.assertEqual(result['inputs']['files'][0]['status'], 'capture_limit')

    def test_chmod_during_digest_is_incomplete(self):
        real_lstat = Path.lstat
        calls = [0]
        def changing(path, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                os.chmod(str(path), 0o700)
            return real_lstat(path, *args, **kwargs)
        with mock.patch.object(Path, 'lstat', changing):
            self.assertEqual(source.digest_file(self.helper)['status'], 'changed_during_capture')


class InputUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = tempfile.TemporaryDirectory(prefix='buildr-input-old-')
        archive = subprocess.check_output(['git', 'archive', 'v0.16.1', 'agent_builds'], cwd=str(ROOT))
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(cls.archive.name)
        path = Path(cls.archive.name)/'agent_builds'
        alias = 'buildr_inputs_old_0161'
        spec = importlib.util.spec_from_file_location(alias, path/'__init__.py', submodule_search_locations=[str(path)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[alias] = package
        spec.loader.exec_module(package)
        cls.old_plans = __import__(alias+'.plans', fromlist=['control'])
        cls.old_store = __import__(alias+'.store', fromlist=['Store']).Store

    @classmethod
    def tearDownClass(cls):
        cls.archive.cleanup()

    def test_read_only_old_schema_does_not_create_input_columns_or_triggers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = self.old_store(root)
            before = list(old.db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name'))
            reader = Store(root, read_only=True)
            try:
                self.assertEqual(plans.listing(reader), [])
                columns = {row[1] for row in reader.db.execute('PRAGMA table_info(plans)')}
                self.assertNotIn('input_protocol', columns)
                self.assertEqual([tuple(row) for row in reader.db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')],
                                 [tuple(row) for row in before])
            finally:
                reader.close()
                old.close()
            missing = root/'absent'
            reader = Store(missing, read_only=True)
            reader.close()
            self.assertFalse(missing.exists())

    def test_actual_old_takeover_and_attachment_fail_before_dispatch_and_roll_back_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root/'queue'
            env = dict(os.environ, AGENT_BUILDS_STATE=str(state))
            env.pop('AGENT_BUILDS_JOB_ID', None)
            with mock.patch.dict(os.environ, env, clear=True):
                # Load an old writable connection before the migration as well.
                old = self.old_store(state)
                current = Store(state)
                try:
                    current.update_config(dict(min_free_gb=0, min_available_gb=0))
                    definition = plans.validate(dict(input_files=['helper'], source_policy='require_unchanged',
                        stages=[dict(name='verify', command=['true'], jobs=1, memory_gb=.25)]), root, current.config())
                    handle = plans.create(current, definition)
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'Declared input files'):
                        self.old_plans.control(handle)
                    plan = plans.get(current, handle)
                    self.assertIsNone(plan['owner_pid'])
                    job, token = plans.prepare(current, plan, plan['stages'][0])
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'Declared input files'):
                        self.old_plans.attach_job(old, job, token)
                    self.assertEqual(current.db.execute('SELECT attached FROM plan_stages WHERE job_id=?', (job,)).fetchone()[0], 0)
                    args, attached = plans.attach_job(current, job, token)
                    self.assertEqual(args.input_files, [str(root/'helper')])
                    self.assertEqual(attached, job)
                    current.finish(job, 'failed', 2)
                    # A prior current takeover cannot authorize a later old one.
                    for index in range(2):
                        current.db.execute('UPDATE plans SET owner_pid=NULL,input_owner_epoch=input_owner_epoch+1 WHERE id=?', (handle,))
                        with self.assertRaisesRegex(sqlite3.IntegrityError, 'Declared input files'):
                            self.old_plans.control(handle)
                    self.assertEqual(len(current.rows()), 1)
                finally:
                    current.close()
                    old.close()


class InputMCPTests(unittest.TestCase):
    # Reuse only the stdio fixture helpers, keeping the original tests separate.
    setUp = test_mcp.MCPTests.setUp
    tearDown = test_mcp.MCPTests.tearDown
    client = test_mcp.MCPTests.client
    send = test_mcp.MCPTests.send
    receive = test_mcp.MCPTests.receive
    request = test_mcp.MCPTests.request
    invoke = test_mcp.MCPTests.invoke
    arguments = test_mcp.MCPTests.arguments

    def test_reconnect_freezes_input_evidence_and_rejects_conflicting_payload(self):
        helper = self.root/'helper'
        helper.write_text('original')
        self.store.update_config(dict(paused=True))
        first = self.client()
        args = self.arguments()
        args['input_files'] = ['helper']
        result = self.invoke(first, 'buildr_submit', args)['result']
        self.assertFalse(result['isError'], result)
        handle = result['structuredContent']['result']['job_id']
        first.stdin.close()
        first.wait(timeout=5)
        helper.write_text('changed')
        second = self.client()
        repeated = self.invoke(second, 'buildr_submit', args)['result']['structuredContent']
        self.assertEqual(repeated['result']['job_id'], handle)
        conflict = self.invoke(second, 'buildr_submit', dict(args, input_files=['other']))['result']
        self.assertTrue(conflict['isError'])
        self.assertEqual(len(self.store.rows()), 1)
        self.assertEqual(self.store.get(handle)['source']['submission']['inputs']['files'][0]['sha256'], hashlib.sha256(b'original').hexdigest())
        self.store.update_config(dict(paused=False))
        result = self.invoke(second, 'buildr_wait', dict(job_ids=[handle], timeout=5))['result']['structuredContent']
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(self.store.get(handle)['source']['changed_since_submission'])

    def test_batch_inputs_and_invalid_input_arrays(self):
        helper = self.root/'helper'
        helper.write_text('stable')
        client = self.client()
        args = dict(request_key='batch-input-test', cwd=str(self.root), input_files=['helper'],
                    track_source=False, manifest=dict(checks=[dict(name='one', command=[sys.executable, '-c', 'raise SystemExit(7)'])]))
        result = self.invoke(client, 'buildr_batch_submit', args)['result']
        self.assertFalse(result['isError'], result)
        handle = result['structuredContent']['result']['job_id']
        result = self.invoke(client, 'buildr_wait', dict(job_ids=[handle], timeout=5))['result']['structuredContent']
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(self.store.get(handle)['source']['finish']['inputs']['status'], 'captured')
        for value in ('helper', ['helper']*17, [''], ['\0']):
            response = self.invoke(client, 'buildr_submit', dict(self.arguments(), input_files=value))
            if 'error' in response:
                self.assertEqual(response['error']['code'], -32602)
            else:
                self.assertTrue(response['result']['isError'])
        self.assertEqual(len(self.store.rows()), 1)
