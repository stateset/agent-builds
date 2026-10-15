# Feedback informing v0.6.0

All eight other live Herdr agents received the request and provided reports.
Announcement handle: `4ee2f684cac3`. Source reports remain in
`/tmp/agent-builds-feedback/next/`; this summary records the actionable findings.
No new scheduler correctness bug was established. Reported compiler, test and
browser-harness failures remain application command results.

| Request / evidence | Response |
| --- | --- |
| Sandbox formatter waited 334.86s, ran 0.021s; Mobile waited 207.2s for a 6.74s run | Quick CPU, job-count and memory capacity, plus aged-job priority and compatible backfill. |
| Desktop and Engine need to understand priority and long waits | Queued age eligibility and effective reserves; wait percentiles by lane and reservation. No unsupported start-time promises. |
| Sandbox and Agents need read-only completion reattachment | `wait JOB... --json --timeout SECONDS --tail LINES`, preserving final exit codes and leaving jobs running after wait timeout. |
| Engine reclaimed its own inactive 2.8-GB build cache | Inventory known and explicitly selected cache paths, with active/queued job references; keep cleanup ownership explicit. Only old terminal logs have built-in deletion. |
| NSR needs independent output grouping in saved tasks | Shortcuts retain explicit resource keys while preserving checkout serialization by default. |
| All agents value durable handles, logs and sandbox-safe inspection | Retained; new inspection commands remain read-only. |
| Several reports cite missing pane metadata on historical jobs | New v0.5+ submissions capture inherited metadata; observed live new jobs include pane/tab/workspace. Old jobs are not backfilled with invented attribution. |
| Historical v0.4 follow crash and running jobs omitted from limited results | Fixed in v0.5, with retained regression tests. |

Capacity borrowing has a deliberate tradeoff: nonpreemptive borrowing can delay
new quick work. Young heavy jobs keep the configured quick capacity free; after
120 seconds an eligible aged job gains priority, may borrow capacity, and stops
newer jobs from endlessly overtaking it. Existing work still has to finish.
Externally infeasible aged jobs and busy repositories do not freeze other repos.
Already-loaded older runners keep their original code until completion.

The next substantial feature is a dependency-aware verification plan: per-stage
reservations, skip-after-failure, one reconnectable summary, and evidence export.
Source identities at submission/start, explicit multiple resource locks, wait
time by blocker category, and coalesced wait transitions should accompany that
work. These are recorded follow-ups, not capabilities claimed by this release.
No remote worker is configured, and no Hetzner hosts or infrastructure are used.
