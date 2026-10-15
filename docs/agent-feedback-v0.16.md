# Fresh nine-agent feedback round

At the user's request, announcement `51e3fdfc60bf` asked all nine other live
Herdr agents for observed problems, job examples, proposed behavior and a way to
measure improvements. All nine inputs were submitted through native agent input;
actual replies were then collected, not inferred from delivery. Four replies
were visible directly (Response, NSR, iCommerce, Agents). Five needed a file
fallback, announcement `7487c94d62ad`, because their working alternate-screen
history could not be read. Those preserved replies are under
`/tmp/buildr-feedback-v016/`. No builds were interrupted or budgets changed.

| Feedback | Example / disposition |
| --- | --- |
| Eight respondents: actionable disk waits | Response `a8b0a1604e2e`, NSR `718d0e7d5eba`, Desktop `2f7ca7aa8811`, Mobile `00b711aaf4f8`, Sandbox `9ead8a4e6f93`, Engine `7dae5a5ee26a`, Agents `da9ee4a0d336`, Widget `cc44a9a20f8a`; diagnostics now shows exact deficit, components, contributing reservation handles and a cache inventory command; inventory labels ownership and unknown last use |
| NSR, iCommerce, Agents, Widget: explain protection draining | Specific jobs/controllers, loaded or unrecorded versions, exact conditions, request/activation timestamps for new requests; no automatic controller replacement |
| iCommerce: default status overwhelms the agent | Reported roughly 305,000 tokens from historical detail; default is now concise active jobs, with explicit detailed history and stable pagination |
| Sandbox, Widget, Mobile: short-check responsiveness needs evidence | Earlier short jobs waited minutes; completed ≤30s quick-check p50/p95 and native failures now separate by actual policy/admission cohort; censored waiting and maximum completed waits remain visible |
| Diagnostics follow-up: protected heavy draining still delays eligible scratch checks | A focused reproduction on v0.15 identified a global-budget reason masking the protected heavy-pool blocker. Policy v5 checks heavy-only feasibility and admits eligible quick work while that pool drains, retaining normal aging when quick work itself blocks the head |
| Response: fail prerequisites before admission | `5fe13a3ed25e` was a shell guard that waited roughly 1,243s; dependency plans already skip failed dependents without jobs. Added a regression with the queue paused and disk budget impossible, plus migration guidance |
| Engine, Desktop, Widget: avoid broad locks for independent checks | Existing exact resource keys/additional locks support explicit isolation. Added guidance for frozen inputs and independent outputs; no unsafe automatic inference of read-only commands |
| Desktop: one compact acceptance result | Existing `plan wait PLAN --summary --json` already records native/verification results, skipped stages, blockers and artifact evidence; documented the recipe |
| Engine: import and selectively rerun scripts | Existing `plan draft` preserves literal commands; automatic shell parsing or retrying failed/unknown work is deferred. New plans require explicit submission against reviewed inputs |
| Sandbox, NSR: external/scoped input fingerprints | A helper changed after submission; large source captures can be unavailable. Explicit external-input manifests remain a follow-up; repository-only capture limitations are documented, not silently widened |
| Mobile: watch freshness and version | Existing watch inspection records probe timing, next probe and native outcomes; new controllers now record loaded version. Provider completion time itself cannot be inferred from local polling |

Several respondents had inspected v0.15 but had not yet submitted through it.
Mobile had used batching and confirmed separate outcomes preserved 128 passing
tests while identifying a TypeScript failure. Prior-minute waits span different
versions and protection states. Repository failures, HTTP 403 responses and
AppImage harness issues were not presented as queue defects. The survey is
workflow evidence, not a causal performance benchmark or an A+ grade.
