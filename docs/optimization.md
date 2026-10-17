# Measuring and optimizing Buildr

Herdr organizes agents and terminals. Buildr coordinates their builds and
verification. The new `buildr` command and existing `agent-builds` command address
the same queue; state directories, Python package and GitHub repository keep
their existing names. No relocation or new daemon is required.

## Controlled paired experiments

Queue the entire benchmark once:

```sh
buildr run --lane quick --jobs 2 --memory-gb 2 -- \
  buildr experiment run --output /tmp/new-buildr-experiment.json \
  --pairs 3 --agents 4 --cpus 2
```

The experiment accepts only built-in fixtures, never commands taken from job
history or user-provided shell snippets. Both arms run inside that outer
reservation with identical CPU affinity and worker hints. The direct arm launches
all fixture agents together. The queued arm uses a private inner queue with one
CPU per job, `cpus` total slots and no quick reserve. The inner queue does not
contact the production queue. This lab configuration does not alter live budgets.
Local reservations and affinity are not RAM hard limits.

Three scenarios run by default (repeat `--scenario` to select a subset):

- `shared_output`: competing writers verify their own output in one directory.
  Queue serialization can preserve correctness at a wall-time cost.
- `resource_contention`: independent directories, a fixed CPU workload and
  96 MiB per fixture agent compare simultaneous launches against bounded admission.
- `python_verification`: each agent runs Buildr's actual worker-budget unit tests
  in a separate directory. This exposes scheduler overhead on very short checks.

One to five pairs and two to six fixture agents are supported. Pair order
alternates. Each report keeps per-arm verified completions, native exit codes,
wall time, completion p95, sampled process-tree RSS, child CPU, driver event-loop
lag and before/after machine pressure. The event-loop measurement is a proxy,
not Ghostty input latency. Logs, isolated state and result files remain beneath a
private fixture directory alongside the report. Existing output paths are never
overwritten. Interrupted work is not retried automatically.

Use verified throughput alongside wall time: finishing faster with corrupted
results is not a useful win. Sampled RSS can miss peaks or count shared pages
twice. Host activity, caches, Python startup and small samples affect the result.
This compares controlled scheduling behaviors; it does **not** establish that
autonomous agents produce more accepted code. That requires matched real tasks,
the same acceptance criteria and explicit task-level observations.

## Optional local AI advice

The local provider is experimental. A first one-thread trial on this workstation
hit the 90-second request timeout and was rejected without applying anything.
The request is not automatically retried; check that server-side work has settled
before choosing a new measured trial. Model latency under host contention is a
separate cost to assess, not an assumed optimization benefit.

First preview evidence without calling a model:

```sh
buildr optimize prepare --policy current --output /tmp/new-buildr-pack.json
```

This reads existing queue history without migrating it. At least three matching
successful runs are required by the underlying advisor before a trial exists.
Only twelve candidates at most are considered. The report includes private local
bindings to original evidence, plus the exact model payload and its SHA-256.
The payload uses an explicit allowlist of numeric measurements, known trial
kinds, lanes and reservations. It excludes command lines, paths, source contents,
logs, environment values, job IDs and arbitrary historical text.

When a small model is already installed in a local Ollama server:

```sh
buildr run --lane quick --jobs 2 --memory-gb 3 --timeout 180 -- \
  buildr optimize ask --model qwen3.5:0.8b --threads 2 \
  --output /tmp/new-buildr-ai-advice.json
```

The model name is explicit. The client uses only `127.0.0.1:11434`, ignores proxy
and `OLLAMA_HOST` overrides, and refuses cloud/remote models. It does not download
models or start a server. The model's on-disk size must fit the requested memory
reservation with 2 GiB estimated overhead. CPU inference requests at most the
reserved thread count and no GPU layers. The server is a separate process:
these hints and estimates are not hard CPU/RAM limits, and job telemetry does
not account for that server's whole process tree.

Structured output may rank zero to three existing candidate IDs and explain the
choice. Local validation rejects unknown or duplicate IDs, extra fields, control
characters, excessive text and incomplete/truncated responses. The model cannot
introduce commands, choose new values or apply changes. Its explanations remain
unverified advice. Try one change on representative inputs, measure runtime and
correctness, and save a profile only after reviewing the comparison. Empty
candidate sets skip inference. Reports retain request, model digest, evidence
bindings and accepted/rejected status privately; a rejected response exits 2.
Network failures are not retried. A timed-out server-side request may still be
settling. Successful requests ask Ollama to unload the model immediately.

The optional provider uses the official [Ollama generation API](https://docs.ollama.com/api/generate)
and [structured output format](https://docs.ollama.com/capabilities/structured-outputs).
There is no required model service or third-party Python dependency.

## First controlled workstation measurements

Three counterbalanced pairs, four fixture agents and two CPUs at source commit
`5c51103` produced the following outcomes. The queued inner configuration had no
quick reserve; production budgets were unchanged. Ratios are means of the three
paired queued/direct ratios, so they are descriptive rather than significance
estimates.

| Fixture | Direct verified | Queued verified | Queued/direct wall time | Queued/direct sampled RSS |
| --- | --- | --- | --- | --- |
| Shared output | 3/12 | 12/12 | 3.64 | 1.29 |
| Resource contention | 12/12 | 12/12 | 2.06 | 0.66 |
| Short Python verification | 12/12 | 12/12 | 3.07 | 1.51 |

Serialization preserved shared-output correctness and bounded admission reduced
sampled memory for the allocation fixture. Per-job startup and polling had a
substantial cost for these short commands. This evidence motivates batching
related tiny checks while retaining independent acceptance results. It does not
prove that the queue makes every build faster or improves overall autonomous
coding productivity. The original private measurements and per-job logs are in
the release checkout's `.agent-builds-output/paired-experiment.json` and referenced
fixture directory; source identity was unchanged throughout that experiment.

## Measuring batching strategies (v0.18)

```sh
buildr run --lane quick --jobs 2 --memory-gb 2 -- \
  buildr experiment run --output /tmp/new-batching-comparison.json \
  --pairs 4 --agents 4 --cpus 2 --scenario python_verification --include-batches
```

This opt-in comparison adds sequential and parallel batches to the existing
independent-launch and separate-queued-job arms for the fixed worker-budget unit
checks. Every arm stays within the same outer reservation and CPU affinity. Each
batch holds one aggregate inner reservation; separate jobs use one slot each.
Sequential batch checks receive the full batch worker hint; parallel checks divide
it. This compares workflow strategies, rather than holding every process setting
identical. Unit checks remain fixed literal argv; no historical commands execute.

Mode order rotates between pairs; four pairs put each mode in each position once.
Reports preserve per-check native outcomes, wrapper outcomes and handles for
batches, wall time, sampled RSS, CPU and paired batch/separate-queue ratios.
The fixture measures repeated small verification checks, not compilation,
Ghostty responsiveness, accepted code changes or autonomous coding productivity.
Report costs and all verified outcomes alongside any speedup. Source-before/after
observations and machine pressure remain part of the report. Existing two-arm
experiments retain their default behavior.

## Buildr's own package and TUI verification workloads

The opt-in `buildr_verification` scenario runs actual Buildr package builds and
the browser/prompt regression suites, rather than synthetic allocation work.
Use a clean, committed developer checkout and a Python environment with the
optional `build` tool and its packaging backend already installed. Package
builds use `--no-isolation`; the experiment never installs dependencies or
downloads packages.

```sh
buildr run --lane quick --jobs 2 --memory-gb 2 --timeout 1800 -- \
  python -m agent_builds experiment run --scenario buildr_verification \
  --include-batches --pairs 4 --agents 4 --cpus 2 \
  --output /tmp/new-buildr-verification.json
```

Each logical agent gets an independent copy of the same frozen Git archive.
It builds wheel/source distributions, runs `test_tui_browser.py` and runs
`test_tui_prompt.py`. These fixed test suites use private state and mocked
actions; they do not launch repository builds, cancel live jobs or send Herdr
announcements. No command is chosen from history or supplied by a model.

The driver independently rechecks all three native stage exits, artifact hashes,
package versions and required modules. Separate queued jobs hold one CPU slot;
serial/parallel batches share one aggregate reservation. All arms inherit the
same CPU affinity. Four pairs rotate each of the four strategies into every
execution position once. Source-copy setup time is reported separately from
execution wall time, which includes admission, worker startup, build/test work
and evidence checking. End-to-end time includes setup as well.

Reports retain failures, native/wrapper exits, artifacts, measured CPU/RSS,
source identity and host pressure. Buildr-workload performance ratios require
unchanged complete source and fully verified results in both arms. Otherwise
they remain null and the command returns nonzero. A failed arm also retains
completed-arm evidence and its active-arm identity. A `.progress.json` receipt
records the private fixture directory before any arm starts. An existing report
or receipt is never reused: inspect it rather than automatically repeating work
whose outcome is uncertain.

This measures Buildr's own Python package/test workflow on the observed machine.
It does not establish Rust/Node throughput, long-duration reliability, statistical
significance, terminal input latency or accepted autonomous coding outcomes.

### Process telemetry cost

Job telemetry still samples the current user's visible processes at the existing
rate. The scanner uses directory entries and bounded binary stat reads, and
indexes parents once to expand each selected process tree. It keeps the original
PID/start-time membership rules, including observed reparented descendants;
process-group discovery requires the original leader. Telemetry never authorizes
process signalling. It remains a sampled estimate that may miss short peaks or
count shared pages more than once.

To compare scanner cost and measurement parity with v0.26.0:

```sh
buildr run --lane quick --jobs 1 --memory-gb 2 --timeout 120 -- \
  python3 tools/verify_telemetry_scan.py --output /tmp/new-telemetry-study.json
```

This runs four rotating baseline/candidate pairs against fixed metadata for
1,000 processes, including shallow and deep trees (depths 1, 4 and 32) plus
unrelated processes. Every
arm must produce the same identities, CPU counters and peak RSS. Fixture process
metadata never executes commands. The report preserves source identity and
failures; timings measure this scanner workload rather than compiler throughput,
terminal responsiveness or agent productivity. CI checks measurement parity
without imposing a timing threshold on shared runners.

### Before/after runner workload comparisons

`verify_runner_cost.py` compares the current runner with the pinned, accepted
commit immediately before the telemetry optimization (`c0aaabc`). Both runners
execute exactly the same frozen copy of the current Buildr package/browser/prompt
workload, with identical Python, queue limits, CPU affinity and worker hints. The
controller uses the same candidate instrumentation for both engines, sampling
at a recorded 100ms interval and reporting its own CPU cost. Sampled RSS may
miss shorter peaks. This avoids
comparing unrelated measurements taken in different sessions.

Run from a clean committed checkout with the optional packaging tools installed:

```sh
buildr run --lane quick --jobs 2 --memory-gb 2 --timeout 1800 -- \
  /path/to/developer/python tools/verify_runner_cost.py \
    --pairs 4 --agents 4 --cpus 2 --output /tmp/new-runner-cost.json
```

The whole driver has one shared reservation. Inner queues, runner libraries and
native build outputs belong to private fixtures. Four rounds alternate engine
order; every logical agent builds a wheel/sdist and runs the browser and prompt
suites once. Native workers import the frozen workload explicitly, even when
launched by the older runner. No historical command is selected or retried.

Reports retain native stage exits, artifact hashes, workload/archive identities,
controller and child CPU observations, sampled RSS and execution/setup timings.
Outer queue wait is excluded from execution time and remains in the outer job's
record. Ratios require every expected job to verify and unchanged source; failures
or interruptions preserve evidence and suppress conclusions. Existing report or
progress paths cannot be reused. CPU/RSS are observations rather than cgroup
limits. Host activity, caches and small samples affect timings; this measures
Buildr's fixed verification workload, not broader agent coding productivity.
