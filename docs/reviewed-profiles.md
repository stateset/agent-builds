# Review, trial and promote a resource profile

The advisor can suggest smaller reservations after repeated matching successes.
A reviewed profile connects that evidence to an explicit queued trial and a saved
task. It never changes an existing reservation or adopts a suggestion automatically.

```sh
buildr workbench JOB_ID
buildr task propose JOB_ID --kind memory_trial --name checked-tests --output /tmp/tests-proposal.json
# Review the literal argv, source, declared inputs and proposed budgets in the file.
buildr task trial /tmp/tests-proposal.json
buildr plan wait TRIAL_PLAN_ID --summary --json
buildr task review /tmp/tests-proposal.json --plan TRIAL_PLAN_ID
buildr task promote /tmp/tests-proposal.json --plan TRIAL_PLAN_ID
buildr task run checked-tests --cwd /absolute/repo --detach
```

Available kinds are `memory_trial`, `low_average_cpu`, `tiny_check_trial` and
`long_quick_command`. Proposals require an explicitly selected successful local
job, at least three matching successes and complete current Git/input evidence.
They copy literal argv, locks, backend, disk budgets, timeout and declared inputs.
Only CPU, RAM and lane can vary. Long batches must be split into explicit stages
instead of being silently relabeled. CPU trials retain batch parallelism and the
largest explicit per-check CPU budget.

Save proposal files outside the source checkout, for example under `/tmp`, to
avoid including a changing proposal in its own source fingerprint. The file is
private and cannot overwrite an existing path. Trial submission fingerprints the
proposal alongside the original declared inputs; there can be at most 16 inputs
in total. `--repeats` permits three to five stages, each using ordinary shared
admission and strict unchanged-source verification. The controller holds no build
reservation. Run `task trial` directly, not inside a queued driver.

Promotion checks the entire matching trial plan, not a selection of successful
children. Every native result and source observation must succeed, and the
proposal and current source must still match. Baseline settings/timings are
checked against their original recorded jobs. RAM reductions additionally need
complete local process-tree measurements from every trial, at least three samples
and one second of runtime each, and 30% headroom above the largest sampled RSS.
Tiny unsampled jobs cannot qualify for a RAM reduction through this path.

`task review` reports baseline/candidate median runtime and observed peak RSS.
Review regressions before explicitly promoting; successful trials do not guarantee
a faster workflow. Sampling may miss peaks, local reservations are estimates,
and unrecorded external inputs, services, environment or future workloads can
differ. Choose representative inputs and declare relevant helpers.

Reviewed tasks use a separate profile section that older runners cannot discover.
`task list` includes both ordinary and reviewed tasks. Reviewed `task run` creates
a **plan handle** with strict source checks; use `plan show/wait`, rather than job
inspection, for that handle. Changed or unavailable source/input identity blocks
creation before any command is submitted. Ordinary saved tasks keep their existing
behavior. Saving an ordinary shortcut over a reviewed name is rejected; validate
a fresh proposal or choose a different name.

## TUI and MCP

Press `r` from jobs/details to open the workbench. It combines the selected job's
recorded blocker, current CPU/RAM projections and matching historical measurements.
Background reads keep terminal input responsive. Narrow splits support inspection
and scrolling. In the full view, `f` selects a trial, `e` writes a proposal for
review, and `t` submits an explicitly selected reviewed proposal file. Saving the
draft does not submit it or promote a profile. `r` returns to jobs.

CLI `workbench JOB_ID --json` and the read-only MCP `buildr_workbench` tool expose
the same report. Panels have separate fresh snapshots; a future-run projection
retains existing locks and does not resize or replace the selected job.
