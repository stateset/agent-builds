# Agent Builds contributor notes

This is a standalone Python 3.8+ application with no runtime dependencies. Keep
the TUI usable inside Ghostty, Herdr, and tmux. Do not add a required Node or Rust
build just to launch the dashboard.

For meaningful changes, run the relevant tests. Full verification is:

```sh
./buildr run --lane quick --jobs 2 --memory-gb 2 -- python3 -m unittest discover -s tests -v
```

Don't nest queue wrappers. Integration tests use isolated state directories and
clear the parent's queue marker before deliberately testing child submissions.

Preserve compatibility with the live SQLite queue. Inspection commands must not
initialize state or mutate it. Migrations belong in writable opens and must be
serialized. Do not free reservations for live orphan jobs or uncertain Docker
outcomes. Never automatically retry work with an unknown result.

Do not alter running agents, stop builds, broadcast messages, or change Herdr
layout without user authorization. Announcements must distinguish input submitted
from policy adopted. Never auto-retry an uncertain message submission.

Local CPU/memory slots are scheduling reservations, not cgroup limits. Keep that
distinction visible. Docker limits must apply to the actual worker container.
Don't mount the Docker socket into jobs or route builds to production k3s.
