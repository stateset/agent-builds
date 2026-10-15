# Understanding queue waits

`agent-builds show JOB` and `show JOB --json` now preserve a breakdown of
the time a job spent queued. `stats` combines that breakdown across its bounded
completed-job history, including the selected `--repo` filter. Plan evidence
includes the same job diagnostics automatically. The TUI job details view (`i`)
shows the breakdown and observation coverage.

Categories distinguish `repository_lock`, `priority`, `cpu_capacity`,
`job_capacity`, `heavy_capacity`, `memory_capacity`, `quick_reserve`, `disk`,
`ram_headroom`, `oversized`, `paused`, and other `scheduling` delays. Only the
primary admission blocker is counted: a checkout lock can hide a simultaneous
CPU shortage. These numbers explain observed waiting; they do not establish how
much faster a different budget would have been.

The runner samples while queued and checkpoints its observations about every
two seconds, or when the blocker changes. Each category interval ends at the
next observation, admission, or settlement. Gaps over five seconds are entirely
`unobserved`, including slow scheduler calls, stopped controllers/runners, and
long recovery delays. The time before the first blocker observation is also
unobserved. Legacy jobs and already-loaded older runners remain unrecorded;
their wait is never attributed to an invented cause. Read-only inspection can
project a recent interval without writing any state. Wall-clock discontinuities
can reduce coverage; these are elapsed wall-time estimates, not a precise trace.

JSON includes category seconds, total and observed seconds, coverage, the latest
observation timestamp, a transition count, and at most 64 recent transitions.
Each transition records only timestamp, category and blocking job handle. Full
category totals continue accumulating after old transitions leave that bounded
list. New jobs do not allocate an unbounded event history.

CLI wait updates emit immediately on a category or blocker-handle change. Stable
waiting emits at most one heartbeat per minute, including with `--events-json`.
Changing measurements do not create repeated transition messages. The SQLite
observations and live inspection continue updating between those heartbeats.

Narrow TUI panes automatically show a compact monitor, with version, reservation
estimates, the selected job's handle/state/reason/output, or plan stage states.
Use `j`/`k` or arrows to select, `v` to toggle jobs/plans, and `q` to exit. It is
usable from 20 columns by 8 rows; more height reveals more detail. Editing,
submitting, cancellation and budget actions require the full 65x15 view, so
hidden controls cannot alter work from a narrow monitor. Resizing changes the
view automatically and does not stop queued or running commands.
