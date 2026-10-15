# Verification plans

A plan links normal queued jobs into a dependency graph. Its controller reserves
no CPU or memory while waiting. Each ready stage gets its own budgets, output
directory lock, timeout, exit code and log. Commands remain argv arrays; use an
explicit shell command only when you want shell evaluation. Do not put queue
wrappers inside a plan command: the plan already queues each stage.

```sh
agent-builds plan validate examples/verify.json --cwd /home/dom/agent-builds
agent-builds plan run examples/verify.json --cwd /home/dom/agent-builds
agent-builds plan list --active --json
agent-builds plan show PLAN_ID --json
agent-builds plan wait PLAN_ID --timeout 600 --json
agent-builds plan logs PLAN_ID tests --follow
agent-builds plan export PLAN_ID --output /tmp/new-verification-evidence.json
agent-builds plan cancel PLAN_ID
agent-builds plan resume PLAN_ID
```

`run` returns a durable handle after starting a detached controller. `run --wait`
also waits and returns the plan's aggregate exit code. Closing the terminal or
dashboard does not cancel the plan. `wait` is read-only; a wait timeout returns
124 and leaves work running. A failed command preserves its original exit code.
Plans report the first failing stage's nonzero command code in manifest order,
or 1 for a verification failure such as a missing artifact after command exit 0.
Plan cancellation returns 130 once active stages have settled.

## Manifest

```json
{
  "version": 1,
  "name": "Repository verification",
  "max_parallel": 2,
  "source_policy": "report",
  "stages": [
    {"name": "tests", "command": ["cargo", "test"], "lane": "heavy", "jobs": 3, "memory_gb": 6},
    {"name": "lint", "command": ["cargo", "clippy"], "after": ["tests"]},
    {"name": "build", "command": ["cargo", "build", "--release"], "after": ["lint"], "artifacts": ["target/release/program"]}
  ]
}
```

A stage without `after` depends on the preceding stage; the first stage has no
dependencies. Use `after: []` for an independent stage. Dependencies may refer
forward, but cycles and missing names are rejected. Failed, cancelled or lost
prerequisites skip their dependents without creating jobs. Independent stages
continue after a different branch fails. At most `max_parallel` jobs from the
plan are submitted at once (default 1, maximum 16); the machine's shared queue
still governs actual admission. Sharing a checkout/output key still serializes
jobs, even when the graph allows parallelism.

Stages support `cwd` within the plan checkout, `lane`, `jobs`, `memory_gb`,
`backend` (`local` or `docker`), `image`, `timeout`, `resource_key`, `min_free_gb`,
`scratch_gb`, `source_policy` and `artifacts`. Defaults are resolved when the
manifest is submitted. An explicit output key can allow independent outputs to
run together; it does not disable the global budget. Stages do not inherit a
separate reservation from the controller. Local reservations remain estimates;
Docker stages retain normal container CPU/memory limits.

Manifests are limited to 64 KiB and 64 stages. Artifact declarations contain at
most 32 exact relative regular-file paths per stage. Globs and directory bundles
are not expanded; declare the report/archive files produced by the command.
Missing, escaping, symlinked or incompletely hashed required artifacts fail the
stage while preserving the command job's original result. Dependent stages wait
until evidence capture has settled that verification result.

## Source identity

Plan submission and stage start/finish record Git HEAD, a tracked-diff hash and
an aggregate hash of nonignored untracked files. Source contents and inherited
environment values are not stored. Literal command arguments are stored as
submitted. The default `report` policy flags observed changes while preserving
the command result. `require_unchanged` refuses to launch when the submission
identity no longer matches, or cannot be verified; changes during a successful
command fail the stage verification without rewriting command exit 0.

These are observations of a live checkout, not atomic or immutable source
snapshots. Ignored files, toolchains, environment and external services are
outside the fingerprint. Generated build artifacts should be ignored by Git when
using strict source checks. Non-Git directories work with `report`, but their
source identity is explicitly unavailable. Capture is bounded to six seconds,
16 MiB of tracked diff and 64 MiB of nonignored untracked contents. Limit failures
are recorded as unavailable; an absent fingerprint is never claimed unchanged.
Ordinary jobs can opt into the same observations with `run --track-source`.

## Recovery and evidence

Stage job creation and its plan link are committed together before launch. A
prepared worker can attach once. If a controller crashes, `resume` monitors its
existing jobs and dispatches only never-submitted stages. It never retries a
submitted stage whose outcome is unknown. A launch that cannot be reconciled
can remain lost/failed rather than risk duplicate work. Terminal plans cannot be
resumed as retries; submit a new plan explicitly. Cancellation only affects that
plan's jobs, and retains reservations for unsettled process/container outcomes.

Completion records per-stage source observations, artifact and log hashes,
native job results, timings and resource measurements. Hashes are captured before
dependent stages start; later overwrites do not replace the recorded evidence.
File hashing is bounded to 256 MiB and five seconds per file; unavailable or
partial hashes have explicit statuses. Evidence exports serialize those stored
observations and never rehash mutable artifacts to invent a new historic result.
Reports do not embed artifact contents. `--output` creates a private file and
refuses to overwrite an existing path, including an earlier report.

In the TUI, press `o` to submit a plan and `v` to reach its plan view. Arrows
select plans, PgUp/PgDn scroll stages, Enter opens a stage job's log, `c` cancels
the selected plan after confirmation, `r` resumes a stopped controller, and `e`
exports a report. Queued stages continue to show their ordinary admission reason.

## Replacing shell prerequisite guards

If a queued shell command starts by reading another job's result, it still waits
for admission before that guard can fail. Put the commands in dependency stages
instead: a failed prerequisite skips its dependent stages **before job creation**,
even when the queue is paused or disk headroom blocks admission. Ordinary jobs
are literal commands; Buildr does not parse shell scripts to infer dependencies.

Use `buildr plan draft JOB... --cwd PATH` to draft a future manifest from existing
commands, then review dependencies, source scope, resource keys and artifacts.
The draft does not rerun jobs. Submit the reviewed plan explicitly and obtain one
compact acceptance result with `buildr plan wait PLAN --summary --json`. Preserve
unknown, pending and skipped outcomes rather than treating them as passed gates.

Independent checks may declare distinct exact resource keys only when their
inputs and outputs are compatible. An image-manifest lookup can use a key for
that image; a compiler and installer writing the same target/node_modules need
shared locks. Read-only commands can still observe files another stage changes:
use an immutable checkout for frozen-source checks. Source fingerprinting detects
some drift but does not make a mutable checkout atomic. Never remove serialization
merely to improve wait metrics.
