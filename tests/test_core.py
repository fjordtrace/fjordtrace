"""Offline tests for the core: input parsing, model contract, registry parsers, refresh, pipeline."""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from signalpost import registry
from signalpost.budget import Budget
from signalpost.http import FetchResult, check_url_safe, UnsafeURL
from signalpost.model import Contribution, make_claim, make_evidence, validate_envelope
from signalpost.refresh import RefreshStore


def fr(url: str, body, status: int = 200) -> FetchResult:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return FetchResult(url, url, status, "2026-11-06T00:00:00Z", content, "application/json",
                       error=None if status < 400 else f"http_{status}")


# ------------------------------------------------------------------ input
def test_valid_org_checksum():
    assert registry.valid_org("810034882")
    assert registry.valid_org("971228113")
    assert not registry.valid_org("810034883")
    assert not registry.valid_org("12345678")
    assert not registry.valid_org("abcdefghi")


@pytest.mark.parametrize("content,name", [
    ('{"organisation_number": "810034882"}\n{"organisation_number": "971228113"}\n', "b.jsonl"),
    ('["810034882", "971228113"]', "b.json"),
    ('{"organisation_numbers": ["810034882", "971228113"]}', "b.json"),
    ("organisation_number,name\n810034882,A\n971228113,B\n", "b.csv"),
    ("810034882\n971228113\n", "b.txt"),
    ("810 034 882\n971228113\n", "c.txt"),
])
def test_read_batch_formats(tmp_path: Path, content: str, name: str):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    assert registry.read_batch(str(p)) == ["810034882", "971228113"]


def test_read_batch_keeps_duplicates_and_bad(tmp_path: Path):
    p = tmp_path / "b.txt"
    p.write_text("810034882\nnotanumber\n810034882\n", encoding="utf-8")
    assert registry.read_batch(str(p)) == ["810034882", "notanumber", "810034882"]


# ------------------------------------------------------------------ model
def test_available_claim_requires_evidence():
    with pytest.raises(ValueError):
        make_claim("legal_name", "X AS", [])
    c = make_claim("official_website", None, availability="not_available", note="none found")
    assert c["evidence_ids"] == []


def test_evidence_ids_are_stable():
    a = make_evidence("u", "c", "2026-01-01T00:00:00Z", "h", "span", "m")
    b = make_evidence("u", "c", "2027-01-01T00:00:00Z", "h", "span", "m")
    assert a["id"] == b["id"]  # retrieval time does not change identity


def test_validate_envelope_flags_dangling_evidence():
    e = make_evidence("u", "c", "t", "h", "s", "m")
    c = make_claim("legal_name", "X", [e])
    env = {"state": "available", "claims": [c], "evidence": [], "sections": {}}
    probs = validate_envelope(env)
    assert any("missing evidence" in p for p in probs)
    assert any("missing section" in p for p in probs)


# ------------------------------------------------------------------ registry parsers
ACCOUNTS = [{
    "id": 1, "journalnr": "j", "regnskapstype": "SELSKAP", "valuta": "NOK",
    "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
    "revisjon": {"ikkeRevidertAarsregnskap": False, "fravalgRevisjon": False},
    "resultatregnskapResultat": {"aarsresultat": 10.0, "driftsresultat": {"driftsresultat": 5.0,
                                 "driftsinntekter": {"sumDriftsinntekter": 100.0}}},
    "eiendeler": {"sumEiendeler": 50.0}, "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": -3.0}},
}]


def test_accounts_missing_values_are_not_zero():
    out = Contribution()
    recs = registry.accounts_claims("810034882", fr("https://x/regnskap", ACCOUNTS), out)
    figs = recs[0]["figures"]
    assert figs["revenue"] == 100.0 and figs["equity"] == -3.0
    assert "total_debt" not in figs  # missing stays missing
    assert out.claims[0]["reporting_period"] == {"start": "2025-01-01", "end": "2025-12-31"}


def test_accounts_404_is_not_available():
    out = Contribution()
    registry.accounts_claims("810034882", fr("https://x", b"", 404), out)
    assert out.family_states["annual_accounts"]["state"] == "not_available"


def test_group_relations_only_direct():
    body = {"organisasjonsnummer": "900000001", "navn": "TOP AS", "children": [
        {"organisasjonsnummer": "900000002", "navn": "MID AS", "parentOrganisasjonsnummer": "900000001", "grunnlag": "100%"},
        {"organisasjonsnummer": "810034882", "navn": "ME AS", "parentOrganisasjonsnummer": "900000002", "grunnlag": "60%"},
        {"organisasjonsnummer": "900000003", "navn": "SISTER AS", "parentOrganisasjonsnummer": "900000002"},
        {"organisasjonsnummer": "900000004", "navn": "CHILD AS", "parentOrganisasjonsnummer": "810034882"},
    ]}
    out = Contribution()
    registry.group_claims("810034882", fr("https://x/konsern", body), out)
    rel = {c["value"]["relation"]: c["value"]["organisation_number"] for c in out.claims}
    assert rel == {"parent": "900000002", "ultimate_parent": "900000001", "subsidiary": "900000004"}


def test_roles_drop_birth_dates_and_deregistered():
    body = {"rollegrupper": [{"sistEndret": "2020-01-01", "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                              "roller": [{"person": {"fodselsdato": "1980-01-01", "navn": {"fornavn": "Kari", "etternavn": "Nordmann"}},
                                          "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}},
                                         {"avregistrert": True, "person": {"navn": {"fornavn": "Old"}},
                                          "type": {"kode": "DAGL"}}]}]}
    out = Contribution()
    roles = registry.roles_claims("810034882", fr("https://x/roller", body), out)
    assert len(roles) == 1 and roles[0]["name"] == "Kari Nordmann"
    assert "1980" not in json.dumps(out.claims) + json.dumps(out.evidence)


# ------------------------------------------------------------------ http policy
@pytest.mark.parametrize("url", ["ftp://example.no/", "http://127.0.0.1/", "http://localhost/", "http://10.0.0.1/",
                                 "http://example.no:8080/"])
def test_unsafe_urls_refused(url):
    with pytest.raises(UnsafeURL):
        check_url_safe(url)


def test_budget_soft_stop_and_cap():
    b = Budget(max_requests=2, time_budget_s=100)
    assert b.take("a") and b.take("a") and not b.take("a")
    b2 = Budget(max_requests=10, time_budget_s=0.0)
    assert not b2.take("opt") and b2.take("reg", essential=True)


# ------------------------------------------------------------------ refresh
def _claim(field, value, disc=None, url="u"):
    e = make_evidence(url, "c", "t", "h" + str(value), str(value), "m")
    return make_claim(field, value, [e], discriminator=disc), e


def test_refresh_idempotent_added_removed_and_carry_forward(tmp_path: Path):
    store = RefreshStore(str(tmp_path))
    fam = {"FAMILY_OF_FIELD": None}
    ff = {"role": "roles", "job_posting": "jobs", "legal_name": "legal_identity"}

    def env(claims_ev, run):
        claims = [c for c, _ in claims_ev]
        ev = [e for _, e in claims_ev]
        return claims, ev

    c1 = [_claim("legal_name", "A AS"), _claim("role", {"name": "Kari"}, "DAGL:Kari"), _claim("job_posting", {"t": 1}, "u1")]
    claims, ev = env(c1, "r1")
    claims, ev, ch, meta = store.diff("810034882", claims, ev, {}, ff, "2026-01-01T00:00:00Z")
    assert ch == [] and meta["baseline"]
    store.save("810034882", {"run": {"run_id": "r1"}, "claims": claims, "evidence": ev})

    # same snapshot -> no changes, first_seen preserved
    claims, ev = env([_claim("legal_name", "A AS"), _claim("role", {"name": "Kari"}, "DAGL:Kari"),
                      _claim("job_posting", {"t": 1}, "u1")], "r2")
    states = {"roles": {"state": "available"}, "jobs": {"state": "available"}, "legal_identity": {"state": "available"}}
    claims, ev, ch, _ = store.diff("810034882", claims, ev, states, ff, "2026-01-02T00:00:00Z")
    assert ch == []
    assert all(c["first_seen"] == "2026-01-01T00:00:00Z" for c in claims)

    # role removed (roles checked OK) -> role_ended; jobs source blocked -> carried forward, not closed
    claims, ev = env([_claim("legal_name", "A AS")], "r3")
    states = {"roles": {"state": "not_available"}, "jobs": {"state": "blocked"}, "legal_identity": {"state": "available"}}
    claims, ev, ch, _ = store.diff("810034882", claims, ev, states, ff, "2026-01-03T00:00:00Z")
    assert [c["change_type"] for c in ch] == ["role_ended"]
    carried = [c for c in claims if c.get("carried_forward")]
    assert [c["field"] for c in carried] == ["job_posting"]
    assert all(i in {e["id"] for e in ev} for c in carried for i in c["evidence_ids"])


# ------------------------------------------------------------------ pipeline end to end (no network)
def test_pipeline_one_row_per_input(tmp_path: Path, monkeypatch):
    from signalpost import cli, http as sp_http

    entity = {"organisasjonsnummer": "810034882", "navn": "SANDNES ELEKTRISKE AS",
              "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
              "forretningsadresse": {"adresse": ["Strandgata 21"], "postnummer": "4307", "poststed": "SANDNES"},
              "erIKonsern": False}

    def fake_get(self, url, phase, **kw):
        self.budget.take(phase, essential=True)
        if "/enheter/810034882/roller" in url:
            return fr(url, {"rollegrupper": []})
        if url.endswith("/enheter/810034882"):
            return fr(url, entity)
        if "/enheter/971228113" in url:
            return fr(url, b"", 410)
        if "underenheter" in url:
            return fr(url, {"_embedded": {"underenheter": []}})
        if "regnskap" in url:
            return fr(url, ACCOUNTS)
        return fr(url, b"", 404)

    monkeypatch.setattr(sp_http.Fetcher, "get", fake_get)
    batch = tmp_path / "batch.txt"
    batch.write_text("810034882\n971228113\nbad\n810034882\n", encoding="utf-8")
    out = tmp_path / "out"
    rc = cli.main(["--input", str(batch), "--output-dir", str(out), "--state-dir", str(tmp_path / "st"),
                   "--connectors", "none", "--no-html", "--quiet"])
    assert rc == 0
    rows = [json.loads(x) for x in (out / "envelopes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["organisation_number"] for r in rows] == ["810034882", "971228113", "bad", "810034882"]
    assert [r["state"] for r in rows] == ["available", "not_available", "failed", "available"]
    report = json.loads((out / "run-report.json").read_text(encoding="utf-8"))
    assert report["validation"]["passed"], report["validation"]
