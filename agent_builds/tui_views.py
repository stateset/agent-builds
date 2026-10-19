"""Searchable dashboard navigation; selecting a view never executes an action."""

from dataclasses import dataclass
import curses
import time

from .tui_logs import wrap_line


@dataclass(frozen=True)
class View:
    number: int
    name: str
    description: str
    full: bool = False
    job: bool = False


VIEWS = (
    View(0, "Jobs", "Browse jobs, filters, checkout focus and output"),
    View(1, "Processes", "Inspect your user's CPU and RAM use", full=True),
    View(
        2,
        "Announcements",
        "Inspect previous agent deliveries; selecting sends nothing",
        full=True,
    ),
    View(
        3,
        "Job details",
        "Read the selected job's command, outcome and evidence",
        job=True,
    ),
    View(4, "History", "Inspect completed commands for the selected checkout"),
    View(
        5,
        "Remote workers",
        "Inspect configured workers; selecting never connects",
        full=True,
    ),
    View(
        6,
        "Disk inventory",
        "Inspect cache inventory; press d to scan, never deletes",
        full=True,
    ),
    View(7, "Plans", "Browse verification plans and stage outcomes"),
    View(8, "Advisor", "Read suggestions; selecting never applies changes", full=True),
    View(9, "Groups", "Inspect tracked job groups and outcomes", full=True),
    View(10, "External watches", "Browse external observations and probe outcomes"),
    View(11, "Queue health", "Inspect global reservations, waits and blockers"),
    View(
        12,
        "Workbench",
        "Inspect the selected job's bottlenecks and reviewed trials",
        job=True,
    ),
    View(
        13,
        "Job logs",
        "Inspect selected output; returning keeps held log position",
        job=True,
    ),
    View(
        14,
        "Why waiting?",
        "Diagnose the selected job's waits, capacity and blockers",
        job=True,
    ),
    View(
        15, "Submissions", "Inspect this dashboard's receipts and unconfirmed attempts"
    ),
)


def unavailable(view, height, width, has_job, has_log=False):
    if height < 8 or width < 20:
        return "Resize to 20x8"
    if view.full and (height < 15 or width < 65):
        return "Resize to 65x15"
    if view.job and not (has_job or (view.number == 13 and has_log)):
        return "Select a job first"
    return ""


class ViewPicker:
    def __init__(self, current):
        self.current = current
        self.query = ""
        self.matches = list(VIEWS)
        self.selected = next(
            (i for i, view in enumerate(self.matches) if view.number == current), 0
        )
        self.top, self.room = 0, 1

    def search(self, query):
        previous = self.choice()
        self.query = query[:256]
        tokens = self.query.casefold().split()
        self.matches = [
            view
            for view in VIEWS
            if all(
                token in (view.name + " " + view.description).casefold()
                for token in tokens
            )
        ]
        self.selected = next(
            (
                i
                for i, view in enumerate(self.matches)
                if previous and view.number == previous.number
            ),
            0,
        )
        self.top = 0

    def choice(self):
        return self.matches[self.selected] if self.matches else None

    def move(self, amount):
        self.selected = min(
            max(0, self.selected + amount), max(0, len(self.matches) - 1)
        )

    def page(self, room):
        self.room = max(1, room)
        self.top = min(max(0, self.top), max(0, len(self.matches) - self.room))
        if self.selected < self.top:
            self.top = self.selected
        elif self.selected >= self.top + self.room:
            self.top = self.selected - self.room + 1
        return list(enumerate(self.matches[self.top : self.top + self.room], self.top))


def choose(screen, current, has_job, paint, has_log=False):
    """Consume all picker input, returning an explicitly accepted eligible view."""
    picker = ViewPicker(current)
    screen.timeout(200)
    try:
        while True:
            height, width = screen.getmaxyx()
            usable = height >= 8 and width >= 20
            screen.erase()
            if not usable:
                paint(screen, 0, 0, "Resize: views 20x8")
                paint(screen, height - 1, 0, "Esc back")
            else:
                paint(screen, 0, 0, "BUILDR VIEWS", curses.A_BOLD)
                query_tail = wrap_line(picker.query, max(1, width - 7))[-1]
                paint(screen, 1, 0, "Find: " + query_tail)
                paint(
                    screen,
                    2,
                    0,
                    "{}/{} | * current".format(
                        picker.selected + 1 if picker.matches else 0,
                        len(picker.matches),
                    ),
                )
                for index, view in picker.page(height - 5):
                    reason = unavailable(view, height, width, has_job, has_log)
                    selected = index == picker.selected
                    line = (
                        (">" if selected else " ")
                        + ("!" if reason else " ")
                        + view.name
                        + ("*" if view.number == current else "")
                    )
                    paint(
                        screen,
                        3 + index - picker.top,
                        0,
                        line,
                        (curses.A_REVERSE if selected else 0)
                        | (curses.A_DIM if reason else 0),
                    )
                choice = picker.choice()
                if choice:
                    note = (
                        unavailable(choice, height, width, has_job, has_log)
                        or choice.description
                    )
                else:
                    paint(screen, 3, 0, "No matching views")
                    note = "Ctrl+U clears"
                paint(screen, height - 2, 0, note)
                paint(screen, height - 1, 0, "Arrows Enter Esc")
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error as error:
                if screen.getmaxyx() != (height, width):
                    continue
                if str(error) == "no input":
                    time.sleep(0.05)
                    continue
                raise
            if key == "\x1b":
                return None
            if (
                not usable
                or screen.getmaxyx() != (height, width)
                or key == curses.KEY_RESIZE
            ):
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                choice = picker.choice()
                if choice and not unavailable(choice, height, width, has_job, has_log):
                    return choice.number
            elif key in (curses.KEY_DOWN, "\t", curses.KEY_UP, curses.KEY_BTAB):
                picker.move(1 if key in (curses.KEY_DOWN, "\t") else -1)
            elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                picker.move(picker.room if key == curses.KEY_NPAGE else -picker.room)
            elif key == curses.KEY_HOME:
                picker.selected = 0
            elif key == curses.KEY_END:
                picker.selected = max(0, len(picker.matches) - 1)
            elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                picker.search(picker.query[:-1])
            elif key == "\x15":
                picker.search("")
            elif isinstance(key, str) and key.isprintable():
                picker.search(picker.query + key)
    finally:
        screen.timeout(200)


_FULL_FOOTERS = {
    15: "V views Tab/Shift+Tab receipt j/k PgUp/Dn Home/End J job U jobs ? q",
    14: "V views / find j/k PgUp/Dn Home/End w jobs i details l logs g blocker ? q",
    0: "V views  J job  U submissions  F follow  w why  R repo  C clear  Tab table  / find  f filter  l log  ? q",
    3: "V views  PgUp/Dn Home/End  j/k job  i jobs  l logs  ? help  q quit",
    4: "V views / find  j/k scroll  PgUp/PgDn page  Home/End  H jobs  v views  ? help  q quit",
    7: "V views Enter stage log o plan c cancel r resume e export PgUp/Dn v next ? q",
    9: "V views j/k Enter log e export PgUp/Dn v jobs ? q",
    10: "V views  c stop observation  r resume controller  e export  Enter probe log  v next  q quit",
    11: "V views / find  j/k scroll  PgUp/PgDn page  u jobs  v next  q quit | read only",
    12: "V views / find  j/k scroll  f next trial  e draft profile  t trial reviewed file  r jobs  q quit",
    13: "V views  b back  J job  / find  n/N match  End live  l jobs  ? help  q quit",
}
_MEDIUM_FOOTERS = {
    15: "V Tab receipt j/k Pg Home/End U jobs ? q",
    14: "V / j/k PgUp/Dn Home/End w jobs i details l logs g ? q",
    0: "V views J job F follow w why R repo C clear Tab / f l ? q",
    3: "V views PgUp/Dn Home/End j/k job i jobs l logs ? q",
    4: "V views / find j/k PgUp/Dn Home/End H jobs ? q",
    7: "V Enter log o plan c cancel r resume e export PgUp/Dn ? q",
    9: "V j/k Enter log e export PgUp/Dn v jobs ? q",
    10: "V views c stop r resume e export Enter log PgUp/Dn ? q",
    11: "V views / find j/k scroll PgUp/Dn u jobs ? q",
    12: "V views / find j/k scroll f next e draft t trial r jobs ? q",
    13: "V b back J job / find n/N match End live l jobs ? q",
}
_COMPACT_FOOTERS = {
    15: "V Tab j/k Pg U ? q",
    14: "V / j/k w i l g ? q",
    0: "V j/k w / f l J ? q",
    3: "V PgUp/Dn i/l ? q",
    4: "V / j/k Pg H q",
    7: "V j/k Enter ? q",
    10: "V views j/k Enter q",
    11: "V / j/k u ? q",
    12: "V / j/k r ? q",
    13: "V b back / l ? q",
}


def footer(view, width, compact=False):
    """Use complete hints that fit; never clip the quit/navigation controls."""
    brief = _COMPACT_FOOTERS.get(view, "V views v ? q")
    options = (
        (brief,)
        if compact
        else (
            _FULL_FOOTERS.get(view, "V views v next ? help q quit"),
            _MEDIUM_FOOTERS.get(view, "V views v next ? q"),
            brief,
        )
    )
    return next(
        (
            text
            for text in options + ("V ? q", "q", "")
            if len(text) <= max(0, width - 1)
        ),
        "",
    )
