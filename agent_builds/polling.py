"""Read-only admission wakeups; actual admission always revalidates atomically."""

import time
from typing import Any, Callable, Optional, Tuple


class AdmissionPoll:
    def __init__(
        self,
        store: Any,
        interval: float = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.interval = interval
        self.clock = clock
        self.previous: Optional[Tuple[int, Any]] = None
        self.next_refresh = 0.0

    def retry_soon(self) -> None:
        """The writer was busy before admission began; try on the next poll."""
        self.next_refresh = 0.0

    def due(self) -> bool:
        now = self.clock()
        generation = self.store.db.execute(
            "SELECT generation FROM queue_events WHERE id=1"
        ).fetchone()[0]
        signature = (generation, self.store.config_signature())
        if signature != self.previous or now >= self.next_refresh:
            self.previous = signature
            self.next_refresh = now + self.interval
            return True
        return False


class ExecutionPoll:
    """Output readiness must not determine the database control-read rate."""

    def __init__(
        self,
        store: Any,
        job: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store, self.job, self.clock = store, job, clock
        self.interval = 0.2
        self.next_refresh = 0.0
        self.row: Any = None

    def read(self, force: bool = False) -> Any:
        if force or self.row is None or self.clock() >= self.next_refresh:
            row = self.store.get_execution_control(self.job)
            self.row = row
            self.next_refresh = self.clock() + self.interval
        return self.row

    def wait_timeout(self) -> float:
        """Quiet output must wake by the next scheduled control check."""
        return min(self.interval, max(0.0, self.next_refresh - self.clock()))
