"""Global daily request budget, persisted in SQLite so it survives restarts.

Without persistence a crash-restart loop resets the counter and the budget
stops being a budget.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date


class BudgetExceeded(RuntimeError):
    def __init__(self, spent: int, cap: int) -> None:
        super().__init__(
            f"Global daily request budget exhausted: {spent}/{cap} requests. "
            f"Remaining work is deferred to the next run rather than pushed "
            f"through. Raise [budget].max_requests_per_day if this is wrong."
        )
        self.spent = spent
        self.cap = cap


class GlobalBudget:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        max_per_day: int,
        warn_at_fraction: float = 0.8,
    ) -> None:
        self._conn = conn
        self._cap = max_per_day
        self._warn_at = int(max_per_day * warn_at_fraction)
        self._lock = threading.Lock()
        self._warned = False

    @property
    def cap(self) -> int:
        return self._cap

    def spent(self, day: date | None = None) -> int:
        d = (day or date.today()).isoformat()
        row = self._conn.execute(
            "SELECT n FROM request_budget WHERE day = ?", (d,)
        ).fetchone()
        return int(row[0]) if row else 0

    def remaining(self) -> int:
        return max(0, self._cap - self.spent())

    def consume(self, n: int = 1) -> int:
        """Reserve `n` requests. Raises BudgetExceeded if that would overrun."""
        d = date.today().isoformat()
        with self._lock:
            cur = self._conn.execute(
                "SELECT n FROM request_budget WHERE day = ?", (d,)
            ).fetchone()
            spent = int(cur[0]) if cur else 0
            if spent + n > self._cap:
                raise BudgetExceeded(spent, self._cap)
            new_total = spent + n
            self._conn.execute(
                "INSERT INTO request_budget (day, n) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET n = excluded.n",
                (d, new_total),
            )
            self._conn.commit()
            return new_total

    def should_warn(self) -> bool:
        if self._warned:
            return False
        if self.spent() >= self._warn_at:
            self._warned = True
            return True
        return False
