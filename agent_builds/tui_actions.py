"""Searchable action routing; choosing never bypasses existing review flows."""

from dataclasses import dataclass

from .store import TERMINAL
from .tui_views import VIEWS, ViewPicker, choose_menu, unavailable


@dataclass(frozen=True)
class Command:
    number: str
    name: str
    description: str
    key: int = 0
    view: object = None
    full: bool = False
    reason: str = ""


def commands(view, job=None, plan=None, focused=False):
    result = [
        Command(
            "view." + str(item.number),
            item.name,
            (job["id"] + " | " + item.description)
            if job and item.number in (3, 4, 12, 13, 14)
            else item.description,
            view=item.number,
        )
        for item in VIEWS
    ]
    result.extend(
        [
            Command(
                "help",
                "Keyboard help",
                "Read searchable instructions; no actions execute",
                key=ord("?"),
            ),
            Command(
                "jump",
                "Open log by ID",
                "Enter a full job ID; read-only lookup",
                key=ord("J"),
            ),
            Command(
                "focus",
                "Show host metrics" if focused else "Focus reading area",
                "z toggles two host rows; global queue warnings and reservations stay visible",
                key=ord("z"),
                full=True,
                reason="Use a dashboard view" if view >= 13 else "",
            ),
        ]
    )
    if view in (0, 3):
        result.append(
            Command(
                "blocker",
                "Recorded blocker log",
                (job["id"] + " | inspect its recorded blocker")
                if job
                else "Select a job first",
                key=ord("g"),
                reason="Select a job first" if job is None else "",
            )
        )
    if view in (0, 3, 7):
        result.extend(
            [
                Command(
                    "new_job",
                    "New job",
                    "Review checkout, command, lane and backend before submission",
                    key=ord("n"),
                    full=True,
                ),
                Command(
                    "new_plan",
                    "New verification plan",
                    "Review checkout and plan JSON path before submission",
                    key=ord("o"),
                    full=True,
                ),
                Command(
                    "task",
                    "Run saved task",
                    "Review the repository and registered task name",
                    key=ord("t"),
                    full=True,
                ),
                Command(
                    "budgets",
                    "Edit queue budgets",
                    "Review each setting; Esc aborts the edit",
                    key=ord("b"),
                    full=True,
                ),
            ]
        )
        target = plan if view == 7 else job
        terminal = target is not None and target["state"] in TERMINAL
        reason = (
            "Select a plan first"
            if view == 7 and target is None
            else "Select a job first"
            if target is None
            else "Outcome already terminal"
            if terminal
            else ""
        )
        result.append(
            Command(
                "cancel",
                "Cancel selected plan" if view == 7 else "Cancel selected job",
                (target["id"] + " | requires typing yes") if target else reason,
                key=ord("c"),
                full=view == 7,
                reason=reason,
            )
        )
    return tuple(result)


def blocked(command, height, width, has_job=False):
    if command.view is not None:
        return unavailable(VIEWS[command.view], height, width, has_job)
    if height < 8 or width < 20:
        return "Resize to 20x8"
    if command.full and (height < 15 or width < 65):
        return "Resize to 65x15"
    return command.reason


def choose(screen, view, job, plan, focused, paint):
    entries = commands(view, job, plan, focused)
    picker = ViewPicker(None, entries)
    selected = choose_menu(
        screen,
        picker,
        paint,
        "BUILDR ACTIONS",
        lambda command, height, width: blocked(command, height, width, job is not None),
    )
    return next((command for command in entries if command.number == selected), None)
