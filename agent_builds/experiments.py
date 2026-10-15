"""Bounded, paired local experiments. Fixture results are not agent-productivity claims."""
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
import uuid

from .metrics import snapshot
from .plans import write_export
from .store import Store, identity
from .telemetry import JobSampler

SCENARIOS=('shared_output','resource_contention','python_verification')
NOTE=('Controlled local benchmark with fixed fixtures, not a measurement of autonomous coding productivity. '
      'Both arms inherit the same CPU affinity and worker hints. The independent arm bypasses only the isolated '
      'inner scheduler; the entire experiment remains inside its outer shared-queue reservation. '
      'Host background activity, caches and sample size can affect results. No historical command is executed.')


def parent_budget(cpus):
    if type(cpus) is not int or cpus<1 or cpus>6:
        raise ValueError('Experiment CPU budget must be between 1 and 6')
    handle=os.environ.get('AGENT_BUILDS_JOB_ID')
    if not handle:
        raise ValueError('Run the whole experiment through the shared queue with a matching --jobs reservation')
    reader=Store(read_only=True)
    try:
        row=reader.get(handle)
    finally:
        reader.close()
    if row['state']!='running' or row['slots']<cpus or row['memory_gb']<2:
        raise ValueError('Experiment requires a running outer reservation of the requested CPUs and at least 2 GiB')
    if not hasattr(os,'sched_getaffinity') or len(os.sched_getaffinity(0))<cpus:
        raise ValueError('Experiment requires Linux CPU affinity and enough available CPUs')
    return dict(job_id=handle,slots=row['slots'],memory_gb=row['memory_gb'])


def worker(scenario,path,identifier):
    root=Path(path)
    marker=root/'shared-output.txt'
    if scenario=='shared_output':
        marker.write_text(identifier)
    data=bytearray(96*1024**2) if scenario=='resource_contention' else bytearray(65536)
    digest=hashlib.sha256()
    for _ in range(2048):
        digest.update(memoryview(data)[:65536])
    time.sleep(.2)
    ok=scenario!='shared_output' or marker.read_text()==identifier
    result=dict(id=identifier,verified=ok,digest=digest.hexdigest())
    report=root/('result-'+identifier+'.json')
    report.write_text(json.dumps(result)+'\n')
    report.chmod(0o600)
    return 0 if ok else 7


def percentile(values):
    return sorted(values)[math.ceil(len(values)*.95)-1] if values else None


def arm(directory,scenario,mode,agents,cpus,cancelled):
    directory.mkdir(parents=True,mode=0o700)
    state=directory/'queue'
    source=Path(__file__).resolve().parent.parent
    base_env=dict(os.environ,PYTHONPATH=str(source)+os.pathsep+os.environ.get('PYTHONPATH',''),
                  CARGO_BUILD_JOBS='1',RUST_TEST_THREADS='1',OMP_NUM_THREADS='1')
    if mode=='queued':
        queue=Store(state)
        queue.update_config(dict(cpu_slots=cpus,max_running=cpus,max_heavy=cpus,memory_gb=2,
            quick_reserve_slots=0,quick_reserve_jobs=0,quick_reserve_memory_gb=0,
            min_free_gb=0,min_available_gb=0,nice=0))
        queue.close()
    processes=[]
    results=[]
    sampler=JobSampler(os.getpid(),identity(os.getpid()))
    before=resource.getrusage(resource.RUSAGE_CHILDREN)
    started=time.monotonic()
    deadline=started+120
    last_tick=started
    lag=[]
    try:
        for index in range(agents):
            identifier='agent-'+str(index)
            work=directory/('shared' if scenario=='shared_output' else identifier)
            work.mkdir(exist_ok=True,mode=0o700)
            command=[sys.executable,'-m','agent_builds.experiments','_worker',scenario,str(work),identifier]
            if scenario=='python_verification':
                command=[sys.executable,'-m','unittest','discover','-s',str(source/'tests'),'-p','test_budgets.py','-v']
            env=dict(base_env)
            if mode=='queued':
                # This is an explicit isolated scheduler experiment, not a
                # nested submission to the production queue.
                env.pop('AGENT_BUILDS_JOB_ID',None)
                env['AGENT_BUILDS_STATE']=str(state)
                command=[sys.executable,'-m','agent_builds','run','--cwd',str(work),'--lane','quick',
                         '--jobs','1','--memory-gb','.25','--resource-key',str(work),'--timeout','90','--']+command
            log=directory/(identifier+'.log')
            with log.open('xb') as output:
                log.chmod(0o600)
                child=subprocess.Popen(command,cwd=str(work),env=env,stdin=subprocess.DEVNULL,
                    stdout=output,stderr=output,start_new_session=True)
            processes.append(dict(id=identifier,child=child,work=work,log=log))
        while processes:
            now=time.monotonic()
            lag.append(max(0,now-last_tick-.02))
            last_tick=now
            sampler.sample()
            for item in list(processes):
                code=item['child'].poll()
                if code is None:
                    continue
                verified=code==0
                if scenario!='python_verification':
                    try:
                        record=json.loads((item['work']/('result-'+item['id']+'.json')).read_text())
                        verified=verified and record['verified'] is True and record['id']==item['id']
                    except (OSError,ValueError,KeyError):
                        verified=False
                results.append(dict(id=item['id'],exit_code=code,verified=verified,
                    completion_seconds=now-started,log_path=str(item['log'])))
                processes.remove(item)
            if cancelled[0] or now>=deadline:
                raise ValueError('Experiment cancelled or timed out; incomplete work is not retried')
            if processes:
                time.sleep(.02)
    finally:
        for item in processes:
            try:
                os.killpg(item['child'].pid,signal.SIGTERM)
            except ProcessLookupError:
                pass
        # Fixture children exit on SIGTERM; let isolated runners settle their
        # command groups. No global queue job is cancelled by this driver.
        for item in processes:
            try:
                item['child'].wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(item['child'].pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
                item['child'].wait()
    ended=time.monotonic()
    after=resource.getrusage(resource.RUSAGE_CHILDREN)
    verified=[r for r in results if r['verified']]
    return dict(mode=mode,scenario=scenario,wall_seconds=ended-started,jobs=len(results),
        verified_jobs=len(verified),failed_jobs=len(results)-len(verified),
        verified_completions_per_second=len(verified)/(ended-started),
        completion_p95_seconds=percentile([r['completion_seconds'] for r in results]),
        verified_completion_p95_seconds=percentile([r['completion_seconds'] for r in verified]),
        sampled_process_tree_peak_rss_gb=sampler.peak/1024**3,
        child_cpu_seconds=after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,
        driver_loop_lag_p95_seconds=percentile(lag),results=sorted(results,key=lambda r:r['id']))


def run(output,pairs=3,agents=4,cpus=2,scenarios=None):
    if type(pairs) is not int or not 1<=pairs<=5 or type(agents) is not int or not 2<=agents<=6:
        raise ValueError('Use 1 to 5 pairs and 2 to 6 fixture agents')
    selected=list(scenarios or SCENARIOS)
    if not selected or len(set(selected))!=len(selected) or any(s not in SCENARIOS for s in selected):
        raise ValueError('Select unique supported benchmark scenarios')
    budget=parent_budget(cpus)
    output=Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Experiment output must be a new path')
    affinity=os.sched_getaffinity(0)
    chosen=sorted(affinity)[-cpus:]
    directory=output.parent/('buildr-experiment-'+uuid.uuid4().hex[:12])
    directory.mkdir(mode=0o700)
    previous={}
    cancelled=[False]
    records=[]
    before=snapshot(directory)
    from .source import capture, changed
    source_before=capture(Path(__file__).resolve().parent.parent)
    try:
        os.sched_setaffinity(0,chosen)
        def signal_handler(signum,frame):
            cancelled[0]=True
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous[sig]=signal.signal(sig,signal_handler)
        for pair in range(pairs):
            order=('independent','queued') if pair%2==0 else ('queued','independent')
            for scenario in selected:
                for mode in order:
                    result=arm(directory/str(pair)/scenario/mode,scenario,mode,agents,cpus,cancelled)
                    result['pair']=pair
                    records.append(result)
                    print('{} pair {} {}: {}/{} verified, {:.3f}s'.format(scenario,pair,mode,
                        result['verified_jobs'],result['jobs'],result['wall_seconds']),flush=True)
    finally:
        os.sched_setaffinity(0,affinity)
        for sig,handler in previous.items():
            signal.signal(sig,handler)
    comparisons=[]
    for scenario in selected:
        matched=[r for r in records if r['scenario']==scenario]
        for pair in range(pairs):
            base=next(r for r in matched if r['pair']==pair and r['mode']=='independent')
            queued=next(r for r in matched if r['pair']==pair and r['mode']=='queued')
            comparisons.append(dict(scenario=scenario,pair=pair,verified_delta=queued['verified_jobs']-base['verified_jobs'],
                queued_to_independent_wall_ratio=queued['wall_seconds']/base['wall_seconds'],
                queued_to_independent_sampled_rss_ratio=queued['sampled_process_tree_peak_rss_gb']/base['sampled_process_tree_peak_rss_gb'] if base['sampled_process_tree_peak_rss_gb'] else None))
    from . import __version__
    source_after=capture(Path(__file__).resolve().parent.parent)
    report=dict(schema='buildr-paired-experiment-v1',application_version=__version__,pairs=pairs,agents=agents,
        cpu_affinity=chosen,outer_reservation=budget,fixture_directory=str(directory),
        before_machine=before,after_machine=snapshot(directory),records=records,comparisons=comparisons,
        independent_arm_scope='fixed fixtures launched directly inside the same bounded outer reservation',
        queued_arm_config=dict(cpu_slots=cpus,max_running=cpus,memory_gb=2,quick_reserves=0),
        source_before=source_before,source_after=source_after,source_changed=changed(source_before,source_after),
        autonomous_coding_productivity_measured=False,note=NOTE)
    write_export(output,report)
    return report


if __name__=='__main__':
    if len(sys.argv)!=5 or sys.argv[1]!='_worker':
        raise SystemExit('Internal fixture worker only; use buildr experiment run')
    raise SystemExit(worker(*sys.argv[2:]))
