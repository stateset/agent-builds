"""Explicit drafts from saved argv; never parse or execute shell harnesses."""
from pathlib import Path
import time

from .plans import validate
from .source import digest_file


def draft(store, jobs, cwd, inputs=None):
    handles=list(dict.fromkeys(jobs))
    if not 1<=len(handles)<=64:
        raise ValueError('Drafts require 1 to 64 existing job handles')
    if len(inputs or [])>32:
        raise ValueError('Capture at most 32 explicit input files')
    root=Path(cwd).expanduser().resolve()
    stages=[]
    bindings=[]
    with store.read_snapshot():
        config=store.config()
        rows=[store.get(job) for job in handles]
    for row in rows:
        location=Path(row['cwd']).resolve()
        if location!=root and root not in location.parents:
            raise ValueError('All draft jobs must be within the selected checkout')
        stage=dict(name='job-'+row['id'],command=row['argv'],cwd=str(location.relative_to(root)),
                   lane=row['lane'],backend=row['backend'],jobs=row['slots'],memory_gb=row['memory_gb'],
                   timeout=row['timeout'],image=row['image'],resource_key=row['resource_key'],
                   min_free_gb=row.get('min_free_gb',0),scratch_gb=row.get('scratch_gb',0))
        stages.append(stage)
        bindings.append(dict(stage=stage['name'],job_id=row['id'],state=row['state'],
                             source_observations=row['source']))
    manifest=dict(version=1,name='Draft verification',source_policy='report',stages=stages)
    validate(manifest,root,config)
    captured=[dict(digest_file(Path(path).expanduser().absolute(),limit=1024**2,seconds=1),
                   observation_scope='draft_creation_only') for path in (inputs or [])]
    return dict(schema='agent-builds-plan-draft-v1',manifest=manifest,job_bindings=bindings,
                input_observations=captured,observed_at=time.time(),commands_submitted=False,
                note='Review before running. Caller order becomes success-only dependencies for a future run; no past dependencies are inferred. Shell harnesses remain literal commands. Input hashes describe files now, not at the original job start, and are not enforced as execution inputs. Source observations are retained only when originally recorded.')
