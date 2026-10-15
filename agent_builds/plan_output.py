"""Compact, versioned plan results for agents; detailed evidence stays available."""
from pathlib import Path

from .plans import PLAN_TERMINAL


def validate_tail(lines):
    if type(lines) is not int or not 0 <= lines <= 200:
        raise ValueError('Log tails must contain between 0 and 200 lines')


def summary(plan, tail_lines=0):
    validate_tail(tail_lines)
    counts = {}
    stages = []
    for stage in plan['stages']:
        counts[stage['state']] = counts.get(stage['state'], 0)+1
        job = stage['job']
        evidence = stage['evidence']
        source = job['source'] if job else {}
        item = dict(name=stage['name'], state=stage['state'], job_id=stage['job_id'],
                    reason=stage['reason'] or (job['reason'] if job else ''),
                    command_exit_code=job['exit_code'] if job else None,
                    wait_seconds=job['wait_seconds'] if job else 0,
                    elapsed_seconds=job['elapsed_seconds'] if job else 0,
                    log_path=job['log_path'] if job else None,
                    source_changed_before_start=source.get('changed_since_submission'),
                    source_changed_during_run=source.get('changed_during_run'),
                    evidence_captured=bool(evidence),
                    artifacts_captured=sum(a['status']=='captured' for a in evidence.get('artifacts', [])),
                    artifact_issues=[dict(path=a['path'], status=a['status']) for a in evidence.get('artifacts', []) if a['status']!='captured'])
        # Failure/active tails only; successful logs remain reachable by handle.
        if tail_lines and job and stage['state'] != 'succeeded' and stage['state'] != 'skipped':
            try:
                with Path(job['log_path']).open('rb') as log:
                    log.seek(0, 2)
                    log.seek(max(0, log.tell()-65536))
                    item['log_tail'] = log.read().decode('utf-8', errors='replace').splitlines()[-tail_lines:]
                item['log_tail_status'] = 'available'
            except OSError:
                item.update(log_tail=[], log_tail_status='unavailable')
        stages.append(item)
    result = dict(schema='agent-builds-plan-summary-v1', id=plan['id'], name=plan['name'], cwd=plan['cwd'],
                  state=plan['state'], complete=plan['state'] in PLAN_TERMINAL, exit_code=plan['exit_code'],
                  elapsed_seconds=plan['elapsed_seconds'], stage_counts=counts, stages=stages,
                  source=dict(status=plan['source'].get('status', 'unavailable'),
                              head=plan['source'].get('head'), dirty=plan['source'].get('dirty')))
    result['verification_failed_after_command_success'] = any(s['state']=='failed' and s['command_exit_code']==0 for s in stages)
    return result


def compact(result, tail_lines=0):
    """Preserve the wait wrapper and exit codes when summarizing its plan."""
    if isinstance(result, list):
        return [summary(plan, tail_lines) for plan in result]
    if 'plan' in result:
        return dict(result, plan=summary(result['plan'], tail_lines))
    return summary(result, tail_lines)


def text(result):
    current = result.get('plan', result)
    output = ['{} {} | exit {} | {}'.format(current['id'], current['state'], current['exit_code'], current['name'])]
    for stage in current['stages']:
        output += ['{} {} | job {} | command exit {} | {}'.format(stage['name'], stage['state'],
                   stage['job_id'] or '-', stage['command_exit_code'], stage['reason'])]
        output += stage.get('log_tail', [])
    if result.get('timed_out'):
        output += ['Wait timed out; plan continues.']
    if current['verification_failed_after_command_success']:
        output += ['A command exited 0 but stage verification failed; inspect the artifact/source result.']
    return output
