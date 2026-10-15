"""Review explicit plans for long quick checks without executing or splitting argv."""
import json

from . import advice, plans
from .protection import validate_job
from .runner import resource_key


def report(store,manifest,cwd):
    normalized=plans.validate(manifest,cwd,store.config())
    findings=advice.report(store,cwd,limit=2000,policy='current')['findings']
    long=[]
    for finding in findings:
        if finding['code']=='long_quick_command':
            row=store.get(finding['evidence']['job_ids'][0])
            long.append((row,finding))
    proposed=json.loads(json.dumps(manifest))
    stages=[]
    for index,stage in enumerate(normalized['stages']):
        key=stage['resource_key'] or resource_key(stage['cwd'])
        resources=sorted(set([key]+stage['locks']))
        observation=next((f for row,f in long if row['cwd']==stage['cwd'] and row['argv']==stage['command'] and
            row['backend']==stage['backend'] and row['lane']==stage['lane'] and row['slots']==stage['jobs'] and
            row['memory_gb']==stage['memory_gb'] and sorted(row['resource_keys'])==resources),None)
        warnings=[]
        classification='bounded_short' if stage['timeout']<=30 else 'unmeasured'
        suggested=stage['lane']
        if stage['lane']=='quick' and observation:
            classification='long_observed'
            warnings.append('Repeated matching quick checks run for at least 60 seconds; separate short prerequisites from long work.')
            if len(stage['command'])>=2 and stage['command'][-2]=='batch-worker':
                warnings.append('Split this batch into explicit independent short batches or plan stages; its commands are not parsed automatically.')
            else:
                try:
                    validate_job(dict(store.config(),protected_quick=store.config().get('protected_quick_requested',False)),
                                 'heavy',stage['jobs'],stage['memory_gb'])
                    suggested='heavy'
                    proposed['stages'][index]['lane']='heavy'
                except ValueError:
                    warnings.append('The historical budget does not fit the protected heavy pool; validate a measured resource trial before moving it.')
        elif stage['lane']=='quick' and stage['timeout']>30:
            warnings.append('No short runtime bound: use an explicit short prerequisite or collect repeated matching timings.')
        stages.append(dict(name=stage['name'],lane=stage['lane'],suggested_lane=suggested,
            classification=classification,timeout=stage['timeout'],after=stage['after'],warnings=warnings,
            historical_evidence=observation['evidence'] if observation else None))
    plans.validate(proposed,cwd,store.config())
    return dict(schema='buildr-workflow-review-v1',stages=stages,manifest=proposed,
        changes_proposed=sum(stage['lane']!=stage['suggested_lane'] for stage in stages),commands_submitted=False,
        note='Review lane suggestions before submitting this manifest. Historical timing does not prove future runtime. Dependencies, literal commands and running work are preserved; shell scripts are never split automatically.')
