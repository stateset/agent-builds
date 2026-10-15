# Changelog

## 0.8.0

- Preserve bounded per-job wait observations, category totals and recent blocker
  transitions through admission, cancellation and recovery. Explicitly mark
  legacy history and long observation gaps as unobserved.
- Expose wait breakdowns and coverage in CLI inspection, statistics, plan
  evidence and TUI details without changing scheduling or resource budgets.
- Coalesce stable CLI wait messages to one heartbeat per minute while emitting
  category and blocking-job changes promptly.
- Add a compact jobs/plans monitor for narrow terminal splits, with hidden
  editing controls disabled until the pane is large enough for the full view.

## 0.7.0

- Add durable dependency-aware verification plans with per-stage queue budgets,
  skip-after-failure, independent branches, one reconnectable handle, cancellation,
  and explicit controller resume without retrying previously submitted work.
- Atomically link stage jobs before launch and permit each prepared worker to
  attach once. Preserve normal repository locks and uncertain reservations.
- Record bounded source identities at submission/start/finish; report drift by
  default and support opt-in unchanged-source verification.
- Capture per-stage artifact/log evidence and export private, non-overwriting
  JSON reports with native command results, timings and resource measurements.
- Add a plan dashboard view, submission, stage-log navigation, cancellation,
  resume and evidence export. Include an executable verification-plan example.
- Add measured/reserved CPU comparisons and wait percentiles by recorded policy.

## 0.6.0

- Reserve quick-check capacity and prioritize aged queued work without preempting
  running jobs. Skip externally blocked repos and preserve per-repo FIFO.
- Record local process-tree CPU time and sampled peak RSS; expose wait/runtime
  history and advisory memory suggestions without changing reservations.
- Add bounded disk inventory and preview/apply retention of old completed-job logs.
- Add explicit per-checkout task shortcuts through the normal queue, including a
  TUI task picker, history, disk inventory and measured job details.
- Add opt-in dedicated SSH worker profiles and explicit remote queue commands,
  with durable dispatch records, duplicate-token protection, and reconciliation
  after uncertain connection outcomes. No automatic offload or host configuration.
- Add validated CLI budget updates and TUI quick-reserve/aging controls.
- Incorporate fresh agent feedback: reserve quick job-count and memory capacity,
  show age eligibility, add read-only multi-handle `wait` with exit codes and log
  tails, group statistics by lane/reservation, and mark active caches in inventory.
- Retain explicit output-directory locks in task shortcuts; reject invalid budget
  types and zero worker counts without silently replacing them with defaults.
- Record scheduler policy on new submissions; report legacy runner policy as
  unknown rather than attributing new aging rules to already-loaded old runners.

## 0.5.0

- Fix a live-log-following crash caused by a shadowed `time` import; test an active
  log producer through completion.
- Keep running jobs visible ahead of queued records, and preserve TUI selection
  when job states change.
- Capture available Herdr pane/tab/workspace metadata, including detached jobs.
- Explain memory headroom and link CPU waits to contributing reservations, with
  separate timestamps for admission and live observations.
- Report queued-blocker wait duration separately from execution time.
- Describe Cargo/test defaults and explicit direct-command worker overrides while
  preserving the original command. Do not claim measured runtime concurrency.
- Add stable zero counters and explicit status count scope.
- Keep status counts, detailed rows and contributing jobs in a consistent
  read-only SQLite snapshot while workers change state.
- Add a scrollable job-details view with `i`; start the dashboard on job logs.

## 0.4.0

- Make inspection truly read-only, including an empty-state response that creates
  no files. Explain sandbox permission failures for write operations.
- Expose structured argv, readable commands, job handles, logs, and durations.
- Add compact summary, active/repository filters and limits for status consumers.
- Identify blocking jobs and show current resource measurements and thresholds
  in CLI wait updates and the TUI. Jump to blocker logs with `g`.
- Support per-job disk minimums and conservative scratch-space reservations.
- Add explicit start/finish events and optional JSON lifecycle events on stderr.
- Add detached submission, stable receipts, per-job inspection and log following.
- Reject nested queue wrappers with actionable guidance.
- Retain Herdr announcements, custom message files, delivery tracking, shared
  scheduling, local/Docker execution, and process monitoring.

Validation includes queue/CLI integration tests. Read-only status was also verified against the live
queue from a filesystem sandbox without escalation. Earlier live verification
covered Docker CPU/memory limits and container cleanup. Local reservations remain
estimates; Docker job limits are enforced by Docker.
