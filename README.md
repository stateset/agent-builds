# Buildr

Herdr organizes coding agents and terminals. Buildr coordinates their builds,
tests and verification results on the shared machine. Use the `buildr` launcher;
the `agent-builds` command remains a compatibility entrypoint to the same queue.

Agents can use the same queue through MCP: start `buildr mcp serve` as a stdio
server. It exposes job submission, durable reconnects, bounded logs/waits and
verification plans. See [MCP setup and tool reference](docs/mcp.md).

Use `buildr diagnose [JOB]` (or `u` in the dashboard) to explain live blockers,
exact disk deficits and pending policy activation without changing shared state.
`status --json` now returns at most 20 concise active-job summaries by default;
use `status --history --details --json` for completed evidence and `show JOB`
for one full result. See [operational diagnostics](docs/diagnostics.md).

Use `buildr watch start` for existing external work: short read-only status
probes use the shared queue, while the durable watch holds no build reservation
between probes. CLI, MCP and full/compact TUI views retain the same watch handle.
See [external completion watches](docs/external-watches.md).

`buildr experiment run` records paired queue/direct fixture outcomes under one
shared-queue reservation. `buildr optimize prepare` previews anonymized trial
evidence; `buildr optimize ask` optionally asks an installed local model to rank
it. Neither changes scheduling settings. See [measuring and optimizing](docs/optimization.md).

Use `group create JOB...` to observe existing work under one handle,
`plan draft JOB... --cwd PATH` to prepare a future verification plan, and
`plan wait PLAN --summary --json` for compact results. See
[job workflows](docs/job-workflows.md) for cleanup stages and policy-aware advice.

Use `agent-builds advise` (or `h` in the full TUI) for evidence-based queue and
reservation findings. `agent-builds simulate --fixture` compares conservative
quick backfill with a draining model without executing commands or changing
settings. See [advisor and replay](docs/advisor.md) and [quality gates](docs/quality-gates.md).

Queue wait diagnostics preserve primary blocker timings and observation coverage;
see [wait diagnostics](docs/wait-diagnostics.md). Narrow terminal splits now use
a compact jobs/plans monitor automatically.

A standalone terminal dashboard and shared build queue for a Linux workstation
running several coding agents. Works inside Ghostty, Herdr, and tmux. Python 3.8+
and the standard library are sufficient; no npm install or Cargo build is needed.

## Start

```sh
cd /home/dom/agent-builds
./buildr
```

Open it in a normal terminal (at least 65 columns by 15 rows). Press `n` to
submit work, arrows or `j`/`k` to select jobs, and `c` to request cancellation.
The bottom panel shows the selected job's output. `q` closes the dashboard;
already submitted jobs continue in separate runner processes.

Press `p` to pause/resume new starts, `b` to edit resource budgets, and `v` to
switch between job logs, system processes, and announcement deliveries. The process view shows
visible processes owned by your user, CPU usage (100% = one logical core), RSS,
and working directories. It does not inspect command-line arguments or send
signals to unrelated work. Docker daemon processes owned by root may not appear.
Budget edits are validated and saved atomically; lowering a limit waits for
existing jobs to finish instead of interrupting them.

Agents use the same queue from any repository:

```sh
/home/dom/agent-builds/agent-builds run --lane quick -- cargo check
/home/dom/agent-builds/agent-builds run -- cargo test
/home/dom/agent-builds/agent-builds status --json
/home/dom/agent-builds/agent-builds status --summary --json
/home/dom/agent-builds/agent-builds status --active --repo /absolute/repo --limit 5 --json
/home/dom/agent-builds/agent-builds logs JOB_ID
/home/dom/agent-builds/agent-builds logs --follow JOB_ID
/home/dom/agent-builds/agent-builds show --json JOB_ID
/home/dom/agent-builds/agent-builds cancel JOB_ID
/home/dom/agent-builds/agent-builds pause
/home/dom/agent-builds/agent-builds resume
/home/dom/agent-builds/agent-builds processes --json
```

`run` waits for resources, streams output, and returns the command's exit code.
It doesn't invoke a shell implicitly. Explicit shell commands are supported:
`agent-builds run -- bash -lc 'npm run lint && npm test'`.
Jobs are noninteractive; run interactive programs in an ordinary pane.

## v0.7.0: verification plans

Link tests, lint, builds and acceptance into one durable verification handle:

```sh
agent-builds plan run examples/verify.json --cwd /home/dom/agent-builds
agent-builds plan show PLAN_ID --json
agent-builds plan wait PLAN_ID --timeout 600 --json
agent-builds plan export PLAN_ID --output /tmp/new-verification-evidence.json
```

Each ready stage uses the shared queue with its own budgets. Failed prerequisites
skip dependent stages; independent branches may continue. Closing the dashboard
does not cancel the plan. Read-only inspection and waiting preserve sandbox-safe
access. Press `o` in the TUI to submit a plan, or `v` to view plan progress.

Plans record source fingerprints at submission/start/finish, and retain per-stage
commands, native exit codes, log/artifact hashes and resource measurements. Drift
is reported by default; strict unchanged-source checks are opt-in. These are live
checkout observations, not isolated snapshots. Controllers can be resumed without
retrying already submitted work; uncertain outcomes remain explicit.

See [verification plans](docs/plans.md) for manifest fields, cancellation,
recovery, source/hash limits and evidence semantics. `stats --json` also adds
observed versus reserved CPU totals and wait percentiles by recorded scheduling
policy. These are descriptive comparisons; budgets are never changed automatically.

## v0.6.0: shorter feedback loops

The default policy reserves two of the six CPU slots, one job slot, and 2 GiB of
the memory reservation budget for quick checks. Young
heavy jobs may use four slots collectively; a three-slot heavy build can run
alongside a two-slot quick check. This trades some bulk-build throughput for
responsiveness. After 120 seconds, a queued job gains priority and may use the
reserved slots. New jobs then wait for existing work to drain rather than
continually overtaking it. Running work is never preempted or resized.
An aged job blocked by its repository, disk, physical-memory headroom, or a
reservation larger than the current total budget does not freeze other repos.
FIFO is preserved within a shared repository/output directory.

```sh
agent-builds config --set quick_reserve_slots=2 --set aging_seconds=120
# Restore full bulk-build capacity if preferred:
agent-builds config --set quick_reserve_slots=0 --set quick_reserve_jobs=0 --set quick_reserve_memory_gb=0
agent-builds stats --repo /absolute/repo --json
agent-builds disk --repo /absolute/repo --json
agent-builds clean --older-than-days 7          # preview only
agent-builds clean --older-than-days 7 --apply  # eligible completed-job logs only
agent-builds wait JOB_ID --json --timeout 300 --tail 10
agent-builds wait JOB_ID ANOTHER_ID --json
```

Local jobs now record process-tree CPU time and sampled peak RSS in `usage`.
Samples run about every half-second and persist about every two seconds; final
CPU accounting also uses reaped-child accounting. RSS sums can count shared
pages more than once and miss short peaks. Measurements are observations, not
resource enforcement. Docker usage stays unmeasured rather than reporting the
Docker client's memory as container memory. Old jobs retain empty usage.
`stats` shows wait percentiles, runtime and measured memory for exact commands
and output-directory keys, lanes and reservations in up to 1,000 recent records,
with lane-level wait percentiles. Memory suggestions need three measured
successes and never update budgets automatically. Queued job JSON and TUI details
show age eligibility and effective reserves; age is not an ETA.

Disk inventory scans only known checkout caches (`target`, `node_modules`,
`.venv`, `.next`) and queue logs, with time limits. It does not scan your entire
home directory or delete repository artifacts. Use `--path /explicit/cache` to
include a custom output directory. Inventory marks caches with active/queued
users, separates measured sizes from declared scratch reservations, and requires
owner review before any cache cleanup. `clean` only removes ordinary,
owned log files belonging to terminal jobs older than the requested retention;
history and active logs remain available. No cleanup runs automatically.

Register explicit shortcuts per checkout; options may follow the task name,
and the literal `--` separates the saved command:

```sh
agent-builds task add check --cwd /absolute/repo --lane quick --jobs 2 -- cargo check
agent-builds task add test --cwd /absolute/repo --lane heavy --jobs 3 --memory-gb 6 -- cargo test
agent-builds task list --cwd /absolute/repo
agent-builds task run check --cwd /absolute/repo
agent-builds task run test --cwd /absolute/repo --detach
```

Shortcuts live in private queue configuration and do not edit repository files,
shell aliases or system Cargo settings. Running a shortcut submits its saved
argv and budgets through the ordinary queue; it never evaluates command text
as a shell. Press `t` in the TUI to select a shortcut, `d` for a background disk
inventory, and `v` to cycle logs, processes, announcements, details, history,
remote records and disk inventory. The job-details view includes measurements.
An explicit `task add --resource-key /output/path` can group an independent
artifact directory; the default still serializes the checkout. Choose an
override only when the commands really do use independent outputs.

`wait` reconnects read-only to queued, running or completed handles and returns
the command's exit code. For multiple handles it waits for all and returns the
first non-success exit in input order. `--timeout` returns 124 while jobs continue;
JSON distinguishes that wait timeout from a command result. Optional tails are
bounded to the last 64 KiB and at most 200 lines per job. Lost jobs return failure.

Dedicated SSH workers are opt-in; see [worker setup](docs/workers.md). Nothing
connects or offloads automatically. Existing Hetzner infrastructure is untouched.
After an upgrade, already-running and queued runner processes keep their loaded
policy; newly submitted jobs use the new policy. No runners are restarted.
New jobs record their scheduler policy version. Old queued runners show an
unrecorded policy and unknown age eligibility rather than claiming that the new
rules apply to them.

## v0.5.0: understand the work using capacity

Running and cancelling jobs appear first in both the TUI and limited status
results. Queued jobs follow in submission order, then recent completed work.
The TUI keeps the selected job stable as other jobs change state. Press `i` for
a full job-details view; PgUp/PgDn scroll it in a small terminal.

New submissions capture available `HERDR_PANE_ID`, `HERDR_TAB_ID`, and
`HERDR_WORKSPACE_ID` values. Job JSON exposes a native `origin` object while
retaining the original `herdr` field. This is submission metadata, not verified
agent identity. Old jobs or environments that lack these values stay unattributed.
Detached runners inherit the submitter's available context.

`wait_context` explains the job's memory reservation separately from admission
headroom. A 2-GiB reservation plus 4-GiB headroom requires 6 GiB available; it does
not reserve 6 GiB. CPU waits include contributing running job IDs, reservations,
command previews and links to logs. `contributing_jobs`, `current_used_cpu_slots`,
and `current_reserved_memory_gb` are live read-only observations timestamped by
`reservations_observed_at`; the original admission measurements retain their
`measured_at` timestamp. Refreshing the view does not change scheduling.

Queued blockers expose their wait duration and total age separately from
execution time. The app's own CPU/memory reservations remain estimates for local
jobs, rather than measured resource use or OS-enforced limits.

Every job includes `worker_budget`: the Cargo/test environment defaults and any
recognized explicit options in a direct Cargo command. For example, a three-slot
`cargo test -- --test-threads=8` retains its original arguments and shows both the
three-thread queue default and the explicit eight-thread setting. Configured
values are not runtime measurements. Shell scripts, custom harnesses, subprocess
settings, Cargo configuration overrides and non-Rust worker flags are outside
this inspection; the app does not silently rewrite commands. See the official
[Cargo test documentation](https://doc.rust-lang.org/cargo/commands/cargo-test.html)
and [Rust test documentation](https://doc.rust-lang.org/rustc/tests/index.html).

Counts always include all seven job states, including zero values. `count_scope`
identifies active-only and repository filters and confirms that counts cover all
matching jobs independently of `--limit`.

Live `logs --follow` now handles its wait loop correctly. The regression test
follows an active producer through its final output, rather than testing only
an already completed log.

## v0.4.0: agent feedback incorporated

Inspection commands (`status`, `show`, `logs`, `announcements`, `processes`,
`doctor`, and config inspection) open existing state read-only. They never create
the state directory, change permissions, run migrations, or recover jobs.
Missing queue state is reported as `initialized: false` with an empty job list.
Actual sandbox read restrictions still require normal approval. Submission and
administration need write access; errors explain that distinction.

JSON jobs now include an `argv` array, `command_display`, log path, elapsed/wait
durations, and structured `wait_context`. The original `command` JSON string is
retained for compatibility. Status is an observation; `owner_alive: false` can
indicate stale state until a writable runner/dashboard reconciles it.

Waiting output identifies the blocking job, names its command, and reports disk
free/required values. It refreshes at least every ten seconds while waiting.
The TUI shows measurements and their timestamp; press `g` on a blocked job to
open its blocker's logs. Every foreground job prints an explicit running event
with its wait duration and a final result.

Use `--events-json` for JSON lifecycle events on stderr. Child stdout and stderr
remain combined on stdout, as in ordinary queued runs. Events include stable job
IDs, timestamps, log paths, waiting resources, and final exit codes.

```sh
# Return immediately; the runner survives a closing tool/terminal session.
agent-builds run --detach -- cargo test
agent-builds show --json JOB_ID
agent-builds logs --follow JOB_ID

# A repository requiring at least 15 GiB before starting.
agent-builds run --min-free-gb 15 -- npm run build

# Reserve an extra 5 GiB for expected output, above the machine watermark.
agent-builds run --scratch-gb 5 -- cargo build --release
```

Disk admission requires `max(global watermark, per-job minimum) + this job's
scratch reservation + active jobs' scratch reservations`. Reservations are
conservative estimates across this workstation, not disk quotas. They cannot
limit an external build or a job that writes more than it declared. No artifacts
are deleted automatically, and wait estimates are not guessed.

Queue a whole validation driver once, or queue its individual expensive steps.
Don't combine both approaches: a nested wrapper now fails immediately with the
parent job ID and guidance, instead of waiting for its parent's reservation.
Child tools inherit Cargo/test thread budgets from their outer queued job.
Detached submission writes a durable receipt before returning the job handle;
completed results and logs remain available in the private state directory.

## Resource policy

Initial settings for the current 8-logical-CPU, 31-GiB workstation:

| Setting | Default |
| --- | --- |
| Combined managed CPU reservations | 6 slots |
| Concurrent managed jobs | 3 |
| Concurrent heavy jobs | 2 |
| CPU slots reserved for quick checks | 2 |
| Job slots reserved for quick checks | 1 |
| Reserved memory capacity for quick checks | 2 GiB |
| Age priority for waiting jobs | 120 seconds |
| Heavy job | 3 CPU slots, 6 GiB reserved |
| Quick job | 2 CPU slots, 2 GiB reserved |
| Combined managed memory reservations | 16 GiB |
| Disk headroom needed to start work | 10 GiB |
| Available RAM left after a new reservation | 4 GiB |
| Command timeout | 2 hours |

These are initial budgets, not a benchmarked optimum. `config --init` creates
an editable JSON file in the private state directory. Use `--jobs`,
`--memory-gb`, `--timeout`, and `--resource-key` per job. Jobs sharing a Git
checkout (or an explicit `CARGO_TARGET_DIR`) serialize to avoid duplicate builds
and Cargo output locks. Worktrees sharing another output directory should pass
that directory as `--resource-key`.

Local jobs receive lower CPU and I/O scheduling priority through `nice` and
`ionice`, plus `CARGO_BUILD_JOBS` and `RUST_TEST_THREADS`. Local CPU/RAM
reservations are estimates, **not OS-enforced limits**. Commands, native
compilers, and other test runners may create extra threads; set their own
worker limits where appropriate. Existing builds, language servers, VM processes,
Docker builds launched elsewhere, and other users remain outside this budget.

Queue state is shared via SQLite transactions; there is no always-on daemon.
Each submitted job has a runner that owns its environment and lifecycle. Closing
the TUI leaves its submitted runners alive. A killed command-line runner is not
automatically retried. Recovery preserves reservations for identifiable live
orphan processes; inspect and cancel those jobs rather than launching duplicates.
For uncatchable runner crashes, process-tree cleanup is best-effort; this is not
a replacement for cgroup containment or an untrusted-code sandbox.

Disk pressure blocks new starts; the application never deletes build artifacts.
Machine load and Linux pressure readings are visible in the TUI and `doctor`.
Budgets are deterministic; pressure values are diagnostic rather than an
unvalidated automatic controller.

## Docker

Choose `docker` in the TUI and enter a suitable toolchain image, or use:

```sh
agent-builds run --backend docker --image rust:1.88 -- cargo check
```

The selected working directory is mounted at `/workspace`; the command runs
with your UID/GID, CPU and memory limits, and no automatic secret forwarding.
Use an image with the dependencies your repository needs. An immutable image
digest gives reproducible toolchains. Host compile caches are not mounted by
default; configure a suitable image rather than exposing your whole home.

Docker jobs also consume the shared scheduling budget. Cancelling one stops
its named container before the runner exits. If a runner crashes, its reservation
is held until explicit cancellation reconciles the container. Access to Docker
is required. Building an image inside a Docker CLI job is intentionally not
supported: it would need access to a separate, resource-limited BuildKit worker.

For a local acceptance harness that launches Docker containers, reserve the
combined expected footprint with `--jobs` and `--memory-gb`, and set explicit
`--cpus`/`--memory` on each container inside the harness. Host niceness and the
queue's estimates do not constrain a separate Docker daemon. Queue the harness
once, not each subprocess again. Its containers remain the harness's cleanup
responsibility; only jobs using this app's Docker backend get managed container
cancellation. Production images can be tested without mounting a Docker socket
inside a queued worker container.

## Herdr and Ghostty

Run the TUI in any normal pane. No Ghostty patch or Herdr server restart is
needed. TUI-submitted jobs request a Herdr notification on completion only if
they inherit `HERDR_ENV=1`; CLI jobs opt in with `--notify`. Integration uses
the installed `herdr notification show` command and does not send instructions
to other agents, move focus, or change workspace layout.

Have each coding agent prefix expensive build/test commands with
`/home/dom/agent-builds/agent-builds run --`. Shell aliases are deliberately not
installed: noninteractive agent shells may ignore them, and transparently
shadowing Cargo can interfere with rustup and build scripts.

### Announce queue adoption

Press `a` in the TUI to preview the current Herdr agents and confirm sending the
queue instructions. From a Herdr-managed agent pane:

```sh
agent-builds announce           # preview recipients and exact policy
agent-builds announce --send    # explicitly send to that snapshot
agent-builds announce --message-file /path/to/message.txt  # preview custom text
agent-builds announce --send --message-file /path/to/message.txt
agent-builds announcements --json
```

The caller is excluded. Running Codex agents receive the instruction through
Herdr's native prompt input so they can adopt it during their current task;
no stop keys are sent. Idle agents receive normal prompts. Blocked/unknown agents
wait for a safe input state, for up to 15 minutes. Agent-session identities are
checked before submission, so a replacement in the same pane is skipped.
Uncertain submissions are recorded and never automatically retried.

`submitted` proves that Herdr accepted the input, not that the agent adopted the
policy. Confirm adoption by looking for its repository's jobs in the queue.
The preset instructs agents to finish existing builds, wrap future expensive
commands, preserve normal approvals, and continue their original tasks. It does
not modify repository instructions or agent configuration files. New agents
started later need a new announcement. Nothing is broadcast without an explicit
`--send` or TUI confirmation.

## Hetzner and mobile follow-up

No server is provisioned or production workload changed by this tool. A trusted
dedicated build host can run the same queue and TUI via SSH:

```sh
ssh -t YOUR_BUILD_HOST 'cd /srv/build/YOUR_REPO && /path/to/agent-builds/agent-builds'
```

Remote routing with source snapshots and artifact return is a follow-up, not
implemented automatic offload. The existing Hetzner plan on this workstation is
marked not provisioned; verify available capacity before adding a worker. Keep
build workers separate from the production k3s cluster.

Next phase: a responsive iPhone dashboard over an authenticated private
connection, backed by an explicit job API. This version opens no network port.

## State and verification

State, logs, and configuration live under `~/.local/state/agent-builds` (override
with `AGENT_BUILDS_STATE`). Environment variables are not persisted. Command
arguments and output can contain secrets, so state is private to your user; use
environment variables rather than command-line credentials.

```sh
python3 -m unittest discover -s tests -v
./agent-builds doctor
```

## v0.14.0: protect several resources together

A build can now protect its checkout and shared output directories in one
atomic admission. Repeat `buildr run --lock KEY` to add locks alongside the
existing primary key. Saved tasks, plan stages, MCP submissions and external
probes preserve these declarations. Queue diagnostics show every key and the
actual conflicting keys. See [resource locks](docs/resource-locks.md) for
examples, exact-key semantics and compatibility with older running workers.

Detached workers now launch from their own package directory, so another
checkout named `agent_builds` cannot replace the worker implementation. The
build command still executes in the explicitly selected working directory.

## v0.15.0: verification responsiveness

`buildr batch run checks.json` submits related short checks under one reservation
with separate native outcomes. `buildr latency --json` reports target attainment,
active overdue jobs and measured reservation trials. Protected quick capacity is
requested with `buildr config --set protected_quick=true`; activation waits for
incompatible workers to drain and keeps total CPU/RAM/job budgets unchanged.
See [verification](docs/verification.md) for tradeoffs, examples and evidence limits.

Explicit helper files outside Git can be recorded with repeated `--input-file`
flags or plan-wide `input_files`. See [declared input evidence](docs/declared-inputs.md)
for strict gates, MCP usage, limits and live-checkout caveats.

For independent checks, batches can opt in to `max_parallel` within a single
CPU/RAM reservation. See [parallel verification](docs/verification.md) and the
[controlled batching comparison](docs/optimization.md).
