"""Transport boundaries: bounded admission, draining and durable-call safety."""
import io
import json
import threading
import unittest
from unittest import mock

from agent_builds import mcp


class Replies(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.changed = threading.Condition()

    def flush(self):
        with self.changed:
            self.changed.notify_all()

    def rows(self):
        return [json.loads(line) for line in self.getvalue().splitlines()]

    def until(self, count):
        with self.changed:
            return self.changed.wait_for(lambda: len(self.rows()) >= count, timeout=5)


class MCPConcurrencyTests(unittest.TestCase):
    def test_each_lane_is_bounded_without_a_hidden_request_backlog(self):
        output, release = Replies(), threading.Event()
        calls, changed = [], threading.Condition()
        names = dict(read='buildr_status', wait='buildr_wait', mutation='buildr_cancel')

        def execute(name, arguments, read_only):
            with changed:
                calls.append(arguments['ordinal'])
                changed.notify_all()
            if not release.wait(5):
                raise AssertionError('Fixture did not release worker')
            return dict(ok=True)

        connection = mcp.Connection(output)
        try:
            with mock.patch('agent_builds.mcp.call', side_effect=execute):
                ordinal = 1
                for lane, limit in mcp.REQUEST_LIMITS.items():
                    for _ in range(limit):
                        connection.submit(ordinal, mcp.ToolRequest(names[lane], dict(ordinal=ordinal), False, lane))
                        ordinal += 1
                with changed:
                    self.assertTrue(changed.wait_for(lambda: len(calls) == 7, timeout=5))
                for lane in names:
                    connection.submit(ordinal, mcp.ToolRequest(names[lane], dict(ordinal=ordinal), False, lane))
                    ordinal += 1
                self.assertEqual(len(output.rows()), 3)
                self.assertEqual({r['error']['data']['lane'] for r in output.rows()}, set(names))
                self.assertTrue(all(r['error']['data']['request_executed'] is False for r in output.rows()))
                self.assertEqual(len(calls), 7)
                release.set()
                self.assertTrue(output.until(10))
                # Reuse IDs after completion, and verify capacity is already free
                # when a client has received the preceding response.
                connection.submit(1, mcp.ToolRequest('buildr_cancel', dict(ordinal=100), False, 'mutation'))
                self.assertTrue(output.until(11))
                self.assertEqual(calls.count(100), 1)
        finally:
            release.set()
            connection.close()
        self.assertFalse(connection.active)
        self.assertEqual(sum(connection.counts.values()), 0)

    def test_duplicate_id_cannot_execute_and_string_and_numeric_ids_are_distinct(self):
        output, release, entered = Replies(), threading.Event(), threading.Event()
        connection = mcp.Connection(output)

        def execute(*args):
            entered.set()
            release.wait(5)
            return dict(ok=True)

        try:
            with mock.patch('agent_builds.mcp.call', side_effect=execute) as call:
                request = mcp.ToolRequest('buildr_status', {}, False, 'read')
                connection.submit(12, request)
                self.assertTrue(entered.wait(5))
                self.assertTrue(connection.reject_duplicate(12))
                self.assertFalse(connection.reject_duplicate('12'))
                connection.submit(12, request)  # defensive admission check too
                connection.submit('12', request)
                self.assertEqual([r['id'] for r in output.rows()], [None, None])
                self.assertTrue(all(r['error']['data']['request_executed'] is False for r in output.rows()))
                release.set()
                self.assertTrue(output.until(4))
                self.assertEqual(call.call_count, 2)
                self.assertEqual({type(r['id']) for r in output.rows() if 'result' in r}, {int, str})
        finally:
            release.set()
            connection.close()

    def test_failure_and_large_response_release_capacity_and_keep_json_lines_intact(self):
        output, connection = Replies(), None
        connection = mcp.Connection(output)
        try:
            with mock.patch('agent_builds.mcp.call', side_effect=RuntimeError('private error')):
                connection.submit(1, mcp.ToolRequest('buildr_cancel', {}, False, 'mutation'))
                self.assertTrue(output.until(1))
            self.assertEqual(output.rows()[0]['error']['code'], -32603)
            self.assertNotIn('private error', output.getvalue().decode())
            with mock.patch('agent_builds.mcp.call', return_value=dict(text='x'*mcp.MAX_RESPONSE)):
                connection.submit(2, mcp.ToolRequest('buildr_cancel', {}, False, 'mutation'))
                self.assertTrue(output.until(2))
            self.assertIn('Response too large', output.rows()[1]['error']['message'])
            with mock.patch('agent_builds.mcp.call', return_value=dict(ok=True)):
                connection.submit(3, mcp.ToolRequest('buildr_cancel', {}, False, 'mutation'))
                self.assertTrue(output.until(3))
            self.assertFalse(output.rows()[2]['result']['isError'])
        finally:
            connection.close()

    def test_oversize_closes_input_but_drains_an_already_accepted_mutation(self):
        init = dict(jsonrpc='2.0', id=1, method='initialize', params=dict(
            protocolVersion=mcp.VERSIONS[0], capabilities={}, clientInfo={}))
        ready = dict(jsonrpc='2.0', method='notifications/initialized')
        request = dict(jsonrpc='2.0', id=2, method='tools/call',
            params=dict(name='buildr_cancel', arguments=dict(job_id='abcdefabcdef')))
        source = io.BytesIO(b''.join(json.dumps(row).encode()+b'\n' for row in (init, ready, request))+
            b'x'*(mcp.MAX_REQUEST+1))
        output, entered, release, exit_codes = Replies(), threading.Event(), threading.Event(), []

        def execute(*args):
            entered.set()
            release.wait(5)
            return dict(ok=True)

        def serve():
            exit_codes.append(mcp.serve(input_stream=source, output_stream=output))

        thread = threading.Thread(target=serve)
        with mock.patch('agent_builds.mcp.call', side_effect=execute) as call:
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(output.until(2))  # init and oversize error
                self.assertTrue(thread.is_alive(), 'Accepted mutation was abandoned')
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(call.call_count, 1)
        self.assertEqual(exit_codes, [2])
        rows = output.rows()
        self.assertEqual(len(rows), 3)
        self.assertTrue(any(r['id'] == 2 and 'result' in r for r in rows))

    def test_broken_output_does_not_cancel_or_retry_accepted_calls(self):
        class BrokenOutput:
            def write(self, data):
                raise BrokenPipeError()

        release, entered = threading.Event(), threading.Event()
        connection = mcp.Connection(BrokenOutput())

        def execute(*args):
            entered.set()
            release.wait(5)
            return dict(ok=True)

        try:
            with mock.patch('agent_builds.mcp.call', side_effect=execute) as call:
                connection.submit(1, mcp.ToolRequest('buildr_cancel', {}, False, 'mutation'))
                self.assertTrue(entered.wait(5))
                connection.submit(2, mcp.ToolRequest('buildr_wait', {}, False, 'wait'))
                release.set()
                connection.close()
                self.assertTrue(connection.closed)
                self.assertEqual(call.call_count, 2)
                connection.submit(3, mcp.ToolRequest('buildr_cancel', {}, False, 'mutation'))
                self.assertEqual(call.call_count, 2)
        finally:
            release.set()
            connection.close()
        self.assertFalse(connection.active)


    def test_executor_startup_failure_cannot_execute_an_already_enqueued_mutation(self):
        output = Replies()
        connection = mcp.Connection(output)
        pool = connection.pools['mutation']
        original = pool.submit

        def enqueue_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('Worker startup failed after enqueue')

        try:
            with mock.patch('agent_builds.mcp.call', return_value=dict(ok=True)) as call:
                with mock.patch.object(pool, 'submit', side_effect=enqueue_then_fail):
                    connection.submit(1, mcp.ToolRequest('buildr_cancel', {}, False, 'mutation'))
                self.assertEqual(output.rows()[0]['error']['data'], dict(request_executed=False))
                # Disable only the failed transport lane instead of accumulating
                # more rejected work items or retrying the mutation.
                connection.submit(1, mcp.ToolRequest('buildr_cancel', {}, False, 'mutation'))
                self.assertTrue(output.rows()[1]['error']['data']['reconnect'])
                connection.submit(2, mcp.ToolRequest('buildr_status', {}, False, 'read'))
                self.assertTrue(output.until(3))
                connection.close()
                self.assertEqual(call.call_count, 1)  # inspection only
                self.assertFalse(output.rows()[2]['result']['isError'])
        finally:
            connection.close()
