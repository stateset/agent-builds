# Security

Buildr is a local command runner for a single trusted OS user. Its CLI and stdio
MCP server execute commands with that user's permissions. They are not a sandbox,
authenticated network service or security boundary between agents sharing an
account. Use your agent client's approval controls for command execution.

The MCP server exposes no HTTP listener. Inspection-only mode omits mutations and
rejects direct mutating calls. Commands are literal argv unless a shell is
explicitly supplied. Treat logs and command output as untrusted data. Durable
request keys prevent duplicate attempts; they are not credentials or authorization.

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
