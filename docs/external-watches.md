# External completion without idle build reservations

An existing remote job or service startup can take minutes while this workstation
has little to do. Buildr can observe it under a durable **watch handle**. Each
status probe runs as an ordinary short job in the shared queue. Between confirmed
pending results, the controller holds no CPU slots, RAM reservation, job slot,
scratch allocation or checkout lock. Other agents can use that capacity.

A watch has its own handle and a history of individual probe job handles. It does
not convert an already-running build into an idle watch or release that build's
reservation. Split external submission, completion observation and subsequent
local work into their respective steps. A provider accepting a submission is a
separate observation from its later completion probe.

## Probe contract

Supply an explicit foreground status command that is safe to repeat and declare
`--read-only-probe` (or `read_only_probe: true` via MCP):

| Native probe outcome | Watch behavior |
| --- | --- |
| Exit `0` | Record that this completion probe reported success; stop observing |
| Exit `75` | Record a confirmed pending result; release the completed probe's normal reservation and schedule the next probe after the interval |
| Other exit, timeout or cancellation | Stop observation and preserve the native result; do not repeat it |
| Unknown result or uncertain worker launch | Stop automatic progression; retain handles and existing live-job reservations for inspection |

The declaration is an operator contract, not static verification of arbitrary
code. A create/deploy/launch/submit command is unsuitable as a repeating probe.
Do not leave background processes running after a probe exits. Your probe must
check the intended task ID and completion criteria; the supplied external
reference is retained as context, not independently authenticated by Buildr.
Buildr provides no automatic cloud connection or provider-specific completion
assumption. Local probes and their worker defaults use the same queue as builds.

## CLI

For an external task that already exists:

```sh
buildr watch start \
  --request-key unique-task-observation-001 \
  --cwd /absolute/repository \
  --external-ref provider:existing-task-id \
  --read-only-probe --interval 15 --timeout 3600 \
  --probe-timeout 30 --jobs 1 --memory-gb 0.5 \
  -- python3 tools/check_existing_task.py existing-task-id
```

`tools/check_existing_task.py` represents your existing read-only status helper;
it must implement the exit-code contract above. Buildr never substitutes a
submission command for it. The receipt contains the request key and `watch_id`.
Keep them, then use:

```sh
buildr watch show WATCH_ID --json
buildr watch list --active --json
buildr watch wait WATCH_ID --timeout 120 --json
buildr watch export WATCH_ID --output /tmp/new-watch-evidence.json
buildr logs PROBE_JOB_ID
```

Start the watch outside `buildr run`: each probe is queued by its controller.
Use `watch wait` as an orchestration command outside build reservations. Closing
the submitting terminal does not cancel a watch. A read-only wait timing out
returns `124` and leaves observation running.

Defaults are a 15-second interval, a one-hour watch deadline, a 30-second per-probe
timeout, at most 120 probes, one CPU slot and 0.5 GiB per probe. Probes use the
quick lane and local backend. Source observations are enabled for each probe;
`--no-track-source` omits them. They describe local probe code, not the remote
artifact. `--resource-key` can name an explicit shared status/output resource;
the lock is held only while that probe job runs. Existing global admission
budgets still apply when the next probe becomes due.

## Cancellation and recovery

```sh
buildr watch cancel WATCH_ID
buildr watch resume WATCH_ID
```

Cancellation prevents new probe intents, requests cancellation of the current
local probe, and settles after that probe actually stops. The external task
continues. A watch deadline similarly stops observation and returns `124` after
its local probe settles. Neither operation releases a live orphan reservation.

If the controller stops, `resume` observes the existing prepared or running job.
Only probe intents with a recorded, unattempted launch may launch; an uncertain
attempt is never launched a second time. Worker attachment is one-time. A known
pending probe may schedule another observation after its interval; other outcomes
cannot. A terminal `uncertain` watch requires independent inspection before an
operator chooses a new observation attempt with a new key. It cannot be resumed
as an automatic retry. Terminal successful/failed/cancelled watches are also not
reexecuted by `resume`.

Reusing a submission key with identical arguments returns the original watch
handle. A changed payload under the same key is rejected. Keys and watch handles
survive client reconnects and controller restarts. An uncertain watch-creation
receipt may include a handle whose controller startup needs inspection; reusing
its key never starts a duplicate controller or watch.

When the probe limit is reached after a pending result, the watch becomes
`exhausted` and returns `75`. This means the last confirmed observation was still
pending; it does not report external completion or stop the external task.

## MCP and dashboard

Five additional MCP tools expose the same lifecycle:

- `buildr_watch_submit`: explicit probe contract, external reference, argv and
  caller submission key; returns a durable watch handle.
- `buildr_watch` and `buildr_watches`: read-only status and bounded history.
- `buildr_watch_cancel`: stop observation and its current local status probe.
- `buildr_watch_resume`: resume an interrupted controller while retaining existing
  probe handles; unknown outcomes are not retried.

Reconnect an existing MCP client to discover the new tools. Read-only server
mode exposes only inspection tools. `status --json` and MCP status include
`external_watch_counts` separately from ordinary job counts and reservations.

Press `v` to reach **EXTERNAL COMPLETION** in the full TUI, or cycle jobs, plans
and watches in narrow panes. Arrows select watches, Enter opens the latest probe
log, and `e` exports private evidence. The full view supports explicit `c` to
stop observation and `r` to resume the controller. Compact mode is a monitor.
An empty watch view cannot apply these actions to an unrelated selected job.

## Measurements and compatibility

Reports separate watch wall time, confirmed idle time without reservations,
actual local probe reservation duration, per-probe queue waiting and sampled
CPU usage. Missing or incomplete measurements remain explicit. Native exit 75
remains 75 in each ordinary job; the watch adds its pending interpretation.
Local reservations are estimates, not CPU/RAM hard limits. Small controller
overhead runs outside build reservations, as with verification-plan controllers.

Older runners continue to see ordinary probe jobs. No existing job states,
reservation budgets or scheduler policy are changed. Watch tables are created
only by writable opens; read-only inspection does not initialize or migrate a
queue. There is no automatic SSH, Hetzner or production-cluster dispatch.

The regression suite tests full-budget admission between probes, native results,
controller restart, one-time attachment, cancellation races, deadlines, unknown
outcomes, read-only inspection and dashboard action isolation. The optional
official MCP client check also records a controlled external-wait fixture and
its idle/reservation timings. These local fixtures do not contact a remote host
or establish overall autonomous coding-productivity gains.
