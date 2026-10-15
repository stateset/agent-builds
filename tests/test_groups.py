import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_builds import groups
from agent_builds.store import Store

ROOT=str(Path(__file__).resolve().parent.parent)


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'state')
        self.env=dict(os.environ,AGENT_BUILDS_STATE=str(self.store.root),PYTHONPATH=ROOT)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job(self):
        return self.store.submit(self.root,['never-execute'], 'local','quick',1,1,'repo','',5)

    def cli(self,*args,env=None):
        return subprocess.run([sys.executable,'-m','agent_builds']+list(args),env=env or self.env,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8)

    def test_create_only_groups_existing_jobs_and_deduplicates_in_order(self):
        first,second=self.job(),self.job()
        before=self.store.db.execute('SELECT * FROM jobs ORDER BY id').fetchall()
        with mock.patch('subprocess.Popen',side_effect=AssertionError('no dispatch')):
            group=groups.create(self.store,[second,first,second],'existing checks')
        self.assertEqual(groups.definition(self.store,group)['jobs'],[second,first])
        self.assertEqual(before,self.store.db.execute('SELECT * FROM jobs ORDER BY id').fetchall())

    def test_unknown_jobs_roll_back_group_creation(self):
        job=self.job()
        with self.assertRaises(ValueError):
            groups.create(self.store,[job,'unknown'])
        self.assertEqual(groups.listing(self.store),[])
        self.assertEqual(len(self.store.rows()),1)

    def test_group_reconnect_preserves_native_failure_and_log_tail(self):
        first,second=self.job(),self.job()
        group=groups.create(self.store,[first,second])
        self.store.finish(first,'succeeded',0)
        self.store.finish(second,'failed',7)
        self.store.log_path(first).write_text('success\n')
        self.store.log_path(second).write_text('earlier\nfailure detail\n')
        reader=Store(self.store.root,read_only=True)
        try:
            result=groups.wait(reader,group,tail_lines=1)
        finally:
            reader.close()
        self.assertEqual(result['exit_code'],7)
        self.assertEqual(result['group']['jobs'][1]['log_tail'],['failure detail'])
        self.assertEqual(result['group']['state'],'failed')
        self.assertEqual(result['group']['result_scope'],'native_job_outcomes')
        self.assertEqual(self.cli('group','wait',group,'--json','--tail','1').returncode,7)

    def test_timeout_does_not_cancel_jobs_or_claim_native_completion(self):
        job=self.job()
        group=groups.create(self.store,[job])
        result=groups.wait(self.store,group,timeout=.02)
        self.assertEqual(result['exit_code'],124)
        self.assertIsNone(result['group']['exit_code'])
        self.assertFalse(result['group']['complete'])
        self.assertEqual(self.store.get(job)['cancel_requested'],0)

    def test_group_export_keeps_missing_source_explicit_and_never_hashes_later_files(self):
        job=self.job()
        group=groups.create(self.store,[job])
        self.store.finish(job,'succeeded',0)
        with mock.patch('agent_builds.source.capture',side_effect=AssertionError('not historic evidence')):
            result=groups.export(self.store,group)
        self.assertEqual(result['jobs'][0]['source'],{})
        self.assertIsNone(result['group']['jobs'][0]['source_changed_during_run'])
        self.assertIn('not reconstructed',result['note'])

    def test_missing_and_legacy_state_inspection_is_read_only(self):
        missing=self.root/'absent'
        result=self.cli('group','list','--json',env=dict(self.env,AGENT_BUILDS_STATE=str(missing)))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),[])
        self.assertFalse(missing.exists())
        self.store.db.execute('DROP TABLE job_groups')
        reader=Store(self.store.root,read_only=True)
        try:
            self.assertEqual(groups.listing(reader),[])
            with self.assertRaises(ValueError):
                groups.get(reader,'not-a-group')
            self.assertFalse(groups.available(reader))
        finally:
            reader.close()

    def test_cli_create_show_list_export_and_no_cancel_surface(self):
        job=self.job()
        created=self.cli('group','create',job,'--name','all checks')
        self.assertEqual(created.returncode,0,created.stderr)
        group=json.loads(created.stdout)['group_id']
        self.assertEqual(json.loads(self.cli('group','show',group,'--json').stdout)['jobs'][0]['id'],job)
        self.assertEqual(len(json.loads(self.cli('group','list','--json').stdout)),1)
        export=self.root/'new-evidence.json'
        self.assertEqual(self.cli('group','export',group,'--output',str(export)).returncode,0)
        self.assertEqual(export.stat().st_mode&0o777,0o600)
        self.assertEqual(self.cli('group','export',group,'--output',str(export)).returncode,2)
        self.assertEqual(self.cli('group','cancel',group).returncode,2)
        self.assertEqual(self.store.get(job)['state'],'queued')

    def test_invalid_group_limits_and_titles_are_rejected(self):
        job=self.job()
        for title in ('',' '*3,'x'*101):
            with self.assertRaises(ValueError):
                groups.create(self.store,[job],title)
        with self.assertRaises(ValueError):
            groups.create(self.store,[])
        with self.assertRaises(ValueError):
            groups.create(self.store,[str(i) for i in range(65)])
        for limit in (0,-1,201,True):
            with self.assertRaises(ValueError):
                groups.listing(self.store,limit)
