"""Actual loaded v0.15/v0.16 runners must agree during an ordering upgrade."""
import importlib.util
import io
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

from agent_builds import protection, plans, external
from agent_builds.store import Store

ROOT=Path(__file__).resolve().parent.parent


class PolicyUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archives=[]
        cls.versions={}
        for version in ('0.15.0','0.16.0'):
            temp=tempfile.TemporaryDirectory(prefix='buildr-policy-'+version+'-')
            cls.archives.append(temp)
            archive=subprocess.check_output(['git','archive','v'+version,'agent_builds'],cwd=str(ROOT))
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                bundle.extractall(temp.name)
            path=Path(temp.name)/'agent_builds'
            alias='buildr_policy_'+version.replace('.','_')
            spec=importlib.util.spec_from_file_location(alias,path/'__init__.py',submodule_search_locations=[str(path)])
            package=importlib.util.module_from_spec(spec)
            sys.modules[alias]=package
            spec.loader.exec_module(package)
            cls.versions[version]=__import__(alias+'.store',fromlist=['Store']).Store

    @classmethod
    def tearDownClass(cls):
        for temp in cls.archives:
            temp.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.old15=self.versions['0.15.0'](self.root/'state')
        self.old16=self.versions['0.16.0'](self.root/'state')
        self.readers=[self.old15,self.old16]
        self.old15.update_config(dict(min_free_gb=0,min_available_gb=0,nice=0))

    def tearDown(self):
        for reader in reversed(self.readers):
            reader.close()
        self.temp.cleanup()

    def job(self,store,key,lane='quick',slots=1,scratch=0):
        return store.submit(self.root,['true'],'local',lane,slots,1,key,'',30,scratch_gb=scratch)

    def upgrade(self):
        store=Store(self.root/'state')
        self.readers.append(store)
        return store

    def test_actual_old_priority_cycle_resolves_without_cancelling_or_resubmitting(self):
        self.old15.update_config(dict(protected_quick=True))
        a=self.job(self.old15,'a','heavy',2)
        b=self.job(self.old15,'b','heavy',2)
        self.assertTrue(self.old15.claim(a))
        self.assertTrue(self.old15.claim(b))
        head=self.job(self.old15,'head','heavy',3)
        self.old15.db.execute('UPDATE jobs SET created=? WHERE id=?',(time.time()-121,head))
        older=self.job(self.old15,'older',scratch=.1)
        newer=self.job(self.old16,'newer')
        self.assertFalse(self.old15.claim(older))
        self.assertFalse(self.old16.claim(newer))
        self.assertIn(newer,self.old15.get(older)['reason'])
        self.assertIn(older,self.old16.get(newer)['reason'])
        store=self.upgrade()
        state=protection.status(store)
        self.assertTrue(state['requested'])
        self.assertFalse(state['effective'])
        self.assertEqual(state['required_protocol'],2)
        self.assertEqual(len(state['blocking_jobs']),5)
        # Old activation tries happen inside claim; the SQL barrier must keep
        # both already-loaded implementations in identical soft ordering.
        self.assertTrue(self.old16.claim(newer))
        self.assertFalse(store.config()['protected_quick'])
        self.assertFalse(self.old15.claim(older))
        self.old16.finish(newer,'succeeded',0)
        self.old15.finish(a,'succeeded',0)
        self.assertTrue(self.old15.claim(head))
        self.assertTrue(self.old15.claim(older))
        for job in (b,head,older):
            self.old15.finish(job,'succeeded',0)
        fresh=self.job(store,'fresh')
        self.assertTrue(store.config()['protected_quick'])
        self.assertTrue(store.claim(fresh))
        self.assertEqual(sum(store.counts().values()),6)
        self.assertEqual(store.counts()['succeeded'],5)

    def test_loaded_old_activation_cannot_bypass_idle_controller_barrier(self):
        with mock.patch.dict(os.environ,{},clear=True):
            old_plans=__import__(self.old15.__module__.rsplit('.',1)[0]+'.plans',fromlist=['create'])
            handle=old_plans.create(self.old15,old_plans.validate(dict(stages=[dict(name='verify',command=['true'])]),self.root,self.old15.config()))
        store=self.upgrade()
        store.update_config(dict(protected_quick=True))
        self.old15.update_config(dict(protected_quick=True))
        self.assertFalse(store.config()['protected_quick'])
        self.assertEqual(protection.status(store)['blocking_controllers'][0]['id'],handle)
        self.old15.db.execute("UPDATE plans SET state='succeeded' WHERE id=?",(handle,))
        self.job(store,'new')
        self.assertTrue(store.config()['protected_quick'])

    def test_old_job_and_controller_creation_rejected_before_creation_after_activation(self):
        store=self.upgrade()
        store.update_config(dict(protected_quick=True))
        for old in (self.old15,self.old16):
            with self.assertRaisesRegex(sqlite3.IntegrityError,'installed Buildr runner'):
                self.job(old,'old')
        for table,sql,args in (
            ('plans',"INSERT INTO plans(id,name,cwd,definition,created,state,reserve_protocol) VALUES ('old','old',?,'{}',0,'queued',1)",(str(self.root),)),
            ('external_watches',"INSERT INTO external_watches(id,created,definition,state,reserve_protocol) VALUES ('old',0,'{}','queued',1)",())):
            with self.assertRaisesRegex(sqlite3.IntegrityError,'installed Buildr runner'):
                self.old16.db.execute(sql,args)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        self.assertEqual(sum(store.counts().values()),0)

    def test_reopening_new_and_old_stores_does_not_restart_a_finished_barrier(self):
        store=self.upgrade()
        store.update_config(dict(protected_quick=True))
        activated=protection.status(store)['effective_at']
        for kind in (Store,self.versions['0.15.0'],self.versions['0.16.0']):
            reader=kind(store.root)
            self.readers.append(reader)
        self.assertTrue(store.config()['protected_quick'])
        self.assertEqual(protection.status(store)['effective_at'],activated)
        with self.assertRaises(sqlite3.IntegrityError):
            self.job(self.readers[-1],'still-old')

    def test_read_only_new_inspection_never_changes_old_active_policy(self):
        self.old15.update_config(dict(protected_quick=True))
        reader=Store(self.old15.root,read_only=True)
        self.readers.append(reader)
        self.assertTrue(protection.status(reader)['effective'])
        self.assertEqual(protection.status(reader)['required_protocol'],1)
        self.assertNotIn('required_protocol',{row[1] for row in reader.db.execute('PRAGMA table_info(admission_policy)')})


if __name__=='__main__':
    unittest.main()
