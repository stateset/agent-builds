"""Explicit proposals, queued trials and evidence-gated saved task promotion."""
import json
import math
from pathlib import Path
import statistics
import time

from . import advice, plans, profiles
from .source import capture, changed, digest_file, normalize_inputs

KINDS=('memory_trial','low_average_cpu','tiny_check_trial','long_quick_command')
FIELDS=('cwd','command','lane','backend','jobs','memory_gb','timeout','image','resource_key','locks',
        'min_free_gb','scratch_gb','input_files')


def matching(findings, row):
    return [finding for finding in findings if finding['code'] in KINDS and
            row['id'] in finding['evidence'].get('job_ids',[])]


def propose(store, job, kind, name):
    profiles.name(name)
    if kind not in KINDS:
        raise ValueError('Choose an evidence-backed resource or lane trial')
    row=store.get(job)
    if row['backend']!='local' or row['state']!='succeeded' or row['exit_code']!=0:
        raise ValueError('Proposals require a successful local job')
    findings=matching(advice.report(store,row['cwd'],limit=2000,policy='current')['findings'],row)
    finding=next((f for f in findings if f['code']==kind),None)
    if finding is None:
        raise ValueError('No matching repeated-success evidence for this trial')
    if row['batch'] and kind=='long_quick_command':
        raise ValueError('Split a long batch into explicit plan stages instead of relabeling it')
    inputs=[item['path'] for item in row['source'].get('submission',{}).get('inputs',{}).get('files',[])]
    task={key:row[key] for key in FIELDS if key in row}
    task.update(command=row['argv'],jobs=row['slots'],input_files=normalize_inputs(row['cwd'],inputs))
    candidate=dict(task,**finding['suggested_trial'])
    plans.validate(dict(stages=[dict({k:v for k,v in candidate.items() if k!='input_files'},name='trial')]),row['cwd'],store.config())
    source=capture(row['cwd'],task['input_files'])
    if source['status']!='captured':
        raise ValueError('Complete current source/input evidence is required for a proposal')
    baseline=[store.get(handle)['elapsed_seconds'] for handle in finding['evidence']['job_ids']]
    result=dict(schema='buildr-task-proposal-v1',name=name,created_at=time.time(),kind=kind,
        baseline=task,candidate=candidate,evidence=finding['evidence'],source=source,
        baseline_median_seconds=statistics.median(baseline),commands_submitted=False,
        note='Review literal argv and inputs before trial. Promotion requires an entire matching successful trial plan; sampled RSS and these inputs do not prove safety for other workloads.')
    verify_baseline(store,result)
    return result


def read(path):
    path=Path(path).expanduser().absolute()
    evidence=digest_file(path,limit=256*1024,seconds=1)
    if evidence['status']!='captured':
        raise ValueError('Proposal must be an existing regular file of at most 256 KiB')
    with path.open('rb') as source:
        raw=source.read(256*1024+1)
    import hashlib
    if len(raw)>256*1024 or hashlib.sha256(raw).hexdigest()!=evidence['sha256']:
        raise ValueError('Proposal changed while reading')
    value=json.loads(raw)
    if not isinstance(value,dict) or value.get('schema')!='buildr-task-proposal-v1':
        raise ValueError('Unsupported task proposal')
    profiles.name(value.get('name',''))
    for key in ('candidate','baseline'):
        if not isinstance(value.get(key),dict) or set(value[key])!=set(FIELDS):
            raise ValueError('Proposal task fields are incomplete or unsupported')
        for field in ('jobs','memory_gb'):
            number=value[key][field]
            if type(number) not in (int,float) or not math.isfinite(number) or number<=0:
                raise ValueError('Proposal CPU and RAM values must be finite positive numbers')
    if (not isinstance(value.get('source'),dict) or not isinstance(value['source'].get('repository'),str) or
            type(value.get('baseline_median_seconds')) not in (int,float) or not math.isfinite(value['baseline_median_seconds']) or value['baseline_median_seconds']<=0):
        raise ValueError('Proposal source and baseline measurements are required')
    baseline,candidate=value['baseline'],value['candidate']
    if baseline['backend']!='local' or any(candidate[k]!=baseline[k] for k in FIELDS if k not in ('jobs','memory_gb','lane')):
        raise ValueError('Trials may change only CPU, RAM or lane of a local task')
    if Path(value['source']['repository']) in path.parents:
        raise ValueError('Save proposals outside the source checkout, for example under /tmp')
    return path,value,evidence


def verify_baseline(store,proposal):
    handles=proposal.get('evidence',{}).get('job_ids',[])
    if not isinstance(handles,list) or not 3<=len(set(handles))<=10 or len(set(handles))!=len(handles):
        raise ValueError('Proposal must retain three to ten distinct matching baseline results')
    baseline=proposal['baseline']
    durations=[]
    for handle in handles:
        row=store.get(handle)
        actual={key:row[key] for key in FIELDS if key in row}
        actual.update(command=row['argv'],jobs=row['slots'],input_files=[item['path'] for item in
            row['source'].get('submission',{}).get('inputs',{}).get('files',[])])
        if row['state']!='succeeded' or row['exit_code']!=0 or actual!=baseline:
            raise ValueError('Proposal baseline differs from its recorded successful jobs')
        durations.append(row['elapsed_seconds'])
    if statistics.median(durations)!=proposal['baseline_median_seconds']:
        raise ValueError('Proposal baseline timing differs from its recorded evidence')


def definition(store,path,proposal,repeats=3):
    if type(repeats) is not int or not 3<=repeats<=5:
        raise ValueError('Use three to five representative trial repetitions')
    candidate=proposal['candidate']
    if len(candidate['command'])>=2 and candidate['command'][-2]=='batch-worker':
        from .batch import validate,worker_slots
        worker_slots(validate(json.loads(candidate['command'][-1])),candidate['jobs'])
    values={k:v for k,v in candidate.items() if k!='input_files'}
    return plans.validate(dict(version=1,name='Profile trial: '+proposal['name'],source_policy='require_unchanged',
        input_files=normalize_inputs(candidate['cwd'],candidate['input_files']+[str(path)]),
        stages=[dict(values,name='trial-'+str(index+1)) for index in range(repeats)]),candidate['cwd'],store.config())


def unchanged_proposal_source(proposal,observed,path=None):
    observed=dict(observed)
    if path is not None and 'inputs' in observed:
        inputs=dict(observed['inputs'])
        inputs['files']=[item for item in inputs['files'] if item['path']!=str(path)]
        if inputs['files']:
            observed['inputs']=inputs
        else:
            observed.pop('inputs')
    return changed(proposal['source'],observed) is False


def trial(store,filename,repeats=3,emit=True,processes=None):
    path,proposal,evidence=read(filename)
    verify_baseline(store,proposal)
    if not unchanged_proposal_source(proposal,capture(proposal['candidate']['cwd'],proposal['candidate']['input_files'])):
        raise ValueError('Source or declared inputs changed since proposal; draft a fresh proposal')
    manifest=definition(store,path,proposal,repeats)
    bound=capture(proposal['candidate']['cwd'],manifest['input_files'])
    hashes={item['path']:item.get('sha256') for item in bound.get('inputs',{}).get('files',[])}
    if hashes.get(str(path))!=evidence['sha256'] or not unchanged_proposal_source(proposal,bound,path):
        raise ValueError('Proposal/source changed before submission')
    handle=plans.create(store,manifest,expected_source=bound)
    # Receipt precedes detached controller startup. An uncertain startup must
    # never be retried as a new plan.
    if emit:
        print(json.dumps(dict(plan_id=handle,proposal_sha256=evidence['sha256'],state='created')),flush=True)
    child=plans.spawn(store,handle)
    if processes is not None:
        processes.append(child)
    return handle


def review(store,filename,plan_id):
    path,proposal,file_evidence=read(filename)
    verify_baseline(store,proposal)
    plan=plans.get(store,plan_id)
    issues=[]
    expected=definition(store,path,proposal,len(plan['stages']))
    if plan['definition']!=expected:
        issues.append('trial plan does not match the reviewed proposal')
    if plan['state']!='succeeded':
        issues.append('the entire trial plan must succeed; failed, pending and unknown work cannot be promoted')
    if not unchanged_proposal_source(proposal,capture(proposal['candidate']['cwd'],proposal['candidate']['input_files'])):
        issues.append('current source or declared inputs differ from the proposal')
    jobs=[stage.get('job') for stage in plan['stages']]
    measured=[]
    for job in jobs:
        if not job or job['state']!='succeeded' or job['exit_code']!=0:
            issues.append('every trial needs a recorded successful native result')
            continue
        source=job['source']
        if source.get('changed_since_submission') is not False or source.get('changed_during_run') is not False:
            issues.append('trial source evidence is changed or incomplete')
        for phase in ('submission','start','finish'):
            observed=source.get(phase,{})
            declared={item['path']:item for item in observed.get('inputs',{}).get('files',[])}
            if (not unchanged_proposal_source(proposal,observed,path) or
                    declared.get(str(path),{}).get('sha256')!=file_evidence['sha256']):
                issues.append('trial proposal/source identity is missing or changed')
        usage=job['usage']
        if (usage.get('source')=='local_process_tree' and usage.get('complete') and
                usage.get('samples',0)>=3 and usage.get('sampled_peak_rss_gb',0)>0 and job['elapsed_seconds']>=1):
            measured.append(usage['sampled_peak_rss_gb'])
    smaller=proposal['candidate']['memory_gb']<proposal['baseline']['memory_gb']
    peak=max(measured) if measured else None
    if smaller and (len(measured)!=len(jobs) or peak*1.3>proposal['candidate']['memory_gb']):
        issues.append('RAM reduction needs complete measurements from every trial and 30% headroom above sampled peak RSS')
    durations=[job['elapsed_seconds'] for job in jobs if job and job['started']]
    return dict(schema='buildr-task-review-v1',eligible=not issues,issues=list(dict.fromkeys(issues)),
        proposal=proposal,proposal_path=str(path),proposal_sha256=file_evidence['sha256'],plan_id=plan_id,
        jobs=[job['id'] for job in jobs if job],sampled_peak_rss_gb=peak,measured_jobs=len(measured),
        median_runtime_seconds=statistics.median(durations) if durations else None,
        baseline_median_seconds=proposal['baseline_median_seconds'],changes_applied=False)


def promote(store,filename,plan_id):
    result=review(store,filename,plan_id)
    if not result['eligible']:
        raise ValueError('Profile not eligible: '+'; '.join(result['issues']))
    proposal=result['proposal']
    values=dict(proposal['candidate'],name=proposal['name'],review=dict(plan_id=plan_id,
        jobs=result['jobs'],proposal_sha256=result['proposal_sha256'],promoted_at=time.time(),source=proposal['source']))
    # Old task runners ignore this section rather than bypassing the new source
    # gate while using a reduced reservation.
    profiles.update(store,'reviewed_tasks',profiles.task_key(values['cwd'],values['name']),values)
    return dict(result,changes_applied=True,saved_task=values)


def run_saved(store,args):
    from .runner import publish_receipt, validate_receipt
    receipt=validate_receipt(store,args.receipt_file) if getattr(args,'receipt_file',None) else None
    task=vars(args)
    manifest=plans.validate(dict(name='Reviewed task',source_policy='require_unchanged',input_files=task['input_files'],
        stages=[dict({key:task[key] for key in FIELDS if key!='input_files'},name='task')]),task['cwd'],store.config())
    handle=plans.create(store,manifest,expected_source=task['review']['source'])
    if receipt is not None:
        publish_receipt(store,receipt,dict(plan_id=handle,cwd=manifest['cwd'],state='created',reviewed_task=True))
    print(json.dumps(dict(plan_id=handle,state='created',reviewed_task=True)),flush=True)
    child=plans.spawn(store,handle)
    if task['detach']:
        return 0
    while True:
        current=plans.get(store,handle)
        if current['state'] in plans.PLAN_TERMINAL:
            child.wait(timeout=3)
            from .plan_output import compact
            print(json.dumps(compact(current),indent=2))
            return current['exit_code']
        time.sleep(.2)
