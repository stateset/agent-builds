# Declared input files

Buildr v0.17 records explicitly declared local helper files alongside its Git
source observations. This addresses feedback about `/tmp` verification scripts
changing while a build waits. It does not discover paths inside shell commands.

```sh
buildr run --cwd /path/to/repo --lane quick --jobs 1 --memory-gb 1 \
  --input-file /tmp/verify-project.sh -- bash /tmp/verify-project.sh
buildr batch run checks.json --cwd /path/to/repo --input-file /tmp/verify-project.sh
buildr task add verify --cwd /path/to/repo --input-file /tmp/verify-project.sh \
  -- bash /tmp/verify-project.sh
```

Repeat `--input-file` up to 16 times. Paths are literal files, relative to the
submitted working directory, normalized before detaching, deduplicated and sorted.
Saved tasks retain normalized absolute paths. There are no globs, directory walks,
or automatic remote reads. For Docker jobs these observations describe host files;
Buildr does not add mounts or attest image contents.

Each submission/start/finish observation records path, status, SHA-256, byte size
and permission mode, never file contents. Inspect `buildr show JOB --json`, MCP
`buildr_job`, or detailed plan evidence. Ordinary jobs, tasks and batches use
report-only observations: a changed or missing input does not rewrite the native
command exit code. Declaring an input enables tracking even without `--track-source`
or with MCP `track_source:false`.

For a strict verification gate, declare inputs at the top level of a plan:

```json
{
  "version": 1,
  "name": "verify-project",
  "source_policy": "require_unchanged",
  "input_files": ["/tmp/verify-project.sh"],
  "stages": [
    {"name":"verify", "jobs":1, "memory_gb":1,
     "command":["bash","/tmp/verify-project.sh"]}
  ]
}
```

All stages inherit this plan-wide list, including stages with a different `cwd`.
A definite change or incomplete identity before start prevents command execution
(exit 2), fails the stage and skips dependent success stages. A change or incomplete
identity during execution fails verification while preserving the command's native
result. An exit-0 command can therefore have a failed stage and aggregate plan
exit 1. Per-stage `source_policy` overrides remain supported. Compact plan summaries
include declared input count and submission capture status; detailed evidence
includes all three observations.

MCP `buildr_submit` and `buildr_batch_submit` accept the optional `input_files`
array. Plan submission/validation accept it inside the manifest. Reconnect using
the same request key and exact original arguments: Buildr returns the original
handle and stored submission hashes even after the helper changes. Changing the
array under an existing key is rejected without submitting another job.

Capture is bounded to 16 paths, 8 MiB per file, 16 MiB of reads and three seconds
for the input set, in addition to the existing Git capture bounds. Missing files,
leaf symlinks, directories, FIFOs, oversized files, unreadable files, timeouts and
files changing during hashing are explicit incomplete evidence. Unknown evidence
never means unchanged. Permission changes count as drift. A known difference can
still be reported when another required input is unknown. Paths and hashes may be
sensitive metadata; queue evidence stays private to the local user.

These files supplement the whole Git observation, including its existing limits;
they do not replace it or rescue an oversized/unavailable checkout in a strict
plan. Capturing hashes does not freeze a live checkout or prevent a file changing
between capture and execution, or changing and reverting between observations.
Environment, undeclared inputs, remote state and container contents remain outside
this evidence. No immutable execution or overall productivity claim is implied.

A serialized, input-specific SQLite ownership guard rejects older controllers and
prepared workers before dispatching declared-input plans. The proof is renewed on
every takeover, so one current controller cannot authorize a future old one. Plans
without declared inputs remain compatible. Scheduler protocol 2 and protected quick
ordering are unchanged; this feature does not require another queue drain.
