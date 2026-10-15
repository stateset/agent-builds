# Protect a checkout and shared outputs

Buildr v0.14 can acquire several named resource locks atomically. A job starts
only when all its keys are free and its ordinary CPU, memory, disk and lane
requirements fit. Waiting jobs acquire no partial locks and hold no running
reservations. This prevents deadlock from jobs listing keys in opposite orders.

```sh
buildr run --cwd /absolute/repo --lane heavy --jobs 3 --memory-gb 6 \
  --lock /absolute/shared-target -- cargo test --locked
```

The default primary key remains the Git checkout (or `CARGO_TARGET_DIR` when
that variable is set at submission). `--resource-key KEY` overrides it, as in
older releases. Repeat `--lock KEY` to add up to 15 additional keys. If the
primary key is already a target directory, add the checkout explicitly:

```sh
buildr run --cwd /absolute/repo --resource-key /absolute/shared-target \
  --lock /absolute/repo -- cargo test --locked
```

Keys are exact strings. Buildr does not resolve aliases, symlinks, parent/child
paths, shell variables inside argv, or tool output settings. Every participant
must declare the same key for a shared resource. Locks coordinate queued work;
they cannot protect against commands launched outside Buildr. Unique keys do
not prove that two commands have independent outputs.

Saved tasks accept the same repeatable `--lock` option. Plan stages and MCP
`buildr_submit`/`buildr_watch_submit` accept `"locks": ["shared-key"]` alongside
their existing `resource_key`. A durable MCP request key remains bound to the
complete original arguments, including locks. Plan drafts preserve declared
locks. A watch holds its locks only while its local probe has a reservation.
Remote protocol 1 does not support multiple locks; use the local queue.

`show --json`, status, MCP job results, TUI job details and contributing-job
snapshots expose `resource_keys`. Wait details name the blocking job and the
`conflicting_resource_keys`. Old rows expose only the primary key they actually
declared. Scheduler history records `quick-aging-locks-v3`; old loaded workers
keep their original policy and are not restarted.

## Upgrades with workers already running

Writable opens add lock metadata, a normalized lock table and a guard trigger
under a serialized migration. Read-only clients never migrate the queue.
Multi-lock jobs use an internal `held` state while waiting, exposed as `queued`
by current clients. This keeps them out of older workers' admission queries.

Overlapping legacy queued jobs drain before a new multi-lock job can start,
even when the legacy job was submitted later. While a multi-lock job runs, a
legacy insertion on an additional key is rejected before a job is created,
with an instruction to use the installed runner. Older workers already
understand its primary key. Independent legacy work can continue within the
same global budgets. Continuous overlapping legacy submissions can delay a
multi-lock job; restarting the submitting client onto the installed version
removes that compatibility exception.

Cancellation prevents admission without turning held work into a running
reservation. Live orphan children and uncertain Docker jobs retain all their
declared locks and reservations until confirmed stopped. No uncertain command
is retried automatically. Older dashboards may display the internal `held`
state; reopen the dashboard after upgrading for current counts and details.
