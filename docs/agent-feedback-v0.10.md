# Fresh feedback round for v0.10.0

All eight other live Herdr agents received and replied to the fresh user-requested
survey. Announcement `5869ec3ac554`; four replies required file fallback because
Herdr cannot scroll a working agent's alternate-screen history. Fallback request
`e620d8f80a4c` targeted only those four agents. Captured reports are under
`/tmp/agent-builds-feedback/v0.10.0/`. Submission alone was not treated as a reply.

No respondent established a new v0.9 queue correctness failure. Several timed
examples used older `quick-aging-v1` runners and are not controlled comparisons.
None of the respondents had yet evaluated dependency plans in depth.

| Feedback | Evidence / disposition |
| --- | --- |
| Engine, Mobile: collect existing handles without resubmitting | Durable native-job observation groups; no invented dependency or artifact guarantees |
| NSR, Engine: migrate known harness jobs into plans | Draft literal argv/resources in caller order; explicit present-time harness hashes; no shell parsing or execution |
| Engine, NSR, Mobile, Desktop, Widget: legacy observations obscure current telemetry | Cohorts by recorded policy, loaded version, wait instrumentation and lane; consistent `--policy current` filtering; old versions remain unrecorded |
| NSR: tiny checks need an optional concrete one-slot trial | Repeated successful <=10s quick checks can receive a one-slot trial; no unsupported memory reduction |
| Widget: validate/build/browser/cleanup migration | `when: always` cleanup after settled prerequisites; original failure retained; global cancellation and uncertain results remain authoritative |
| Desktop: concise stage outcomes and copyable migration | Versioned compact summaries plus documented npm migration recipe and evidence semantics |
| Response: worktree-aware advice and install/commit/gate/audit/image recipe | Common-Git-family aggregation and a broader release recipe remain follow-ups; inferred grouping must not mix unrelated repos or claim old source identities |
| Sandbox: external cloud completion | Submission success remains distinct from external completion; requires an explicit external-wait result model |
| Agents: remote startup holds CPU slots | Example `437930701d63` held two slots during roughly nine minutes of startup; phase-aware waits are a subsequent implementation priority |

The user additionally requested controlled queue-versus-independent-process
measurement, AI-assisted optimization, phase-aware waiting and multiple explicit
resource locks. These are active subsequent work, not v0.10 capabilities claimed
by this feedback release. A+ outcome improvement has not been established merely
by the survey or simulated scheduling results.
