"""Request plan for a tight budget.

With a generous budget (the default, 18 requests per company) nothing here applies and the run behaves
exactly as before. When the request cap is below what a full run needs (about 9-10 requests per company),
the run switches to a *tight plan*:

* the registry entity comes from the evaluator's bulk snapshot; the live entity call is made only for
  companies missing from it;
* roles and registered subunits come from Brønnøysund's two bulk files (one request each for the whole
  batch) instead of one call per company;
* the remaining requests are reserved per source in order of measured yield per request, so a small cap
  is spread over every company instead of being spent on the first ones.

Yield order (v2, random 1,000 companies): batch-level sources (LEI, Wikidata, NAV, TED, Mattilsynet:
~60 requests for the whole batch) > annual accounts (1 per company, ~100% hit) > group links (konsern
members only) > workforce (~96%) > website (~7% per request) > public support (~10%) > central approval (~2%).
A source that finishes under its reservation returns the rest to a shared pool the others can borrow from.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# requests per company a full (untight) run spends per phase, measured on v2 (run-report-1000-v2.json)
FULL_PER_COMPANY = {
    "registry.entity": 1.0, "registry.accounts": 1.0, "registry.roles": 1.0, "registry.subunits": 1.0,
    "registry.group": 0.07, "workforce": 1.01, "website": 1.94, "public_support": 1.0, "central_approval": 1.0,
}
# batch-level connectors (Wikidata, LEI, TED, Mattilsynet, NAV feed pages) for the whole batch: measured 60 for
# 1,000 companies and 16 for 100; the NAV feed's page count does not grow with the batch
BATCH_FLOOR, BATCH_PER_COMPANY = 40, 0.08
BATCH_PHASES = ("wikidata", "lei", "procurement", "food_inspections", "nav_jobs")  # draw on the "_batch" reserve


def batch_reserve(n: int) -> int:
    return max(BATCH_FLOOR, int(BATCH_PER_COMPANY * n))
FULL_MARGIN = 1.05


@dataclass
class Plan:
    tight: bool
    full_estimate: int
    reserve: dict[str, int] = field(default_factory=dict)
    needs: dict[str, int] = field(default_factory=dict)
    live_entity: str = "all"  # "all" | "missing" (only companies absent from the bulk snapshot)
    bulk_roles: bool = False
    bulk_subunits: bool = False

    def report(self) -> dict:
        return {"tight": self.tight, "full_estimate": self.full_estimate, "live_entity": self.live_entity,
                "bulk_roles": self.bulk_roles, "bulk_subunits": self.bulk_subunits, "reserve": dict(self.reserve),
                "needs": dict(self.needs)}


def full_estimate(n: int) -> int:
    return int(sum(FULL_PER_COMPANY.values()) * n + batch_reserve(n))


def is_tight(max_requests: int, n: int) -> bool:
    return max_requests < full_estimate(n) * FULL_MARGIN


def make_plan(available: int, n: int, *, in_snapshot: int, in_group: int | None, enabled: set[str] | None = None,
              roles_missing: int | None = None, subunits_from_bulk: bool = True) -> Plan:
    """Reserve ``available`` requests (cap minus what the bulk downloads used) in yield order.

    ``in_snapshot``: batch companies in the bulk snapshot; ``in_group``: of those, marked erIKonsern;
    ``roles_missing``: companies the roles bulk file did not cover (``None`` = no bulk roles at all)."""
    on = (lambda c: True) if enabled is None else (lambda c: c in enabled)
    group = in_group if in_group is not None else int(round(FULL_PER_COMPANY["registry.group"] * n))
    roles = n if roles_missing is None else roles_missing
    order = [  # (phase, requests it would need), highest yield per request first
        ("registry.entity", n - in_snapshot),
        ("_batch", batch_reserve(n)),
        ("registry.accounts", n),
        ("registry.roles", roles),
        ("registry.group", group),
        ("registry.subunits", 0 if subunits_from_bulk else n),
        ("workforce", n if on("workforce") else 0),
        ("website", int(FULL_PER_COMPANY["website"] * n) if on("website") else 0),
        ("public_support", n if on("public_support") else 0),
        ("central_approval", n if on("central_approval") else 0),
    ]
    left = max(0, available)
    reserve: dict[str, int] = {}
    for phase, need in order:
        take = max(0, min(need, left))
        left -= take
        reserve[phase] = take  # "_batch" is held through the registry pass, then released to the batch-level sources
    return Plan(True, full_estimate(n), reserve, dict(order), live_entity="missing",
                bulk_roles=roles_missing is not None, bulk_subunits=subunits_from_bulk)
