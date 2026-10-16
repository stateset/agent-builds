# Real-process scale checks

The connection comparison in [scaling](scaling.md) isolates admission overhead.
`buildr benchmark scale` adds actual runner processes and short synthetic commands.
It is an explicit experiment, not automatic tuning or a simulation of model agents.

Queue the entire driver once. Reserve at least two CPU slots and 2 GiB for up to
30 runners, or 4 GiB for 31–100 runners. These are scheduling reservations, not
hard host memory or CPU limits. The benchmark never changes host affinity,
global queue budgets or other agents' jobs.

```sh
buildr run --lane quick --jobs 2 --memory-gb 4 --timeout 900 -- \
  buildr benchmark scale --agents 100 --backlog 200 --history 1000 --hold 4 \
  --baseline-checkout /absolute/frozen-v0.22.2 \
  --output /tmp/new-process-scale.json
```

Use a clean source checkout for both arms. Source identity is captured before and
after; a changed or unavailable identity rejects the comparison. Omit the baseline
to check the current checkout alone. Existing report files are never overwritten.
Private fixtures inherit a working-directory scope restricted to their own root.

At the 100-runner setting, the harness starts 100 real CLI runner processes and
waits for their paused-queue receipts. It then adds 200 synthetic backlog records,
giving 300 queued jobs. Historical records and backlog are metadata fixtures;
they do not represent executed commands. Idle measurements cover all 100 workers
and include instrumentation overhead. Bounded read-only inspection probes record
response latency while those processes wait.
CPU snapshots are sampled every 0.2 seconds by an independent thread, including
while a runner waits inside SQLite. Reports retain each arm's minimum and maximum
snapshot interval and aggregate CPU use normalized to those intervals.

Before resuming, it cancels one known queued job and kills one known queued
runner. Neither has a native child. Recovery must retain the second as `lost`;
it is never retried. The other 98 commands run once each, including deliberate
exit-7 cases whose native failures must be preserved. Shared output locks are
checked with nonblocking OS file locks and a start/end ledger. A test-only SQLite
trigger records every admission atomically, catching even short reservation
overflows. The native ledger independently checks execution overlap and completion.

All work runs in newly created private queues. The live queue holds only the
outer reservation. No Docker daemon, SSH host, Herdr agent or model provider is
contacted. Only these fixture runners are stopped during cleanup; failed or
uncertain fixtures retain their state for inspection. Successful fixtures are
removed after their evidence is retained in the report.

CI compares the frozen v0.22.2 baseline and candidate at 30 and 100 runners, with
downloadable JSON reports. Each report includes source identity, idle CPU/counter
observations, inspection latency, actual attempts, expected failures, reservation
peaks, locks, cancellation and loss. A separate CI step reads those reports and
checks the required outcomes without relying solely on Buildr's exit handling.

One ordered synthetic comparison does not establish coding productivity, real
compiler throughput, safe RAM reductions or the behavior of 100 model clients.
Use the results to identify coordination bottlenecks, then validate representative
repository workflows with [workflow studies](workflow-studies.md).
