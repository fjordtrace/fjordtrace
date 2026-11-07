"""Connector registry. Order matters only for display; connectors are independent given registry facts."""
from __future__ import annotations

import importlib

from .base import BatchContext, CompanyContext, Connector

# module path, class name. Missing modules are skipped so partial builds still run.
CONNECTORS = [
    ("signalpost.connectors.website", "WebsiteConnector"),
    ("signalpost.connectors.jobs_nav", "NavJobsConnector"),
    ("signalpost.connectors.workforce", "WorkforceConnector"),
    ("signalpost.connectors.wikidata", "WikidataConnector"),
    ("signalpost.connectors.lei", "LeiConnector"),
    ("signalpost.connectors.public_support", "PublicSupportConnector"),
    ("signalpost.connectors.central_approval", "CentralApprovalConnector"),
]


def load_connectors(enabled: set[str] | None = None) -> list[Connector]:
    out = []
    for mod, cls in CONNECTORS:
        try:
            module = importlib.import_module(mod)
        except ModuleNotFoundError as exc:
            if exc.name == mod:
                continue
            raise
        conn = getattr(module, cls)()
        if enabled is None or conn.name in enabled:
            out.append(conn)
    return out


__all__ = ["BatchContext", "CompanyContext", "Connector", "load_connectors"]
