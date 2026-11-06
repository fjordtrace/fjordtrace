"""Optional search discovery: candidates only, exact org-number proof required, metered cost."""
from __future__ import annotations

import json
from collections import Counter

from signalpost.budget import Budget
from signalpost.connectors import website
from signalpost.connectors.base import BatchContext, CompanyContext
from signalpost.connectors.website_identity import Candidate, IdentityEvidence, decide
from signalpost.http import FetchResult
from signalpost.model import Contribution


def test_search_candidate_needs_exact_proof():
    cand = Candidate("example.no", "https://example.no/", ["search"], {"search": "https://example.no/"})
    ev = IdentityEvidence(name_in_text=True, domain_match="example", corroborations=[("person", "u", "s")])
    assert decide(cand, ev, group_member=False).level == "unverified"
    ev.ours_count = 1
    assert decide(cand, ev, group_member=False).level == "exact"


class _FakeFetcher:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, phase, **kw):
        self.calls.append((url, phase, kw))
        return FetchResult(url, url, 200, "t", json.dumps(self.payload).encode(), "application/json")


def _run(monkeypatch, payload, profile, accounts):
    monkeypatch.setattr(website, "SEARCH_KEY", "test-key")
    fetcher = _FakeFetcher(payload)
    budget = Budget(max_requests=100, time_budget_s=1000)
    batch = BatchContext("r", ["810034882"], fetcher, budget, None, {})
    reg = Contribution(facts={"profile": profile, "accounts": accounts})
    ctx = CompanyContext("810034882", batch, reg)
    run = website._CompanyRun(ctx, Contribution(), {"site": Counter({"taken.no": 1}), "email": Counter()})
    return run, fetcher


def test_search_filters_directories_socials_and_taken_domains(monkeypatch):
    payload = {"web": {"results": [{"url": "https://www.proff.no/selskap/x"}, {"url": "https://www.facebook.com/x"},
                                   {"url": "https://taken.no/"}, {"url": "https://www.sandneselektriske.no/kontakt"}]}}
    profile = {"navn": "SANDNES ELEKTRISKE AS", "antallAnsatte": "10", "forretningsadresse.poststed": "SANDNES"}
    run, fetcher = _run(monkeypatch, payload, profile, [])
    assert run._search_allowed()
    cands = run.search_candidates()
    assert [c.key for c in cands] == ["sandneselektriske.no"]
    assert cands[0].origins == ["search"]
    assert fetcher.calls[0][1] == "search"
    assert run.out.facts["third_party_cost_usd"] > 0
    assert "test-key" not in json.dumps(run.out.facts) + json.dumps(run.out.errors)


def test_search_skipped_for_dormant_companies(monkeypatch):
    profile = {"navn": "HOLDING AS", "antallAnsatte": ""}
    accounts = [{"account_type": "SELSKAP", "figures": {"revenue": 0}}]
    run, _ = _run(monkeypatch, {}, profile, accounts)
    assert not run._search_allowed()


def test_guess_strong_needs_name_and_registry_contact():
    cand = Candidate("example.no", "https://example.no/", ["guess"], {"guess": "example.no"})
    ev = IdentityEvidence(name_in_text=True, corroborations=[("person", "u", "Kari Nordmann")])
    assert decide(cand, ev, group_member=False).level == "unverified"  # a person's name is not enough
    ev.corroborations.append(("phone:telefon", "u", "51 68 57 00"))
    assert decide(cand, ev, group_member=False).level == "strong"
    assert decide(cand, ev, group_member=True).level == "unverified"  # group members need the org number
    ev.others = Counter({"999999999": 1})
    assert decide(cand, ev, group_member=False).level == "unverified"  # another org number on the site
    ev2 = IdentityEvidence(name_in_text=False, corroborations=[("phone:telefon", "u", "51 68 57 00")])
    assert decide(cand, ev2, group_member=False).level == "unverified"
