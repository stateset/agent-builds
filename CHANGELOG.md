# Changelog

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
