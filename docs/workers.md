# Dedicated SSH workers

Remote workers are available only through explicit commands. No host is configured
by default. The application never reads infrastructure repositories, provisions
machines, connects in the background, or dispatches builds to production k3s.

On a dedicated build host, install Agent Builds v0.6.0 or later so that its Python
package is available to `python3 -m agent_builds.remote` in a noninteractive SSH
session. Prepare a checkout and its toolchains/dependencies yourself. Configure
the worker's queue budgets for its machine. Source files, credentials, environment
values and build outputs are not included in the application protocol or
synchronized. SSH authentication and explicit environment rules in your SSH
configuration remain under your control.

On the workstation, register an SSH alias already configured for that build host:

```sh
agent-builds worker add buildbox --host my-dedicated-build-alias \
  --state-dir /srv/build-user/agent-builds-state --dedicated-build-host
agent-builds worker list
agent-builds worker check buildbox
agent-builds remote run buildbox --cwd /srv/build-user/repo \
  --lane heavy --jobs 4 --memory-gb 8 -- cargo test
```

Registration only writes a private profile. `worker check` is an explicit network
operation. SSH uses batch authentication, normal host-key checking and the fixed
command `python3 -m agent_builds.remote`; variable arguments travel as JSON on
stdin. The remote checkout must already exist at the absolute path supplied.
RPC disables agent/X11/port forwarding and SSH LocalCommand through explicit
options; see the [OpenSSH configuration reference](https://man.openbsd.org/ssh_config).
The remote worker owns its own shared queue, resource policy, logs and timeouts.
Remote work does not consume the workstation's CPU reservations.

`remote run` returns a local dispatch handle and, when acknowledged, a remote job
ID. The remote runner detaches from SSH, so disconnecting does not cancel it.

```sh
agent-builds remote list
agent-builds remote status buildbox
agent-builds remote show DISPATCH_HANDLE
agent-builds remote logs DISPATCH_HANDLE --follow
agent-builds remote cancel DISPATCH_HANDLE
agent-builds remote reconcile DISPATCH_HANDLE
```

A dispatch is journaled before contacting SSH. If the connection or acknowledgement
fails, the result stays `unknown`; it is never automatically resubmitted. Reconcile
looks up the request token on the worker without starting anything. The worker
records a unique token before submission and refuses to execute the same token
again. If no job is found, the outcome remains unknown: inspect before deciding to
submit new work. Cancel and log commands also require a known job ID. Cancellation
still follows the worker queue's normal process/container reconciliation rules.

The TUI's remote view reads local dispatch records only; it does not poll SSH.
Use `remote reconcile` to refresh a dispatch record. Real-host execution must be
validated on the designated build machine before relying on it. The automated
suite exercises protocol, dispatch, disconnect and reconciliation with isolated
local fixtures and no live hosts.

## Native deadline guard (v0.22)

Local runners signal their own process group at the runtime deadline from a
separate watchdog, so a slow telemetry scan or blocked output forwarding cannot
delay the initial termination signal. SIGKILL follows the existing three-second
grace period. The runner stops and joins the guard before reaping the process
leader, preventing signals from reaching a reused process-group ID. This is
best-effort Linux process signalling, not a hard real-time execution limit.
Docker retains its existing stop and uncertain-outcome reconciliation behavior.
