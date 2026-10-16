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
| Enter or `i` | Open job details in the full view |
| `g` | Jump to the selected job's recorded blocker |
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

Browsing controls change only this dashboard. The help view consumes action
keys, so typing `p`, `c` or `n` there cannot pause, cancel or submit work. Outside
help, full-view controls include `p` for shared new-start pause, `n` for a new
job and `c` for cancellation with a typed confirmation. Budgets, plan submission,
saved tasks and explicitly selected idle-recipient announcements remain available
through their existing deliberate flows. Read help before taking a shared action.
