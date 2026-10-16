import json
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_builds import announce
from agent_builds.store import Store


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state')
        self.env = mock.patch.dict(os.environ, HERDR_ENV='1', HERDR_PANE_ID='self')
        self.env.start()
        self.agent = dict(pane_id='other', agent='codex', agent_status='idle',
                          cwd='/repo', agent_session={'value': 'stable-session'})

    def tearDown(self):
        self.env.stop()
        self.store.close()
        self.temp.cleanup()

    def response(self, agent=None, code=0, stderr=''):
        return subprocess.CompletedProcess([], code,
            json.dumps({'result': {'agent': agent or self.agent}}), stderr)

    def test_preview_excludes_caller_and_agents_without_identity(self):
        with mock.patch.object(announce, 'agents', return_value=[self.agent, dict(self.agent, pane_id='self'),
                                                               dict(self.agent, pane_id='missing', agent_session=None)]):
            self.assertEqual(announce.preview(), [self.agent])

    def test_no_inspection_outside_herdr(self):
        with mock.patch.dict(os.environ, HERDR_ENV='0'):
            with self.assertRaises(ValueError):
                announce.require_herdr()

    def test_selected_idle_codex_is_submitted_once(self):
        item = announce.create(self.store, [self.agent])
        with mock.patch.object(announce, 'call', side_effect=[self.response(), self.response()]) as call:
            announce.dispatch_once(self.store, item)
            announce.dispatch_once(self.store, item)
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args[0][:3], ('agent', 'prompt', 'other'))
        self.assertEqual(announce.deliveries(self.store, item)[0]['state'], 'submitted')

    def test_recipient_that_becomes_blocked_is_skipped_without_input(self):
        item = announce.create(self.store, [self.agent])
        with mock.patch.object(announce, 'call', return_value=self.response(dict(self.agent, agent_status='blocked'))) as call:
            announce.dispatch_once(self.store, item)
            self.assertEqual(call.call_count, 1)
        self.assertEqual(announce.deliveries(self.store, item)[0]['state'], 'skipped')

    def test_replacement_session_is_not_prompted(self):
        item = announce.create(self.store, [self.agent])
        changed = dict(self.agent, agent_session={'value': 'replacement'})
        with mock.patch.object(announce, 'call', return_value=self.response(changed)) as call:
            announce.dispatch_once(self.store, item)
            self.assertEqual(call.call_count, 1)
        self.assertEqual(announce.deliveries(self.store, item)[0]['state'], 'skipped')

    def test_uncertain_prompt_is_not_retried(self):
        item = announce.create(self.store, [self.agent])
        with mock.patch.object(announce, 'call', side_effect=[self.response(), subprocess.TimeoutExpired('herdr', 5)]) as call:
            announce.dispatch_once(self.store, item)
            announce.dispatch_once(self.store, item)
            self.assertEqual(call.call_count, 2)
        self.assertEqual(announce.deliveries(self.store, item)[0]['state'], 'unknown')

    def test_blocked_race_is_retried_only_when_no_input_was_sent(self):
        item = announce.create(self.store, [self.agent])
        with mock.patch.object(announce, 'call', side_effect=[self.response(), self.response(code=1, stderr='agent_blocked')]):
            announce.dispatch_once(self.store, item)
        self.assertEqual(announce.deliveries(self.store, item)[0]['state'], 'skipped')

    def test_expiration_never_sends_input(self):
        item = announce.create(self.store, [self.agent], ttl=-1)
        with mock.patch.object(announce, 'call') as call:
            announce.dispatch_once(self.store, item)
            call.assert_not_called()
        self.assertEqual(announce.deliveries(self.store, item)[0]['state'], 'expired')

    def test_custom_message_cli_passes_exact_newlines(self):
        from agent_builds.__main__ import main
        path = Path(self.temp.name) / 'message.txt'
        path.write_text('Feature requests?\nPlease write your suggestions.\n')
        with mock.patch.dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root)), \
                mock.patch('sys.argv', ['agent-builds', 'announce', '--send', '--recipient', 'other', '--message-file', str(path)]), \
                mock.patch.object(announce, 'preview', return_value=[self.agent]), \
                mock.patch.object(announce, 'create', return_value='custom') as create, \
                mock.patch.object(announce, 'spawn_worker'), \
                mock.patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(), 0)
            self.assertEqual(create.call_args[1]['message'], path.read_text())

    def test_empty_custom_message_cannot_send(self):
        from agent_builds.__main__ import main
        path = Path(self.temp.name) / 'message.txt'
        path.write_text('  ')
        with mock.patch.dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root)), \
                mock.patch('sys.argv', ['agent-builds', 'announce', '--send', '--message-file', str(path)]), \
                mock.patch.object(announce, 'create') as create, \
                mock.patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(main(), 2)
            create.assert_not_called()


    def test_working_codex_is_never_selected_or_prompted(self):
        working = dict(self.agent, agent_status='working')
        with mock.patch.object(announce, 'agents', return_value=[working]):
            self.assertEqual(announce.preview(), [])
        item = announce.create(self.store, [self.agent])
        with mock.patch.object(announce, 'call', return_value=self.response(working)) as call:
            announce.dispatch_once(self.store, item)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(announce.deliveries(self.store, item)[0]['state'], 'skipped')

    def test_send_requires_explicit_current_idle_recipients(self):
        with self.assertRaises(ValueError):
            announce.select([self.agent], [])
        with self.assertRaises(ValueError):
            announce.select([self.agent], ['unseen'])
        with self.assertRaises(ValueError):
            announce.create(self.store, [dict(self.agent, agent_status='working')])
        self.assertEqual(announce.select([self.agent], ['other']), [self.agent])
