"""Connector interface.

A connector adds claims for one company after the registry phase. It may prepare batch-level data
once (e.g. an index lookup or one SPARQL query for the whole batch). Connectors must:

* publish only exact-entity facts, each with evidence (see model.make_claim / make_evidence);
* record a family state (available / not_available / blocked / ambiguous / failed / not_applicable)
  for every field family they own, so absence is never silent;
* charge every request to the budget through ``ctx.fetcher`` with their own phase name;
* never raise for a source problem - record ``Contribution.error`` and a ``failed``/``blocked`` state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..budget import Budget
from ..http import Fetcher
from ..model import Contribution


@dataclass
class BatchContext:
    run_id: str
    orgs: list[str]
    fetcher: Fetcher
    budget: Budget
    snapshot: Any  # registry.BulkSnapshot
    registry: dict[str, Contribution]  # org -> registry contribution (facts['profile'], accounts, roles, workplaces)
    shared: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompanyContext:
    org: str
    batch: BatchContext
    registry: Contribution

    @property
    def profile(self) -> dict[str, str]:
        return self.registry.facts.get("profile") or {}

    @property
    def fetcher(self) -> Fetcher:
        return self.batch.fetcher

    @property
    def budget(self) -> Budget:
        return self.batch.budget


class Connector:
    name = "base"
    families: tuple[str, ...] = ()  # field families this connector owns (for blocked/failed fallback states)
    lane = "default"  # connectors in the same lane share a worker pool
    lane_workers = 16

    def prepare(self, batch: BatchContext) -> None:  # pragma: no cover - optional
        return None

    def run(self, ctx: CompanyContext) -> Contribution:
        raise NotImplementedError
