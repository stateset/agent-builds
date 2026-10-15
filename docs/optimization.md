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
buildr run --lane quick --jobs 1 --memory-gb 3 --timeout 180 -- \
  buildr optimize ask --model qwen3.5:0.8b --threads 1 \
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
