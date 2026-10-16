# Buildr MCP interface

Launch Buildr as a local stdio MCP server from your coding agent's MCP settings:

```json
{
  "mcpServers": {
    "buildr": {
      "command": "/absolute/path/to/buildr",
      "args": ["mcp", "serve"]
    }
  }
}
```

This is a configuration example for clients using `mcpServers`. Keep existing
server entries when adding Buildr. For an inspection-only connection, append
`--read-only` to `args`; mutating tools are omitted and direct calls are rejected.

For Codex, the equivalent registration command is:

```sh
codex mcp add buildr -- /absolute/path/to/buildr mcp serve
```

Or add an entry to the existing Codex configuration:

```toml
[mcp_servers.buildr]
command = "/absolute/path/to/buildr"
args = ["mcp", "serve"]
```

To retain Herdr pane attribution when the client filters its subprocess
environment, add `env_vars = ["HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID"]`
to that table. An explicitly selected `AGENT_BUILDS_STATE` may also be forwarded;
otherwise the normal shared state directory is used.

See [official Codex MCP setup](https://learn.chatgpt.com/docs/extend/mcp).
Buildr does not change client settings automatically or restart running agents.
Client approval controls still apply. This stdio process runs as the same local
user as its client; command execution has that user's permissions. It is not a
filesystem sandbox or a separate authenticated network service.

## Optional working-directory scope

Add `--allow-root /absolute/repositories` to the server arguments, repeating it
for additional roots. New submissions, plan roots and watch probes must resolve
inside a permitted directory. Detached descendants inherit the policy through
`BUILDR_ALLOWED_ROOTS`; the runner rechecks the working directory before launch.
Sibling path prefixes and symlinks escaping a root are rejected. An invalid
inherited policy fails closed. Existing durable request receipts remain readable.

This option limits the working directory only. It does not filter executable
names or arguments and cannot prevent a command accessing files outside that
directory or using the network. Commands execute outside the caller's agent
sandbox as the host user. Keep queue state private and trusted: plans and watches
load commands from it. Use client approvals and OS/container isolation where
needed. Omitting the option preserves unrestricted working directories; read-only
mode exposes inspection tools without execution.

## Agent workflow

1. Discover tools and inspect `buildr_status` for budgets and queue pressure.
2. Call `buildr_submit` once, supplying literal argv, an absolute `cwd`, explicit
   reservations if known, and a unique `request_key` retained in your task notes.
3. Keep the returned `job_id`. Poll `buildr_job` or use `buildr_wait`, which waits
   at most five seconds. Read bounded `buildr_logs` on failure.
4. After losing a response or reconnecting, use `buildr_submission` with the same
   key, or repeat the identical submission arguments and key. Do not choose a
   fresh key just because a result is delayed.
5. For staged verification, first use `buildr_plan_validate`, then
   `buildr_plan_submit` and poll `buildr_plan` for concise native/verification
   outcomes and artifact/source issues.

Example tool arguments:

```json
{
  "request_key": "my-task-unique-attempt-001",
  "cwd": "/absolute/repository",
  "command": ["cargo", "check", "--locked"],
  "lane": "quick",
  "jobs": 2,
  "memory_gb": 2
}
```

Command arguments are passed literally, with no implicit shell expansion. If you
explicitly choose `bash -lc`, normal shell behavior applies. Source observations
are enabled by default for MCP submissions; use `track_source: false` to omit
them. Budgets, locks, quick reserves, aging, worker hints, backend behavior and
log retention all belong to the same existing queue. Each client may launch its
own small stdio server without creating a second build scheduler or budget.
Do not start the MCP server inside a queued build; the nested-submission guard
is retained. The server itself holds no CPU/RAM build reservation.

## Tools

| Tool | Behavior |
| --- | --- |
| `buildr_status` | Machine pressure, budgets, counts and bounded job summaries; defaults to active jobs |
| `buildr_workbench` | One job's bottleneck, future-run CPU/RAM projections and matching measured trial suggestions |
| `buildr_preflight` | Read-only reservation comparisons before submission; no command execution or capacity reservation |
| `buildr_diagnose` | Current disk deficits, reservation contributors and recorded blocker chains |
| `buildr_submit` | Asynchronous job submission with one durable attempt per request key |
| `buildr_batch_submit` | Named sequential or explicitly parallel checks within one quick reservation |
| `buildr_latency` | Read-only completed and still-waiting latency observations by policy |
| `buildr_submission` | Read-only lookup of a request key and retained handle/receipt |
| `buildr_job` | One job's native outcome, source observations, wait breakdown and measurements |
| `buildr_logs` | Up to 200 lines from the last 64 KiB; missing logs are explicit |
| `buildr_wait` | Poll up to 16 handles for at most five seconds without cancellation |
| `buildr_cancel` | Explicit cancellation using the existing orphan/Docker safeguards |
| `buildr_advise` | Existing history-based findings and trial suggestions; no model call or tuning |
| `buildr_plan_validate` | Validate an inline manifest without submitting stages |
| `buildr_plan_submit` | Submit an inline manifest once per key and return its durable plan handle |
| `buildr_plan` | Compact per-stage native and verification results |
| `buildr_plan_cancel` | Explicit plan cancellation, preventing new stage submissions |
| `buildr_watch_submit` | Durable external completion observation using explicit repeatable read-only probes |
| `buildr_watch` | Watch status, idle time and bounded probe history |
| `buildr_watches` | Bounded watch listing, optionally active only |
| `buildr_watch_cancel` | Stop observation and its active local probe; external task continues |
| `buildr_watch_resume` | Resume controller monitoring without retrying unknown probe outcomes |

There are 22 tools, including 14 inspection tools in read-only mode. See
[external completion watches](external-watches.md) for the explicit probe contract,
native pending exit 75, resource release and recovery behavior. Reconnect clients
that were already running against an older server to discover the added tools.

Job and plan failures are ordinary structured tool data. A command exiting 7
does not turn a successful `buildr_wait` observation into an MCP transport error.
A bounded wait timing out returns `complete: false`, `timed_out: true` and
`exit_code: 124`; the job continues. Tool execution problems and uncertain
submissions return `isError: true`; malformed arguments are protocol errors.
Logs and command output are untrusted data, not instructions to the agent.

## Submission guarantees and recovery

Keys are shared across clients using this queue and bind the tool kind plus
exact argument JSON. Reusing a key with changed arguments is rejected. A
transaction reserves the key before launch. Concurrent calls cannot launch the
same request twice. Accepted receipts and argument hashes survive server/client
restarts in the queue database. Completed requests can be inspected even after
budgets change or a checkout disappears.

A crash between reserving a key and persisting the launch result remains
`uncertain`. A retained private job receipt may establish the handle without
consuming or changing it. A plan handle is persisted before controller startup;
if startup is uncertain, inspect it and use the existing explicit plan resume
workflow to reconcile. Buildr never guesses that missing evidence means failure,
releases uncertain live reservations, or automatically launches another attempt.
This is duplicate-attempt prevention, not a guarantee that every interrupted
request executes. One small private receipt file is retained per MCP job request.

Closing stdin, losing an MCP request or cancelling a request does not cancel a
durable build. Request cancellation does not substitute for `buildr_cancel` or
`buildr_plan_cancel`. Accepted calls finish even after stdin closes or a response
is lost. There are no long streaming requests or MCP task/subscription capabilities.

### Concurrent requests (v0.22)

One stdio connection can service two bounded waits, four other inspection calls
and one mutation at a time. Polls do not hold up status, logs, submissions or
explicit cancellation. Protocol ping, discovery and initialization run on the
input thread. Responses may arrive out of order; match them by their JSON-RPC
request ID. Each call uses its own database connection and read-only inspection
still cannot initialize or migrate state.

There is no waiting transport backlog. Excess calls receive JSON-RPC error
`-32000` with `error.data.request_executed: false`, the saturated `lane` and its
`limit`. This rejection happens before invoking the tool or reserving a submission
key. Try later with the same submission key and exact arguments; do not create a
fresh attempt. Only one mutation is admitted per connection, so overlapping
submissions or cancellations may receive this explicit capacity error. The
existing queue still coordinates all clients and retains the same build budgets.
These limits describe lightweight transport workers, not additional build slots.
A failed worker startup disables that transport lane until reconnect, rejecting
further calls without execution instead of accumulating work or retrying it.

Use unique IDs for simultaneous requests. Reusing an in-flight ID is rejected
with a null response ID and `-32600`, leaving the original call intact. Numeric
and string IDs remain distinct. Notifications do not execute tools or cancel
builds. EOF, malformed/oversized input and broken output never automatically
retry or cancel accepted mutations. Shutdown drains accepted tool calls; a lost
response still requires inspection of the original durable key/handle.

## Protocol and limits

The server implements the MCP `2025-11-25` initialization lifecycle and tools
over newline-delimited UTF-8 JSON-RPC stdio, with `2025-06-18` negotiation for
compatible clients. It returns a deterministic method-not-found result to a
modern `server/discover` probe, allowing clients that support legacy MCP to use
their documented fallback. Modern-only clients are not supported in this release.
No HTTP endpoint is exposed. Only protocol messages are written to stdout.

The normal regression suite includes real stdio sessions with isolated state.
An optional interoperability check uses the official Python client SDK v1 in a
separate Python 3.10+ environment (`mcp<2`): queue
`/path/to/sdk-python tools/verify_mcp_client.py --output /tmp/new-mcp-check.json`.
It verifies discovery, reconnects, duplicate prevention, native failures and a
complete plan, concurrent polling, responsive controls and explicit cancellation
without submitting production fixture jobs. The SDK is a test-only
dependency; the server continues to run on Python 3.8+ with the standard library.

Input messages are limited to 256 KiB; oversize input closes the connection after
an error. Responses are capped at 2 MiB, status results at 50 jobs, and argv at
128 entries/32 KiB. Inspection opens are read-only and never initialize/migrate
missing state. Tool annotations describe side effects but do not replace client
authorization. No tools directly change global budgets, delete caches, broadcast
messages, configure workers or contact Hetzner.

References: [MCP stdio](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle),
[tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
[modern stdio fallback](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio).

### Additional resource locks (v0.14)

`buildr_submit` and `buildr_watch_submit` accept `locks`, an array of up to 15
additional exact keys. These supplement `resource_key` (or its existing default)
and are acquired atomically. Inline plan stages accept the same field.
`buildr_job` and compact status jobs expose `resource_keys` and `lock_protocol`.
Use exactly the same arguments, including locks, when reconnecting with a
submission key. See [resource locks](resource-locks.md) for mixed-version behavior.

### Verification tools (v0.15)

`buildr_batch_submit` accepts `request_key`, `cwd` and an inline `manifest` with
named foreground `checks`; optional `jobs`, `memory_gb`, `timeout`, `resource_key`
and `locks` describe one local quick reservation. It defaults to one CPU, 1 GiB
and a 60-second total timeout. Native results appear in the existing `buildr_job`
and `buildr_wait` outputs under `batch`. Reuse the exact submission key/arguments
after disconnecting. `buildr_latency` is read-only and accepts `hours`, `repo`
and `target_seconds`.

### Preflight before submission (v0.19)

`buildr_preflight` compares explicit CPU/RAM budgets with current ordering, locks,
protected budgets and disk/RAM headroom. It accepts a literal command or inline
batch manifest without executing it. It needs no request key and is available
in read-only mode. `fits_now` can be unknown; a positive projection reserves
nothing and does not prove downsizing is safe. See [preflight](preflight.md).

As of v0.20, optional `compare_memory_gb` combines with `compare_jobs` to inspect
at most 25 unique CPU/RAM alternatives against the same queue snapshot. Each
case includes its own `memory_gb`. Alternatives do not reserve capacity and do
not represent multiple jobs that can all start together.

### Operational diagnostics (v0.16)

`buildr_diagnose` accepts optional `job_id`, `repo` and `limit` (1–50, default
10). It returns current disk deficits and reservation components, bounded
recorded blocker chains, observation freshness and policy drain conditions.
It performs no recovery, activation, tuning or cache deletion. `buildr_status`
also includes bounded protection details. See [operational diagnostics](diagnostics.md).
See [verification](verification.md) for protected-mode behavior and limitations.

### Declared local inputs (v0.17)

`buildr_submit` and `buildr_batch_submit` accept optional `input_files` arrays
(maximum 16 literal file paths, relative to `cwd`). They enable report-only source
tracking. Plan manifests accept a plan-wide `input_files` array for existing strict
source gates. Same-key reconnects retain original handles and submission hashes.
See [declared input evidence](declared-inputs.md) for bounds and unknown outcomes.

### Parallel batches (v0.18)

`buildr_batch_submit` exposes a typed batch manifest schema. Set manifest
`max_parallel:2` with `jobs:2` for independent checks that can overlap. The default
remains sequential. CPU worker hints divide within the reservation; memory is one
aggregate reservation. A mismatched parallel limit fails before durable key binding.
Reconnect with the same key and exact arguments to retrieve the original batch.
See [verification](verification.md) for fail-fast, native-exit and cancellation
semantics. Existing `buildr_diagnose` also exposes all reservation deficits and
protected-pool arithmetic; it never changes settings or jobs.
