# Inspect a request before submission

Preflight compares an explicit reservation with the loaded scheduler, queue
ordering, locks and current host headroom. It never executes the supplied command,
creates a job/request key, reserves capacity, initializes missing state or migrates
an existing database. It can run inside an existing queued job for inspection.

```sh
buildr preflight --cwd /absolute/repo --lane quick --jobs 2 --compare-jobs 1 --memory-gb 2 -- cargo check
buildr preflight --cwd /absolute/repo --lane quick --jobs 2 --compare-jobs 1 --memory-gb 4 --compare-memory-gb 2 --json
buildr preflight --cwd /absolute/repo --lane heavy --jobs 3 --compare-jobs 2 --json
buildr batch preflight checks.json --cwd /absolute/repo --jobs 2 --compare-jobs 1 --json
```

Ordinary CLI preflight defaults to the heavy lane, matching `run`; specify the
quick lane for quick checks. MCP defaults to quick, matching `buildr_submit`.
Batches always use the local backend and quick lane, defaulting to one CPU,
1 GiB and a 60-second timeout. Otherwise CPU defaults follow lane configuration,
memory defaults to 6 GiB heavy / 2 GiB quick, and timeout follows configuration.
Supply the same working directory, primary `--resource-key`, repeated `--lock`,
`--min-free-gb`, `--scratch-gb` and other budgets intended for the eventual run.
An omitted primary key uses the same repository/output inference as submission.
Ordinary preflight also accepts `--backend docker` and `--image`.

Repeat `--compare-jobs` and `--compare-memory-gb` up to eight times each. The
requested budget appears first, duplicates are removed, and every unique CPU/RAM
combination is inspected. At most 25 combinations are allowed; larger requests
fail before host probes without creating anything. CPU-only requests retain their
existing ordering. Within each CPU count, requested RAM appears first, followed
by RAM alternatives in supplied order. The JSON `comparisons` object records the
unique dimension values, case count and limit. Each case includes its own `jobs`
and `memory_gb`; the top-level request retains the original RAM budget.

For example, two CPU slots and 4 GiB compared with one slot and 2 GiB produces
four cases: (2, 4), (2, 2), (1, 4), (1, 2). These are alternatives for **one job**,
not four simultaneous reservations. A case may fit when another does not, even
with the same CPU count, because global RAM or protected heavy RAM is full.
Each case preserves the other budgets, locks and command. Host RAM requirements
include the request plus configured available-memory headroom. CPU/RAM reservations
are scheduling estimates for local jobs, not process limits or measured needs.
Use repeated representative measurements and [advisor trials](advisor.md) before
choosing a smaller reservation; the comparison cannot establish its sufficiency.

For batches, `max_parallel` must fit the CPU count, and the report shows the worker
slots allocated to each simultaneous check. Explicit direct Cargo `-j` and Rust
test-thread flags can override those hints; warnings identify oversubscription
without rewriting commands or treating a warning as an admission rejection.
Shell wrappers, other tools and workload requirements are not inferred.

The JSON schema is `buildr-preflight-v1`. Each case includes:

- `submission_acceptable` and `submission_issues` for checked structural/budget
  rules, separately from transient capacity. Executable availability, Docker
  daemon readiness and actual workload sufficiency are not validated.
- `fits_now`: true if this hypothetical job would be selected, false if rejected
  or blocked, or null when the projection cannot determine this safely.
- `reason`, the selected competing job when known, exact lock blockers, global
  and protected-pool arithmetic, disk/RAM requirements and deficits.
- Optional command worker budgets or batch parallelism and worker warnings.

Queue ordering includes aged-job draining and conservative safe backfill; free
CPU slots alone do not establish eligibility. Running and cancelling work both
retain reservations. Disk requirements include global running scratch reservations,
the configured floor and the request's own scratch/minimum. Disk is measured on
both the working-directory filesystem and the existing state filesystem.

At most 200 active scheduling records are loaded. Truncation, incompatible queue
protocols requiring a writable upgrade, pending protected-policy activation and
unavailable host headroom produce unknown fits rather than optimistic admission.
Aggregate running totals still include all running/cancelling jobs. Missing state
uses defaults and an existing ancestor for its disk probe without creating files.
Large command/source evidence is excluded from the active scheduling query.

A successful inspection exits 0 even when a case is invalid or blocked. Malformed
arguments fail with exit 2. Consumers must inspect case fields, not use the exit
code as permission to start work. Queue state, policy and host measurements can
change before submission: this is no reservation, ETA or proof a smaller budget
is safe. Submit through the queue normally and retain its durable handle. Use
[strict verification plans](plans.md) for source evidence and dependency gates.

## MCP

`buildr_preflight` requires `cwd` and accepts optional `command` (literal argv) or
`manifest` (inline batch), plus `lane`, `jobs`, `memory_gb`, `timeout`, `backend`,
`image`, `resource_key`, `locks`, `min_free_gb`, `scratch_gb`, `compare_jobs` and
`compare_memory_gb` (up to eight positive finite RAM values; same 25-case limit).
Command and manifest are mutually exclusive; omit both for a resource-only check.
The tool is included in `buildr mcp serve --read-only` and needs no request key.

```json
{"cwd":"/absolute/repo","lane":"quick","jobs":2,"compare_jobs":[1],
 "memory_gb":4,"compare_memory_gb":[2],"command":["cargo","check"]}
```

Reconnect an already running MCP client to discover the added tool. Inspection
does not reconnect or change other agents. See [MCP setup](mcp.md).
