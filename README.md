# Buildr

A local build queue for coding agents sharing one Linux machine.

Buildr coordinates builds, tests and verification across repositories. Agents
submit through a CLI or MCP server; you monitor the same queue in a terminal UI.
It works in Ghostty, Herdr, tmux and ordinary terminals. Herdr is optional.

```sh
buildr run --lane quick --jobs 2 --memory-gb 2 -- cargo check --locked
buildr run --lane heavy --jobs 2 --memory-gb 4 -- cargo test --locked
buildr status --json
buildr
```

## Why use it?

Several agents can each start a compiler, test runner or bundler. Buildr gives
those commands shared CPU/RAM reservations, resource locks and visible outcomes.
It keeps the native exit code, logs and stable handle even if an agent disconnects.

- **One queue:** coordinate local work across repositories and agents.
- **Explicit concurrency:** CPU, RAM, job and heavy-job budgets, plus atomic locks
  for shared checkouts or output directories.
- **Agent interfaces:** literal command arguments, bounded MCP polling, durable
  submission keys and structured completion results.
- **Verification workflows:** dependency plans, source/input observations, artifact
  checks, and serial or parallel batches under one reservation.
- **Useful diagnostics:** recorded wait reasons, disk deficits, resource preflight
  and evidence-backed resource trials you review before applying.

Buildr is an early beta. Its reservations coordinate participating jobs; native
CPU/RAM reservations are estimates, not hard limits on every host process. Docker
workers use explicit container limits. Buildr never automatically retries uncertain
work or changes global resource budgets to make a job fit.

## Install on Linux

Python **3.8 or newer**, Git and a normal Linux `/proc` filesystem are required.
The application uses only the Python standard library. Optional Docker, Herdr
and local-model features are enabled only when explicitly configured.

Clone and install in a virtual environment:

```sh
git clone https://github.com/stateset/agent-builds.git
cd agent-builds
python3 -m venv .venv
.venv/bin/python -m pip install .
. .venv/bin/activate
buildr --help
```

Keep the virtual environment active, or add its `bin` directory to your PATH.
Agents with a filtered environment can use the absolute path printed by
`command -v buildr`. Do not install with `sudo pip`.

You can also run directly from a checkout with `./buildr`; no package installation
is required. The terminal dashboard needs at least 65 columns by 15 rows, and
adapts to narrow splits. Press `q` to close it; submitted jobs continue.

## Configure your machine

Inspect the defaults before submitting work:

```sh
buildr status --summary --json
buildr config --help
```

Defaults reserve six CPU slots and 16 GiB across up to three jobs, with a 10 GiB
free-disk floor and 4 GiB of available-memory headroom. These may be too large
for your machine. CPU slots are reservation units, not a detected physical-core
count. Choose budgets that leave space for your editor, agents and other work.
For example, on a smaller dedicated test machine:

```sh
buildr config --set cpu_slots=2 --set memory_gb=4 --set max_running=2 \
  --set max_heavy=1 --set quick_jobs=1 --set heavy_jobs=1 \
  --set quick_reserve_slots=1 --set quick_reserve_jobs=1 \
  --set quick_reserve_memory_gb=1 --set min_available_gb=1 --set min_free_gb=2
```

State is private to the local user in `~/.local/state/agent-builds`. Set
`AGENT_BUILDS_STATE` to choose another directory; all participating agents must
use the same directory. Configuration changes never resize or stop running jobs.
Read-only inspection does not initialize a missing queue.

## Everyday workflow

1. Wrap the expensive command once, with explicit budgets when known. Buildr
   waits for resources and streams output while preserving its exit code.
2. Use `--detach --events-json` to retain a job handle without keeping a terminal
   attached. Inspect it with `buildr show JOB --json` and `buildr logs JOB`.
3. Put related checks in a batch, or dependencies in a verification plan. Use
   explicit resource keys/locks when different checkouts share build output.
4. Use `buildr diagnose JOB` to understand a wait. Preflight projections reserve
   nothing and cannot guarantee a start or prove a smaller RAM budget is safe.

Commands run with your user's permissions. They are noninteractive and do not
use a shell unless you explicitly supply one. Queue the driver or individual
steps once; do not nest wrappers or wrap an entire coding-agent session.

## MCP

**Execution boundary:** the MCP server runs commands as your host user, outside
the calling agent's sandbox. Enable mutation tools only for trusted clients and
use their command-approval controls. Anyone able to modify queue state can affect
commands later run by plans or watches. Keep that directory private.

Point your agent's stdio MCP configuration at the installed executable:

```json
{
  "mcpServers": {
    "buildr": {
      "command": "/absolute/path/to/buildr",
      "args": ["mcp", "serve", "--allow-root", "/absolute/path/to/repos"]
    }
  }
}
```

Use `command -v buildr` to find that path. Submit literal arguments once with a
unique `request_key`, retain the returned handle and poll it. After a lost
response, inspect or replay the same key and exact arguments. Existing MCP
sessions need reconnecting to load an updated server.

Repeat `--allow-root` to permit several working-directory trees. This optional
scope is checked at submission and launch, including plan stages and watch
probes; symlinks are resolved. It does not restrict command arguments, file access
or network access. Omit it for the historical unrestricted behavior, or use
`--read-only` for inspection without command execution. Use an OS/container
sandbox when commands need isolation.

See [MCP setup and tools](docs/mcp.md) for bounded concurrent requests,
read-only mode, cancellation and recovery semantics.

## Herdr announcements

Announcements submit prompts to other agents and can influence their work.
Preview the text and idle recipients, then explicitly select each destination:

```sh
buildr announce
buildr announce --send --recipient w1:p2 --recipient w1:p3
buildr announcements --json
```

The TUI's `a` action also requires pane selection and confirmation. Working,
blocked and unknown agents are excluded. Session identity and idle status are
checked again immediately before sending; a changed recipient is skipped rather
than prompted later. Delivery means input was submitted, not that policy was
adopted. Uncertain submissions are never automatically retried.

## Documentation

| Topic | Guide |
| --- | --- |
| Batches and protected verification | [Verification](docs/verification.md) |
| Dependencies and acceptance evidence | [Plans](docs/plans.md) |
| Resource alternatives before submission | [Preflight](docs/preflight.md) |
| Disk and queue blockers | [Diagnostics](docs/diagnostics.md) |
| Why the whole queue is waiting | [Queue health](docs/queue-health.md) |
| Shared checkout/output locks | [Resource locks](docs/resource-locks.md) |
| Measured resource profiles | [Reviewed profiles](docs/reviewed-profiles.md) |
| Representative workflow comparisons | [Workflow studies](docs/workflow-studies.md) |
| Optional worker backends | [Workers](docs/workers.md) |
| Queue coordination and compatibility | [Scaling](docs/scaling.md) |
| Real runner processes and retained correctness evidence | [Process-scale checks](docs/process-scale.md) |
| Additional commands and feature history | [CLI reference](docs/cli-reference.md) |

Tests cover scheduling, recovery, mixed-version compatibility, MCP and native
process cleanup. Controlled measurements demonstrate specific behaviors; they do
not establish an overall autonomous coding-productivity improvement. See
[quality gates](docs/quality-gates.md) and the [changelog](CHANGELOG.md).

## Contribute

See [CONTRIBUTING.md](CONTRIBUTING.md) for queued local tests and isolated fixtures,
and [SECURITY.md](SECURITY.md) for the execution model and private reporting.
Report reproducible bugs and feature requests through
[GitHub issues](https://github.com/stateset/agent-builds/issues).

Licensed under [Apache-2.0](LICENSE). See [NOTICE](NOTICE).
