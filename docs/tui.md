# Buildr dashboard

Run `buildr tui` in Ghostty, Herdr or another terminal. The dashboard adapts to
split panes down to 20 columns by 8 rows. `q` closes it while jobs continue.
Keyboard input is checked every 200 ms. Jobs/settings, host metrics and the global
queue summary refresh independently in the background, at most once a second
for jobs/metrics and every two seconds for the summary after a completed read.
Each channel permits only one in-flight read. A slow metric or filesystem probe
does not hold up job snapshots, navigation or quitting. Displayed state can lag;
it is not an admission decision.

Startup paints a loading screen while the first queue read is pending. Monitoring
uses read-only connections and does not initialize/migrate an absent queue or run
recovery sweeps. Missing state is labelled; explicit queue actions can initialize
it. Each worker owns and closes its connection. Help suspends baseline refreshes.
Successful data remains visible after a failed refresh with a stale label;
initial errors and unknown global reservations are explicit. Filtered or bounded
browser rows are never used to estimate global reservation totals.

Processes, plans, groups, external watches and advisor reports show the age of
their accepted snapshot and whether a refresh is pending. A retained snapshot
older than two refresh intervals is labelled stale even when the reader has
not returned an error. A completed refresh clears the age warning. Supported
narrow tool views retain this status without replacing the selected handle;
their compact heading uses `~` for a pending refresh.
These labels describe observations; they do not change job state or scheduling.

Deliberate actions open separate writable access. Pause, budget editing, plan
validation and new-job defaults reload configuration at action time, so a stale
display does not determine a toggle or overwrite current defaults. Queue lifecycle
recovery stays with runners/controllers and the explicit recover command.

The job browser reads lightweight rows rather than decoding full evidence for
every loaded job. Full source, telemetry, batch results and wait history load
only for the selected job while details are visible, including in narrow panes. Inline wait
observations load separately for the selected job in the full split view;
expanded tables, narrow monitors, help and focused logs do not request them.
Selected reads run in their own background channels, refresh at most once a
second after completion and are released when hidden. Late evidence for a
previous selection is discarded. A failed selected read clears its evidence and
shows an error; an in-flight refresh can retain the previous accepted snapshot.
Filtering and sorting reuse the same display snapshot until the jobs or local
controls change. Selection and scrolling do not repeat that work.
Unavailable details show a read error and retry on refresh, without displaying
another job's cached evidence. Job details, workbench and wait diagnosis keep
their original job when it leaves the browser snapshot. Explicit job navigation
or filter changes in details select another job; passive refreshes do not.
Confirmations and logs opened from details use that inspected job. Checkout
history likewise keeps its checkout until you leave the view.

| Key | Action |
| --- | --- |
| `V` | Find and open a dashboard view; Esc returns |
| `J` | Open a log by its full 12-character job ID, from any view |
| `?` | Open searchable help; Esc, `?` or `q` returns without taking an action |
| `j` / `k`, arrows | Select a job |
| `F` | Toggle following the first matching row in the jobs view |
| PgUp / PgDn, Home / End | Page through jobs or select first/last in the jobs view |
| `/` | Search job ID, repository, command, wait reason or Herdr pane; blank clears; Esc cancels |
| `f` | Cycle all, active, waiting and problems in jobs/details views |
| `R` | Toggle focus on the selected job's exact checkout; press again to remove focus |
| `C` | Clear repository focus, search and state filter; retain order and selected job |
| `s` | Cycle display order: queue, wait, runtime, repository; retain selected job |
| Tab | Toggle an expanded job table in the jobs view |
| Enter or `i` | Toggle the selected job’s details in full or narrow panes |
| `w` | Why waiting? Selected-job diagnosis; `w` returns to jobs |
| `g` | Jump to the selected job's recorded blocker |
| `l` | Focus the selected job’s log; `l` returns to jobs |
| `H` | Selected-checkout history; `H` returns to jobs |
| `u` | Queue health |
| `U` | This dashboard's submission attempts and retained receipts; `U` returns to jobs |
| `r` | Selected-job workbench |
| `v` | Cycle views |

In Plans, `/` searches the loaded snapshot by plan ID/name/state/reason, stage
name/state/job ID, and confirmed job command/reason/checkout. Matching is literal
and ignores case. Blank clears the search; Esc retains it. Counts cover only
the loaded plans, and a `/` marker in narrow headings indicates an active filter.
No matches shows guidance instead of claiming there are no plans. A matching
selection stays pinned across refreshes and clearing the search. Search does not
change job-browser filters or fetch additional plans.

Selected stage headers put the recorded stage state before the name, so it
remains visible even with long names in a 20-by-8 pane. The exact log handle and
snapshot freshness stay visible beside it. This is the stage outcome: evidence
validation can fail while the native job exits zero, and a lost stage remains
an unknown outcome. A failed refresh retains the last observed state with a
stale warning.

In Plans, `]` jumps to the next stage explicitly marked failed or lost; `[`
jumps to the previous one. Navigation wraps in recorded stage order, skips
successful/pending/skipped/cancelled stages, and opens no logs automatically.
Without a failed/lost stage, or with only the current stage matching, selection
and reading position stay unchanged. A lost stage retains its unknown outcome;
a stage may fail evidence checks even when its native command exited zero.
Press Enter separately to inspect the selected recorded job log.

In Plans, arrows or j/k select a plan. Tab/Shift+Tab cycles its stages and the
overview; Enter opens the selected stage's recorded job log. The overview keeps
the existing preference for a live stage, then a failed stage, then a recent
submitted stage. A selected stage without a confirmed job shows “No recorded
log” and never opens another stage's output. Selection follows the stage name
across snapshot refreshes and reordering; removing the stage or changing plans
returns to the overview. `b` returns from the log to the selected stage. These
controls also work in a 20-by-8 split and do not submit or retry stages.

Selecting a stage focuses its evidence: state, recorded reason, job outcome,
wait/runtime, command and source warnings. Long lines wrap instead of clipping.
PgUp/PgDn and Home/End scroll these bounded details, while the stage name, exact
log shortcut and snapshot freshness remain visible. Taller narrow panes show the
same detail page; at 20-by-8 the stage/log controls take priority. Resizing keeps
the reviewed text position; selecting another stage or a new recorded job starts
at its beginning. The overview retains all stages and plan controller warnings.

In `U` submissions, Tab/Shift+Tab cycles the overview and individual receipts,
newest first. The selected receipt's number stays visible; j/k, paging and
Home/End scroll its details. Selection remains pinned as other attempts update
or arrive. Cycling past the last receipt returns to the full overview.

Enter opens the selected confirmed **job** log through a background read, even
outside browser filters/history limits or while the initial job list is pending.
The overview, plans, starting attempts, invalid confirmations and unconfirmed
outcomes never open a job or retry work. The pinned action hint shows the exact
job ID when Enter is available. Help defers a pending handoff; changing receipt
or view abandons it. A missing job displays an error without retrying. Returning
to jobs preserves filters; reopening `U` keeps receipt selection. Both selection
and log opening work at 20x8. Plans remain inspectable via the view picker.

`V` opens a searchable view picker in full and narrow panes. Type to filter
names and descriptions; Backspace edits and Ctrl+U clears. Arrows or Tab/Shift+Tab
move through results, PgUp/PgDn page, and Home/End select first/last. Enter opens
the highlighted view; Esc returns. Inside the picker, letters including `q`,
`n`, `c` and `p` are search text. They never quit, submit, cancel or pause work.

Inside help, `/` finds instructions using literal, case-insensitive text. Search
matches complete instructions before wrapping, so a phrase can span screen lines.
Blank input clears the search; Esc cancels the prompt and preserves the current
query and position. j/k and arrows scroll, PgUp/PgDn page, and Home/End move to the
first/last page. Query and position survive leaving and reopening help.
Resizing retains the instruction and character position, even when a taller
pane temporarily moves the top upward to fill the view. Manual navigation or a
new query replaces the retained position. A missing
match is labelled explicitly. Help search is independent of job/log filters and
remains available while background reads are stalled. Help consumes action keys
and performs no queue writes. Its wrapped layout is reused across idle frames,
scrolling and height changes; width or query changes rebuild it.

The picker marks the current view with `*` and unavailable views with `!`.
The selected description explains its purpose or requirement. Full-pane tools
require 65 columns by 15 rows; details, logs, workbench and wait diagnosis require a selected
job. An already open log remains available even if its job leaves the browser.
Opening details, workbench, diagnosis or history from it keeps its job/checkout.
An unavailable selection stays in the picker until you resize or choose another
view. Below 20x8, only Esc is accepted; query and selection survive resizing.

Cancelling preserves the current view, following mode, job selection and held
log position/search. Opening an inspection pins a followed job. The picker
performs no inspections itself; periodic dashboard reads resume after it closes.
Selecting a view never dispatches a build, sends an announcement, connects to a
remote worker or applies advice. Existing deliberate action flows remain in
their views. Lowercase `v` retains its existing cycling behavior.

When a full-pane tool is resized into a narrow pane, it shows its name and size
requirement rather than unrelated job output. `V` chooses an available view;
`v` returns to jobs. Navigation keys do not move the hidden job selection.
Footer hints adapt to width, retaining view-picker and quit controls without
clipping them or advertising full-pane actions in narrow panes.

Search is a literal, case-insensitive substring of one field; it does not read
logs or source evidence. At most 256 search characters are retained. The job
browser covers the bounded 200 loaded rows, not the complete history. Its label
shows matching and loaded counts. Problems include failed, lost and timed-out
jobs; deliberate cancellations are separate. Global queue health and reservation
totals remain global when jobs are filtered. Narrow panes put the selected wait
reason before repository details and logs. The smallest pane puts the full
selected job handle before its state so the handle stays visible.

`F` enables optional following in the jobs view. `JOBS FOLLOW` (or `FOLLOW` in
narrow panes) marks the mode. Each refreshed snapshot selects its first row
after the current filter, search, repository focus and display order. This is
the first displayed row, not necessarily the newest submission or next job to
start. An empty result stays in follow mode until a matching row appears.

Following starts disabled. Press `F` again to retain the current selection.
Other input also stops following, including navigation, changing browsing
controls, shared actions and opening details/logs. Tab, terminal resize and idle
frames keep it enabled. Help temporarily suspends following and consumes `F`;
returning from help resumes the previous mode. This only changes this dashboard's
selection; it never pauses, reprioritizes or starts queue work.

Repository focus combines with search and the state filter in jobs and details.
It compares the exact recorded checkout path: similarly named repositories,
subdirectories and other worktrees are separate. The heading marks focus with
`@checkout`; full panes also show the complete path when focus is selected.
Counts still compare matches with all loaded rows, at most 200; this is not a
complete repository history. Global health and reservation totals stay global.
Focus survives refreshes and returning from logs. `R` removes it even when no
jobs match; `C` also clears search and the state filter without changing sort
order or the expanded-table setting. Both retain the selected job when it
remains in the result. These keys are ignored inside help and focused logs.

Tab gives the job table more of the pane by hiding the inline log tail. Larger
panes show CPU/RAM reservations, wait and run durations, repository and command.
CPU is reserved slots and RAM is GiB, matching the global reservation totals.
Narrower tables retain job ID and an abbreviated state. Page keys move by the visible table
capacity, and the selected row remains visible. Tab restores the split with
inline output; `l` opens focused logs without losing the expanded-table setting.

`s` changes only the ordering of the loaded display rows. Queue order preserves
the default browser order; it is not a forecast of start order. Wait order puts
waiting jobs first, longest observed wait first; runtime order puts running and
cancelling jobs first, longest observed run first. Repository order sorts by full
checkout path, ignoring case. Ties use creation time and job ID. Sorting preserves
the selected job across refreshes. Filtering/search still applies, global health
remains global, and the browser still covers at most 200 loaded rows. Display
sorting never changes admission priority or the order in which commands execute.
Wait and run values are observed durations, not completion estimates.

Browsing controls change only this dashboard. The help view consumes action
keys, so typing `p`, `c` or `n` there cannot pause, cancel or submit work. Outside
help, full-view controls include `p` for shared new-start pause, `n` for a new
job and `c` for cancellation with a typed confirmation. Budgets, plan submission,
saved tasks and explicitly selected idle-recipient announcements remain available
through their existing deliberate flows. Read help before taking a shared action.

## Why a job is waiting

Press `w` from jobs or details, or find **Why waiting?** with `V`. The report
shows the inspected job's recorded wait reason and its observation age, current
global and lane reservation arithmetic, host RAM, disk requirements and deficits,
and recorded blocker links. Unrecorded and stale wait evidence is labelled.
A running blocker is not labelled stale merely because it has no queued wait
observation. Terminal jobs show their recorded outcome; missing exit evidence
stays explicit.

These are observations, not a predicted start time or a new admission decision.
CPU slots and RAM reservations are estimates, not hard host limits. Free capacity
alone does not establish that a job can start: locks, priority, protected quick
capacity, pause and uncertain workers also matter. Reports do not change jobs,
reservations, settings or files.

Use j/k, arrows or PgUp/PgDn to scroll, Home/End for first/last, `i` for the
inspected job's details, `l` for its log, and `g` for the first recorded blocker
log if present. `w` or `v` returns to jobs. Shared action keys are consumed in
this view. The full job handle stays visible even in the 20x8 monitor.

Why waiting and queue health refresh every two seconds after completion;
workbench refreshes every three. These three views share at most one in-flight
background read on a separate read-only connection. Navigation and quitting do
not wait for it. Changing view/job discards a mismatched result; help, hidden
views and undersized panes do not start reads. Failed refreshes label retained
reports stale; initial errors wrap in the body. Report formatting/wrapping is
cached and bounded at 64 Ki characters, with an explicit clipping notice.
`buildr diagnose JOB`, `buildr diagnose`, and `buildr workbench JOB` provide
CLI reports. The remaining periodic tool views (processes, announcements, remote records,
plans, advisor, groups and watches) also load only while visible, in background
workers. Plans and watches distinguish unverified controller liveness from an
observed stop. Read failures remain visible even for previously empty reports.
Read-only disk scans run only on `d` in an owned helper process. Closing the
dashboard stops that helper and its `du` descendants without signalling queued
builds. The helper uses a separate read-only connection and bounded output. Explicit
action flows and bounded local log reads retain their own behavior.

## Command history

`H` opens history for the selected job's checkout (or the dashboard checkout if
no job matches). It inspects the latest 1,000 matching jobs and groups completed
jobs by command and reservations, showing p95 queue wait and average runtime.
The complete checkout path wraps with the results. Global reservations remain
visible; suggestions never change configuration automatically.

History supports narrow panes: j/k or arrows scroll lines, PgUp/PgDn page,
Home/End select the beginning/end, and `H` returns to jobs. Help and
quit remain available while loading. Shared action keys are ignored in history.

Reads run on a separate read-only connection, with at most one in flight and a
three-second refresh interval after completion. Hidden history and help do not
start refreshes. The history worker never delays process exit when quitting.
Changing checkouts discards the previous checkout's result.
Failed refreshes label retained results as stale; an initial failure displays
an error. Formatting and wrapping are cached between observations. Oversized
history text is capped at 64 Ki characters with an explicit clipping label;
`buildr stats --cwd PATH` provides the full result.

## Submission confirmation

In the full jobs view, `n` launches a new command and `t` launches a saved task.
The first notice is **Starting**, not a claim that a job exists. A detached helper
records the attempt and CLI transcript privately. The dashboard checks its
receipt and matching queue row in the background before displaying **Submitted
job ID** or **Created plan ID** for a reviewed task. Creation does not establish
that a worker has attached, a command has started, or checks have passed.

`U`, also available in the view picker, shows this dashboard's retained attempts,
handles, details, receipt paths and transcript paths. Multiple simultaneous
outcomes remain available there. It supports narrow panes, j/k, paging, Home/End,
help and quitting. Shared action keys do nothing in this view. A compact notice
in the jobs monitor points back to `U` after resizing.

A helper exit or delayed confirmation without a verified receipt is **Outcome
unconfirmed**. Inspect the retained receipt, transcript and queue before deciding
what to do. Confirmation reads continue so a late receipt can resolve the attempt;
the dashboard never resubmits it automatically. Closing the dashboard does not
stop a helper, cancel a job, or wait for a stalled read. Private records remain
in the queue state directory after closing; the view covers attempts made by the
current dashboard session, rather than a complete historical submission index.

## Input prompts

Search, command entry and confirmations open a focused input screen. The full
question wraps above the input; Enter accepts, Esc cancels and Ctrl+U clears.
Long values show their tail while retaining the complete entered value.

Resizing preserves the text already entered. If the full question and controls
cannot fit (at least 20 columns by 8 rows), the prompt shows `Resize to edit`.
Typing, deletion and Enter are ignored until the pane has enough room again;
Esc still cancels, even in a tiny pane. This prevents accepting a confirmation
whose question is hidden. The prompt itself never changes shared queue state;
the existing action runs only after its answer is accepted and validated.

Cancellation prompts retain the selected job's checkout, command, state and
resource request. Announcement selection and confirmation keep their recipient
preview visible, including pane IDs and checkouts. PgUp/PgDn pages this context
without changing the input; the `Snapshot` label shows the visible range.
Context is captured when the prompt opens, so job and recipient states may
change while you read it. Existing action validation still applies.

Context wraps to the pane width and retains at most 16 Ki characters, with an
explicit clipping notice. Use `buildr show JOB_ID --json` for full job evidence
or `buildr announce` for a fresh recipient preview. Context prompts require
enough room for the complete question, input controls and at least one context
row. Resizing preserves the entered value and clamps the context position.

## Job details

Enter or `i` opens the selected job's evidence in full or narrow panes. Long
commands, checkout paths, wait reasons and source evidence wrap to the pane
width, including wide Unicode characters. The header shows the visible row
range and total. PgUp/PgDn scroll one visible page; Home/End jump to the first
or last wrapped row. `j`/`k` still select jobs and reset the details position.
Enter or `i` returns to jobs; `l` opens the selected log. Repository focus,
search and sort settings stay in effect.

Details include the recorded job exit, checkout and reason. Queue timeout and
cancellation use 124/130; these are job results, not necessarily native process
exit codes. A missing exit
remains "not recorded" even for a terminal state. Selected reads refresh at most
once a second. Formatting is reused until that snapshot changes; wrapping is
reused until its content or pane width changes. Resizing retains the logical line and character position, including after a
taller pane temporarily moves the top upward to fill the view. Manual paging,
Home and End replace the retained position. Hidden or failed reads release
cached evidence while retaining the position for that job; changing jobs resets it. Help and hidden views release cached evidence; read errors cannot
show another job's old details. Narrow panes keep shared actions disabled.

Rendered details retain at most 64 Ki characters plus a clipping notice. This
bounds wrapping work, not the underlying selected database payload. Use
`buildr show JOB_ID --json` for the complete evidence when the notice appears.

## Focused log inspection

Press `J` to paste a full job ID from a CLI result or submission receipt. The
lookup runs in the background and works outside the browser’s 200-row window
and filters, including from an empty browser or a full-only view resized narrow.
A delayed initial job-list read does not block direct log inspection.
Uppercase hexadecimal IDs and surrounding whitespace are accepted; abbreviated
IDs and command lines are rejected. Esc or a blank value preserves the current
view and held log. While loading, help defers the handoff and navigation abandons
it; passive browser refreshes do not change the requested target. A missing job
shows an error without retrying. `l` returns to jobs with its filters preserved.

Enter in plans, groups or external watches opens the relevant stage, member or
latest probe log. `g` from jobs/details opens the recorded blocker's log; the
wait inspector also exposes `g`. These links resolve exact job handles in the
background, including jobs hidden by browser filters or outside the 200-row
window. Opening a linked log leaves browser filters and selection unchanged.

While a link is loading, its originating view stays visible with an opening
notice. Navigation and quitting remain available. Leaving the originating view
or changing its selected item abandons the pending handoff; a late result cannot
switch you back. Help defers the handoff until it closes. A failed lookup shows
an error and stays in the originating view; it is not retried automatically.
Plans, watches and blocker links work in narrow panes; groups retain their
existing full-pane size requirement.

`b` returns from a log to the view that opened it, including its selected receipt,
plan, group, watch or pinned inspected job. `l` returns directly to jobs.
The source is recorded only when a log opens successfully; cancelling or failing
a lookup leaves it unchanged. A `J` jump from an already open log keeps the same
source rather than building a navigation stack. Help consumes `b`, and panes
smaller than 20x8 ignore it. Returning to a full-size-only source in a narrow
pane shows its size requirement. Views refresh normally after returning; this
preserves the source identity, not a frozen copy of its evidence. Missing tool
items follow their view's existing selection rules.

Focused logs refresh the pinned job's state on a separate read-only connection,
including after it leaves the 200-row browser. Holding or searching output does
not hold the job's status. The header retains its last-known state while showing
`State loading` or `STALE state` when confirmation is pending, old or unavailable.
This state label is independent of log-content freshness. Metadata reads stop
while the log is hidden, help is open, or the pane is smaller than 20x8.
Changing views or selections discards earlier read generations, so reopening a
view cannot label a pre-navigation result as a fresh inspection.

Press `l` from jobs or details, including in a narrow pane. `j`/`k` and arrow keys
scroll one wrapped row; PgUp/PgDn scroll a page. Home shows the oldest **loaded**
output. Space holds a fixed output snapshot; End or Space resumes live output.
Holding output never pauses the shared queue. The selected log stays attached to
its job even if completion removes that job from the active filter. `l` returns
to jobs; `q` closes the dashboard. Shared action keys are ignored in log focus.

The viewer loads at most the recent 256 KiB and labels omitted earlier output.
Use `buildr logs JOB_ID` or the job’s log file for the full history. Changed logs
refresh at most twice a second. Metadata-only observations reuse existing output,
with a bounded content recheck on the first visible poll after five seconds
since the last accepted content read. This catches same-size rewrites even when
metadata is unchanged. End requests fresh content immediately; metadata polls,
failed reads and discarded results do not postpone the recheck. Successful
content reads with identical sanitized text reuse the focused viewer’s search
index and wrapped layout. They still update content freshness, file identity,
clipping and error/empty-state labels; actual text changes rebuild the caches.
This saves rendering work without skipping the periodic content read. Held snapshots
do not start new log-file reads. Following handles file replacement, truncation and
missing files. Lines wrap to the pane width; ANSI control sequences are removed
and carriage-return progress uses the last segment of each line. This is a text
viewer, not a complete terminal emulator.

Focused and inline logs share one background reader. Metadata checks, opening,
reading and decoding happen in that worker; keyboard input and rendering use
only accepted snapshots. A pending read never starts a backlog or delays quit.
Loading, unavailable output and stale refreshes are labelled. An empty focused
viewer shows `Loading log` before its first read, `Log unavailable` after a read
error, or `Empty log snapshot` only after accepting empty content. Holding before
a read completes shows `No captured output`. The messages fit at 20x8 even when
job-state loading or freshness occupies the status row. They describe the retained
snapshot, not whether a job will produce output; they are not log lines and do
not enter search results or row counts. Actual blank log lines remain log lines.
Help, hidden logs,
expanded job tables and panes without room for inline output do not start reads.
An already pending read may finish, but changing job, view, visibility, hold or
search intent discards its result and error. End resumes from a new observation.
Resizing rewraps the loaded snapshot without reading the file again. Held
output retains its logical line and character position as width changes, so
repeated narrow/wide resizing does not drift to unrelated output. A taller pane
may temporarily move the top upward to fill the screen; shrinking it restores
the inspected position. Manual scrolling or Home establishes a new position;
End resumes following. Selected search matches remain the resize target. These
anchors cover only the loaded snapshot and do not survive selecting another job.

In log focus, `/` searches literal text, ignoring case, across the loaded logical
lines (including text spanning a visual wrap). Enter accepts, Esc leaves the
existing search unchanged, and a blank query clears it. Search holds the current
snapshot and reveals the first occurrence in the first matching logical line,
including hits deep inside wrapped lines. Resizing keeps that occurrence visible.
`n`/`N` move to the first occurrence in the next/previous matching line, wrapping
at either end; after manual scrolling they use the hit's screen position rather
than the beginning of its logical line. Multiple occurrences on one line count
as one match. Matching lines are highlighted, with the selected match reversed.
The header shows the visible wrapped-row range and total; the search label shows
the selected match and matching-line count. Searches cover the recent loaded
256 KiB, never the entire log. Queries are limited to 256 characters. Clearing
search keeps the current live/held mode; End resumes live output. Job-browser
search and log search are independent and do not change shared queue state.
Search uses the same resize-aware input prompts described above.

The system process list is sampled only while its view is open. Other views
retain their existing one-second job/host and two-second queue-health refreshes.

Inline log tails retain at most 64 KiB for one selected job. Metadata is polled
at most twice a second; unchanged metadata reuses the content snapshot, with a
content recheck on the first poll after five seconds for rewrites that preserve
metadata. Selection
changes clear the prior job's snapshot immediately, and resize can reuse its
lines. Detected appends, replacement, truncation and same-size rewrites refresh
on the next poll. Nonregular paths are rejected without waiting for a FIFO writer, and
reads have a fixed byte limit even if a log grows while being read. Visible rows
remove ANSI decoration before painting. Expanded tables and help skip inline
reads; help over focused logs also skips polling that hidden snapshot. Returning
to a live view resumes normal refreshes. These caches affect only this dashboard.

## Reproduce the cache comparison

```sh
buildr run --lane quick --jobs 1 --memory-gb 2 --timeout 120 -- \
  python3 tools/verify_tui_cache.py --output /tmp/new-tui-cache.json
```

The developer tool compares the actual v0.25.0 package with the current source
in both orders. Each arm processes 300 simulated 5 Hz frames and 200 synthetic
display rows renewed once a second, with unchanged or once-a-second growing
logs. It checks settled output and display order, counts reads/bytes and
filter/sort calls, and retains CPU/wall observations plus source identity.
No fixture command executes and the global queue configuration is unchanged.
Reports require a new path outside the checkout. CI independently verifies the
report and retains it as `tui-cache`. Timings are observations, not thresholds;
this measures selected hot paths, not terminal frame rate or coding productivity.

## Repeatable terminal observations

The repository's native terminal check uses private pseudo-terminals at 100x24
and 20x8, while a real background queue read remains deliberately stalled. It
opens and closes help repeatedly, records key-to-screen times, help wrapping
calls, quit time and host pressure, then reverses baseline/candidate order.
It submits no fixture jobs, preserves missing private queue state, and never
touches real Ghostty or Herdr panes.

Use clean source checkouts, a full clone with tests, and a new report path outside
both trees. The entire observer belongs in one shared reservation:

```sh
git worktree add --detach /tmp/buildr-tui-baseline v0.26.1
buildr run --lane quick --jobs 1 --memory-gb 2 --timeout 180 -- \
  python3 tools/verify_tui_responsiveness.py \
  --baseline-checkout /tmp/buildr-tui-baseline --output /tmp/tui-response.json
```

Reports retain completed arms and the current unconfirmed arm when a read,
terminal, source check or catchable interruption fails. SIGTERM follows the same
owned-child cleanup and report path. There is no automatic retry or
overwrite. A changed or dirty source cannot produce a passing result. Timings
include process scheduling and terminal parsing; p95 uses nearest rank over
22 key-to-screen samples per arm, with quit measured separately. CI retains
these observations and checks native completion and isolation. Passing proves
the controlled interaction completed; it sets no timing SLA and does not prove
agent productivity or statistical significance. Initial loading, waiting for
queue admission and terminal startup are outside the key-response samples.


Report views retain the logical text position while pane width changes or a
taller pane temporarily clamps scrolling. This applies to queue health, why,
workbench, history and submission receipts. Manual scrolling, Home and End
replace that position; changing the report's scope resets it. Refreshed content is clamped within the current report and may change the text
at that logical position. Rendering and
height-only resizing use cached text; resizing does not request fresh evidence.


Press `/` in health, why, workbench or history to filter already-loaded report
lines with a literal, case-insensitive query (up to 256 characters). This is not
a regular expression and does not change job filters, selected jobs, saved trials
or backend queries. The status line shows the query and matching logical-line
count, independently of wrapping. Use j/k, PgUp/PgDn and Home/End to browse the
filtered text. Entering a new or blank query starts at the top; blank restores
all loaded text, and cancelling preserves the current query and position.

Queries stay with each report widget and clear when its checkout/job/candidate
scope changes. Refreshed snapshots update matches from current text. Pending
reads show a pending search; read errors stay visible even when their text does
not match. Search is limited to retained report text, including its existing
64 Ki-character clipping rules, and never loads omitted evidence.

Report content distinguishes loading, an accepted empty report and a search with
no matching lines, even at 20x8. Empty-search guidance is display-only: it never
counts as a report line or search match. Press `/` to edit; blank restores text.

Input prompts support Left/Right, Home/End (also Ctrl+A/Ctrl+E), Delete and
Backspace at the cursor. Ctrl+U clears the value. Inserting text keeps the rest
of the command or query. The visible window and cursor account for wide
characters and resize without truncating the stored value. Editing uses Unicode
codepoints rather than a full grapheme editor. Enter explicitly accepts; Esc
cancels. Hidden prompts ignore editing and acceptance until they fit again.

The queue banner fits its text to the pane instead of clipping the disk gap.
`DISK: +6.2GiB` means the smallest waiting request needs another 6.2 GiB on the
shared-state filesystem (`G` abbreviates GiB when space is tighter); `DISK: 1/3` means that filesystem currently limits one
of three waiting jobs. Wider panes show the largest-request gap for partial
limits. Other job filesystems may have additional limits. `STALE` stays first
when observations are old or refresh fails, and pause takes priority over disk
limits. `W` and `R` abbreviate waiting and running counts when space is tight.
These are observations, not a new admission decision or an automatic cleanup.

Captured prompt context retains its logical line and character position across
split resizing, including temporary height clamping. PgUp/PgDn establishes a new
position. This keeps the same captured snapshot and typed response; it does not
refresh job or recipient state or accept a confirmation. Existing context bounds
and resize requirements remain in effect. Idle and height-only frames reuse
the wrapped snapshot.
