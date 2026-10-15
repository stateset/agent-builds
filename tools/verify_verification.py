"""Controlled latency/overhead comparison; run through the shared queue."""
import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from agent_builds import batch, __version__
from agent_builds.experiments import parent_budget
from agent_builds.plans import write_export
from agent_builds.store import Store


def wait_for(store,handles,condition,timeout=45):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        rows=[store.get(handle) for handle in handles]
        if condition(rows):
            return rows
        time.sleep(.02)
    raise AssertionError('Fixture condition did not settle: '+str(handles))


def fixture(path,protected):
    path.mkdir(mode=0o700)
    store=Store(path/'queue')
    store.update_config(dict(cpu_slots=2,max_running=3,max_heavy=2,memory_gb=4,
        quick_reserve_slots=1,quick_reserve_jobs=1,quick_reserve_memory_gb=1,
        min_free_gb=0,min_available_gb=0,nice=0,aging_seconds=.05,protected_quick=protected))
    env=dict(os.environ,AGENT_BUILDS_STATE=str(store.root),PYTHONPATH=str(ROOT))
    env.pop('AGENT_BUILDS_JOB_ID',None)  # deliberately isolated inner fixture
    processes=[]
    def launch(argv,lane,key,receipt_name=None):
        label=receipt_name or key
        receipt=store.root/('receipt-'+label+'.json')
        log=path/(label+'.log')
        with log.open('xb') as output:
            process=subprocess.Popen([sys.executable,str(ROOT/'buildr'),'run','--cwd',str(path),
                '--lane',lane,'--jobs','1','--memory-gb','.25','--resource-key',key,
                '--timeout','30','--receipt-file',str(receipt),'--']+argv,
                env=env,cwd=str(ROOT),stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
        processes.append(process)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            if receipt.exists():
                return json.loads(receipt.read_text())['job_id']
            if process.poll() is not None:
                raise AssertionError(log.read_text()[-1000:])
            time.sleep(.01)
        raise AssertionError('Fixture receipt timed out')
    return store,launch,processes


def settle_processes(processes):
    interrupted=sys.exc_info()[0] is not None
    if interrupted:
        for process in processes:
            if process.poll() is None:
                process.terminate()
    for process in processes:
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def latency_arm(path,protected):
    store,launch,processes=fixture(path,protected)
    heavy=[sys.executable,'-c','import hashlib; data=bytes(16384); digest=None\nfor i in range(200000): digest=hashlib.sha256(data).hexdigest()\nassert len(digest)==64; print(digest)']
    quick=[sys.executable,'-m','unittest','discover','-s',str(ROOT/'tests'),'-p','test_budgets.py']
    started=time.monotonic()
    try:
        a=launch(heavy,'heavy','heavy-a')
        wait_for(store,[a],lambda rows:rows[0]['child_pid'] is not None)
        b=launch(heavy,'heavy','heavy-b')
        if protected:
            wait_for(store,[b],lambda rows:bool(rows[0]['reason']))
        else:
            wait_for(store,[b],lambda rows:rows[0]['child_pid'] is not None)
        q=launch(quick,'quick','quick-check')
        a,b,q=wait_for(store,[a,b,q],lambda rows:all(row['state'] in ('succeeded','failed','cancelled','lost') for row in rows))
        assert all(row['exit_code']==0 for row in (a,b,q))
        if protected:
            assert q['finished']<a['finished'], 'Quick check did not complete while heavy work ran'
        else:
            assert q['started']>=min(a['finished'],b['finished']), 'Baseline did not saturate its reservation budget'
        return dict(protected=protected,quick_wait_seconds=q['wait_seconds'],quick_runtime_seconds=q['elapsed_seconds'],
            heavy_runtime_seconds=sum(row['elapsed_seconds'] for row in (a,b)),wall_seconds=time.monotonic()-started,
            all_native_success=True,heavy_completions=2,fixture_directory=str(path))
    finally:
        settle_processes(processes)
        store.close()


def batch_arm(path,batched):
    store,launch,processes=fixture(path,False)
    argv=[sys.executable,'-m','unittest','discover','-s',str(ROOT/'tests'),'-p','test_budgets.py']
    started=time.monotonic()
    try:
        handles=[launch(batch.command(dict(checks=[dict(name='check-'+str(i),command=argv) for i in range(4)])),'quick','shared-checkout')] if batched else []
        if not batched:
            # Same resource key serializes identical checks in both arms.
            # Distinct receipt paths are needed without changing that lock.
            for i in range(4):
                handle=launch(argv,'quick','shared-checkout',receipt_name='check-'+str(i))
                # Submit sequentially to ensure the fixture never races its
                # resource declaration. This includes ordinary CLI overhead.
                wait_for(store,[handle],lambda rows:rows[0]['state'] in ('succeeded','failed','cancelled','lost'))
                handles.append(handle)
        rows=wait_for(store,handles,lambda rows:all(row['state'] in ('succeeded','failed','cancelled','lost') for row in rows))
        assert all(row['exit_code']==0 for row in rows)
        if batched:
            assert [check['exit_code'] for check in rows[0]['batch']['checks']]==[0,0,0,0]
        return dict(batched=batched,jobs=len(rows),verified_checks=4,wall_seconds=time.monotonic()-started,fixture_directory=str(path))
    finally:
        settle_processes(processes)
        store.close()


def main(output,pairs):
    budget=parent_budget(2)
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Verification fixture cancelled; no retry')
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,interrupted)
    os.sched_setaffinity(0,sorted(os.sched_getaffinity(0))[:2])
    root=Path(tempfile.mkdtemp(prefix='buildr-verification-'))
    results=[]
    for pair in range(pairs):
        item=dict(pair=pair,latency=[],batch=[])
        for protected in ([False,True] if pair%2==0 else [True,False]):
            item['latency'].append(latency_arm(root/('latency-{}-{}'.format(pair,protected)),protected))
        for batched in ([False,True] if pair%2==0 else [True,False]):
            item['batch'].append(batch_arm(root/('batch-{}-{}'.format(pair,batched)),batched))
        results.append(item)
    report=dict(schema='buildr-verification-experiment-v1',application_version=__version__,pairs=results,outer_budget=budget,
        note='Controlled fixed workloads on identical two-CPU affinity and queue budgets. Protected capacity trades heavy concurrency for responsiveness. Batching compares sequential CLI submissions with one batch. Host activity affects timings. This does not prove overall autonomous coding productivity.',changes_applied=False)
    write_export(output,report)
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--pairs',type=int,choices=range(1,4),default=3)
    args=parser.parse_args()
    main(args.output,args.pairs)
