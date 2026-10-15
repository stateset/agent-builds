"""One read-only view of a job's bottleneck, alternatives and historical evidence."""
import json
import shlex

from . import advice, diagnostics, preflight, task_review


def report(store,job_id):
    row=store.get(job_id)
    evidence=advice.report(store,row['cwd'],limit=2000,policy='current')
    expected=dict(cwd=row['cwd'],resource_keys=sorted(row['resource_keys']),command_display=row['command_display'],
                  backend=row['backend'],lane=row['lane'],slots=row['slots'],memory_gb=row['memory_gb'])
    candidates=[]
    for finding in evidence['findings']:
        values=finding['evidence']
        if finding['code'] not in task_review.KINDS or any(
                (sorted(values.get(key,[])) if key=='resource_keys' else values.get(key))!=value
                for key,value in expected.items()):
            continue
        anchor=values['job_ids'][0]
        candidates.append(dict(kind=finding['code'],title=finding['title'],action=finding['action'],
            proposed=finding['suggested_trial'],evidence=values,anchor_job=anchor,
            proposal_command=['buildr','task','propose',anchor,'--kind',finding['code'],'--name','reviewed-task',
                              '--output','/tmp/buildr-'+anchor+'-'+finding['code']+'.json']))
    request=dict(cwd=row['cwd'],lane=row['lane'],jobs=row['slots'],memory_gb=row['memory_gb'],backend=row['backend'],
        image=row['image'],timeout=row['timeout'],resource_key=row['resource_key'],locks=row['locks'],
        min_free_gb=row['min_free_gb'],scratch_gb=row['scratch_gb'],command=row['argv'],
        compare_jobs=list(dict.fromkeys(c['proposed']['jobs'] for c in candidates if 'jobs' in c['proposed'])),
        compare_memory_gb=list(dict.fromkeys(c['proposed']['memory_gb'] for c in candidates if 'memory_gb' in c['proposed'])))
    if row['batch'] and len(row['argv'])>=2 and row['argv'][-2]=='batch-worker':
        request['manifest']=json.loads(row['argv'][-1])
        request.pop('command')
    try:
        projection=preflight.report(store,request)
    except ValueError as error:
        projection=dict(error=str(error))
    return dict(schema='buildr-workbench-v1',job_id=job_id,job=dict(state=row['state'],lane=row['lane'],
        jobs=row['slots'],memory_gb=row['memory_gb'],elapsed_seconds=row['elapsed_seconds'],usage=row['usage'],
        command_display=row['command_display'][:240]),
        diagnostics=diagnostics.report(store,job=job_id,limit=10),preflight=projection,candidates=candidates,
        history=dict(sampled=evidence['history_sampled'],matching=evidence['history_matching'],truncated=evidence['history_truncated']),
        changes_applied=False,note='Each panel is a fresh read-only observation and may see different queue state. Preflight describes a future run with the same locks, not resizing this job. Trial suggestions need review and representative validation.')


def lines(value,selected=0):
    yield 'WORKBENCH | '+value['job_id']+' | read only'
    job=value['job']
    yield '{} | {} CPU / {:g} GiB | {}'.format(job['state'],job['jobs'],job['memory_gb'],job['lane'])
    yield job['command_display']
    usage=job['usage']
    yield 'Observed: {:.1f}s runtime; sampled peak RSS {}; CPU seconds {}; complete {}'.format(
        job['elapsed_seconds'],usage.get('sampled_peak_rss_gb','unknown'),usage.get('cpu_seconds','unknown'),usage.get('complete','unknown'))
    for item in value['diagnostics']['jobs']:
        if item['id']==value['job_id']:
            yield 'Current/recorded blocker: '+(item['reason'] or 'none recorded')
            if item.get('capacity'):
                yield 'Reservation deficits: '+(', '.join(item['capacity']['blocking_dimensions']) or 'none')
    if 'error' in value['preflight']:
        yield 'Preflight unavailable: '+value['preflight']['error']
    else:
        yield 'Future-run CPU/RAM alternatives (keeps current locks):'
        for case in value['preflight']['cases']:
            yield '  {} CPU / {:g} GiB: {} {}'.format(case['jobs'],case['memory_gb'],
                'fits snapshot' if case['fits_now'] is True else 'blocked' if case['fits_now'] is False else 'unknown',case['reason'])
    yield 'Reviewed trial candidates: '+str(len(value['candidates']))
    for index,candidate in enumerate(value['candidates']):
        yield ('> ' if index==selected else '  ')+candidate['kind']+': '+str(candidate['proposed'])
        evidence=candidate['evidence']
        yield '  {} successes; {} measured; sampled peak RSS {}'.format(evidence.get('successful_jobs'),
            evidence.get('measured_jobs','unrecorded'),evidence.get('sampled_peak_rss_gb','unrecorded'))
        yield '  '+candidate['action']
        if index==selected:
            yield '  '+shlex.join(candidate['proposal_command'])
    yield value['note']
