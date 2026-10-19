"""Explicit representative plans, normal shared admission and honest paired outcomes."""
import json
import math
import os
from pathlib import Path
import time
import uuid

from . import __version__, plans
from .metrics import snapshot
from .study_pressure import StudyPressure
from .source import capture, changed, normalize_inputs, digest_file

NOTE=('Counterbalanced executions of explicitly supplied verification plans through the shared queue. '
      'Queue wait, failures, source drift and missing measurements are retained. Host load and caches '
      'are uncontrolled; stage completions are not accepted autonomous coding tasks. No historical commands '
      'are discovered or executed, no automatic retries, global budget changes or cache deletion.')


def _identity(value):
    return dict(cwd=value['cwd'],input_files=value['input_files'],stages=[
        {key:item for key,item in stage.items() if key not in ('jobs','memory_gb','lane')}
        for stage in value['stages']])


def validate_pair(store,baseline,candidate,cwd):
    values=[plans.validate(value,cwd,store.config()) for value in (baseline,candidate)]
    if _identity(values[0])!=_identity(values[1]):
        raise ValueError('Compared plans must retain identical commands, stages, dependencies, inputs, locks, timeouts and acceptance criteria; change only CPU/RAM/lane or plan parallelism')
    return values


def percentile(values):
    return sorted(values)[math.ceil(len(values)*.95)-1] if values else None


def summary(plan):
    stages=plan['stages']
    jobs=[stage['job'] for stage in stages if stage['job']]
    verified=[stage for stage in stages if stage['state']=='succeeded' and stage['job'] and
        stage['job']['exit_code']==0 and stage['job']['source'].get('changed_since_submission') is False and
        stage['job']['source'].get('changed_during_run') is False]
    short=[job for job in jobs if job['lane']=='quick' and job['started'] and job['elapsed_seconds']<=30]
    measurements=[job['usage'] for job in jobs if job['usage'].get('complete') and
                  job['usage'].get('source')=='local_process_tree']
    wall=plan['elapsed_seconds']
    return dict(plan_id=plan['id'],state=plan['state'],exit_code=plan['exit_code'],wall_seconds=wall,
        stages=len(stages),verified_stages=len(verified),failed_stages=sum(s['state'] in ('failed','lost','cancelled') for s in stages),
        skipped_stages=sum(s['state']=='skipped' for s in stages),
        verified_completions_per_hour=3600*len(verified)/wall if wall else None,
        wait_p95_seconds=percentile([job['wait_seconds'] for job in jobs]),short_quick_checks=len(short),
        short_quick_wait_p95_seconds=percentile([job['wait_seconds'] for job in short]),
        measured_jobs=len(measurements),max_job_sampled_peak_rss_gb=max((u.get('sampled_peak_rss_gb',0) for u in measurements),default=None),
        observed_cpu_seconds=sum(u.get('cpu_seconds',0) for u in measurements),
        heavy_completed=sum(job['lane']=='heavy' and job['state']=='succeeded' for job in jobs),
        heavy_wait_max_seconds=max((job['wait_seconds'] for job in jobs if job['lane']=='heavy'),default=None),
        source=plan['source'],stage_results=[dict(name=s['name'],state=s['state'],job_id=s['job_id'],
            native_exit_code=s['job']['exit_code'] if s['job'] else None,reason=s['reason'],
            source_changed_before=s['job']['source'].get('changed_since_submission') if s['job'] else None,
            source_changed_during=s['job']['source'].get('changed_during_run') if s['job'] else None) for s in stages])


def compare(store,baseline_id,candidate_id):
    if baseline_id==candidate_id:
        raise ValueError('Compare two distinct plan handles, not the same observation twice')
    with store.read_snapshot():
        baseline,candidate=[plans.get(store,handle) for handle in (baseline_id,candidate_id)]
    if _identity(baseline['definition'])!=_identity(candidate['definition']):
        raise ValueError('Existing plans have different commands, dependencies, inputs or acceptance criteria')
    same=changed(baseline['source'],candidate['source'])
    complete=all(plan['state'] in plans.PLAN_TERMINAL for plan in (baseline,candidate))
    observations=[summary(plan) for plan in (baseline,candidate)]
    eligible=complete and same is False and all(
        arm['verified_stages']==arm['stages'] for arm in observations)
    first,second=observations
    ratio=second['verified_completions_per_hour']/first['verified_completions_per_hour'] if eligible and first['verified_completions_per_hour'] else None
    return dict(schema='buildr-workflow-comparison-v1',comparable=eligible,complete=complete,
        source_changed=same,baseline=first,candidate=second,verified_throughput_ratio=ratio,
        changes_applied=False,note=NOTE)


def _read(path):
    evidence=digest_file(path,limit=256*1024,seconds=1)
    if evidence['status']!='captured':
        raise ValueError('Workflow manifests must be regular files of at most 256 KiB')
    with Path(path).expanduser().open('rb') as source:
        data=source.read(256*1024+1)
    if len(data)>256*1024:
        raise ValueError('Workflow manifests must be at most 256 KiB')
    import hashlib
    if hashlib.sha256(data).hexdigest()!=evidence['sha256']:
        raise ValueError('Workflow manifest changed while reading')
    return json.loads(data),evidence['sha256']


def _checkpoint(path,value,initial=False):
    if initial:
        plans.write_export(path,value)
        return
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x') as output:
            os.chmod(str(temporary),0o600)
            json.dump(value,output,indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run(store,baseline_path,candidate_path,cwd,output,pairs=2,timeout=3600):
    if os.environ.get('AGENT_BUILDS_JOB_ID'):
        raise ValueError('Workflow study is a controller: run it directly; each stage uses the shared queue')
    if type(pairs) is not int or not 1<=pairs<=3 or type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=86400:
        raise ValueError('Use one to three pairs and a finite study timeout up to 86400 seconds')
    filenames=[Path(path).expanduser().absolute() for path in (baseline_path,candidate_path)]
    loaded=[_read(path) for path in filenames]
    raw=[value[0] for value in loaded]
    definitions=validate_pair(store,*raw,cwd)
    inputs=normalize_inputs(cwd,definitions[0]['input_files']+[str(path) for path in filenames])
    for definition in definitions:
        definition.update(source_policy='require_unchanged',input_files=inputs)
        for stage in definition['stages']:
            stage['source_policy']='require_unchanged'
    initial_source=capture(cwd,inputs)
    if initial_source['status']!='captured':
        raise ValueError('Complete source/input identity is required before a workflow study')
    hashes={item['path']:item.get('sha256') for item in initial_source['inputs']['files']}
    if any(hashes[str(path)]!=loaded[index][1] for index,path in enumerate(filenames)):
        raise ValueError('Workflow manifest changed after review; no plans submitted')
    output=Path(output).expanduser().absolute()
    progress=output.with_name(output.name+'.progress.json')
    if output.exists() or output.is_symlink() or Path(initial_source['repository']) in output.parents:
        raise ValueError('Use a new report outside the source checkout')
    state=dict(schema='buildr-workflow-study-progress-v1',status='running',arms=[],pairs=pairs,output=str(output),note=NOTE)
    _checkpoint(progress,state,initial=True)
    deadline=time.monotonic()+timeout
    records=[]
    try:
        for pair in range(pairs):
            current={}
            for index in ((0,1) if pair%2==0 else (1,0)):
                if time.monotonic()>=deadline:
                    raise ValueError('Study deadline reached before submission; no work was retried')
                if changed(initial_source,capture(cwd,inputs)) is not False:
                    raise ValueError('Source/input drift or unknown identity; remaining arms were not submitted')
                mode='baseline' if index==0 else 'candidate'
                # Persist the attempt before creating a plan; crashes cannot
                # silently turn an uncertain submission into an automatic retry.
                handle=uuid.uuid4().hex[:12]
                attempt=dict(pair=pair,mode=mode,plan_id=handle,state='submission_attempted')
                state['arms'].append(attempt)
                _checkpoint(progress,state)
                plans.create(store,definitions[index],expected_source=initial_source,plan_id=handle)
                attempt.update(plan_id=handle,state='created')
                _checkpoint(progress,state)
                print(json.dumps(dict(pair=pair,mode=mode,plan_id=handle)),flush=True)
                controller=plans.spawn(store,handle)
                pressure=StudyPressure()
                heavy_wait_max=0
                heavy_waiting_max=0
                start=time.time()
                while True:
                    plan=plans.get(store,handle)
                    try:
                        measured=snapshot(cwd)
                    except OSError:
                        measured={}
                    pressure.observe(measured)
                    waiting=store.db.execute("SELECT COUNT(*),MIN(created) FROM jobs WHERE lane='heavy' AND state IN ('queued','held') AND cancel_requested=0").fetchone()
                    heavy_waiting_max=max(heavy_waiting_max,waiting[0])
                    if waiting[1] is not None:
                        heavy_wait_max=max(heavy_wait_max,time.time()-waiting[1])
                    if plan['state'] in plans.PLAN_TERMINAL:
                        break
                    if controller.poll() is not None:
                        # Re-read after exit to avoid racing the controller's
                        # final commit. Never relaunch an unsettled plan.
                        plan=plans.get(store,handle)
                        if plan['state'] not in plans.PLAN_TERMINAL:
                            raise ValueError('Controller ended before plan '+handle+' settled; inspect its handle, no retry')
                        break
                    if time.monotonic()>=deadline:
                        raise ValueError('Study deadline reached; existing plan '+handle+' continues; no automatic cancellation or retry')
                    time.sleep(.5)
                controller.wait(timeout=3)
                attempt.update(state=plan['state'])
                observed=summary(plan)
                observed.update(pair=pair,mode=mode,pressure=pressure.report(),
                    host_heavy_waiting_max=heavy_waiting_max,host_heavy_observed_wait_max_seconds=heavy_wait_max,
                    host_heavy_finished=store.db.execute("SELECT COUNT(*) FROM jobs WHERE lane='heavy' AND finished>=? AND state IN ('succeeded','failed','cancelled','lost')",(start,)).fetchone()[0])
                records.append(observed)
                current[mode]=handle
                state['records']=records
                _checkpoint(progress,state)
            state.setdefault('comparisons',[]).append(compare(store,current['baseline'],current['candidate']))
        final_source=capture(cwd,inputs)
        result=dict(schema='buildr-workflow-study-v1',application_version=__version__,pairs=pairs,records=records,
            comparisons=state['comparisons'],source_before=initial_source,source_after=final_source,source_changed=changed(initial_source,final_source),
            note=NOTE,global_settings_changed=False,autonomous_coding_productivity_measured=False)
        plans.write_export(output,result)
        state.update(status='completed',report=str(output))
        _checkpoint(progress,state)
        return result
    except BaseException:
        state['status']='interrupted_or_failed; inspect recorded plan handles before any new submission'
        _checkpoint(progress,state)
        raise
