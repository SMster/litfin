"""Per-rate-key circuit breaker.

Prevents a crash-restart loop from hammering a host that is already down, and
lets the orchestrator distinguish DEGRADED (host outage, watermark untouched,
backfills naturally on the next run) from BROKEN (our parser is wrong).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = "closed"        # normal
    OPEN = "open"            # failing; reject immediately
    HALF_OPEN = "half_open"  # probing with a single request


class CircuitOpen(RuntimeError):
    def __init__(self, key: str, retry_after: float) -> None:
        super().__init__(
            f"Circuit breaker OPEN for {key!r}; retry in {retry_after:.0f}s. "
            f"The host is treated as down (DEGRADED), not as a parser fault."
        )
        self.key = key
        self.retry_after = retry_after


@dataclass(slots=True)
class _Breaker:
    failures: int = 0
    opened_at: float = 0.0
    state: BreakerState = BreakerState.CLOSED
    probing: bool = False


class BreakerRegistry:
    def __init__(self, *, failure_threshold: int = 5, open_seconds: int = 1800) -> None:
        self._threshold = failure_threshold
        self._open_seconds = open_seconds
        self._breakers: dict[str, _Breaker] = {}
        self._lock = threading.Lock()

    def _get(self, key: str) -> _Breaker:
        with self._lock:
            return self._breakers.setdefault(key, _Breaker())

    def state(self, key: str) -> BreakerState:
        return self._get(key).state

    def check(self, key: str) -> None:
        """Raise CircuitOpen if this key is currently rejecting."""
        b = self._get(key)
        with self._lock:
            if b.state is BreakerState.CLOSED:
                return
            elapsed = time.monotonic() - b.opened_at
            if b.state is BreakerState.OPEN:
                if elapsed < self._open_seconds:
                    raise CircuitOpen(key, self._open_seconds - elapsed)
                # Cooled off: admit exactly one probe.
                b.state = BreakerState.HALF_OPEN
                b.probing = True
                return
            # HALF_OPEN: only one probe in flight at a time.
            if b.probing:
                raise CircuitOpen(key, 5.0)
            b.probing = True

    def record_success(self, key: str) -> None:
        b = self._get(key)
        with self._lock:
            b.failures = 0
            b.state = BreakerState.CLOSED
            b.probing = False

    def record_failure(self, key: str) -> None:
        b = self._get(key)
        with self._lock:
            b.failures += 1
            b.probing = False
            if b.failures >= self._threshold:
                b.state = BreakerState.OPEN
                b.opened_at = time.monotonic()
