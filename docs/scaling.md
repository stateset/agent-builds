# Queue coordination and compatibility

The next capacity target is 30–100 agents and hundreds of queued jobs. This is
a target for validation, not a claim that the entire workload has been benchmarked
at that scale. Reservations still describe participating jobs, not hard host limits.

Updated waiting runners read a SQLite queue generation and configuration-file
metadata before attempting admission. State, capacity, lock and policy changes
advance the generation. Telemetry and diagnostic updates do not. Unchanged waits
refresh admission at most once every two seconds; the existing short poll still
notices queue changes and cancellation. Host headroom and aging are reconsidered
on the periodic refresh. Every actual claim rechecks the current queue and budgets
inside the same transactional admission path.

Configuration-file settings are cached per connection until file metadata changes.
SQLite policy activation flags remain fresh. Writable opens use numbered
`PRAGMA user_version` migrations, serialized by a local file lock. Current-schema
opens skip migration transactions, and a newer unsupported schema is rejected.
Read-only opens never migrate or initialize state.

Periodic recovery uses one nonblocking file lock and a shared two-second lease.
Only the lease holder sweeps; a crashed holder releases its OS lock. Explicit
recovery remains immediate. This is local coordination on a Linux filesystem,
not a distributed leader election protocol.

Python 3.8 and historical queue guards remain supported. Older runners can still
use the database and their updates wake new runners through triggers; their own
polling and migration behavior is unchanged. Reconnect MCP clients and start
future jobs with the updated executable to receive the reduction in polling.
Running commands are not restarted or resized on upgrade.

Updated live runners poll only the job's cancellation state, backend, PID and
Linux start identity while collecting output. They do not repeatedly decode
source evidence, batch results, telemetry or presentation fields. The launch
gate's cancellation checks and cancellation write transactions also use small
control reads. Raw held state is preserved for older workers. Live reservation
snapshots read only reservation and display fields for running jobs. Detailed
inspection and final reports still include the full evidence. These changes
reduce control-path data work; they do not change poll cadence, reservations,
timeout codes, PID checks, or admission ordering.

## Controlled coordination measurement

`tools/verify_coordination.py` compares actual Store polling methods with a
previous-release checkout. Queue the driver once and provide that checkout:

```sh
buildr run --lane quick --jobs 2 --memory-gb 2 -- \
  python3 tools/verify_coordination.py --baseline-checkout /absolute/v0.22.1 \
  --output /tmp/coordination.json
```

An initial local run used 100 waiting connections across four processes, eight
poll cycles each, and private paused queues:

| Observation | v0.22.1 | Updated runners |
| --- | ---: | ---: |
| Writable-open migration transactions | 300 | 0 |
| Poll-loop write transactions | 1,600 | 202 |
| Configuration-file reads | 1,600 | 100 |
| Admission attempts | 800 | 200 |
| Recovery sweeps | 800 | 2 |

All 100 fixture jobs stayed queued in both arms; no commands executed. This is a
synthetic overhead comparison with several connections per process, not a test
of 100 independent runner processes or end-to-end build throughput. Counts can
vary with elapsed time because host refresh and recovery use real clocks. A
representative multi-agent workload remains necessary to validate the full target.

Native commands start behind an inherited-pipe gate. The runner must persist the
child's PID and Linux start time before releasing it. If the runner dies during
attachment, EOF prevents the command from executing. Unknown outcomes after an
authorized launch still require inspection and are never retried automatically.
For compatibility, native timeouts retain state `cancelled` and exit code 124.
