"""Offline tests for the deterministic summary (synthetic envelopes, no network)."""
from __future__ import annotations

import copy

from signalpost import synthesis as syn
from signalpost.model import SECTIONS, make_claim, make_evidence

ORG = "923609016"
T = "2026-11-06T10:00:00Z"


def ev(tag: str, url: str = "https://data.brreg.no/enhetsregisteret/api/enheter/923609016", cls: str = "official_registry_live") -> dict:
    return make_evidence(url, cls, T, "a" * 64, f"span for {tag}", "test_extractor", locator=tag)


def acc(kind: str, start: str, end: str, **fig) -> tuple[dict, dict]:
    e = ev(f"acc-{kind}-{end}", "https://data.brreg.no/regnskapsregisteret/regnskap/923609016", "official_annual_accounts")
    value = {"account_type": kind, "currency": "NOK", "period_start": start, "period_end": end, "audited": True,
             "audit_opt_out": False, "liquidation_accounts": False, "small_company_rules": False, "figures": fig}
    return make_claim("annual_accounts", value, [e], discriminator=f"{kind}:{end}",
                      reporting_period={"start": start, "end": end}, effective_at=end), e


def envelope(pairs: list[tuple[dict, dict]], availability: dict | None = None, changes: list | None = None,
             state: str = "available", baseline: bool = True, previous: str | None = None) -> dict:
    claims = [c for c, _ in pairs]
    evidence = list({e["id"]: e for _, e in pairs}.values())
    av = dict(availability or {})
    for c in claims:
        fam = syn.FAMILY_OF_FIELD.get(c["field"], c["field"])
        av.setdefault(fam, {"state": "available", "reason": "supported claims found"})
    sections = {s: {"state": "available", "claims": 0, "families": {}} for s in SECTIONS}
    sections["refresh"].update({"baseline": baseline, "previous_run_id": previous, "changes": len(changes or []),
                                "material_changes": sum(1 for c in changes or [] if c.get("material"))})
    name = next((c["value"] for c in claims if c["field"] == "legal_name"), None)
    return {"organisation_number": ORG, "legal_name": name, "public_brand": None, "state": state,
            "run": {"run_id": "t1", "agent_version": "0", "started_at": T, "completed_at": T, "terminal_status": "completed"},
            "sections": sections, "availability": av, "summary": None, "claims": claims, "evidence": evidence,
            "changes": changes or [], "errors": [], "operations": {"runtime_ms": 0, "third_party_cost_usd": 0.0}}


def put(field: str, value, disc=None, **kw) -> tuple[dict, dict]:
    e = ev(f"{field}:{disc}")
    return make_claim(field, value, [e], discriminator=disc, **kw), e


def role(code: str, name: str, desc: str) -> tuple[dict, dict]:
    field = "auditor" if code in ("REVI", "REGN") else "role"
    return put(field, {"name": name, "role_code": code, "role": desc, "holder_type": "person"}, f"{code}:{name}")


def full_env() -> dict:
    pairs = [
        put("legal_name", "FJORDLYS TEKNIKK AS"),
        put("legal_form", {"code": "AS", "description": "Aksjeselskap"}),
        put("registered_address", {"adresse": "Kaigaten 1", "postnummer": "5015", "poststed": "BERGEN", "kommune": "BERGEN"}),
        put("founded_date", "2015-03-01"),
        put("industry", {"code": "62.100", "description": "Dataprogrammeringstjenester", "rank": 1}, "62.100"),
        put("industry", {"code": "70.220", "description": "Bedriftsrådgivning", "rank": 2}, "70.220"),
        put("activity_description", "Utvikling av programvare for havbruk."),
        put("registration_status", {"bankrupt": False, "under_liquidation": False, "forced_dissolution": False,
                                    "in_business_register": True, "vat_registered": True, "in_group": True}),
        put("registered_employees", 14, effective_at="2026-05-01"),
        acc("SELSKAP", "2024-01-01", "2024-12-31", revenue=10_000_000, operating_result=500_000, annual_result=400_000, equity=2_000_000),
        acc("SELSKAP", "2025-01-01", "2025-12-31", revenue=12_500_000, operating_result=-250_000, annual_result=-300_000, equity=1_700_000),
        acc("KONSERN", "2025-01-01", "2025-12-31", revenue=99_000_000, annual_result=5_000_000),
        role("DAGL", "Kari Nordmann", "Daglig leder"),
        role("LEDE", "Ola Nordmann", "Styrets leder"),
        role("MEDL", "Per Hansen", "Styremedlem"),
        role("VARA", "Liv Berg", "Varamedlem"),
        role("REVI", "REVISJON VEST AS", "Revisor"),
        put("workplace", {"organisation_number": "999999999", "name": "FJORDLYS OSLO", "address": {"kommune": "OSLO"}}, "999999999"),
        put("workplace", {"organisation_number": "999999998", "name": "FJORDLYS BERGEN", "address": {"kommune": "BERGEN"}}, "999999998"),
        put("group_relation", {"organisation_number": "811111111", "name": "FJORDLYS HOLDING AS", "relation": "parent",
                               "ownership_basis": "100%", "group_size": 3}, "parent:811111111"),
        put("official_website", {"url": "https://fjordlys.no", "domain": "fjordlys.no", "identity": "org_number_on_site",
                                 "origin": "registry", "proofs": [{"type": "org_number_in_footer"}, {"type": "name_in_title"}]}),
        put("company_description", "Vi lager programvare som gjør havbruk smartere."),
        put("public_brand", "Fjordlys"),
        put("social_profile", {"platform": "linkedin", "handle": "fjordlys", "url": "https://linkedin.com/company/fjordlys"}, "linkedin"),
        put("job_posting", {"title": "Utvikler", "uuid": "j1", "published": "2026-10-20", "expires": "2026-12-01",
                            "public_url": "https://arbeidsplassen.nav.no/stillinger/stilling/j1"}, "j1"),
        put("job_posting", {"title": "Selger", "uuid": "j2", "published": "2026-08-01", "expires": "2026-09-01",
                            "public_url": "https://arbeidsplassen.nav.no/stillinger/stilling/j2"}, "j2"),
        put("news_item", {"title": "Fjordlys henter 20 millioner", "url": "https://e24.no/a/1", "published": "2026-09-15"}, "n1"),
        put("workforce_fte", {"value": 12.5, "measure": "avtalte årsverk", "year": 2025, "scope": "company"}, "2025"),
        put("wikidata_item", {"item": "Q123", "label": "Fjordlys", "description": "Norwegian software company"}),
    ]
    return envelope(pairs, availability={"news_activity": {"state": "available", "reason": "x"}})


def topics(summary: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for s in summary["statements"]:
        out.setdefault(s["topic"], []).append(s["text"])
    return out


def test_every_statement_is_backed_by_claims_and_their_evidence():
    env = full_env()
    s = syn.summarise(env)
    claims = {c["key"]: c for c in env["claims"]}
    assert s["generated_by"] == "deterministic_templates_v1"
    assert s["statements"]
    for st in s["statements"]:
        assert st["claim_keys"], st
        assert all(k in claims for k in st["claim_keys"])
        expected = {e for k in st["claim_keys"] for e in claims[k]["evidence_ids"]}
        assert set(st["evidence_ids"]) == expected
        assert st["text"].endswith((".", "”"))
    t = topics(s)
    assert set(t) >= {"what_it_does", "size_and_finances", "people", "footprint", "hiring_and_workforce", "group",
                      "status", "recent_activity"}


def test_topics_render_known_fields():
    s = syn.summarise(full_env())
    text = " ".join(st["text"] for st in s["statements"])
    assert "Dataprogrammeringstjenester" in text  # Norwegian value kept as-is
    assert "company-owned copy" in text
    assert "Daglig leder (CEO): Kari Nordmann" in text
    assert "Chair of the board (styrets leder): Ola Nordmann" in text
    assert "2 members and 1 deputy" in text
    assert "REVISJON VEST AS" in text
    assert "2 registered workplaces" in text and "OSLO" in text
    assert "https://fjordlys.no" in text and "org number in footer" in text
    assert "LinkedIn: fjordlys" in text
    assert "1 active job ad on arbeidsplassen.nav.no" in text and "“Utvikler”" in text
    assert "1 further job ad" in text  # the expired one is counted separately
    assert "12.5 avtalte årsverk in 2025" in text
    assert "subsidiary of FJORDLYS HOLDING AS (811111111)" in text
    assert "no bankruptcy" in text and "VAT register" in text
    assert "Fjordlys henter 20 millioner" in text
    assert "Wikidata (Q123)" in text
    assert "FJORDLYS TEKNIKK AS" in s["headline"] and "NOK 12.5M (FY2025)" in s["headline"]


def test_finances_keep_selskap_and_konsern_apart_and_compare_with_preceding_year():
    t = topics(syn.summarise(full_env()))["size_and_finances"]
    company = next(x for x in t if x.startswith("Company accounts (SELSKAP) for FY2025"))
    assert "revenue NOK 12.5M" in company and "99" not in company
    assert "operating result NOK -250k" in company
    yoy = next(x for x in t if x.startswith("Compared with the preceding year"))
    assert "revenue up 25.0% from NOK 10M in FY2024" in yoy
    group = next(x for x in t if x.startswith("Group accounts (KONSERN)"))
    assert "NOK 99M" in group and "not comparable" in group


def test_no_year_over_year_without_an_adjacent_equal_period():
    gap = envelope([put("legal_name", "A AS"),
                    acc("SELSKAP", "2023-01-01", "2023-12-31", revenue=1_000_000),
                    acc("SELSKAP", "2025-01-01", "2025-12-31", revenue=2_000_000)])
    assert not any("preceding" in x["text"] for x in syn.summarise(gap)["statements"])
    short = envelope([put("legal_name", "B AS"),
                      acc("SELSKAP", "2024-06-01", "2024-12-31", revenue=300_000),
                      acc("SELSKAP", "2025-01-01", "2025-12-31", revenue=2_000_000)])
    assert not any("preceding" in x["text"] for x in syn.summarise(short)["statements"])
    broken = envelope([put("legal_name", "C AS"),
                       acc("SELSKAP", "2024-07-01", "2025-06-30", revenue=1_000_000),
                       acc("SELSKAP", "2025-07-01", "2026-06-30", revenue=1_100_000)])
    stmts = [x["text"] for x in syn.summarise(broken)["statements"]]
    assert any("2025-07-01 to 2026-06-30" in x for x in stmts)
    assert any("up 10.0% from NOK 1M in 2024-07-01 to 2025-06-30" in x for x in stmts)


def test_missing_values_are_never_computed_or_zero_filled():
    env = envelope([put("legal_name", "D AS"),
                    acc("SELSKAP", "2024-01-01", "2024-12-31", annual_result=100_000),
                    acc("SELSKAP", "2025-01-01", "2025-12-31", annual_result=50_000, equity=10_000)])
    t = topics(syn.summarise(env))["size_and_finances"]
    assert "Revenue is not included in the filed figures." in t[0]
    assert "revenue" not in t[1].lower()  # comparison only uses figures present in both years
    kf = {r["id"]: r for r in syn.key_figures(env)}
    assert kf["revenue"]["text"] is None and kf["revenue"]["state"] == "not_available"
    assert kf["revenue_change"]["text"] is None


def test_negative_equity_is_flagged():
    env = envelope([put("legal_name", "E AS"), acc("SELSKAP", "2025-01-01", "2025-12-31", revenue=0, equity=-40_600)])
    t = topics(syn.summarise(env))["size_and_finances"]
    assert any("Equity was negative (NOK -40.6k) at 2025-12-31" in x for x in t)
    assert {r["id"]: r for r in syn.key_figures(env)}["equity"]["text"] == "NOK -40.6k (negative)"


def test_insolvency_flags_only_when_true():
    env = envelope([put("legal_name", "F AS"), put("registration_status", {"bankrupt": True, "under_liquidation": False,
                                                                           "forced_dissolution": False})])
    s = syn.summarise(env)
    assert "bankrupt (konkurs)" in topics(s)["status"][0] and "liquidation" not in topics(s)["status"][0]
    assert "BANKRUPT" in s["headline"]


def test_no_ceo_registered_is_stated_from_roles():
    env = envelope([put("legal_name", "G AS"), role("LEDE", "A B", "Styrets leder")])
    assert "No daglig leder (CEO) is registered in the roles register." in topics(syn.summarise(env))["people"]


def test_unknowns_cover_every_non_available_family_in_plain_words():
    env = envelope([put("legal_name", "H AS")], availability={
        "official_website": {"state": "not_available", "reason": "no candidate domain matched the organisation number"},
        "jobs": {"state": "blocked", "reason": "time budget reached"},
        "annual_accounts": {"state": "failed", "reason": "HTTP 503"},
        "group_relation": {"state": "not_applicable", "reason": "not in a group"},
        "social_profiles": {"state": "ambiguous", "reason": "two LinkedIn pages"},
    })
    un = {u["family"]: u for u in syn.summarise(env)["unknowns"]}
    assert un["official_website"]["text"] == "No official website could be verified: no candidate domain matched the organisation number."
    assert un["jobs"]["text"] == "Job ads could not be checked (time budget reached)."
    assert un["annual_accounts"]["state"] == "failed" and "HTTP 503" in un["annual_accounts"]["text"]
    assert un["group_relation"]["state"] == "not_applicable"
    assert un["social_profiles"]["state"] == "ambiguous"
    assert un["news_activity"]["state"] == "not_checked"  # never checked in this run
    assert "legal_identity" not in un


def test_changes_in_plain_words_and_baseline():
    changes = [
        {"change_type": "new_role", "key": "role:DAGL:X", "field": "role", "material": True, "previous_value": None,
         "current_value": {"name": "Nina Dahl", "role_code": "DAGL", "role": "Daglig leder"}, "current_evidence_ids": ["ev-1"]},
        {"change_type": "changed", "key": "registered_address", "field": "registered_address", "material": True,
         "previous_value": {"adresse": "Gate 1", "postnummer": "0150", "poststed": "OSLO"},
         "current_value": {"adresse": "Vei 2", "postnummer": "5015", "poststed": "BERGEN"}},
        {"change_type": "closed_job", "key": "job_posting:j9", "field": "job_posting", "material": True,
         "previous_value": {"title": "Kokk"}, "current_value": None},
        {"change_type": "new_activity", "key": "news_item:n2", "field": "news_item", "material": False,
         "previous_value": None, "current_value": {"title": "Ny avtale", "published": "2026-11-01"}},
        {"change_type": "changed", "key": "annual_accounts:SELSKAP:2024-12-31", "field": "annual_accounts", "material": True,
         "previous_value": {"account_type": "SELSKAP", "period_start": "2024-01-01", "period_end": "2024-12-31", "figures": {"revenue": 1_000_000}},
         "current_value": {"account_type": "SELSKAP", "period_start": "2024-01-01", "period_end": "2024-12-31", "figures": {"revenue": 1_200_000}}},
    ]
    env = envelope([put("legal_name", "I AS")], changes=changes, baseline=False, previous="t0")
    out = syn.summarise(env)["changes"]
    texts = [c["text"] for c in out]
    assert all(c["change_keys"] for c in out)
    assert "New role registered: Daglig leder: Nina Dahl." in texts
    assert any(t.startswith("Business address changed from “Gate 1, 0150 OSLO” to “Vei 2, 5015 BERGEN”") for t in texts)
    assert "Job ad no longer listed: Kokk." in texts
    assert any("revenue NOK 1M → NOK 1.2M" in t for t in texts)
    assert out[-1]["material"] is False  # material changes first

    base = syn.summarise(envelope([put("legal_name", "J AS")]))["changes"]
    assert base[0]["change_type"] == "baseline" and "first snapshot" in base[0]["text"]
    same = syn.summarise(envelope([put("legal_name", "K AS")], baseline=False, previous="t0"))["changes"]
    assert same[0]["text"] == "No changes since the previous run (t0)."


def test_failed_and_not_available_envelopes_make_no_claims():
    for state, reason in (("failed", "input is not a valid Norwegian organisation number"),
                          ("not_available", "organisation number is deleted from Enhetsregisteret")):
        env = envelope([], availability={"legal_identity": {"state": state, "reason": reason}}, state=state)
        env["legal_name"] = None
        s = syn.summarise(env)
        assert s["statements"] == []
        assert reason in s["headline"]
        assert s["unknowns"] and s["unknowns"][0]["family"] == "legal_identity"
        assert all(u["state"] != "not_checked" for u in s["unknowns"])
        rows = syn.key_figures(env)
        assert all(r["text"] is None for r in rows)


def test_carried_forward_values_are_disclosed():
    env = full_env()
    env = copy.deepcopy(env)
    for c in env["claims"]:
        if c["field"] == "job_posting":
            c["carried_forward"] = True
    note = syn.summarise(env)["confidence_note"]
    assert "carried forward" in note and "job ad" in note


def test_amount_formatting():
    f = syn.fmt_amount
    assert f(121_488_759) == "NOK 121.5M"
    assert f(1_000_000) == "NOK 1M"
    assert f(2_340_000_000) == "NOK 2.34bn"
    assert f(850_000) == "NOK 850k"
    assert f(12_345) == "NOK 12.3k"
    assert f(9_500) == "NOK 9,500"
    assert f(-40_600) == "NOK -40.6k"
    assert f(0) == "NOK 0"
    assert f(None) == "not reported"
    assert f(121_488_759, full=True) == "NOK 121,488,759"


def test_generic_rendering_of_unknown_shapes():
    c, _ = put("contact_detail", {"kind": "phone", "value": "+47 55 00 00 00", "page": "https://x.no/kontakt"}, "p1")
    text = syn.describe_claim(c)
    assert "+47 55 00 00 00" in text and "kind: phone" in text
    j, _ = put("job_posting", {"title": "Utvikler", "published": "2026-10-20T08:00:00", "expires": "2026-12-01"}, "j")
    assert syn.describe_claim(j) == "Utvikler (published 2026-10-20, expires 2026-12-01)"
    assert syn.value_url(put("wikidata_item", {"item": "Q5"})[0]) == "https://www.wikidata.org/wiki/Q5"


def test_summary_is_deterministic():
    assert syn.summarise(full_env()) == syn.summarise(full_env())
