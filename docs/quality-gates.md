# Working toward an A+ queue

An A+ assessment requires production evidence as well as passing regression
tests. These gates make the next improvements reviewable:

| Area | Current release gate | Remaining production evidence |
| --- | --- | --- |
| Scheduling correctness | Global CPU/RAM/job/heavy limits, per-key FIFO, quick reserves and aging tests pass; safe backfill checks every completion subset; seeded finite workloads finish | Mixed real workloads preserve these bounds across new runners |
| Interactive latency | Controlled spare-capacity case starts a short quick job without adding a reservation blocker for the aged head | Compare measured wait percentiles for similar commands and reservations under recorded policies; overall historical p95 is not a matched comparison |
| Recovery | Existing process-identity, uncertain Docker, cancellation, one-time worker attachment and controller-resume tests pass | Continue observing real reconnections without lost evidence or duplicate execution |
| Diagnostics | Read-only advisor, history replay, legacy handling, bounded samples and explicit uncertainty are tested | More of total queued time has attributed blockers; act on dominant recorded causes |
| Resource use | Trials require repeated matching successes and completed local measurements | Validate proposed reservations on representative inputs before saving profiles |
| Agent workflow | Stable job/plan handles, logs, wait exit codes, artifact/source evidence, CLI and full/compact TUI | Broader adoption of verification plans, concise completion summaries and saved plan shortcuts |

New scheduler code does not replace already-loaded runners. A production latency
comparison must use recorded policy groups, comparable workloads, enough samples
and the same budgets. A faster synthetic fixture alone does not earn an A+ grade.
Multiple explicit locks are acquired atomically with FIFO on every key. Mixed-version
tests load the actual v0.13.0 scheduler and verify legacy draining and insertion
guards before execution. See [resource locks](resource-locks.md). Continued real
mixed-workload observations remain necessary; these tests do not prove an
overall coding-productivity improvement.

v0.16 acts on a fresh nine-agent survey with bounded status, read-only operational
diagnostics, explicit disk arithmetic, policy drain explanations and short-check
cohorts. Acceptance covers missing state without writes, global reservation
accounting despite repository filters, stale/cyclic/missing blocker links, native
failure preservation, stable history pagination and failed-prerequisite skipping
without admission. Real disk stalls remain operational failures to resolve;
passing these tests does not establish an A+ operational latency result. See
[operational diagnostics](diagnostics.md) and [agent feedback](agent-feedback-v0.16.md).

Live installation of v0.16 exposed a mixed-version priority cycle despite its
passing single-version suite. v0.16.1 adds actual loaded v0.15/v0.16 upgrade
fixtures, coordinated draining and a database activation barrier. Release tests
must include mixed ordering policies; an installed smoke check remains separate
from pre-release protocol/regression gates.

External completion watches now use ordinary queued probes and retain no build
reservation during confirmed idle intervals. Gates cover cancellation races,
unknown launch/results, controller restarts and full-budget admission between
probes; the official MCP client fixture records the idle and local-reservation
timings. These are controlled local tests, not remote-provider acceptance or an
overall coding-productivity claim. See [external watches](external-watches.md).

The v0.11 paired experiment harness adds actual fixture outcomes and costs under
the same outer reservation. It is a controlled comparison, not a productivity
claim about autonomous coding. Optional local AI ranks existing evidence-backed
trials; it cannot apply settings. See [measuring and optimizing](optimization.md).


v0.15 adds guarded protected reserves, batched per-check acceptance results and
actual admission snapshots. Release checks cover activation while old work is
queued/running, old insert rejection before creation, heavy progress, native
failures, cancellation, timeout and read-only behavior. Counterbalanced fixtures
measure quick latency alongside the heavy completion cost and batching overhead;
publication requires reporting both gains and costs. An A+ productivity claim
still requires prospective matched real coding tasks, rather than a higher test
count alone. See [verification](verification.md).

v0.17 adds bounded declared-input evidence with strict before/during execution
gates, native-result preservation, durable MCP reconnects and actual v0.16.1
controller/worker downgrade rejection. Read-only old-schema inspection remains
nonmutating. Whole-checkout capture limits and time-of-check gaps remain explicit;
see [declared inputs](declared-inputs.md).

v0.18 adds opt-in parallel-batch gates: bounded overlap, divided worker hints,
deterministic native outcomes, fail-fast siblings, pipe draining, cancellation,
timeouts, worker crash cleanup and one-attempt MCP reconnects. Controlled batching
comparisons preserve all outcomes and costs under the same outer envelope. Live
capacity diagnostics explain global/protected-pool deficits without changing
admission. A faster fixed check fixture still does not establish overall coding
productivity or remove the need for disk headroom and representative reservations.
