# Feedback disposition through v0.9.0

This records the existing eight-agent feedback round, not a new survey. Original
reports are in `/tmp/agent-builds-feedback/next/`; the earlier release disposition
is in `agent-feedback-v0.6.md`.

| Feedback | Status |
| --- | --- |
| One durable handle for dependent verification steps | v0.7 plans, independent branches, skip-after-failure and controller recovery |
| Evidence tied to source and generated artifacts | v0.7 observations and stored evidence export, with explicit snapshot limitations |
| Explain long waits and reduce repeated wait chatter | v0.8 category timings, bounded transitions, coverage and coalesced heartbeats |
| Small checks wait while spare capacity is unusable | v0.9 conservative backfill, subset safety checks and replay; broad latency improvement still requires matched production measurements |
| Repeated resource use should inform reservations | v0.9 read-only evidence-based CPU/RAM/lane trials; no automatic settings changes |
| Dashboard usable within terminal splits | v0.8 compact jobs/plans monitor; v0.9 full-view advisor with wrapped text |
| Multiple output locks, saved plans and concise plan summaries | Recorded follow-ups; multiple-lock compatibility requires more design |

No new agent announcements are sent by these features. No remote worker is
configured or used.
