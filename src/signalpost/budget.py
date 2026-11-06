"""Run-wide request and time budget.

The official limits are announced in Builderr's run instructions. Defaults follow the published
listing as entrants report it (45 minutes, 2,000 requests per 100 companies); every limit is
overridable by flag or environment variable. When a limit is reached, optional work stops and the
affected sections are reported as ``blocked`` - nothing is dropped.
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class Budget:
    max_requests: int
    time_budget_s: float
    soft_stop_fraction: float = 0.85  # optional work stops here; the rest is reserved for finalising
    started: float = field(default_factory=time.monotonic)
    used: int = 0
    by_phase: Counter = field(default_factory=Counter)
    refused: Counter = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def time_left(self) -> float:
        return self.time_budget_s - self.elapsed()

    def soft_deadline_passed(self) -> bool:
        return self.elapsed() >= self.time_budget_s * self.soft_stop_fraction

    def hard_deadline_near(self, margin_s: float = 60.0) -> bool:
        return self.time_left() <= margin_s

    def requests_left(self) -> int:
        return self.max_requests - self.used

    def take(self, phase: str, n: int = 1, essential: bool = False) -> bool:
        """Reserve ``n`` requests. Non-essential work is refused after the soft deadline."""
        with self._lock:
            if not essential and self.soft_deadline_passed():
                self.refused[phase] += n
                return False
            if self.used + n > self.max_requests:
                self.refused[phase] += n
                return False
            self.used += n
            self.by_phase[phase] += n
            return True

    def report(self) -> dict:
        return {
            "max_requests": self.max_requests,
            "requests_used": self.used,
            "requests_by_phase": dict(self.by_phase),
            "requests_refused_by_phase": dict(self.refused),
            "time_budget_s": self.time_budget_s,
            "elapsed_s": round(self.elapsed(), 2),
        }
