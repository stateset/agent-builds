# Representative workflow comparisons

Supply two explicit verification manifests with the same commands, stage names,
dependencies, working directories, locks, timeouts, declared inputs and artifact
criteria. Change only CPU/RAM reservations, lanes or plan `max_parallel`.

```sh
buildr plan review baseline.json --cwd /absolute/repo --output /tmp/reviewed-lanes.json
# Review suggestions before choosing either manifest for execution.
buildr experiment workflow baseline.json candidate.json --cwd /absolute/repo \
  --pairs 2 --timeout 3600 --output /tmp/new-workflow-study.json
buildr experiment compare BASELINE_PLAN_ID CANDIDATE_PLAN_ID --output /tmp/new-comparison.json
```

The workflow command is a controller: **run it directly**. Each native stage is
submitted to the normal shared queue with its declared resources. It does not
hold an outer build reservation or create a private scheduler. Existing fixture
`experiment run` retains its different contract of one outer reservation.

One to three pairs alternate baseline/candidate order. Two pairs put each arm
first once. Both manifest files and declared helpers are fingerprinted, strict
source checking applies to every stage, and source must match before each arm.
Store reports and manifests outside the source checkout. No cache is deleted,
global limit tuned, shell script split or historical command selected for you.
Only choose independent outputs when permitting stage overlap; resource keys
are preserved between arms. Include any required cleanup as an explicit plan
stage. Caches and host background activity are not controlled.

Reports retain native and verification failures, skipped work, source changes,
verified stage completions per hour (including queue wait), short quick-check
wait p95, per-job sampled RSS/CPU coverage, heavy-stage completion/wait data and
sampled host pressure. Host pressure is sampled every half second; maxima/minimum
available RAM cover all samples, while at most 300 raw samples per arm are kept.
The host-heavy completion counter includes other agents and does not attribute
their progress to this study. Per-job peak RSS is not an aggregate concurrent
memory peak. Missing measurements remain missing.
Maximum sampled waiting-heavy counts and ages retain still-waiting work as
censored observations; a completion-only percentile cannot describe that work.

Throughput ratios are withheld unless both whole arms have matching complete
source evidence and all stages verify successfully. Failures remain visible even
when no ratio is appropriate. Existing-plan comparisons are read-only and do not
reinterpret old results using today's resource limits. Changing lanes can change
the observed short-quick cohort; compare cohort sizes as well as percentiles.
The evidence describes these explicit workflows, not accepted autonomous coding
tasks, statistical significance or an overall coding-productivity gain.

A private `.progress.json` file records a preallocated plan handle before each
submission attempt. If monitoring fails, the deadline expires or the terminal is
interrupted, already submitted plans continue. Inspect their recorded handles;
the controller never automatically retries, cancels or resumes uncertain work.
Reports refuse overwrite. A study with native failures still writes its report;
consumers must inspect arm states and `comparable`, not treat report creation as
successful verification.

## Keep short checks responsive

`plan review` inspects an explicit manifest without changing or running it. It
labels stages with total timeout <=30 seconds as bounded short, flags repeated
matching quick work with median runtime >=60 seconds after at least three
successes, and proposes moving such work to heavy only when its budget fits the
protected pool. It does not infer dependencies from shell syntax or alter budgets
to make a move fit. Long batches receive splitting guidance instead.

The optional output contains the proposed manifest for review. Existing commands,
dependencies and the original file remain unchanged. Put short prerequisites in
their own stages and declare success dependencies for the longer work. This is
workflow guidance; it adds no scheduling policy or latency guarantee.
