# Faster verification under load

## Protected quick capacity

```sh
buildr config --set protected_quick=true
buildr latency --target-seconds 30 --json
```

The total machine CPU, memory and job budgets stay unchanged. Protected mode
applies the existing quick CPU, memory and job reserves to heavy work at every
age. An aged heavy job retains ordering priority, but cannot borrow those
reserves. A heavy head blocked by the protected reserve lets eligible quick work
proceed. Shared resource locks and all global budgets still apply. This favors
verification latency at a possible cost to heavy throughput; it does not make
every build faster or guarantee an ETA.

Activation is transactional and guarded across versions. A request first drains
jobs from workers without reserve-protocol support, existing oversized heavy
work and heavy reservations exceeding the protected pool. Those jobs are not
cancelled, restarted or retried. New writable submission/admission attempts
activate the request when the blockers have settled. `config` shows effective,
requested and pending flags; `latency` lists blockers. Inspection never activates
the policy. Continuous submissions from old workers may delay activation.

After activation, an old worker insertion is rejected before a job is created;
reconnect the submitting client onto the installed version. New heavy jobs that
cannot fit outside protected reserves are rejected with an actionable error.
Existing large work drains first. To run a deliberately exclusive heavy job,
explicitly disable protection with `buildr config --set protected_quick=false`.
Protected-policy configuration must use `config --set`; editing the JSON flag
alone does not bypass the database activation guard.

Quick commands can themselves be long or oversized, so reserve protection alone
does not bound latency. Keep quick work short, give it a timeout, and use measured
reservations. Per-key FIFO, disk pressure, available RAM and existing running
work can also delay a check. All running/cancelling reservations, including live
orphans and unknown Docker outcomes, continue to count.

## Related checks in one reservation

Save an explicit manifest, for example `checks.json`:

```json
{"version":1,"keep_going":true,"checks":[
  {"name":"format","command":["cargo","fmt","--check"]},
  {"name":"diff","command":["git","diff","--check"]}
]}
```

```sh
buildr batch run checks.json --cwd /absolute/repo --jobs 1 --memory-gb 1 --timeout 60 --detach --events-json
buildr batch show JOB_ID
buildr wait JOB_ID --json
```

Up to 32 foreground checks share one local quick reservation, working directory,
worker environment and complete resource-key set. Commands remain literal argv;
shell syntax requires an explicitly supplied shell. There is no nested queue
submission, implicit retry, remote execution or Docker batch backend. Commands
must remain in the foreground; do not use this for daemons or external polling.
Use a plan when stages require different budgets, dependencies or independent
artifact/source gates. Use a watch for external completion.

The default total timeout is 60 seconds, with a maximum of 300. Cancellation and
timeout stop the outer process group and prevent subsequent checks. Each check
retains its native exit code; by default remaining checks still run after an
ordinary failure. `keep_going:false` skips later checks after the first failure.
The aggregate ordinary exit is the first nonzero native result. Queue timeout
and cancellation retain their existing 124/130 semantics. A killed worker's
partially recorded result remains unknown; it is never promoted to success.

Job JSON, MCP job results and TUI details expose the check results. The worker
exit and outer job exit are separate, so cancellation/timeout or source evidence
can be assessed without rewriting an observed check result.

## Measure before tuning

`buildr latency` (also the read-only `buildr_latency` MCP tool) reports completed
wait percentiles, target attainment, runtime and blocker totals. Still-waiting
jobs appear separately as censored observations; they are not silently counted
as fast completions. Cohorts include runner/policy versions and the actual
admission budgets and protected-mode flag. Older records with no admission
snapshot remain unknown. These groups are not matched causal experiments.

Reservation trials reuse the advisor's requirements for repeated matching
successes and completed local measurements. Nothing applies them automatically.
Test representative inputs before saving a smaller reservation. The optional
AI provider remains advisory and cannot override budgets or evidence.

The controlled `tools/verify_verification.py` comparison runs only inside an
outer shared reservation. Counterbalanced pairs use the same two-CPU affinity,
budgets and fixed workloads. It records quick latency and heavy completion cost,
then compares four sequential CLI checks with one batch. Results describe these
fixtures, not accepted code or autonomous-agent productivity. The latter still
requires prospective matched coding tasks with explicit acceptance criteria.
