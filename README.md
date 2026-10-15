# Agent Builds

A standalone terminal dashboard and shared build queue for a Linux workstation
running several coding agents. Works inside Ghostty, Herdr, and tmux. Python 3.8+
and the standard library are sufficient; no npm install or Cargo build is needed.

## Start

```sh
cd /home/dom/agent-builds
./agent-builds
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
