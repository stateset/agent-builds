"""Optional interoperability check using the official MCP Python v1 client.

Run the whole check through Buildr. The application itself needs no SDK.
All writes and child submissions below use an explicitly isolated fixture state.
"""
import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_builds import __version__
from agent_builds.plans import write_export
from agent_builds.store import Store


async def verify(output):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    if not os.environ.get('AGENT_BUILDS_JOB_ID'):
        raise ValueError('Run the entire SDK check through the shared build queue')
    if output.exists() or output.is_symlink():
        raise ValueError('Use a new evidence output path')
    root = Path(tempfile.mkdtemp(prefix='buildr-mcp-sdk-'))
    state = root/'queue'
    env = dict(os.environ, AGENT_BUILDS_STATE=str(state), PYTHONPATH=str(ROOT))
    env.pop('AGENT_BUILDS_JOB_ID', None)  # isolated fixture only
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'agent_builds', 'mcp', 'serve'], env=env, cwd=str(ROOT))

    async def call(session, name, arguments):
        result = await session.call_tool(name, arguments)
        if result.isError or not isinstance(result.structuredContent, dict):
            raise AssertionError(name+' failed: '+str(result))
        # Both standard text and structured results must describe the same data.
        assert json.loads(result.content[0].text) == result.structuredContent
        return result.structuredContent

    key = 'official-sdk-'+uuid.uuid4().hex
    arguments = dict(request_key=key, cwd=str(root), lane='quick', jobs=1, memory_gb=.25,
        track_source=False, command=[sys.executable, '-c', 'import time; print("official-client-fixture"); time.sleep(.2); raise SystemExit(7)'])
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 17
            assert all(tool.inputSchema['type'] == 'object' for tool in tools.tools)
            status = await call(session, 'buildr_status', {})
            assert status['initialized'] is False
            assert not state.exists()
            # Configure only this isolated fixture before its first submission.
            store = Store(state)
            store.update_config(dict(cpu_slots=2, max_running=2, max_heavy=2, memory_gb=4,
                min_free_gb=0, min_available_gb=0, quick_reserve_slots=0, quick_reserve_jobs=0,
                quick_reserve_memory_gb=0, nice=0))
            store.close()
            receipt = await call(session, 'buildr_submit', arguments)
            handle = receipt['result']['job_id']
    # A fresh server process must recover the same durable result after disconnect.
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            repeated = await call(session, 'buildr_submit', arguments)
            assert repeated['result']['job_id'] == handle
            result = await call(session, 'buildr_wait', dict(job_ids=[handle], timeout=5))
            assert result['complete'] and result['exit_code'] == 7
            logs = await call(session, 'buildr_logs', dict(job_id=handle, lines=10))
            assert any('official-client-fixture' in line for line in logs['lines'])
            manifest = dict(version=1, stages=[dict(name='verify', jobs=1, memory_gb=.25,
                command=[sys.executable, '-c', 'print("plan verified")'])])
            assert (await call(session, 'buildr_plan_validate', dict(cwd=str(root), manifest=manifest)))['valid']
            plan_receipt = await call(session, 'buildr_plan_submit', dict(request_key=key+'-plan', cwd=str(root), manifest=manifest))
            plan_handle = plan_receipt['result']['plan_id']
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                plan = await call(session, 'buildr_plan', dict(plan_id=plan_handle))
                if plan['complete']:
                    break
                await asyncio.sleep(.1)
            assert plan['state'] == 'succeeded' and plan['exit_code'] == 0
            ready=root/'external-ready'
            probe=[sys.executable,'-c','import sys; from pathlib import Path; sys.exit(0 if Path({!r}).exists() else 75)'.format(str(ready))]
            watch_args=dict(request_key=key+'-watch',cwd=str(root),external_ref='fixture:official-sdk-external',
                read_only_probe=True,command=probe,interval=3,timeout=20,probe_timeout=5,
                max_probes=3,jobs=1,memory_gb=.25,track_source=False)
            watch_receipt=await call(session,'buildr_watch_submit',watch_args)
            watch_handle=watch_receipt['result']['watch_id']
            repeated_watch=await call(session,'buildr_watch_submit',watch_args)
            assert repeated_watch['result']['watch_id']==watch_handle
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                watch=await call(session,'buildr_watch',dict(watch_id=watch_handle))
                if watch['state']=='waiting':
                    break
                await asyncio.sleep(.05)
            assert watch['state']=='waiting' and watch['probes'][0]['native_exit_code']==75
            full_budget=await call(session,'buildr_submit',dict(request_key=key+'-full-budget',cwd=str(root),
                command=[sys.executable,'-c','print("full CPU budget available during external wait")'],
                lane='quick',jobs=2,memory_gb=.25,track_source=False))
            full_result=await call(session,'buildr_wait',dict(job_ids=[full_budget['result']['job_id']],timeout=5))
            assert full_result['complete'] and full_result['exit_code']==0
            ready.touch()  # fixture actor changes state; the probe itself only reads
            while time.monotonic()<deadline:
                watch=await call(session,'buildr_watch',dict(watch_id=watch_handle))
                if watch['complete']:
                    break
                await asyncio.sleep(.05)
            assert watch['state']=='succeeded' and watch['probe_count']==2
            assert watch['external_idle_seconds']>1
    reader = Store(state, read_only=True)
    try:
        assert len(reader.rows()) == 5  # job, plan stage, full-budget job and two probes
        other=reader.get(full_budget['result']['job_id'])
        first=reader.get(watch['probes'][0]['job_id'])
        second=reader.get(watch['probes'][1]['job_id'])
        assert first['finished']<=other['started']<=other['finished']<=second['started']
    finally:
        reader.close()
    report = dict(schema='buildr-official-mcp-client-check-v1', application_version=__version__,
        sdk_version=importlib.metadata.version('mcp'), protocol_version=initialized.protocolVersion,
        tools=len(tools.tools), result='passed', fixture_directory=str(root),
        checks=['handshake', 'tool schemas', 'read-only missing state', 'submission',
                'server reconnect', 'duplicate prevention', 'native failure', 'bounded logs',
                'plan validation', 'plan submission', 'plan completion','external pending probe',
                'external watch duplicate prevention','full CPU budget admitted during external idle',
                'external completion'], production_jobs_submitted=0,
        external_watch=dict(wall_seconds=watch['elapsed_seconds'],idle_without_reservation_seconds=watch['external_idle_seconds'],
            local_reserved_seconds=watch['accounting']['local_reserved_seconds'],probe_count=watch['probe_count'],
            full_budget_job_started_between_probes=True))
    write_export(output, report)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(verify(args.output))
