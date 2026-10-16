# Contributing to Buildr

Buildr is a Linux, Python 3.8+ application with no runtime dependencies. Keep the
CLI and terminal UI usable without Docker, Herdr, a model provider or an SDK.
Discuss substantial scheduler/protocol changes in an issue before implementing
them. Bug reports should include the Buildr/Python version, a small reproduction,
the job state and wait reason, and expected versus observed behavior. Redact
private paths, command arguments, output and environment values before sharing.

## Development

Use a full Git clone with tags: compatibility tests load actual older releases.
Install in a virtual environment as described in the README. From the checkout,
queue meaningful verification once:

```sh
./buildr run --lane quick --jobs 2 --memory-gb 2 -- \
  python3 -m unittest discover -s tests -v
```

Configure reservations to fit your machine first. Do not nest this inside another
queued job. For an optional official MCP client check, install `mcp==1.30.0` in a
separate Python 3.10+ environment and queue its interpreter:

```sh
./buildr run --lane quick --jobs 2 --memory-gb 1 -- \
  /path/to/sdk-python tools/verify_mcp_client.py --output /tmp/new-mcp-report.json
```

Integration fixtures use private temporary queue state and clear the parent job
marker only for their deliberate isolated child submissions. Never point fixtures
at a user's queue. Running the tests must not contact Herdr agents, Docker daemons,
remote workers or production services.

## Changes to preserve

- Read-only inspection must not initialize state, migrate schemas or recover jobs.
- Preserve native results, unknown outcomes and durable one-attempt submission
  keys. Never retry uncertain work automatically or free a live orphan reservation.
- Keep global CPU/RAM/job budgets and atomic resource locks consistent across
  connections, controllers and loaded older runners. Add mixed-version coverage
  when changing a persisted protocol or scheduler policy.
- Local reservations are estimates. Do not describe worker hints as native hard
  limits or infer safe RAM reductions from incomplete sampled measurements.
- Do not broadcast, cancel unrelated work or change another agent's terminal.

Use focused tests for behavioral changes and document operational limitations.
Share measurements with their workload, policy, resource budgets, failures and
censored waiting; avoid claiming overall coding-productivity gains from a fixed
microbenchmark. CI runs Linux regression and optional MCP interoperability checks
through an isolated Buildr queue, then builds and validates Python distributions.
It also runs one plain unittest step so runner exit handling cannot hide a test
failure. Python 3.8 remains in the runtime matrix for compatibility; it no longer
receives upstream security fixes, so use a maintained Python for new installations.

Static checks use Ruff 0.17.1 and mypy 2.4.0 in a separate Python 3.12 development
environment. Queue `python -m ruff check --select E9,F63,F7,F82 agent_builds tests
tools` for correctness checks across the tree. New coordination modules also
have formatter checks and `mypy --follow-imports=skip --check-untyped-defs` checks
in CI. This is incremental coverage, not whole-project strict typing or formatting.

Contributions are made under the project's Apache-2.0 license. Preserve relevant
copyright and third-party notices. No contributor agreement or sign-off is
currently required.
