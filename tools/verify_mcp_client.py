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
    helper = root/'declared-helper'
    helper.write_text('original SDK helper')
    arguments = dict(request_key=key, cwd=str(root), lane='quick', jobs=1, memory_gb=.25,
        resource_key='sdk-checkout', locks=['sdk-output'], input_files=['declared-helper'],
        track_source=False, command=[sys.executable, '-c', 'import time; print("official-client-fixture"); time.sleep(.2); raise SystemExit(7)'])
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 22
            assert all(tool.inputSchema['type'] == 'object' for tool in tools.tools)
            status = await call(session, 'buildr_status', {})
            assert status['initialized'] is False
            diagnostics = await call(session,'buildr_diagnose',{})
            assert diagnostics['initialized'] is False and not diagnostics['changes_applied']
            marker=root/'preflight-must-not-execute'
            preview=await call(session,'buildr_preflight',dict(cwd=str(root),jobs=2,compare_jobs=[1],compare_memory_gb=[1],
                command=[sys.executable,'-c','from pathlib import Path; Path({!r}).touch()'.format(str(marker))]))
            assert preview['initialized'] is False and preview['commands_submitted'] is False
            assert [(case['jobs'],case['memory_gb']) for case in preview['cases']]==[(2,2),(2,1),(1,2),(1,1)]
            assert not marker.exists()
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
    helper.write_text('changed after original submission')
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            repeated = await call(session, 'buildr_submit', arguments)
            assert repeated['result']['job_id'] == handle
            result = await call(session, 'buildr_wait', dict(job_ids=[handle], timeout=5))
            assert result['complete'] and result['exit_code'] == 7
            job = await call(session, 'buildr_job', dict(job_id=handle))
            workbench=await call(session,'buildr_workbench',dict(job_id=handle))
            assert workbench['job_id']==handle and workbench['changes_applied'] is False
            assert job['resource_keys'] == ['sdk-checkout', 'sdk-output']
            import hashlib
            assert job['source']['submission']['inputs']['files'][0]['sha256'] == hashlib.sha256(b'original SDK helper').hexdigest()
            logs = await call(session, 'buildr_logs', dict(job_id=handle, lines=10))
            assert any('official-client-fixture' in line for line in logs['lines'])
            manifest = dict(version=1, input_files=['declared-helper'], stages=[dict(name='verify', jobs=1, memory_gb=.25, locks=['sdk-output'],
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
                max_probes=3,jobs=1,memory_gb=.25,locks=['sdk-probe-output'],track_source=False)
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
            # Two distinct checkouts must serialize on a shared additional key.
            first_args=dict(request_key=key+'-lock-a',cwd=str(root),resource_key='checkout-a',
                locks=['shared-target'],lane='quick',jobs=1,memory_gb=.25,track_source=False,
                command=[sys.executable,'-c','import time; time.sleep(1.2)'])
            first_lock=await call(session,'buildr_submit',first_args)
            second_lock=await call(session,'buildr_submit',dict(first_args,request_key=key+'-lock-b',resource_key='checkout-b',
                command=[sys.executable,'-c','print("serialized shared output")']))
            lock_result=await call(session,'buildr_wait',dict(job_ids=[first_lock['result']['job_id'],second_lock['result']['job_id']],timeout=5))
            assert lock_result['complete'] and lock_result['exit_code']==0
            batch_args=dict(request_key=key+'-batch',cwd=str(root),manifest=dict(checks=[
                dict(name='first',command=[sys.executable,'-c','print("first")']),
                dict(name='native-failure',command=[sys.executable,'-c','raise SystemExit(7)']),
                dict(name='last',command=[sys.executable,'-c','print("last")'])]))
            batched=await call(session,'buildr_batch_submit',batch_args)
            again=await call(session,'buildr_batch_submit',batch_args)
            assert again['result']['job_id']==batched['result']['job_id']
            batch_result=await call(session,'buildr_wait',dict(job_ids=[batched['result']['job_id']],timeout=5))
            assert batch_result['complete'] and batch_result['exit_code']==7
            batch_job=await call(session,'buildr_job',dict(job_id=batched['result']['job_id']))
            assert [check['exit_code'] for check in batch_job['batch']['checks']]==[0,7,0]
            parallel_args=dict(request_key=key+'-parallel',cwd=str(root),jobs=2,memory_gb=.5,
                manifest=dict(max_parallel=2,checks=[dict(name=str(i),command=[sys.executable,'-c',
                    'import os,time; assert os.environ["CARGO_BUILD_JOBS"]=="1"; time.sleep(.15)']) for i in range(3)]))
            parallel=await call(session,'buildr_batch_submit',parallel_args)
            repeated_parallel=await call(session,'buildr_batch_submit',parallel_args)
            assert parallel['result']['job_id']==repeated_parallel['result']['job_id']
            parallel_result=await call(session,'buildr_wait',dict(job_ids=[parallel['result']['job_id']],timeout=5))
            assert parallel_result['complete'] and parallel_result['exit_code']==0
            parallel_job=await call(session,'buildr_job',dict(job_id=parallel['result']['job_id']))
            parallel_checks=parallel_job['batch']['checks']
            assert parallel_job['batch']['max_parallel']==2 and parallel_job['batch']['slots_per_check']==1
            assert max(c['started'] for c in parallel_checks[:2]) < min(c['finished'] for c in parallel_checks[:2])
            batch_preview=await call(session,'buildr_preflight',dict(cwd=str(root),jobs=2,compare_jobs=[1],compare_memory_gb=[.5],
                manifest=parallel_args['manifest']))
            assert [case['submission_acceptable'] for case in batch_preview['cases']]==[True,True,False,False]
            assert [case['memory_gb'] for case in batch_preview['cases']]==[1,.5,1,.5]
            assert batch_preview['commands_submitted'] is False
            weighted_args=dict(request_key=key+'-weighted',cwd=str(root),jobs=2,memory_gb=.5,
                manifest=dict(max_parallel=2,checks=[
                    dict(name='full',jobs=2,command=[sys.executable,'-c','import os,time; assert os.environ["CARGO_BUILD_JOBS"]=="2"; time.sleep(.15)']),
                    dict(name='small-a',jobs=1,command=[sys.executable,'-c','import os,time; assert os.environ["CARGO_BUILD_JOBS"]=="1"; time.sleep(.15)']),
                    dict(name='small-b',jobs=1,command=[sys.executable,'-c','import time; time.sleep(.15)'])]))
            weighted=await call(session,'buildr_batch_submit',weighted_args)
            again=await call(session,'buildr_batch_submit',weighted_args)
            assert weighted['result']['job_id']==again['result']['job_id']
            weighted_done=await call(session,'buildr_wait',dict(job_ids=[weighted['result']['job_id']],timeout=5))
            assert weighted_done['exit_code']==0
            weighted_job=await call(session,'buildr_job',dict(job_id=weighted['result']['job_id']))
            assert weighted_job['batch']['slots_per_check'] is None
            assert [check['cpu_slots'] for check in weighted_job['batch']['checks']]==[2,1,1]
            latency=await call(session,'buildr_latency',dict(target_seconds=30))
            assert latency['changes_applied'] is False and latency['cohorts']
            diagnostics=await call(session,'buildr_diagnose',dict(job_id=batched['result']['job_id']))
            assert diagnostics['jobs'][0]['state']=='failed' and not diagnostics['changes_applied']
    reader = Store(state, read_only=True)
    try:
        assert len(reader.rows()) == 10  # prior fixtures plus explicit CPU packing
        other=reader.get(full_budget['result']['job_id'])
        first=reader.get(watch['probes'][0]['job_id'])
        second=reader.get(watch['probes'][1]['job_id'])
        assert first['finished']<=other['started']<=other['finished']<=second['started']
        lock_a=reader.get(first_lock['result']['job_id'])
        lock_b=reader.get(second_lock['result']['job_id'])
        assert lock_b['created']<lock_a['finished'], 'Fixture failed to submit competing work while the lock was active'
        assert lock_a['finished']<=lock_b['started']
        assert lock_a['resource_keys']==['checkout-a','shared-target']
        assert lock_b['resource_keys']==['checkout-b','shared-target']
    finally:
        reader.close()
    report = dict(schema='buildr-official-mcp-client-check-v1', application_version=__version__,
        sdk_version=importlib.metadata.version('mcp'), protocol_version=initialized.protocolVersion,
        tools=len(tools.tools), result='passed', fixture_directory=str(root),
        checks=['handshake', 'tool schemas', 'read-only missing state', 'submission',
                'server reconnect', 'duplicate prevention', 'native failure', 'bounded logs',
                'declared input hashes survive server reconnect and file changes', 'plan declared input propagation',
                'plan validation', 'plan submission', 'plan completion','external pending probe',
                'external watch duplicate prevention','full CPU budget admitted during external idle',
                'external completion','additional locks survive server reconnect',
                'competing checkouts serialize on shared additional output',
                'batch duplicate prevention and per-check native outcomes',
                'parallel batch overlap and divided CPU hints', 'parallel batch duplicate prevention',
                'CPU/RAM preflight comparisons without state creation or command execution', 'batch preflight minimum CPU guard',
                'read-only workbench', 'explicit per-check CPU budgets and durable duplicate prevention',
                'read-only latency report'], production_jobs_submitted=0,
        shared_output_locks=dict(first_job_id=lock_a['id'],second_job_id=lock_b['id'],
            competing_submission=True,overlap=False,second_wait_seconds=lock_b['wait_seconds']),
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
