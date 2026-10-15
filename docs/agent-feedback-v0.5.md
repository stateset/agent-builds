# Feedback after v0.4.0

Agents confirmed that read-only inspection works without escalation. The next
release addresses the concrete gaps they reported:

| Source | Report | Response |
| --- | --- | --- |
| Desktop | Following an active log raises `UnboundLocalError` | Remove the shadowing import and add a live-follow integration test. |
| Console Mobile | A running Android harness disappears behind five newer queued records | Sort running work first before applying result limits; use FIFO for queued records. |
| NSR | CPU wait reports totals but not the reservations using them | Include contributing jobs and their resource reservations in read-only diagnostics. |
| Sandbox | A detached job loses available Herdr pane attribution | Capture available pane/tab/workspace environment metadata at submission. |
| Sandbox | A 2-GiB job reports a 6-GiB admission requirement without explaining headroom | Expose the job reservation and the additional 4-GiB headroom separately. |
| Engine | Three reserved slots with explicit eight test threads is hard to reconcile | Describe environment defaults and direct Cargo worker options without rewriting them. |
| Desktop | A queued blocker reports zero runtime despite waiting over 200 seconds | Include wait duration and age separately from runtime. |
| Chat Widget | Empty filtered counts omit status keys and scope | Return explicit zero counts and a filter-scope description. |
| Response | Small checks can wait behind heavy work | Retained for scheduling follow-up; this release changes visibility, not admission priority. |

Contributor diagnostics distinguish their live observation time from stored
admission measurements. No running jobs are resized or restarted by this release.
Herdr metadata is informational rather than an authorization boundary.
