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
Multiple explicit output locks remain a follow-up that needs compatibility with
old runners; this release does not claim support for them.
