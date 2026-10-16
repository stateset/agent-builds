# Changelog

## 0.25.1 (unreleased)

- Keep accepted jobs running when lifecycle/native-output readers close, without
  repeated broken writes or interpreter exit-code replacement. Preserve durable
  completion if later notification/reporting fails. Label TUI job exit codes
  accurately, including queue-generated timeout/cancellation results.

- Add opt-in `F` following of the first matching displayed job across refreshes.
  Pin selection on navigation or inspection, preserve following through table
  toggles/resize, and suspend it during help without changing queue state.

- Wrap selected job details in full and narrow panes, with page/Home/End
  navigation, row positions, native exit and checkout context. Cache formatted
  evidence and wrapping; label clipped oversized details explicitly.

- Focus the TUI browser on the selected checkout with `R`, and clear browsing
  filters with `C`. Compose repository focus with search/state filters, retain
  sort and selection, and keep global reservation totals visible.

- Cache the selected inline log tail and browser filtering/sorting between
  snapshot changes. Skip underlying log rendering during help. Bound inline
  reads even during file growth, reject FIFO/directory paths without blocking,
  refresh same-size rewrites and strip ANSI decoration from visible output.

- Defer periodic telemetry on a busy SQLite BEGIN, without retrying transaction
  bodies or interrupting native output handling. Commit final telemetry and
  the native outcome in one update, with contention and atomicity regressions.

- Add repeated native runner-loss acceptance across current and v0.24.2 runners.
  Verify live orphan locks/reservations, unrelated progress, explicit cancellation,
  unknown native outcomes and no duplicate execution. Retain independently
  checked CI reports, including failure evidence.

## 0.25.0 — 2026-10-16

- Avoid growing-set copies while scanning queued resource heads, and replace
  pairwise legacy-lock eligibility comparisons with a per-scan legacy-key set.
  Preserve FIFO, mixed-version lock draining, protected reserves and backfill
  policy; compare decisions with the released v0.24.2 scheduler.

- Decouple live execution control polling from output volume: refresh control
  rows approximately every 200 ms while continuing to drain ready output. Wake
  quiet jobs by the control deadline and re-read PID/start identity explicitly
  before termination. Retain native deadline guards and existing exit codes.

- Refresh the TUI job browser using bounded lightweight rows, without decoding
  every job's source evidence, batch results, telemetry or wait history. Read
  full details only for the selected job while visible; cache selected reads
  for one second and show read errors without stale details from another job.

- Keep running-job cancellation polls, cancellation transactions, and live
  reservation snapshots independent of source evidence, telemetry and batch
  payload size. Execution polls still read current PID/start identity and
  cancellation state; no scheduling, timeout or recovery policy changes.

- Expand the job browser into a table with Tab, using the pane height for more
  jobs and showing reservation and wait/run columns where space permits. Add
  local wait/runtime/repository sorting while retaining selected job identity;
  display ordering never changes scheduler admission priority.

- Find literal text in focused logs with case-insensitive matching, next/previous
  matching-line jumps, highlighting, and visible wrapped-row position. Search
  holds the bounded snapshot; clearing it does not pause or resume the queue.
- Keep typed text visible when prompt labels are wider than a narrow pane.

- Add focused, wrapped log browsing with explicit live/held output, scrolling,
  job pinning, bounded reads, ANSI cleanup and replacement/truncation handling.
  Holding output does not pause jobs or the shared queue.
- Sample the user process list only while its view is open.

## 0.24.2

- Serialize first-time SQLite WAL activation with schema bootstrap. Current
  writable opens only read journal mode and schema version under the short file
  lock; no journal-mode change or migration transaction is repeated.
- Add a deterministic lock-ownership regression guard for WAL activation after
  concurrent first opens exposed the race in the v0.24.1 tag gate. That beta
  remains unpublished; this release includes its TUI and admission improvements.

## 0.24.1

- Refresh job snapshots and host metrics once a second while retaining the
  200 ms keyboard loop; reuse a consistent read snapshot between refreshes.
- Add local TUI search by job, repository, command, wait reason or Herdr pane,
  with all/active/waiting/problems filters and explicit loaded-row counts.
- Add scrollable keyboard help, first/last/page selection, Enter for full job
  details, and earlier selected wait reasons in narrow panes. Keep shared
  reservation totals independent of display filters.
- Wait for recorded telemetry in the profile-review fixture instead of assuming
  a fixed sleep always provides enough RAM samples on a busy host.

- Keep source evidence, telemetry and batch results out of admission snapshots.
  Load command display fields only for running reservations and the selected
  blocker, rather than every queued job while holding the SQLite writer.
- Recover jobs using process identity fields without loading execution evidence.
  Preserve live orphan and uncertain Docker reservations, admission ordering,
  mixed-version locks, schema and global resource budgets.
- Add evidence-read regression guards and a counterbalanced private-queue
  comparison with 300 jobs carrying bulky synthetic metadata.

## 0.24.0

- Show a queue-wide health summary in the dashboard, CLI and MCP status. Identify
  common disk blockages with current smallest/largest shortfalls, including
  running scratch reservations and multi-lock waiting jobs.
- Separate fresh runner reasons from stale, missing and invalid observations.
  Bound observation parsing while keeping queue counts and disk ranges complete.
  Keep filtered queue requirements separate from global running reservations.
- Use the shared opportunistic recovery lease in the dashboard, avoiding a
  synchronous recovery write when another writer is busy. No resource budgets,
  queue schema, cancellation, retry or cleanup policies change.

## 0.23.0

- Defer polling admission and periodic recovery when SQLite's writer is busy,
  keeping cancellation responsive under a large waiting population. Only a busy
  BEGIN is deferred; transaction-body and commit errors still propagate.

- Read only cancellation/control fields during unchanged waits instead of expanding
  job diagnostics twice every polling cycle. Keep the short cancellation poll,
  generation wakeups and transactional admission checks.
- Decode lock lists once per admission snapshot, retaining exact FIFO and
  additional-lock semantics without changing persisted schema or policy.
- Add an explicit queued process-scale benchmark with real runners, synthetic
  backlog/history, exact native-attempt evidence, atomic reservation observations,
  lock checks, cancellation and queued-owner recovery. Retain 30/100-process CI
  reports and verify them independently of the runner's exit status.

## 0.22.2

- Cache configuration files, use read-only queue-generation admission wakeups,
  share periodic recovery and run numbered migrations only when needed. Preserve
  Python 3.8 and historical queue protocol guards.
- Gate native command execution until the child's PID and start time are saved;
  an attachment-time runner crash cannot leave an unrecorded command executing.
- Stop a batch child created during a concurrent group shutdown, including when
  slow outer telemetry delays the main cancellation loop.
- Record pending batch checks before launch so an early bootstrap timeout keeps
  the planned check list without inventing per-check native results.
- Require explicitly selected idle Herdr recipients and recheck before sending.
  Add optional MCP working-directory roots and prominent host-execution guidance.
- Validate remote submissions before consuming request tokens and return database
  errors as protocol JSON. Keep uncertain request outcomes non-retryable.
- Add an independent plain unittest CI step and derive package version metadata
  from the application version. Add focused correctness lint and incremental
  formatting/type checks; consolidate internal feedback notes into a public guide.
- Launch installed-wheel batch workers from the package root instead of requiring
  a repository-root script, retaining protection against imports shadowed by cwd.

## 0.22.1

- Prepare the public beta with Apache-2.0 licensing, portable Linux installation
  instructions, package metadata, contributor/security guidance and hosted CI.
- Derive default Herdr announcement commands from the installed launcher instead
  of embedding the development workstation path. No messages are sent on update.

## 0.22.0

- Service concurrent MCP requests on one stdio connection, separating bounded
  waits, inspection and mutations so polling does not block status or cancellation.
- Bound admitted calls to two waits, four inspections and one mutation, without
  a waiting request backlog. Explicit capacity errors mean no execution or key
  reservation; retain the same submission key and arguments when trying later.
- Serialize complete JSON replies, reject duplicate in-flight IDs, retain protocol
  lifecycle ordering and drain accepted calls on disconnect without retries or
  implicit job cancellation. Keep build budgets and scheduling policy unchanged.
- Enforce native process-group deadlines independently of slow telemetry or
  output handling. Join the deadline guard before reaping the owned leader to
  prevent late signals from targeting a reused process-group ID.

## 0.21.0

- Connect measured advisor findings to explicit profile proposals, strict queued
  trial plans and evidence-gated promotion. Require complete measured headroom
  for RAM reductions and guard reviewed profile reuse against source/input drift.
  Hide reviewed profiles from old task runners that cannot enforce that guard.
- Review explicit plans for repeated long quick-lane work and propose compatible
  heavy-lane stages without parsing scripts or changing running jobs or budgets.
- Pack explicit per-check CPU budgets within one batch reservation; retain legacy
  equal-share behavior, fail-fast semantics, native results and outer cleanup.
- Add a background-loaded TUI workbench, compact inspection, explicit proposal/
  trial actions, and read-only CLI/MCP views of bottlenecks and measured candidates.
- Compare explicitly supplied representative plans through the shared queue with
  counterbalanced order, strict source checks, durable attempted-submission handles,
  native failures, verified throughput, wait tails, telemetry coverage, host pressure
  and heavy progress. Withhold performance ratios for incomplete or failed evidence.

## 0.20.0

- Compare explicit RAM alternatives as well as CPU in CLI and MCP preflight.
  Evaluate each combination independently against one bounded queue snapshot,
  retaining ordering, protected-pool limits, batch CPU minimums and uncertainty.
- Bound comparisons to 25 unique CPU/RAM combinations and eight alternatives
  per dimension. Report each case's reservation and fresh RAM headroom/deficit.
- Keep comparisons read-only: no automatic downsizing, retries, reservations,
  scheduler-policy change or claim that all fitting alternatives can run together.

## 0.19.0

- Add read-only CLI and MCP preflight projections before submission, with explicit
  CPU comparisons against current queue ordering, resource locks, global and
  protected budgets, disk floors, scratch reservations and RAM headroom.
- Validate batch CPU minimums and show per-check worker hints. Warn about direct
  Cargo build/test worker flags exceeding a reservation without rewriting commands.
- Preserve uncertainty for pending policy transitions, incompatible old schemas,
  unavailable host measurements and truncated active snapshots. Inspect scheduling
  fields without loading large historical command/source payloads or migrating state.
- Keep scheduler policy, live limits, remote execution and cache ownership unchanged.
  Preflight creates no jobs or request keys, reserves nothing and offers no ETA or
  evidence that a smaller reservation is safe.

## 0.18.0

- Add opt-in parallel batches for independent checks, with CPU worker hints
  divided within one reservation, named log chunks and per-check native results.
  Keep sequential execution as the default. Fail-fast stops future launches and
  allows existing siblings to finish; aggregate failures follow manifest order.
- Keep resource-advisor CPU trials above each batch’s requested parallel limit,
  including single-check and incomplete legacy metadata cases.
- Preserve cancellation, timeout, crash/unknown and one-attempt MCP semantics.
  Validate parallelism before job creation or durable request-key binding.
- Add explicit four-strategy comparisons for the fixed Python verification
  fixture: independent launches, separate queued jobs, serial batch and parallel
  batch. Rotate order, retain outcomes/costs, and make no coding-productivity claim.
- Explain every reservation deficit, protected heavy-pool arithmetic and requests
  that cannot fit alone through existing CLI/MCP/TUI diagnostics. No scheduling
  policy, live budget, cache ownership or remote-execution change.

## 0.17.0

- Fingerprint explicitly declared helper/input files at submission, start and
  finish through jobs, batches, saved tasks and MCP. Record hashes, sizes, modes
  and incomplete evidence within bounded reads, without storing file contents.
- Add plan-wide `input_files`: strict verification rejects changed or unverifiable
  helpers before launch, and preserves native command results for post-run drift.
- Reject ownership takeover by actual older controllers/workers for input-aware
  plans, without changing scheduler protocol 2 or protected quick ordering.
- Preserve durable MCP request identities and original submission hashes across
  reconnects and helper changes. Document scope, limits and live-checkout gaps.

## 0.16.1

- Fix a priority cycle between already-loaded v0.15 and v0.16 schedulers found
  during live installation checks. Drain both versions under their shared soft
  ordering before enabling protected ordering for protocol-2 runners.
- Serialize the upgrade flag and insertion/activation guards. Already-loaded
  old activation attempts remain ineffective without throwing or terminating
  healthy jobs. Reject future old job/controller creation before side effects
  once the new policy is active.
- Exercise actual v0.15/v0.16 code together across migration, controller idle
  intervals, reopening, insertion guards and subsequent current-runner admission.

## 0.16.0

- Fix protected heavy-pool draining when a global budget is also full: eligible
  quick jobs with declared scratch can use reserved capacity. Preserve aged
  heavy progress when quick work itself is the blocker; record policy v5.
- Act on a fresh nine-agent feedback round: concise active-job status by default,
  explicit detailed history with stable cursors, and one read-only diagnostics
  command through CLI, MCP and full/compact TUI (`u`).
- Explain fresh disk deficits, floor/scratch components, contributing reservation
  handles, recorded blocker chains, stale observations and cache review commands.
  Cache inventory labels ownership, mtime and unknown last use; it never deletes
  repository caches or promises scratch will be released.
- Explain protected-quick drain conditions, blocking runner/controller handles
  and known versions. Record new request/activation timestamps and loaded
  controller versions; older observations remain explicitly unrecorded.
- Report short quick-check latency separately by recorded policy/admission
  cohort, alongside maximum completed waits and still-waiting work.
- Make announcement previews read-only. Verify prerequisite failure propagation
  without resource admission; document existing plan/lock migration options.

## 0.15.0

- Add protected quick reserves with transactional activation after incompatible
  workers drain. Prevent old inserts from bypassing the active policy; retain
  existing hardware budgets, locks, cancellation and uncertain-result safeguards.
- Add one-reservation batches with per-check native results, fail-fast/continue
  behavior, durable handles and process-group cancellation/timeout.
- Add read-only latency targets, censored waiting observations, actual admission
  snapshots and evidence-backed reservation trials without automatic tuning.
- Add MCP batch submission and latency inspection, dashboard check outcomes and
  protection state, plus controlled counterbalanced latency/overhead comparisons.

## 0.14.0

- Add atomic acquisition of a primary resource key and up to 15 additional
  locks, preserving FIFO on every key and safe quick backfill. Record the
  `quick-aging-locks-v3` scheduler policy separately from older workers.
- Preserve mixed-version safety with held admission, legacy draining and an
  insertion guard for old workers that cannot see active additional keys.
- Carry locks through foreground/detached CLI runs, saved tasks, plans, drafts,
  external probes and MCP durable requests; expose conflicting keys in diagnostics.
- Pin background worker imports to their own package directory while retaining
  the requested working directory for the actual command.

## 0.13.0

- Add durable external completion watches with explicit repeatable read-only
  probes: exit 75 confirms pending, 0 reports completion, other/unknown outcomes
  stop progression. Idle watches retain no CPU/RAM/job/output reservations.
- Prepare each probe job transactionally before worker launch, attach workers
  once, and preserve existing cancellation, orphan and unknown-result safeguards.
- Reuse durable submission keys for watch creation; support read-only inspection,
  bounded waits/history, explicit cancellation/resume and private JSON exports.
- Add five MCP watch tools and separate external-watch counts. Add full and
  compact dashboard views with probe logs, idle accounting and guarded actions.
- Extract shared durable submission handling from the MCP transport and expand
  official-client verification to prove full-budget admission between probes.

## 0.12.0

- Add a dependency-free MCP stdio server over the existing queue, with typed
  tools for jobs, bounded logs/waits, advisor findings and verification plans.
- Persist caller submission keys and exact argument hashes before launching
  work. Reconnects reuse receipts/handles; conflicting keys and uncertain
  submissions never cause automatic reexecution.
- Preserve native command failures as job data, read-only inspection without
  initialization/migration, existing budgets, cancellation and orphan safeguards.
- Add read-only server mode and client setup examples. No client configuration
  is overwritten and no network listener or remote worker is started.
- Record controlled correctness, memory and overhead measurements; label the
  optional local model provider experimental after its first timed-out trial.

## 0.11.0

- Name the application Buildr and add the `buildr` launcher/console entrypoint;
  preserve `agent-builds`, existing queue paths and the repository identity.
- Add counterbalanced paired queue/direct experiments with fixed shared-output,
  bounded resource-contention and Python verification workloads. Preserve actual
  verified outcomes, latency, sampled memory, CPU and source identity; explicitly
  distinguish fixture evidence from autonomous coding productivity.
- Add optional local Ollama ranking of existing evidence-backed trial candidates.
  Allowlist anonymized numeric inputs, validate structured candidate references,
  require queued inference and retain private audit reports. No cloud models,
  automatic downloads, historical command execution or settings changes.
- Preserve cancellation exit 130 when a request arrives between queue inspection
  and admission, before a wait reason has been recorded.

## 0.10.0

- Group already-submitted jobs under one durable observation handle without
  dispatching, retrying, cancelling or inventing dependency guarantees.
- Add compact plan summaries with native/verification exit distinctions,
  bounded failure tails and preserved read-only wait/timeout behavior.
- Draft reviewable plans from existing argv and reservations; optionally record
  explicit harness hashes as present-time observations, never historic evidence.
- Add `when: always` cleanup stages after settled prerequisites while preserving
  original failures, global cancellation and uncertain-result reservations.
- Split advisor history by policy, loaded runner version, instrumentation and
  lane; add consistent policy filtering and optional tiny-check CPU trials.
- Add the existing-job group dashboard view and document migration workflows.

## 0.9.0

- Add a read-only queue advisor with evidence-linked lane, memory and CPU trials,
  current blocker findings, bounded history and explicit observation coverage.
- Add a scrollable advisor view in the TUI; disable editing controls in that
  view and wrap findings to the available terminal width.
- Allow conservative quick backfill past an aged head only when every subset
  of running completions preserves the head's reservation eligibility. Retain
  global budgets, checkout locks, aging and uncertain reservations.
- Record `quick-aging-backfill-v2` on new jobs while preserving older policy
  attribution. Already-running older workers continue using their loaded code.
- Add read-only deterministic history/fixture replay against the prior policy,
  explicit modeling assumptions, randomized reservation checks and finite
  workload completion gates. No commands or profiles are executed by replay.
- Document agent feedback disposition and measurable gates toward an A+ queue.

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
