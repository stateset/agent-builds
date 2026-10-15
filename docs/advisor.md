# Queue advisor and scheduler replay

```sh
agent-builds advise
agent-builds advise --repo /path/to/checkout --hours 24 --limit 200 --json
agent-builds simulate --fixture
agent-builds simulate --hours 24 --limit 100 --json
```

Both commands inspect state read-only, including when no queue exists. Neither
executes saved commands, modifies tasks/budgets, cancels jobs, sends messages or
contacts workers. Inspection does not initialize or migrate the database.

The advisor (`h` in the full TUI) identifies fresh long-wait blockers, reservations
that exceed a reduced budget, stale observations, and low historical blocker
coverage. A stale observation does not establish that a runner has died. Total
queue age is never presented as time attributed to the current blocker. Arrow
keys or PgUp/PgDn scroll the advisor. Editing controls are disabled in this view;
`h` returns to logs. Narrow panes retain the jobs/plans monitor.

Command trials require at least three successful executions with matching cwd,
resource key, literal argv, backend, lane and reservations in the selected
history. Quick commands with a median runtime of at least 60 seconds get a
heavy-lane/split-stage suggestion. Resource trials additionally require local,
completed process-tree measurements, at least three samples and a runtime of at
least one second for each of at least three successes.

Memory trials use the maximum sampled RSS of the matching measurements, add 30%
margin and round up to 0.25 GiB; they appear only when at least 30% below the
current reservation. CPU trials appear below 25% observed-to-reserved average
CPU and suggest trying one fewer slot. These are experiments to validate, not
known safe limits: sampling can miss peaks, source/input sizes may differ, and
lower concurrency can increase runtime. Local reservations remain estimates.
Failed, lost, cancelled, incomplete and unmeasured executions do not establish
resource trials. No setting or profile is changed automatically.

The default window is 24 hours and 200 completed jobs (maximum 2000); active
jobs are sampled separately with the same bound. The report discloses matching
counts and truncation, and limits findings to 50 with the full count retained.
`--repo` matches the checkout or primary output key, consistently with status.
Literal commands appear in findings just as they do in job inspection.

## Conservative quick backfill

Policy `quick-aging-backfill-v2` retains normal aging, quick reserves, per-key
serialization and global budgets. When the oldest eligible aged job cannot fit,
a compatible quick job can use spare capacity only if it passes every possible
subset of running jobs completing: whenever the aged head could fit without the
backfill job, it must also fit with that job still present.

This preserves the head's reservation eligibility regardless of completion
order. It does not predict real runtimes or remove CPU/I/O contention. Only
quick jobs with zero scratch reservation qualify, RAM headroom must accommodate
both jobs, and disk checks still apply. With more than eight running jobs the
policy conservatively retains draining rather than exploring exponential
combinations. An aged job that fits still starts first. Unsafe backfill is
rejected, including when releasing a smaller running reservation would otherwise
let the head start. No running work is preempted or automatically retried.

Already-loaded runners keep their previous code. Policy identifiers on jobs and
statistics distinguish new submissions; inspection never attributes the new
policy to an older runner. No global budgets change in this release.

## Replay limits

`simulate --fixture` compares v1 draining with v2 on a documented spare-capacity
case: two one-slot heavy jobs fill the heavy-job count; an aged one-slot heavy
job waits; a two-slot, one-second quick check arrives. V1 starts the quick job
after 50 seconds. V2 starts it immediately, with identical aged-head start time
and modeled makespan. This is a controlled model result, not a measured speedup
for the whole machine.

History replay uses the latest 100 started successful/failed executions finished
in the selected window (maximum 500). Recorded arrival times and runtimes feed a
deterministic event model. Each policy sees identical inputs, budgets and output
keys. Cancelled/lost jobs, current running work and invalid time records are
excluded; truncation and unadmitted jobs are reported. The model intentionally
excludes external load, administrative pauses, physical RAM/disk pressure, I/O,
hidden locks and changes in runtime from different concurrency. Scratch/disk
requirements are not reconstructed from mutable filesystem history. Replay
cannot establish an ETA or prove that a configuration is optimal.

In a pre-release replay of 100 recent real jobs, both policies had identical
aggregate waits and makespan. The new policy fixes a demonstrated spare-capacity
case; it is not evidence that historical 14-minute quick p95 waits are solved.
