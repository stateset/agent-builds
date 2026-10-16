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
