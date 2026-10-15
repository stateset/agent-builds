import copy
import io
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_builds import mcp
from agent_builds.store import Store

ROOT = str(Path(__file__).resolve().parent.parent)
INIT = dict(jsonrpc='2.0', id=1, method='initialize', params=dict(
    protocolVersion='2025-11-25', capabilities={}, clientInfo=dict(name='fixture', version='1')))
READY = dict(jsonrpc='2.0', method='notifications/initialized')


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state')
        self.store.update_config(dict(cpu_slots=2, max_running=2, max_heavy=2, memory_gb=4,
            min_free_gb=0, min_available_gb=0, quick_reserve_slots=0, quick_reserve_jobs=0,
            quick_reserve_memory_gb=0, nice=0))
        self.env = dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root), PYTHONPATH=ROOT)
        # Only these explicitly isolated integration fixtures clear the marker.
        self.env.pop('AGENT_BUILDS_JOB_ID', None)
        self.clients = []

    def tearDown(self):
        # Settle only the short-lived fixture work created by this test.
        deadline = time.monotonic()+8
        while self.store.counts(active=True).get('running', 0) and time.monotonic()<deadline:
            time.sleep(.03)
        for client in self.clients:
            if client.poll() is None:
                client.stdin.close()
                try:
                    client.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    client.kill()
                    client.wait()
            client.stdout.close()
            client.stderr.close()
        self.store.close()
        self.temp.cleanup()

    def client(self, read_only=False, env=None):
        command = [sys.executable, '-m', 'agent_builds', 'mcp', 'serve']
        if read_only:
            command.append('--read-only')
        client = subprocess.Popen(command, env=env or self.env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.clients.append(client)
        self.request(client, INIT)
        self.send(client, READY)
        return client

    def send(self, client, request):
        client.stdin.write(json.dumps(request).encode()+b'\n')
        client.stdin.flush()

    def receive(self, client):
        with selectors.DefaultSelector() as selector:
            selector.register(client.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(8), 'MCP response timeout')
        line = client.stdout.readline()
        self.assertTrue(line, 'Unexpected MCP EOF')
        return json.loads(line)

    def request(self, client, request):
        self.send(client, request)
        return self.receive(client)

    def invoke(self, client, name, arguments=None, identifier=2):
        return self.request(client, dict(jsonrpc='2.0', id=identifier, method='tools/call',
            params=dict(name=name, arguments=arguments or {})))

    def arguments(self, key='fixture-request-001', command=None):
        return dict(request_key=key, cwd=str(self.root), jobs=1, memory_gb=.25,
            track_source=False, command=command or [sys.executable, '-c', 'print("fixture-success")'])

    def test_handshake_discovery_and_read_only_mode_do_not_initialize_state(self):
        missing = self.root/'does-not-exist'
        env = dict(self.env, AGENT_BUILDS_STATE=str(missing))
        client = self.client(read_only=True, env=env)
        listed = self.request(client, dict(jsonrpc='2.0', id=2, method='tools/list'))['result']['tools']
        self.assertTrue(listed)
        self.assertTrue(all(t['annotations']['readOnlyHint'] for t in listed))
        self.assertNotIn('buildr_submit', {t['name'] for t in listed})
        status = self.invoke(client, 'buildr_status')['result']
        self.assertFalse(status['structuredContent']['initialized'])
        denied = self.invoke(client, 'buildr_submit', self.arguments())['result']
        self.assertTrue(denied['isError'])
        self.assertFalse(missing.exists())

    def test_stdio_submission_reconnect_wait_and_log_keep_literal_argv(self):
        first = self.client()
        literal = '$(touch forbidden); echo not-a-shell'
        args = self.arguments(command=[sys.executable, '-c', 'import sys; print(sys.argv[1])', literal])
        receipt = self.invoke(first, 'buildr_submit', args)['result']
        self.assertFalse(receipt['isError'])
        submitted = receipt['structuredContent']
        handle = submitted['result']['job_id']
        first.stdin.close()
        first.wait(timeout=5)
        second = self.client()
        repeated = self.invoke(second, 'buildr_submit', args)['result']['structuredContent']
        self.assertEqual(repeated['result']['job_id'], handle)
        audit = self.invoke(second, 'buildr_submission', dict(request_key=args['request_key']))['result']['structuredContent']
        self.assertEqual(audit['submission_state'], 'submitted')
        result = self.invoke(second, 'buildr_wait', dict(job_ids=[handle], timeout=5))['result']['structuredContent']
        self.assertTrue(result['complete'])
        self.assertEqual(result['exit_code'], 0)
        logs = self.invoke(second, 'buildr_logs', dict(job_id=handle))['result']['structuredContent']
        self.assertTrue(logs['untrusted_output'])
        self.assertIn(literal, '\n'.join(logs['lines']))
        self.assertEqual(len(self.store.rows()), 1)
        self.assertFalse((self.root/'forbidden').exists())

    def test_conflicting_request_key_does_not_submit_again(self):
        client = self.client()
        args = self.arguments()
        self.invoke(client, 'buildr_submit', args)
        changed = dict(args, command=['different-command'])
        result = self.invoke(client, 'buildr_submit', changed)['result']
        self.assertTrue(result['isError'])
        self.assertEqual(len(self.store.rows()), 1)

    def test_concurrent_connections_with_same_key_create_only_one_job(self):
        import concurrent.futures
        args = self.arguments()
        with mock.patch.dict(os.environ, self.env, clear=True):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: mcp.call('buildr_submit', args), range(2)))
            final = mcp.call('buildr_submission', dict(request_key=args['request_key']))
        self.assertEqual(len(self.store.rows()), 1)
        self.assertEqual(final['submission_state'], 'submitted')
        self.assertTrue(all(r['replay_safe'] for r in results))

    def test_pending_or_uncertain_submission_never_launches_on_reconnect(self):
        args = self.arguments()
        self.assertTrue(mcp.reserve(self.store, 'buildr_submit', args, {}))
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch('agent_builds.submissions.submit_detached', side_effect=AssertionError('must not retry')):
            result = mcp.call('buildr_submit', args)
        self.assertEqual(result['submission_state'], 'uncertain')
        self.assertEqual(len(self.store.rows()), 0)

    def test_retained_receipt_recovers_handle_after_lost_audit_update(self):
        args = self.arguments()
        handle = self.store.submit(self.root, ['true'], 'local', 'quick', 1, .25, str(self.root), '', 10)
        path = self.store.root/'receipt-mcp-fixture.json'
        path.write_text(json.dumps(dict(job_id=handle)))
        mcp.reserve(self.store, 'buildr_submit', args, dict(receipt_path=str(path)))
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch('agent_builds.submissions.submit_detached', side_effect=AssertionError('must not retry')):
            result = mcp.call('buildr_submit', args)
        self.assertEqual(result['submission_state'], 'submitted')
        self.assertEqual(result['result']['job_id'], handle)
        self.assertTrue(path.exists())  # inspection does not consume evidence

    def test_submission_receipt_replay_survives_changed_budgets_or_missing_checkout(self):
        args = self.arguments()
        mcp.reserve(self.store, 'buildr_submit', args, dict(job_id='abcdefabcdef'))
        mcp.record(self.store, args['request_key'], 'submitted', dict(job_id='abcdefabcdef'))
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch('pathlib.Path.is_dir', return_value=False), \
                mock.patch('agent_builds.submissions.submit_detached', side_effect=AssertionError('must not retry')):
            result = mcp.call('buildr_submit', args)
        self.assertEqual(result['result']['job_id'], 'abcdefabcdef')

    def test_model_and_protocol_notifications_cannot_execute_tools(self):
        server = mcp.Server()
        with mock.patch('agent_builds.mcp.call', side_effect=AssertionError('must not execute')):
            self.assertIsNone(server.dispatch(dict(jsonrpc='2.0', method='tools/call',
                params=dict(name='buildr_submit', arguments=self.arguments()))))
            with self.assertRaises(mcp.ProtocolError):
                server.dispatch(dict(jsonrpc='2.0', id=2, method='tools/call', params=dict(name='buildr_submit')))

    def test_invalid_arguments_are_protocol_errors_before_state_changes(self):
        server = mcp.Server()
        server.dispatch(INIT)
        server.dispatch(READY)
        for edit in (dict(jobs=True), dict(command='echo hello'), dict(memory_gb=float('nan')),
                     dict(unrecognized=True), dict(command=[]), dict(request_key='short')):
            args = dict(self.arguments(), **edit)
            with self.subTest(edit=edit), mock.patch('agent_builds.mcp.Store', side_effect=AssertionError('no writes')):
                with self.assertRaises(mcp.ProtocolError):
                    server.dispatch(dict(jsonrpc='2.0', id=2, method='tools/call', params=dict(name='buildr_submit', arguments=args)))

    def test_parse_errors_batch_and_oversize_are_bounded_and_protocol_only(self):
        source = io.BytesIO(b'not-json\n[]\n{"jsonrpc":"2.0","method":"ping","id":4}\n')
        output = io.BytesIO()
        self.assertEqual(mcp.serve(input_stream=source, output_stream=output), 0)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[0]['error']['code'], -32700)
        self.assertEqual(rows[1]['error']['code'], -32600)
        self.assertEqual(rows[2]['result'], {})
        source, output = io.BytesIO(b'x'*(mcp.MAX_REQUEST+2)), io.BytesIO()
        self.assertEqual(mcp.serve(input_stream=source, output_stream=output), 2)
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_new_protocol_probe_fails_deterministically_and_legacy_handshake_negotiates(self):
        server = mcp.Server()
        with self.assertRaises(mcp.ProtocolError) as error:
            server.dispatch(dict(jsonrpc='2.0', id=1, method='server/discover'))
        self.assertEqual(error.exception.code, -32601)
        request = copy.deepcopy(INIT)
        request['params']['protocolVersion'] = '2026-07-28'
        self.assertEqual(server.dispatch(request)['protocolVersion'], '2025-11-25')

    def test_native_failure_is_job_data_and_mcp_timeout_does_not_cancel(self):
        client = self.client()
        args = self.arguments(command=[sys.executable, '-c', 'import time; time.sleep(.5); raise SystemExit(7)'])
        receipt = self.invoke(client, 'buildr_submit', args)['result']['structuredContent']
        handle = receipt['result']['job_id']
        pending = self.invoke(client, 'buildr_wait', dict(job_ids=[handle], timeout=.01))['result']
        self.assertFalse(pending['isError'])
        self.assertEqual(pending['structuredContent']['exit_code'], 124)
        done = self.invoke(client, 'buildr_wait', dict(job_ids=[handle], timeout=5))['result']
        self.assertFalse(done['isError'])
        self.assertEqual(done['structuredContent']['exit_code'], 7)
        self.assertEqual(self.store.get(handle)['state'], 'failed')

    def test_nested_mcp_submission_is_rejected_without_creating_a_job(self):
        env = dict(self.env, AGENT_BUILDS_JOB_ID='outer-job')
        client = self.client(env=env)
        result = self.invoke(client, 'buildr_submit', self.arguments())['result']
        self.assertTrue(result['isError'])
        self.assertEqual(self.store.rows(), [])

    def test_inline_plan_validation_submission_and_native_summary(self):
        client = self.client()
        manifest = dict(version=1, stages=[dict(name='verify', command=[sys.executable, '-c', 'print("verified")'], jobs=1, memory_gb=.25)])
        args = dict(cwd=str(self.root), manifest=manifest)
        checked = self.invoke(client, 'buildr_plan_validate', args)['result']
        self.assertTrue(checked['structuredContent']['valid'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM plans').fetchone()[0], 0)
        args['request_key'] = 'fixture-plan-001'
        receipt = self.invoke(client, 'buildr_plan_submit', args)['result']['structuredContent']
        handle = receipt['result']['plan_id']
        self.assertEqual(self.invoke(client, 'buildr_plan_submit', args)['result']['structuredContent']['result']['plan_id'], handle)
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            result = self.invoke(client, 'buildr_plan', dict(plan_id=handle))['result']['structuredContent']
            if result['complete']:
                break
            time.sleep(.04)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM plans').fetchone()[0], 1)

    def test_unknown_handle_is_a_tool_error_and_log_tail_is_bounded(self):
        client = self.client()
        self.assertTrue(self.invoke(client, 'buildr_job', dict(job_id='abcdefabcdef'))['result']['isError'])
        handle = self.store.submit(self.root, ['true'], 'local', 'quick', 1, .25, str(self.root), '', 10)
        path = self.store.log_path(handle)
        path.write_text(('x'*1000+'\n')*100)
        result = self.invoke(client, 'buildr_logs', dict(job_id=handle, lines=2))['result']['structuredContent']
        self.assertEqual(len(result['lines']), 2)
        self.assertTrue(result['byte_window_truncated'])

    def test_explicit_cancellation_is_idempotent_and_preserves_other_jobs(self):
        client = self.client()
        first = self.store.submit(self.root, ['true'], 'local', 'quick', 1, .25, 'one', '', 10)
        second = self.store.submit(self.root, ['true'], 'local', 'quick', 1, .25, 'two', '', 10)
        for _ in range(2):
            result = self.invoke(client, 'buildr_cancel', dict(job_id=first))['result']
            self.assertFalse(result['isError'])
        self.assertTrue(self.store.get(first)['cancel_requested'])
        self.assertFalse(self.store.get(second)['cancel_requested'])
