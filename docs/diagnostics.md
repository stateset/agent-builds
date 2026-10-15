# Operational diagnostics

Start with one small read-only response:

```sh
buildr status --json
buildr diagnose --json
buildr diagnose JOB --json
buildr diagnose --repo /absolute/checkout --limit 5
```

Default status lists up to 20 **active** jobs with concise fields. Counts retain
their existing whole-history scope unless `--active` is specified; `count_scope`
and `job_scope` state the distinction. Large source fingerprints, per-check
results, reservation graphs and log contents belong in `show JOB --json`.
Use `--details` when full records are needed. Truncation is explicit.

Completed history is opt-in, with a stable creation-order cursor:

```sh
buildr status --history --limit 20 --json
buildr status --history --before LAST_CURSOR --limit 20 --json
buildr status --history --details --repo /absolute/checkout --json
```

`next_cursor` identifies the last returned job when another page exists. Pages
use `(created, id)` descending, so equal timestamps and newly completed jobs do
not cause duplicates. Jobs that complete between pages with newer creation keys
appear when history is refreshed from the beginning. `--active --history` and
`--before` without `--history` are rejected. Default status intentionally changed
in v0.16; clients needing completed records must opt in.

## Disk and blocker explanations

`diagnose` samples free disk space on the job and queue-state filesystems and
uses the smaller amount, matching admission. The requirement is:

```
max(machine floor, job floor) + this job's scratch + all running scratch
```

The report includes every component, the exact deficit in GiB and up to 20
contributing reservation handles. Global reservations still count when listing
only one repository. Scratch is an estimate, not observed file growth, and
completion does not guarantee that files are deleted. Another job may consume
space after this sample. Missing/inaccessible paths produce **unknown**, never
an invented zero or a newly created checkout.

The reported reason comes from the runner's latest admission attempt. A current
disk calculation can therefore disagree with an older reason. Reasons carry
sampling times and a stale flag after five seconds. At most eight recorded
blocker links are followed; cycles, missing handles, depth limits and a blocker
that has since settled remain explicit. These links are observations, not an
ETA, dependency graph or fresh scheduling decision.

Each queued job includes a copyable `buildr disk --repo PATH --json` command.
Inventory sizes are measured read-only. Cache records distinguish queue-owned
logs from repository/explicit caches, identify queued/running Buildr users and
record owner UID and directory mtime. **mtime is not last use.** Absence of
Buildr users cannot establish that a direct compiler or another process has
stopped using a cache. Review ownership and preserve binaries/evidence before
any separately authorized cleanup. No repository cache is deleted by these tools.

## Policy activation

Status and diagnostics distinguish requested, pending and effective protection.
Pending explanations list blocking jobs, their loaded runner versions and exact
conditions, plus old controllers that can still dispatch future stages/probes.
Old controller versions may be unrecorded; they are never inferred from a child
job or the currently installed executable. New controllers record their version
when taking ownership.

Protection activates atomically during a current runner's writable submission
or claim after all legacy jobs/controllers finish and existing heavy work fits
outside the reserve. Inspection never activates it or stops old work. Owners can
explicitly resume stopped controllers using the installed runner; a live
controller must finish. Process liveness is not inferred through a sandbox's
restricted process view. New request and activation timestamps support observing
the drain interval. Requests made by older versions retain unknown timestamps.

Policy `protected-verification-v5` also handles a full protected heavy pool when
a global budget is full at the same time. Eligible quick work can use reserved
capacity, including declared scratch, while the heavy pool drains. If quick work
itself blocks an aged heavy head, normal draining/backfill still preserves heavy
progress. Disk/RAM headroom, global budgets and every declared lock still apply.

v0.16.1 requires protected-ordering protocol 2. Upgrading from v0.15/v0.16 puts
requested protection back into draining exactly once: already-loaded runners
use their shared soft ordering until old jobs/controllers finish. This avoids
different schedulers selecting each other as the preferred next job. A SQLite
activation barrier also covers attempts by already-loaded old code, without
raising an error or stopping that runner. New protocol-2 submissions participate
in the same queue; no old jobs are cancelled, recreated or marked compatible.
After activation, old job/controller inserts are rejected before creation.
Read-only inspection never performs this migration or alters effective policy.

## Dashboard and acceptance measurements

Press `u` for read-only health in full or narrow terminal panes. `j`/`k` scroll,
PgUp/PgDn scroll full panes, and `u` returns to jobs. The health view cannot
submit, cancel, clean caches or edit budgets. `buildr_diagnose` exposes the same
report through MCP, including a read-only server.

`buildr latency --json` now includes `short_quick_checks` for completed quick
jobs that ran in at most 30 seconds: count, p50/p95 wait, target fraction and
native failure count. Maximum completed wait remains visible per cohort, and
still-queued overdue work is separate. Cohorts retain actual admission settings,
policy and runner version. These descriptive samples are not matched before/after
comparisons or proof of overall coding productivity; failures and pending jobs
must not be hidden to improve the apparent result.
