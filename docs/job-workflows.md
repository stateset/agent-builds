# Existing jobs, plan drafts and compact results

## One handle for already submitted work

```sh
agent-builds group create JOB_TEST JOB_LINT JOB_BUILD --name 'release checks'
agent-builds group show GROUP --json
agent-builds group wait GROUP --timeout 600 --tail 20 --json
agent-builds group list --json
agent-builds group export GROUP --output /tmp/new-group-evidence.json
```

Groups observe existing native job outcomes. Creation does not submit commands,
change reservations, cancel work, impose dependencies or establish that an
external build completed. Group waits reconnect read-only and preserve native
failure codes; wait timeout returns 124 and leaves all work running. No group
cancel or retry command exists. At most 64 distinct handles belong to a group.
Detailed exports retain existing source observations and explicitly leave absent
historical evidence missing. Exports are private and refuse overwrite.

The full TUI's `v` views include existing job groups. Arrows select groups,
PgUp/PgDn scroll, Enter opens an active/failed job log, and `e` exports evidence.
Editing and cancellation keys are disabled in the group view. Narrow panes keep
the existing compact jobs/plans monitor.

## Draft a future plan from known commands

```sh
agent-builds plan draft JOB_TEST JOB_LINT JOB_BUILD --cwd /path/to/checkout \
  --capture-input /tmp/my-verification-harness.sh --output /tmp/draft-plan.json
agent-builds plan validate /tmp/draft-plan.json --cwd /path/to/checkout
# Review/edit the draft before explicitly executing it:
agent-builds plan run /tmp/draft-plan.json --cwd /path/to/checkout
```

Drafts copy literal argv, cwd, resource keys, reservations and timeouts in caller
order. Defaults create success-only dependencies for a future execution; they
do not claim that the original jobs had those dependencies. All jobs must be
within the selected checkout. Changed machine limits can make a historical
reservation invalid, which validation reports. No saved command is executed by
drafting. Shell harnesses remain literal commands; their contents are not parsed
into guessed stages. The explicit output is a runnable manifest, mode 0600, and
cannot overwrite an existing path. Stdout also retains job bindings and source
observations that were actually recorded.

`--capture-input` fingerprints explicitly named files at draft creation only,
with a 1-MiB/one-second bound per file and at most 32 files. Those hashes do not
establish what a harness contained when an earlier job ran, and they are not
enforced as execution inputs. Paths are relative to the calling shell, not
`--cwd`. No input contents are exported.

## Compact automation results

```sh
agent-builds plan show PLAN --summary --json
agent-builds plan wait PLAN --summary --tail 20 --timeout 600 --json
agent-builds plan list --summary --limit 20 --json
agent-builds plan run /tmp/draft-plan.json --cwd /path/to/checkout --wait --summary
```

Summary schema `agent-builds-plan-summary-v1` retains aggregate state/exit code,
per-stage native command exit, verification reasons, log handles, artifact
issues, source-change flags and stage counts. It omits repeated raw manifests,
process IDs and full fingerprints; detailed `show` and `export` remain available.
Failed artifact/source verification can produce plan exit 1 after command exit
0; summaries show both results. Missing source observations remain null, not
claimed unchanged. Tails apply to failed/active submitted stages only, are bounded
to 200 lines and a 64-KiB read, and explicitly report unavailable logs.

The wait response keeps its wrapper (`complete`, `timed_out`, `exit_code`, `plan`)
and returns 124 on wait timeout. Inspection never cancels or resumes controllers.
`plan run --wait` still prints its submission receipt before the final result.

## Cleanup after failed checks

```json
{
  "version": 1,
  "name": "npm verification",
  "stages": [
    {"name":"validate", "command":["npm","run","quality"], "lane":"quick", "jobs":2, "memory_gb":4},
    {"name":"build", "command":["npm","run","build"], "lane":"heavy", "jobs":3, "memory_gb":6},
    {"name":"browser", "command":["npm","run","e2e","--","--workers=1"], "lane":"quick", "jobs":1, "memory_gb":2},
    {"name":"cleanup", "command":["npm","run","embed:reset"], "when":"always", "after":["validate","build","browser"], "jobs":1, "memory_gb":1}
  ]
}
```

`when: "always"` waits for all declared prerequisites to settle, then runs even
if they failed, were skipped, cancelled individually or lost. Normal stages
retain `when: "success"`. Cleanup does not replace the original failed result or
make a failed plan pass. Global plan cancellation cancels pending cleanup too;
unknown/live outcomes still must settle before dependent cleanup is dispatched.
Cleanup is queued with its own reservation and may itself fail. It is not a
crash-proof finally block. Use `source_policy: "report"` for cleanup when strict
source drift checks would prevent a required cleanup command from launching.

## Policy-aware advice

```sh
agent-builds advise --repo /path/to/checkout --policy current --json
agent-builds advise --policy quick-aging-v1 --json
```

Advisor cohorts separate recorded scheduler policy, loaded runner version,
wait-instrumentation availability and lane. Missing historical versions stay
`unrecorded`. New workers record their application version; prepared plan workers
record their own version when attaching. The policy filter applies to history,
active counts and findings together. Cohort percentiles are not controlled
comparisons of identical workloads.

Repeated successful tiny quick checks (median <=10s, at least three matching
executions) can receive an optional one-slot trial without an unproven memory
cut. Explicit tool worker flags still need review; the queue preserves argv.
