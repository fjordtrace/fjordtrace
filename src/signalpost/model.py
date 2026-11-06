"""Claim, evidence and envelope model shared by every connector.

A *claim* is one published fact about the input company. Every ``available`` claim references at
least one *evidence* record: the exact source URL (or source identifier), retrieval time, content
hash, claim span and extraction method. Claim keys are stable so that refresh can diff them.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

STATES = ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed")

SECTIONS = (
    "legal_identity_and_brand",
    "annual_accounts",
    "leadership_and_workplaces",
    "website_and_profiles",
    "hiring_and_activity",
    "claim_evidence",
    "refresh",
)

# field -> (section, external?, signal_type)
FIELDS: dict[str, tuple[str, bool, str]] = {
    "legal_name": ("legal_identity_and_brand", False, "registry"),
    "legal_form": ("legal_identity_and_brand", False, "registry"),
    "registration_status": ("legal_identity_and_brand", False, "registry"),
    "registered_address": ("legal_identity_and_brand", False, "registry"),
    "postal_address": ("legal_identity_and_brand", False, "registry"),
    "industry": ("legal_identity_and_brand", False, "registry"),
    "founded_date": ("legal_identity_and_brand", False, "registry"),
    "registered_date": ("legal_identity_and_brand", False, "registry"),
    "statutory_purpose": ("legal_identity_and_brand", False, "registry"),
    "activity_description": ("legal_identity_and_brand", False, "registry"),
    "share_capital": ("legal_identity_and_brand", False, "registry"),
    "historical_name": ("legal_identity_and_brand", False, "registry"),
    "registry_contact": ("legal_identity_and_brand", False, "registry"),
    "registered_employees": ("leadership_and_workplaces", False, "registry"),
    "public_brand": ("legal_identity_and_brand", True, "company_profile"),
    "company_description": ("legal_identity_and_brand", True, "company_profile"),
    "annual_accounts": ("annual_accounts", False, "financials"),
    "annual_account_filing": ("annual_accounts", False, "financials"),
    "workforce_fte": ("annual_accounts", True, "workforce_snapshot"),
    "auditor": ("leadership_and_workplaces", False, "registry"),
    "role": ("leadership_and_workplaces", False, "registry"),
    "workplace": ("leadership_and_workplaces", False, "registry"),
    "group_relation": ("leadership_and_workplaces", False, "registry"),
    "official_website": ("website_and_profiles", True, "company_profile"),
    "shared_website": ("website_and_profiles", True, "company_profile"),
    "social_profile": ("website_and_profiles", True, "profile_handle"),
    "contact_detail": ("website_and_profiles", True, "company_profile"),
    "site_location": ("website_and_profiles", True, "company_profile"),
    "site_person": ("leadership_and_workplaces", True, "company_profile"),
    "job_posting": ("hiring_and_activity", True, "job_posting"),
    "news_item": ("hiring_and_activity", True, "public_post"),
    "public_mention": ("hiring_and_activity", True, "public_mention"),
    "wikidata_item": ("website_and_profiles", True, "company_profile"),
}


def _h(*parts: Any) -> str:
    raw = "\x1f".join(json.dumps(p, sort_keys=True, ensure_ascii=False, default=str) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_evidence(
    source_url: str,
    source_class: str,
    retrieved_at: str,
    content_sha256: str,
    claim_span: str,
    extraction_method: str,
    *,
    final_url: str | None = None,
    effective_at: str | None = None,
    locator: str | None = None,
    http_status: int | None = None,
    access_policy: str | None = None,
) -> dict[str, Any]:
    span = (claim_span or "")[:600]
    ev = {
        "id": "ev-" + _h(source_url, content_sha256, span, locator)[:16],
        "source_url": source_url,
        "final_url": final_url or source_url,
        "source_class": source_class,
        "retrieved_at": retrieved_at,
        "effective_at": effective_at,
        "content_sha256": content_sha256,
        "claim_span": span,
        "locator": locator,
        "extraction_method": extraction_method,
        "http_status": http_status,
        "access_policy": access_policy,
    }
    return {k: v for k, v in ev.items() if v is not None}


def make_claim(
    field_name: str,
    value: Any,
    evidence: list[dict[str, Any]] | None = None,
    *,
    availability: str = "available",
    discriminator: Any = None,
    confidence: float = 1.0,
    reporting_period: dict[str, str] | None = None,
    effective_at: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    if availability not in STATES:
        raise ValueError(f"bad availability {availability}")
    if field_name not in FIELDS:
        raise ValueError(f"unknown field {field_name}")
    if availability == "available" and not evidence:
        raise ValueError(f"available claim {field_name} without evidence")
    section, external, signal = FIELDS[field_name]
    key = field_name if discriminator is None else f"{field_name}:{discriminator}"
    claim = {
        "key": key,
        "field": field_name,
        "section": section,
        "external": external,
        "signal_type": signal,
        "value": value,
        "availability": availability,
        "confidence": round(float(confidence), 3),
        "evidence_ids": [e["id"] for e in (evidence or [])],
        "reporting_period": reporting_period,
        "effective_at": effective_at,
        "note": note,
    }
    return {k: v for k, v in claim.items() if v is not None}


@dataclass
class Contribution:
    """What one connector adds to one company's envelope."""

    claims: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    # field family -> availability state, for families with no positive claim (e.g. jobs: not_available)
    family_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    # free-form data for later connectors (e.g. verified website, aliases)
    facts: dict[str, Any] = field(default_factory=dict)

    def add(self, claim: dict[str, Any], *evidence: dict[str, Any]) -> None:
        self.claims.append(claim)
        self.evidence.extend(evidence)

    def state(self, family: str, state: str, reason: str, **extra: Any) -> None:
        if state not in STATES:
            raise ValueError(state)
        self.family_states[family] = {"state": state, "reason": reason, **extra}

    def error(self, connector: str, message: str, url: str | None = None) -> None:
        self.errors.append({k: v for k, v in {"connector": connector, "message": message[:300], "url": url}.items() if v})

    def merge(self, other: "Contribution") -> None:
        self.claims.extend(other.claims)
        self.evidence.extend(other.evidence)
        self.errors.extend(other.errors)
        self.family_states.update(other.family_states)
        self.facts.update(other.facts)


def dedupe(contribution: Contribution) -> Contribution:
    """Drop duplicate claim keys (first wins) and duplicate evidence ids."""
    seen_claims: dict[str, dict] = {}
    for c in contribution.claims:
        if c["key"] in seen_claims:
            prev = seen_claims[c["key"]]
            prev["evidence_ids"] = sorted(set(prev["evidence_ids"]) | set(c["evidence_ids"]))
            continue
        seen_claims[c["key"]] = c
    seen_ev: dict[str, dict] = {}
    for e in contribution.evidence:
        seen_ev.setdefault(e["id"], e)
    contribution.claims = sorted(seen_claims.values(), key=lambda c: c["key"])
    contribution.evidence = sorted(seen_ev.values(), key=lambda e: e["id"])
    return contribution


def validate_envelope(env: dict[str, Any]) -> list[str]:
    """Return a list of contract violations (empty when valid)."""
    problems = []
    if env.get("state") not in STATES:
        problems.append(f"bad envelope state {env.get('state')}")
    ev_ids = {e["id"] for e in env.get("evidence", [])}
    for e in env.get("evidence", []):
        for req in ("source_url", "retrieved_at", "content_sha256", "extraction_method"):
            if not e.get(req):
                problems.append(f"evidence {e.get('id')} missing {req}")
    for c in env.get("claims", []):
        if c.get("availability") not in STATES:
            problems.append(f"claim {c.get('key')} bad availability")
        if c.get("availability") == "available":
            if not c.get("evidence_ids"):
                problems.append(f"claim {c.get('key')} has no evidence")
            missing = [i for i in c.get("evidence_ids", []) if i not in ev_ids]
            if missing:
                problems.append(f"claim {c.get('key')} references missing evidence {missing}")
    for s in SECTIONS:
        if s not in env.get("sections", {}):
            problems.append(f"missing section {s}")
    return problems
