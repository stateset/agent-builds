# First-run agent feedback

Feedback was requested from the seven other agents in the active Herdr session.
All seven replied. Reports distinguish observed behavior from untried ideas.

| Request | v0.4.0 response |
| --- | --- |
| Read-only status fails in a repository sandbox | Inspection uses a read-only SQLite connection, creates nothing, and skips migration/recovery. Verified against the live queue without escalation. |
| Blocking reasons do not identify the job | Structured blocker IDs, commands, paths and durations; CLI output and TUI drill-down. |
| Disk wait requires checking `df` separately | Free/required measurements and timestamps in wait context and the TUI. |
| Release builds need different disk headroom | Per-job minimum free space plus conservative scratch reservations. |
| Quiet startup looks like queue waiting | Explicit queued, waiting, running and final events. |
| Agents must parse JSON twice for command arguments | Native `argv` and readable `command_display`, with the legacy field retained. |
| Status output is truncated by agent tools | Compact summaries, active/repository filters, and limits. |
| Long checks need durable handles | Detached submission, receipts, stable inspection, and log following. |
| A validation driver can double-wrap Cargo | Inherited queue marker and immediate nested-wrapper diagnostic. |
| Docker child workloads need clear budgeting | Documentation distinguishes declared combined reservations from actual per-container limits and cleanup. |
| Estimated wait times | Deferred until there is enough comparable job history; report real elapsed time and resource constraints now. |
| Safe cache cleanup suggestions | Deferred. This version does not crawl other repositories or delete artifacts; disk headroom blocks new admission. |

Report sources: Response, Desktop, Console Mobile, Agents, Engine, Sandbox, NSR.
The reports are user feedback, not proof that every runtime or backend was tested.
