# Native runner-loss acceptance

`tools/verify_recovery.py` repeatedly kills a known fixture runner after its
native child confirms execution. Current code must preserve the orphan's CPU/RAM
reservation and both resource locks. A real follower waits on the secondary lock;
an unrelated real job must complete using the remaining capacity. Four reopened
current Store connections each run recovery while the orphan is alive.

Each cycle runs current and actually loaded v0.24.2 runners, with two outcomes:

- The orphan completes with a deliberate native exit 7. The dead runner cannot
  collect that result, so recovery must report `lost` with an unknown exit code.
- Current code explicitly cancels the orphan. Its reservation remains until
  the native process exits, then the conflicting follower can proceed.

A start ledger must contain exactly one attempt. Further recovery and an explicit
claim must never make the lost/cancelled job runnable again. A private SQLite
trigger audits every admission, including changes between observations.
An OS file lock independently checks that native commands never overlap on the
shared output, rather than trusting scheduler state alone. Readiness,
blocking and completion are observed through receipts and files; fixed sleeps do
not establish that a child has launched or a lock has been exercised.

Run the whole driver once through the shared queue. It reserves two CPU slots
and at least 2 GiB, creates private fixture queues, and never submits historical
commands, changes global settings, uses Docker/SSH or contacts Herdr agents.

```sh
buildr run --lane quick --jobs 2 --memory-gb 2 --timeout 240 -- \
  python3 tools/verify_recovery.py --cycles 3 --output /tmp/new-recovery.json
```

The output path must be new. Source identity is captured before and after;
changes reject the run. Reports include every completed case, actual loaded
versions, job handles, reservation peaks and the fixture directory. A progress
file records that directory before execution. Failures retain their report,
private queue databases, receipts and logs for inspection. Cleanup signals only
processes from these fixtures, using recorded child PID/start identities.

CI runs twelve cases (three cycles) and checks the JSON independently of the
queued driver's exit handling. The regression suite also exercises all four
current/old and completion/cancellation combinations on Python 3.8 and 3.12.

These are bounded fault fixtures. They do not establish long-duration production
reliability, compiler throughput, coding productivity, arbitrary version upgrade
compatibility or recovery from machine/power failure. Local CPU/RAM reservations
remain scheduling budgets rather than OS limits.

## Telemetry during writer contention

Periodic running-job telemetry uses a nonblocking `BEGIN IMMEDIATE`. A busy
writer defers that optional sample; the next cumulative sample supersedes it.
The normal sampling interval still applies, so contention does not create a
retry loop. Only a busy BEGIN is deferred. Transaction-body and commit errors
propagate, and the connection's existing SQLite timeout is restored.

Final telemetry and the native result commit in one update. A rejected write
cannot leave a partially updated outcome or report. Final settlement still
requires a writable database; this does not make persistent storage failures
recoverable or authorize retrying a command with an unknown result. Regression
tests hold a real SQLite writer while a native job emits output and exits 7,
then verify the full log, preserved exit code and complete final telemetry.
