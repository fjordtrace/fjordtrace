"""Tight-budget plan: reservations, borrowing, release, and the bulk roles/subunits parsers."""
from __future__ import annotations

import csv
import gzip
import io
import json

from signalpost import plan as planning, registry, registry_bulk
from signalpost.budget import Budget
from signalpost.model import Contribution


# ------------------------------------------------------------------ budget reservations
def test_budget_without_reservations_is_unchanged():
    b = Budget(max_requests=3, time_budget_s=60)
    assert all(b.take("x") for _ in range(3))
    assert not b.take("x")


def test_reserved_requests_are_protected_from_other_phases():
    b = Budget(max_requests=10, time_budget_s=60)
    b.reserve({"a": 6, "b": 4})
    assert all(b.take("b") for _ in range(4))
    assert not b.take("b")  # b's own share is spent and a's share is held
    assert all(b.take("a") for _ in range(6))
    assert not b.take("a")


def test_release_returns_unspent_share_to_the_pool():
    b = Budget(max_requests=10, time_budget_s=60)
    b.reserve({"a": 6, "b": 4})
    assert b.take("a")
    b.release("a")  # a finished after 1 request: 5 go back
    assert all(b.take("b") for _ in range(9))
    assert not b.take("b")
    assert b.report()["requests_reserved_by_phase"] == {"a": 1, "b": 4}


def test_release_hands_leftovers_down_the_priority_order():
    b = Budget(max_requests=10, time_budget_s=60)
    b.reserve({"a": 6, "b": 2, "c": 2}, needs={"a": 6, "b": 5, "c": 5})
    assert b.take("a")
    b.release("a")  # 5 unspent: b is topped up to its need (3 more), c gets the other 2
    assert b.reserved == {"a": 1, "b": 5, "c": 4}
    b.release("a")  # releasing twice changes nothing
    assert b.reserved == {"a": 1, "b": 5, "c": 4}


def test_small_batch_reserve_does_not_starve_accounts():
    p = planning.make_plan(200, 100, in_snapshot=100, in_group=9, roles_missing=0)
    assert p.reserve["_batch"] == 40 and p.reserve["registry.accounts"] == 100
    assert p.reserve["registry.group"] == 9 and p.reserve["workforce"] == 51


def test_pooled_phases_share_one_reservation_until_all_finish():
    b = Budget(max_requests=10, time_budget_s=60)
    b.reserve({"_batch": 4, "w": 6}, needs={"_batch": 4, "w": 10}, pooled={"lei": "_batch", "nav": "_batch"})
    assert b.take("lei") and b.take("nav")
    assert all(b.take("w") for _ in range(6)) and not b.take("w")  # the batch share stays held
    b.release("lei")
    assert not b.take("w")  # nav is still running
    b.release("nav")
    assert b.take("w") and b.take("w") and not b.take("w")
    assert b.report()["requests_by_phase"] == {"lei": 1, "nav": 1, "w": 8}


def test_unreserved_phase_borrows_only_free_requests():
    b = Budget(max_requests=10, time_budget_s=60)
    b.reserve({"a": 8})
    assert b.take("lei") and b.take("lei")
    assert not b.take("lei")


# ------------------------------------------------------------------ plan
def test_generous_cap_is_not_tight():
    assert not planning.is_tight(18 * 1000, 1000)
    assert planning.is_tight(2000, 1000)
    assert planning.is_tight(9000, 1000)


def test_tight_plan_reserves_in_yield_order():
    p = planning.make_plan(1998, 1000, in_snapshot=1000, in_group=70, roles_missing=5)
    r = p.reserve
    assert p.tight and p.live_entity == "missing" and p.bulk_roles and p.bulk_subunits
    assert r["registry.entity"] == 0
    assert r["_batch"] == planning.batch_reserve(1000) == 80
    assert r["registry.accounts"] == 1000
    assert r["registry.roles"] == 5 and r["registry.group"] == 70 and r["registry.subunits"] == 0
    assert r["workforce"] == 1998 - 80 - 1000 - 5 - 70
    assert r["website"] == r["public_support"] == r["central_approval"] == 0
    assert sum(r.values()) == 1998


def test_tight_plan_without_snapshot_or_bulk_files_pays_for_entity_first():
    p = planning.make_plan(2000, 1000, in_snapshot=0, in_group=None, roles_missing=None, subunits_from_bulk=False)
    assert p.reserve["registry.entity"] == 1000
    assert p.reserve["registry.accounts"] == 920
    assert p.reserve["registry.roles"] == 0 and not p.bulk_roles and not p.bulk_subunits


def test_disabled_connectors_get_nothing():
    p = planning.make_plan(9000, 1000, in_snapshot=1000, in_group=70, roles_missing=0,
                           enabled={"website", "workforce"})
    assert p.reserve["public_support"] == 0 and p.reserve["central_approval"] == 0
    assert p.reserve["website"] == int(1.94 * 1000)


# ------------------------------------------------------------------ bulk roles
def _pretty_roles(objs: list[dict]) -> bytes:
    # Brønnøysund's totalbestand as served: '[\n  {\n  "_links" : ...\n  }, {\n  ...\n  } ]', top-level keys at
    # two spaces, nested content deeper
    parts = []
    for o in objs:
        parts.append(json.dumps(o, indent=2, separators=(",", " : "), ensure_ascii=False))
    return gzip.compress(("[\n  " + ",\n  ".join(parts) + " ]\n").encode())


def _role_obj(org: str, name: str, sub_org: str | None = None) -> dict:
    role = {"avregistrert": False, "person": {"erDoed": False, "navn": {"fornavn": name, "etternavn": "Hansen"}},
            "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}}
    roles = [role]
    if sub_org:  # a nested entity (deeper indentation) must not be mistaken for a top-level company
        roles.append({"avregistrert": False, "enhet": {"organisasjonsnummer": sub_org, "navn": ["REVISOR AS"]},
                      "type": {"kode": "REVI", "beskrivelse": "Revisor"}})
    return {"_links": {"self": {"href": f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller"}},
            "organisasjonsnummer": org,
            "rollegrupper": [{"roller": roles, "sistEndret": "2020-01-01", "type": {"kode": "DAGL", "beskrivelse": "x"}}]}


def test_parse_roles_keeps_only_wanted_top_level_companies(monkeypatch):
    objs = [_role_obj("810034882", "Ada", sub_org="971228113"), _role_obj("971228113", "Bo"),
            _role_obj("123456785", "Cy")]
    data = _pretty_roles(objs)
    monkeypatch.setattr(registry_bulk, "_CHUNK", 64)  # force objects to straddle decompression chunks
    found = registry_bulk.parse_roles(data, {"810034882", "123456785"})
    assert set(found) == {"810034882", "123456785"}
    body = json.loads(found["123456785"])
    assert body["rollegrupper"][0]["roller"][0]["person"]["navn"]["fornavn"] == "Cy"
    assert len(json.loads(found["810034882"])["rollegrupper"][0]["roller"]) == 2


# Layout of the served file (checked 2026-11-13): role groups close at two spaces ("  }, {"); a company closes
# with a brace at column 0 ("\n},\n  {\n"), which is the only safe boundary.
JACKSON = b'''[
  {
  "_links" : {
    "self" : {
      "href" : "https://data.brreg.no/enhetsregisteret/api/enheter/810034882/roller"
    }
  },
  "organisasjonsnummer" : "810034882",
  "rollegrupper" : [ {
    "roller" : [ {
      "avregistrert" : false,
      "person" : {
        "erDoed" : false,
        "navn" : {
          "etternavn" : "Aarrestad",
          "fornavn" : "Morten"
        }
      },
      "type" : {
        "beskrivelse" : "Daglig leder",
        "kode" : "DAGL"
      }
    } ],
    "sistEndret" : "2016-07-11",
    "type" : {
      "beskrivelse" : "Daglig leder",
      "kode" : "DAGL"
    }
  }, {
    "roller" : [ {
      "avregistrert" : false,
      "enhet" : {
        "navn" : [ "OKSENDAL REVISJON AS" ],
        "organisasjonsnummer" : "997039904"
      },
      "type" : {
        "beskrivelse" : "Revisor",
        "kode" : "REVI"
      }
    } ],
    "sistEndret" : "2016-07-11",
    "type" : {
      "beskrivelse" : "Revisor",
      "kode" : "REVI"
    }
  } ]
},
  {
  "_links" : {
    "self" : {
      "href" : "https://data.brreg.no/enhetsregisteret/api/enheter/971228113/roller"
    }
  },
  "organisasjonsnummer" : "971228113",
  "rollegrupper" : [ {
    "roller" : [ {
      "avregistrert" : false,
      "person" : {
        "erDoed" : false,
        "navn" : {
          "etternavn" : "Berg",
          "fornavn" : "Kari"
        }
      },
      "type" : {
        "beskrivelse" : "Styrets leder",
        "kode" : "LEDE"
      }
    } ],
    "sistEndret" : "2020-01-01",
    "type" : {
      "beskrivelse" : "Styre",
      "kode" : "STYR"
    }
  } ]
} ]
'''


def test_parse_roles_on_the_served_layout(monkeypatch):
    for chunk in (32, 1 << 22):
        monkeypatch.setattr(registry_bulk, "_CHUNK", chunk)
        found = registry_bulk.parse_roles(gzip.compress(JACKSON), {"810034882", "971228113", "997039904"})
        assert set(found) == {"810034882", "971228113"}  # the auditor nested inside is not a company row
        groups = json.loads(found["810034882"])["rollegrupper"]
        assert [g["type"]["kode"] for g in groups] == ["DAGL", "REVI"]
        assert json.loads(found["971228113"])["rollegrupper"][0]["roller"][0]["person"]["navn"]["fornavn"] == "Kari"


def test_bulk_roles_feed_the_same_claims_with_bulk_evidence():
    src = registry_bulk.BulkSource(registry_bulk.ROLES_BULK_URL, "roller_2026-11-13.json.gz", "2026-11-13T00:00:00Z",
                                   "ab" * 32, registry_bulk.parse_roles(_pretty_roles([_role_obj("810034882", "Ada")]),
                                                                        {"810034882"}))
    out = Contribution()
    roles = registry.roles_claims("810034882", src.result_for("810034882", b"{}"), out)
    assert roles and roles[0]["name"] == "Ada Hansen"
    ev = out.evidence[0]
    assert ev["source_url"] == registry_bulk.ROLES_BULK_URL
    assert ev["extraction_method"] == "brreg_roles_bulk_file"
    assert "organisasjonsnummer=810034882" in ev["locator"] and "roller_2026-11-13.json.gz" in ev["locator"]


# ------------------------------------------------------------------ bulk subunits
def _subunits_csv(rows: list[dict]) -> bytes:
    cols = ["organisasjonsnummer", "navn", "overordnetEnhet", "beliggenhetsadresse.adresse",
            "beliggenhetsadresse.postnummer", "beliggenhetsadresse.poststed", "naeringskode1.beskrivelse",
            "antallAnsatte", "harRegistrertAntallAnsatte", "hjemmeside", "oppstartsdato", "nedleggelsesdato"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return gzip.compress(buf.getvalue().encode())


def test_parse_subunits_groups_by_parent_and_matches_api_shape():
    data = _subunits_csv([
        {"organisasjonsnummer": "900000001", "navn": "BUTIKK BERGEN", "overordnetEnhet": "810034882",
         "beliggenhetsadresse.adresse": "Strandgaten 1", "beliggenhetsadresse.postnummer": "5013",
         "beliggenhetsadresse.poststed": "BERGEN", "antallAnsatte": "4", "harRegistrertAntallAnsatte": "true"},
        {"organisasjonsnummer": "900000002", "navn": "NEDLAGT", "overordnetEnhet": "810034882",
         "nedleggelsesdato": "2020-01-01"},
        {"organisasjonsnummer": "900000003", "navn": "ANNEN", "overordnetEnhet": "999999999"},
    ])
    found = registry_bulk.parse_subunits(data, {"810034882", "971228113"}, min_rows=1)
    assert set(found) == {"810034882"}
    src = registry_bulk.BulkSource(registry_bulk.SUBUNITS_BULK_URL, "underenheter.csv.gz", "2026-11-13T00:00:00Z",
                                   "cd" * 32, found)
    out = Contribution()
    units = registry.subunit_claims("810034882", src.result_for("810034882", b"{}"), out)
    assert [u["organisation_number"] for u in units] == ["900000001"]
    assert units[0]["address"]["poststed"] == "BERGEN" and units[0]["registered_employees"] == 4
    none = Contribution()
    assert registry.subunit_claims("971228113", src.result_for("971228113", b"{}"), none) == []
    assert none.family_states["workplaces"]["state"] == "not_available"


def test_truncated_subunit_file_is_rejected():
    data = _subunits_csv([{"organisasjonsnummer": "900000001", "navn": "X", "overordnetEnhet": "810034882"}])
    assert registry_bulk.parse_subunits(data, {"810034882"}) is None


# ------------------------------------------------------------------ pipeline end to end, tight cap (no network)
def test_tight_pipeline_uses_bulk_files_and_skips_per_company_calls(tmp_path, monkeypatch):
    from signalpost import cli, http as sp_http
    from test_core import ACCOUNTS, fr

    snap = tmp_path / "enheter.csv"
    snap.write_text('"organisasjonsnummer","navn","organisasjonsform.kode","erIKonsern"\n'
                    '"810034882","SANDNES ELEKTRISKE AS","AS","false"\n', encoding="utf-8")
    roles = tmp_path / "roller.json.gz"
    roles.write_bytes(gzip.compress(JACKSON))
    units = tmp_path / "underenheter.csv.gz"
    units.write_bytes(_subunits_csv([{"organisasjonsnummer": "900000001", "navn": "AVD SANDNES",
                                      "overordnetEnhet": "810034882", "beliggenhetsadresse.poststed": "SANDNES"}]))
    monkeypatch.setattr(registry_bulk, "MIN_SUBUNIT_ROWS", 1)
    calls = []

    def fake_get(self, url, phase, **kw):
        calls.append(url)
        if not self.budget.take(phase, essential=kw.get("essential", False)):
            return sp_http.FetchResult(url, url, None, "t", error="budget_exhausted", state_hint="blocked")
        if url.endswith("/enheter/971228113"):
            return fr(url, b"", 410)
        if "regnskap" in url:
            return fr(url, ACCOUNTS)
        return fr(url, b"", 404)

    monkeypatch.setattr(sp_http.Fetcher, "get", fake_get)
    batch = tmp_path / "batch.txt"
    batch.write_text("810034882\n971228113\n", encoding="utf-8")
    out = tmp_path / "out"
    rc = cli.main(["--input", str(batch), "--output-dir", str(out), "--state-dir", str(tmp_path / "st"),
                   "--bulk", str(snap), "--roles-bulk", str(roles), "--subunits-bulk", str(units),
                   "--max-requests", "50", "--connectors", "none", "--no-html", "--quiet"])
    assert rc == 0
    assert not any("/roller" in u or "underenheter" in u for u in calls)
    assert not any(u.endswith("/enheter/810034882") for u in calls)  # in the snapshot: no live entity call
    report = json.loads((out / "run-report.json").read_text(encoding="utf-8"))
    assert report["validation"]["passed"] and report["operations"]["request_plan"]["tight"]
    env = {json.loads(x)["organisation_number"]: json.loads(x) for x in (out / "envelopes.jsonl").read_text(encoding="utf-8").splitlines()}
    assert env["971228113"]["state"] == "not_available"
    fields = {c["field"] for c in env["810034882"]["claims"]}
    assert {"role", "auditor", "workplace", "annual_accounts"} <= fields
