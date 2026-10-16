# Understand a waiting queue

The dashboard shows a queue-wide status below machine pressure. In a narrow
Ghostty or Herdr pane, a disk blockage appears as `DISK: +3.3 GiB needed` rather
than requiring a particular queued job to be selected.

```sh
buildr status --summary
buildr status --summary --json
buildr diagnose --json
buildr diagnose JOB_ID --json
```

CLI and MCP status include a `queue_health` object. It counts all waiting jobs,
including older-compatible multi-lock rows, independently of the job display
limit. Requested cancellations are counted separately from work awaiting
admission. Repository filters restrict queued counts and requirements; running
reservations remain global and are explicitly labelled.

The shared state filesystem limits every job, even when checkouts live on other
filesystems. If its free space falls below every waiting job's requirement, the
summary identifies the common disk blockage. It reports the smallest and largest
shortfalls, including current running scratch reservations. A positive shortfall
rounds upward at display precision; freeing that space clears only this measured
constraint. Filesystem differences, locks, priority, RAM and reservations still
affect admission. Missing disk measurements remain unknown.

Primary runner wait reasons are grouped only when their timestamps are fresh.
Old, missing, oversized and invalid observations are counted separately. At most
200 small observation records are parsed; counts and disk requirement ranges
still cover every waiting job. Reports exclude job source evidence and logs.
The real-process scale checks include this summary in their inspection-latency
probes while 30 or 100 runners wait; the JSON evidence identifies that coverage.

These are read-only explanations. They do not recover work, retry commands,
resize reservations, reduce disk floors or delete caches. The interactive TUI
continues its existing periodic recovery through the shared opportunistic lease,
so a busy writer does not block the dashboard in recovery. The summary refreshes
every two seconds; it is not an admission promise or an estimated finish time.

To inspect cache sizes, use `buildr disk --repo /absolute/repo --json`. Review
ownership and active use before choosing a cleanup target. A disk shortfall alone
does not authorize deletion or automatically change any setting.
