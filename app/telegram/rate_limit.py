"""Per-user sliding window used by the bot and the Mini App."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int = 3600):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """Return True when this key may start another job.

        A limit of 0 or less disables the limiter.
        """

        if self.limit <= 0:
            return True
        current = time.time() if now is None else now
        with self._lock:
            recent = [
                stamp
                for stamp in self._events.get(key, [])
                if current - stamp < self.window_seconds
            ]
            if len(recent) >= self.limit:
                self._events[key] = recent
                return False
            recent.append(current)
            self._events[key] = recent
            return True

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
