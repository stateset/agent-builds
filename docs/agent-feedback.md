# Agent feedback and resulting behavior

Early feedback from agents sharing a development workstation shaped these
features. This summary omits private repository names, local paths, job handles
and delivery records from the original operational notes.

| Request | Result |
| --- | --- |
| Explain disk waits | Diagnostics show the deficit, reservation contributors and an explicit cache inventory; no automatic deletion |
| Keep routine status small | Active-job summaries and bounded history, with explicit detailed inspection |
| Keep short checks responsive | Protected quick capacity, activation/draining diagnostics and policy-specific latency observations |
| Preserve each check's outcome | Sequential or parallel batches, native results and compact plan acceptance evidence |
| Skip checks after failed prerequisites | Dependency plans skip dependent stages without creating waiting jobs |
| Avoid blocking independent outputs | Explicit resource keys and additional locks; no automatic inference that commands are read-only |
| Observe long external work without holding build slots | Explicit repeatable completion watches with bounded probe history |
| Detect external helper/config changes | Declared input observations with source guards and clear capture limits |
| Reconnect safely | Durable submission keys, retained handles and no automatic retry of uncertain outcomes |
| Reduce coordination overhead | Numbered migrations, cached settings, shared recovery and read-only admission wakeups |
| Keep cross-agent messages deliberate | Explicit idle-recipient selection and a final session/status recheck |

These are usability observations, not a controlled measurement of coding
productivity. Mixed-version feedback previously exposed a scheduling cycle that
single-version tests missed; actual old-release compatibility fixtures now guard
that behavior. See [quality gates](quality-gates.md) and
[workflow studies](workflow-studies.md) for validation methods and limitations.

This cleanup changes the current documentation only. Earlier public commits and
release tags retain their original files; it does not erase publication history.
