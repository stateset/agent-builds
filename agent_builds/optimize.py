"""Optional local model ranking of evidence-backed trials; no execution or tuning."""
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import time

from . import advice
from .plans import write_export
from .store import Store

MAX_BYTES = 131072
MAX_CANDIDATES = 12
CODES = ('long_quick_command', 'tiny_check_trial', 'memory_trial', 'low_average_cpu')
MEASURES = ('successful_jobs', 'measured_jobs', 'median_runtime_seconds',
            'sampled_peak_rss_gb', 'observed_cpu_seconds', 'reserved_cpu_seconds',
            'observed_to_reserved_cpu_ratio')
SYSTEM = ('Rank up to three candidate resource trials using only the supplied numeric evidence. '
          'Return JSON matching the schema. Candidate IDs are references, not commands. '
          'Explain tradeoffs briefly. Sampled averages do not prove safe peaks or speedups. '
          'Recommend testing one change at a time; never claim a change was applied. '
          'If evidence is insufficient, return no selections. No tools are available.')
NOTE = ('Advisory only. Model reasoning is unverified; selected trials require a representative '
        'measurement before saving a profile. No commands, jobs, profiles or scheduler settings '
        'are changed. Local model-server CPU and RAM are not hard limited by this reservation.')


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def prepare(store, repo=None, hours=24, limit=200, policy='current'):
    history = advice.report(store, repo, hours, limit, policy)
    candidates, bindings = [], {}
    for finding in history['findings']:
        if finding['code'] not in CODES or not finding['suggested_trial']:
            continue
        evidence, trial = finding['evidence'], finding['suggested_trial']
        identifier = 'c{:02d}'.format(len(candidates)+1)
        # Explicit allowlists: never forward argv, paths, log text, environment,
        # arbitrary evidence keys or historical free text to the model.
        measurements = {key: evidence[key] for key in MEASURES if finite(evidence.get(key))}
        baseline = {key: evidence[key] for key in ('slots', 'memory_gb') if finite(evidence.get(key))}
        if evidence.get('lane') in ('quick', 'heavy'):
            baseline['lane'] = evidence['lane']
        proposed = {key: trial[key] for key in ('jobs', 'memory_gb') if finite(trial.get(key))}
        if trial.get('lane') in ('quick', 'heavy'):
            proposed['lane'] = trial['lane']
        if not proposed:
            continue
        candidates.append(dict(candidate_id=identifier, kind=finding['code'],
            measurements=measurements, baseline=baseline, proposed_trial=proposed))
        bindings[identifier] = finding
        if len(candidates) == MAX_CANDIDATES:
            break
    payload = dict(schema='buildr-model-input-v1', history_sampled=history['history_sampled'],
                   history_truncated=history['history_truncated'], candidates=candidates)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return dict(schema='buildr-optimization-pack-v1', created_at=time.time(), payload=payload,
                payload_sha256=digest, private_bindings=bindings, changes_applied=False,
                policy_filter=history['policy_filter'], note=NOTE)


def schema(ids):
    return dict(type='object', additionalProperties=False, required=['summary', 'selections'],
        properties=dict(summary=dict(type='string', minLength=1, maxLength=600),
            selections=dict(type='array', maxItems=3, items=dict(type='object',
                additionalProperties=False, required=['candidate_id', 'reason'], properties=dict(
                    candidate_id=dict(type='string', enum=list(ids)),
                    reason=dict(type='string', minLength=1, maxLength=600))))))


def text_field(value):
    return isinstance(value, str) and 1 <= len(value) <= 600 and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)


def validate(value, ids):
    if not isinstance(value, dict) or set(value) != {'summary', 'selections'} or not text_field(value['summary']):
        raise ValueError('Model returned an invalid summary')
    selections = value['selections']
    if not isinstance(selections, list) or len(selections) > 3:
        raise ValueError('Model returned too many selections or invalid selections')
    seen = set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {'candidate_id', 'reason'}:
            raise ValueError('Model returned an invalid selection')
        identifier = selection['candidate_id']
        if not isinstance(identifier, str) or identifier not in ids or identifier in seen or not text_field(selection['reason']):
            raise ValueError('Model returned an unknown/duplicate candidate or invalid reason')
        seen.add(identifier)
    return value


def local_json(method, path, body=None, timeout=90):
    # Fixed numeric loopback address, no redirects/proxies/OLLAMA_HOST and no
    # authentication forwarding. This client cannot silently use a remote URL.
    connection = http.client.HTTPConnection('127.0.0.1', 11434, timeout=timeout)
    try:
        connection.request(method, path, body=None if body is None else json.dumps(body, allow_nan=False),
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(MAX_BYTES+1)
        if len(raw) > MAX_BYTES or response.status != 200:
            raise ValueError('Local model server returned an oversized or unsuccessful response')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('Local model server returned a non-object response')
        return value
    except (OSError, http.client.HTTPException) as error:
        raise ValueError('Local model request failed; it was not retried. A server-side inference may still be settling: '+str(error)) from error
    finally:
        connection.close()


def inference_budget(threads):
    if type(threads) is not int or not 1 <= threads <= 6:
        raise ValueError('Model threads must be between 1 and 6')
    handle = os.environ.get('AGENT_BUILDS_JOB_ID')
    if not handle:
        raise ValueError('Queue the whole optimize ask command before running local inference')
    reader = Store(read_only=True)
    try:
        row = reader.get(handle)
    finally:
        reader.close()
    if row['state'] != 'running' or row['slots'] < threads or row['memory_gb'] < 3 or row['backend'] != 'local':
        raise ValueError('Local inference requires a running local reservation with matching CPUs and at least 3 GiB')
    return dict(job_id=handle, slots=row['slots'], memory_gb=row['memory_gb'])


def ask(pack, model, output, threads=1):
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Model report must use a new output path')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,120}', model) or 'cloud' in model.lower():
        raise ValueError('Select an explicitly installed local model, not a cloud model')
    budget = inference_budget(threads)
    ids = [candidate['candidate_id'] for candidate in pack['payload']['candidates']]
    record = dict(schema='buildr-optimization-result-v1', pack=pack, model=model,
                  provider='ollama-loopback', reservation=budget, threads=threads,
                  changes_applied=False, note=NOTE)
    if not ids:
        record.update(status='no_candidates', model_called=False)
        write_export(output, record)
        return record
    tags = local_json('GET', '/api/tags')
    models = tags.get('models', [])
    if not isinstance(models, list):
        raise ValueError('Local model inventory is invalid')
    installed = next((item for item in models if isinstance(item, dict) and item.get('name') == model), None)
    if not installed or not finite(installed.get('size')) or not 0 < installed['size'] <= (budget['memory_gb']-2)*1024**3 or installed.get('remote_host') or installed.get('remote_model'):
        raise ValueError('Model must already be local and fit the reservation with 2 GiB estimated overhead; no download or remote call is attempted')
    request = dict(model=model, system=SYSTEM, prompt=json.dumps(dict(evidence=pack['payload'],
        response_schema=schema(ids)), allow_nan=False), stream=False, think=False,
        format=schema(ids), keep_alive=0, options=dict(temperature=0, num_predict=512,
            num_ctx=4096, num_thread=threads, num_gpu=0))
    started = time.monotonic()
    try:
        response = local_json('POST', '/api/generate', request)
        if response.get('done') is not True or response.get('done_reason') != 'stop' or response.get('model') != model:
            raise ValueError('Model response was incomplete, truncated, or from an unexpected model')
        result = validate(json.loads(response['response']), ids)
        record.update(status='advisory_ready', model_called=True, model_digest=installed.get('digest'),
                      request=request, response=result, duration_seconds=time.monotonic()-started,
                      selected_trials=[dict(candidate_id=s['candidate_id'],
                        suggested_trial=pack['private_bindings'][s['candidate_id']]['suggested_trial'],
                        evidence=pack['private_bindings'][s['candidate_id']]['evidence'],
                        model_reason=s['reason']) for s in result['selections']])
    except (ValueError, KeyError, TypeError) as error:
        # Keep the exact request and a failed attempt audit, but never turn an
        # invalid model response into accepted advice or retry it automatically.
        record.update(status='model_rejected', model_called=True, request=request,
                      error=str(error), duration_seconds=time.monotonic()-started)
    write_export(output, record)
    return record
