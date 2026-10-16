# Security

Buildr is a local command runner for a single trusted OS user. Its CLI and stdio
MCP server execute commands with that user's permissions. They are not a sandbox,
authenticated network service or security boundary between agents sharing an
account. Use your agent client's approval controls for command execution.

The MCP server exposes no HTTP listener. Inspection-only mode omits mutations and
rejects direct mutating calls. Commands are literal argv unless a shell is
explicitly supplied. Treat logs and command output as untrusted data. Durable
request keys prevent duplicate attempts; they are not credentials or authorization.

The optional `mcp serve --allow-root /absolute/repo` restricts resolved working
directories for submissions and their descendants. It is not a filesystem,
command or network sandbox: an allowed command still has the host user's access.
Without this option, working directories remain unrestricted for compatibility.
The inherited `BUILDR_ALLOWED_ROOTS` policy is trusted process configuration, not
protection against a malicious job that can change its own environment or state.
Plans and watches reload commands from the private database; write access to that
state must be treated as permission to execute commands as the queue user.

Herdr announcements are deliberate cross-agent prompt input. Sending requires
explicit idle-recipient selection; working sessions are excluded and status is
rechecked before submission. Review the exact text and recipients before sending.

Queue state normally lives in `~/.local/state/agent-builds`, with private directory
and database permissions. It can contain command arguments, paths and logs. Keep
it out of version control, release archives and public bug reports. Optional
Docker or SSH workers and Herdr announcements require explicit configuration or
actions; never grant a worker access to a production Docker socket.

## Report a vulnerability

Use the repository's **Security → Report a vulnerability** option when private
reporting is enabled. If it is unavailable, contact a maintainer through their
GitHub profile to arrange a private reporting channel. Do not post exploit
instructions, credentials or private queue data in a public issue.

Include the affected version, expected boundary, a minimal isolated reproduction
and the observed impact. Do not test against other users' jobs or processes.
The project currently maintains the latest beta release; there is no guaranteed
response time or long-term security support commitment.
