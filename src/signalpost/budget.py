"""Run-wide request and time budget.

The official limits are announced in Builderr's run instructions. Defaults follow the earlier published
listing (45 minutes, 2,000 requests per 100 companies); every limit is overridable by flag or environment
variable. A cap below a full run's needs switches on the request plan (plan.py), which reserves requests per
source. When a limit is reached, optional work stops and the affected sections are reported as ``blocked`` -
nothing is dropped.
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
    # phase -> requests reserved for it (a tight budget's plan, see plan.py). A phase may always spend its own
    # reservation; beyond it, it may only borrow requests nobody else has reserved. Empty = v2 behaviour.
    reserved: dict = field(default_factory=dict)
    needs: dict = field(default_factory=dict)
    pooled: dict = field(default_factory=dict)
    spent: Counter = field(default_factory=Counter)  # per reservation key
    finished: set = field(default_factory=set)  # phases done
    released: set = field(default_factory=set)  # reservation keys handed back
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
            key = self.pooled.get(phase, phase)
            held_by_others = sum(max(0, r - self.spent[k]) for k, r in self.reserved.items() if k != key)
            if self.used + n + held_by_others > self.max_requests:
                self.refused[phase] += n
                return False
            self.used += n
            self.by_phase[phase] += n
            self.spent[key] += n
            return True

    def reserve(self, plan: dict, needs: dict | None = None, pooled: dict | None = None) -> None:
        """``plan``: reservation key -> requests, in priority order. ``needs``: what each key could use;
        requests a finished key leaves unspent are handed down this order up to each key's need.
        ``pooled``: phase -> key for phases that share one reservation (e.g. the batch-level sources)."""
        with self._lock:
            self.reserved = {k: int(r) for k, r in plan.items()}
            self.needs = {k: int(v) for k, v in (needs or {}).items()}
            self.pooled = dict(pooled or {})
            self.finished, self.released = set(), set()

    def release(self, phase: str) -> None:
        """The phase is finished. When every phase sharing its reservation is finished, the unspent part goes
        to the next unfinished keys in priority order (up to their need), and whatever remains to the pool."""
        with self._lock:
            key = self.pooled.get(phase, phase)
            self.finished.add(phase)
            if key not in self.reserved or key in self.released:
                return
            if any(k == key and p not in self.finished for p, k in self.pooled.items()):
                return  # another phase still draws on this shared reservation
            self.released.add(key)
            freed = max(0, self.reserved[key] - self.spent[key])
            self.reserved[key] = self.spent[key]
            for p in self.reserved:
                if freed <= 0:
                    break
                if p in self.released:
                    continue
                give = min(freed, max(0, self.needs.get(p, 0) - self.reserved[p]))
                self.reserved[p] += give
                freed -= give

    def report(self) -> dict:
        return {
            "max_requests": self.max_requests,
            "requests_used": self.used,
            "requests_by_phase": dict(self.by_phase),
            "requests_refused_by_phase": dict(self.refused),
            **({"requests_reserved_by_phase": dict(self.reserved)} if self.reserved else {}),
            "time_budget_s": self.time_budget_s,
            "elapsed_s": round(self.elapsed(), 2),
        }
