# Buildr dashboard

Run `buildr tui` in Ghostty, Herdr or another terminal. The dashboard adapts to
split panes down to 20 columns by 8 rows. `q` closes it while jobs continue.
Job snapshots and host metrics refresh once a second; keyboard input is checked
every 200 ms. Queue health refreshes every two seconds. Displayed state can lag
briefly; it is not an admission decision.

| Key | Action |
| --- | --- |
| `?` | Open scrollable help; Esc, `?` or `q` returns without taking an action |
| `j` / `k`, arrows | Select a job |
| PgUp / PgDn, Home / End | Page through jobs or select first/last in the jobs view |
| `/` | Search job ID, repository, command, wait reason or Herdr pane; blank clears; Esc cancels |
| `f` | Cycle all, active, waiting and problems in jobs/details views |
| `s` | Cycle display order: queue, wait, runtime, repository; retain selected job |
| Tab | Toggle an expanded job table in the jobs view |
| Enter or `i` | Open job details in the full view |
| `g` | Jump to the selected job's recorded blocker |
| `l` | Focus the selected job’s log; `l` returns to jobs |
| `u` | Queue health |
| `r` | Selected-job workbench |
| `v` | Cycle views |

Search is a literal, case-insensitive substring of one field; it does not read
logs or source evidence. At most 256 search characters are retained. The job
browser covers the bounded 200 loaded rows, not the complete history. Its label
shows matching and loaded counts. Problems include failed, lost and timed-out
jobs; deliberate cancellations are separate. Global queue health and reservation
totals remain global when jobs are filtered. Narrow panes put the selected wait
reason before repository details and logs.

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

## Focused log inspection

Press `l` from jobs or details, including in a narrow pane. `j`/`k` and arrow keys
scroll one wrapped row; PgUp/PgDn scroll a page. Home shows the oldest **loaded**
output. Space holds a fixed output snapshot; End or Space resumes live output.
Holding output never pauses the shared queue. The selected log stays attached to
its job even if completion removes that job from the active filter. `l` returns
to jobs; `q` closes the dashboard. Shared action keys are ignored in log focus.

The viewer loads at most the recent 256 KiB and labels omitted earlier output.
Use `buildr logs JOB_ID` or the job’s log file for the full history. Changed logs
refresh at most twice a second; unchanged logs are not reread. Held snapshots
perform no log-file reads. Following handles file replacement, truncation and
missing files. Lines wrap to the pane width; ANSI control sequences are removed
and carriage-return progress uses the last segment of each line. This is a text
viewer, not a complete terminal emulator.

In log focus, `/` searches literal text, ignoring case, across the loaded logical
lines (including text spanning a visual wrap). Enter accepts, Esc leaves the
existing search unchanged, and a blank query clears it. Search holds the current
snapshot and jumps to the first matching line. `n`/`N` move to the next/previous
matching line, wrapping at either end; multiple occurrences on one line count
as one match. Matching lines are highlighted, with the selected match reversed.
The header shows the visible wrapped-row range and total; the search label shows
the selected match and matching-line count. Searches cover the recent loaded
256 KiB, never the entire log. Queries are limited to 256 characters. Clearing
search keeps the current live/held mode; End resumes live output. Job-browser
search and log search are independent and do not change shared queue state.
Narrow prompts shorten their label so the text being entered stays visible.

The system process list is sampled only while its view is open. Other views
retain their existing one-second job/host and two-second queue-health refreshes.
