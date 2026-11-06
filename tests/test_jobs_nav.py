"""Offline tests for the NAV job connector (no network)."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from signalpost.budget import Budget
from signalpost.connectors import jobs_nav as J
from signalpost.connectors.base import BatchContext, CompanyContext
from signalpost.http import FetchResult, utcnow
from signalpost.model import Contribution

ORG = "914778271"  # the company
SUB = "973861778"  # its subunit (registry workplaces)
SUB2 = "973861779"  # its subunit known only from the index's parent map
OTHER = "982463718"  # someone else
EMPTY = "923609016"  # a company with no ads


def entry(uuid: str, orgnr: str | None, status: str = "ACTIVE", **extra) -> dict:
    if status != "ACTIVE":
        return {"uuid": uuid, "sistEndret": "2026-11-05T10:00:00+01:00", "status": status}
    ad = {
        "uuid": uuid, "published": "2026-11-01T00:00:00+01:00", "expires": "2099-12-01T00:00:00+01:00",
        "updated": "2026-11-02T09:00:00+01:00", "title": f"Ad {uuid}", "jobtitle": "Elektriker",
        "applicationDue": "2099-11-30T00:00:00", "applicationUrl": "https://ats.example/apply/1",
        "workLocations": [{"country": "NORGE", "address": "Gate 1", "city": "OSLO", "postalCode": "0150",
                           "county": "OSLO", "municipal": "OSLO"}],
        "contactList": [{"name": "Kari Nordmann", "phone": "99999999"}],
        "employer": {"name": "Hydro avd. Oslo", "orgnr": orgnr, "homepage": "https://hydro.example"},
        "occupationCategories": [{"level1": "Bygg", "level2": "Elektriker"}],
        "engagementtype": "Fast", "extent": "Heltid", "positioncount": "1",
    }
    ad.update(extra)
    return {"uuid": uuid, "sistEndret": ad["updated"], "status": status, "ad_content": ad}


class FakeFetcher:
    def __init__(self, routes: dict[str, tuple[int, object]]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, phase, **kw):
        self.calls.append(url)
        status, body = self.routes.get(url, (404, {"error": "not found"}))
        content = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        return FetchResult(url, url, status, utcnow(), content, "application/json",
                           error=None if status == 200 else f"http_{status}")


def registry_contrib(org: str, name: str, subs: list[tuple[str, str]]) -> Contribution:
    c = Contribution()
    c.facts["profile"] = {"navn": name, "organisasjonsnummer": org}
    c.facts["workplaces"] = [{"organisation_number": s, "name": n} for s, n in subs]
    return c


@pytest.fixture
def setup(tmp_path, monkeypatch):
    index = {
        "built_at": "2026-11-06T00:00:00Z",
        "feed_cursor": {"page_id": "p1", "item_time": "2026-11-06T10:00:00+01:00"},
        "parent_of": {SUB: ORG, SUB2: ORG},
        "by_org": {
            ORG: [{"uuid": "u-own", "title": "Own", "updated": "2026-11-03", "homepage": "https://hydro.example"}],
            SUB: [{"uuid": "u-sub", "title": "Sub"}, {"uuid": "u-inactive", "title": "Gone"},
                  {"uuid": "u-expired", "title": "Old", "expires": "2020-01-01T00:00:00+01:00"}],
            SUB2: [{"uuid": "u-sub2", "title": "Sub2"}],
            OTHER: [{"uuid": "u-other", "title": "Other"}],
        },
    }
    path = tmp_path / "nav_index.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(index, fh)
    monkeypatch.setenv("SIGNALPOST_NAV_OFFLINE", "1")
    monkeypatch.setenv("SIGNALPOST_NAV_TOKEN", "test-token")
    routes = {
        J.ENTRY_URL.format(uuid="u-own"): (200, entry("u-own", ORG)),
        J.ENTRY_URL.format(uuid="u-sub"): (200, entry("u-sub", SUB)),
        J.ENTRY_URL.format(uuid="u-inactive"): (200, entry("u-inactive", SUB, status="INACTIVE")),
        J.ENTRY_URL.format(uuid="u-sub2"): (200, entry("u-sub2", SUB2)),
        J.SUBUNIT_URL.format(org=SUB2): (200, {"organisasjonsnummer": SUB2, "navn": "HYDRO KARMØY",
                                               "overordnetEnhet": ORG}),
    }
    fetcher = FakeFetcher(routes)
    budget = Budget(max_requests=1000, time_budget_s=600)
    registry = {ORG: registry_contrib(ORG, "NORSK HYDRO ASA", [(SUB, "HYDRO OSLO")]),
                EMPTY: registry_contrib(EMPTY, "TOM BEDRIFT AS", [])}
    batch = BatchContext("t", [ORG, EMPTY], fetcher, budget, None, registry)
    conn = J.NavJobsConnector(index_path=path)
    conn.prepare(batch)
    return conn, batch, fetcher, routes


def test_publishes_own_and_subunit_ads_and_rejects_inactive(setup):
    conn, batch, fetcher, _ = setup
    out = conn.run(CompanyContext(ORG, batch, batch.registry[ORG]))
    keys = {c["key"] for c in out.claims}
    assert keys == {"job_posting:u-own", "job_posting:u-sub", "job_posting:u-sub2"}
    assert out.family_states["jobs"]["state"] == "available"
    assert out.family_states["jobs"]["inactive_on_verify"] == 1
    # the expired ad is never fetched; the other company's ad is never considered
    assert J.ENTRY_URL.format(uuid="u-expired") not in fetcher.calls
    assert J.ENTRY_URL.format(uuid="u-other") not in fetcher.calls
    by = {c["key"]: c for c in out.claims}
    sub = by["job_posting:u-sub"]["value"]
    assert sub["posted_under_subunit"]["organisation_number"] == SUB
    assert sub["public_url"] == "https://arbeidsplassen.nav.no/stillinger/stilling/u-sub"
    assert "contactList" not in json.dumps(sub) and "Kari" not in json.dumps(sub)
    assert "posted_under_subunit" not in by["job_posting:u-own"]["value"]
    # the parent-map-only subunit is proven by a live Brønnøysund lookup, which is added as evidence
    sub2 = by["job_posting:u-sub2"]
    assert len(sub2["evidence_ids"]) == 2
    ev = {e["id"]: e for e in out.evidence}
    e0 = ev[by["job_posting:u-own"]["evidence_ids"][0]]
    assert e0["source_class"] == "official_job_feed" and e0["extraction_method"] == "nav_feed_entry_json"
    assert f"employer.orgnr={ORG}" in e0["claim_span"] and "status=ACTIVE" in e0["claim_span"]
    assert e0["final_url"].startswith("https://arbeidsplassen.nav.no/stillinger/stilling/")


def test_orgnr_mismatch_is_rejected(setup):
    conn, batch, fetcher, routes = setup
    routes[J.ENTRY_URL.format(uuid="u-own")] = (200, entry("u-own", OTHER))  # employer changed
    out = conn.run(CompanyContext(ORG, batch, batch.registry[ORG]))
    assert "job_posting:u-own" not in {c["key"] for c in out.claims}
    assert out.family_states["jobs"]["orgnr_mismatch"] == 1


def test_parent_map_subunit_not_confirmed_live_is_rejected(setup):
    conn, batch, fetcher, routes = setup
    routes[J.SUBUNIT_URL.format(org=SUB2)] = (200, {"organisasjonsnummer": SUB2, "overordnetEnhet": OTHER})
    out = conn.run(CompanyContext(ORG, batch, batch.registry[ORG]))
    assert "job_posting:u-sub2" not in {c["key"] for c in out.claims}


def test_company_without_ads_is_not_available_without_requests(setup):
    conn, batch, fetcher, _ = setup
    n = len(fetcher.calls)
    out = conn.run(CompanyContext(EMPTY, batch, batch.registry[EMPTY]))
    assert out.claims == []
    assert out.family_states["jobs"]["state"] == "not_available"
    assert "as of" in out.family_states["jobs"]["reason"]
    assert len(fetcher.calls) == n


def test_verify_cap(setup, monkeypatch):
    conn, batch, _, _ = setup
    conn.verify_cap = 1
    out = conn.run(CompanyContext(ORG, batch, batch.registry[ORG]))
    assert [c["key"] for c in out.claims] == ["job_posting:u-own"]  # own orgnr first


def test_homepage_only_for_company_or_subunits(setup):
    _, batch, _, _ = setup
    hp = batch.shared["nav_jobs"]["homepage_by_org"]
    assert hp == {ORG: "https://hydro.example"}


def test_blocked_without_token(setup, monkeypatch):
    conn, batch, _, _ = setup
    batch.shared["nav_jobs"]["token"] = None
    batch.shared["nav_jobs"]["error"] = "no NAV token: http_503"
    out = conn.run(CompanyContext(ORG, batch, batch.registry[ORG]))
    assert out.family_states["jobs"]["state"] == "blocked"
    assert not out.claims


def test_compact_ad_drops_contacts_and_inactive():
    rec = J.compact_ad(entry("u1", ORG))
    assert rec["orgnr"] == ORG and rec["homepage"] == "https://hydro.example"
    assert "contactList" not in rec and "Kari" not in json.dumps(rec)
    assert J.compact_ad(entry("u2", ORG, status="INACTIVE")) is None


def test_parse_token():
    tok = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig"
    assert J.parse_token(f"Current public token for Nav Job Vacancy Feed:\n{tok}\n") == tok
    assert J.parse_token("<html>error</html>") is None


def test_name_matcher_nominates_whole_token_runs():
    match = J.build_name_matcher({ORG: {"NORSK HYDRO ASA", "HYDRO OSLO"}, EMPTY: {"TOM BEDRIFT AS"}})
    assert match("Hydro Oslo avd. Sentrum") == {ORG}
    assert match("NORSK HYDRO ASA") == {ORG}
    assert match("Hydrogen Norge AS") == set()
    assert match("Tom Bedrift") == {EMPTY}


# ---------------------------------------------------------------- feed crawl against a fake feed
class FakeFeed:
    """Canonical pages break every 7 items; an If-Modified-Since request returns an ad-hoc page starting at
    the first item after the given time, which then chains into the canonical pages (as NAV does)."""

    def __init__(self, n: int = 60):
        t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.items = []
        for i in range(n):
            uuid = f"ad{i % 25}"  # ads change several times
            status = "INACTIVE" if i in (40, 55) else "ACTIVE"
            t = (t0 + timedelta(hours=2 * i)).isoformat()
            self.items.append({"id": uuid, "date_modified": t,
                               "_feed_entry": {"uuid": uuid, "status": status, "businessName": f"B{i}", "sistEndret": t}})
        self.requests = 0

    def page_from(self, start: int, canonical: bool) -> dict:
        end = (start // 7 + 1) * 7 if not canonical else start + 7
        end = min(end, len(self.items))
        nxt = f"c{end}" if end < len(self.items) else None
        pid = f"c{start}" if canonical else f"x{start}"
        return {"id": pid, "next_id": nxt, "items": self.items[start:end]}

    def get_json(self, url, headers):
        self.requests += 1
        if url == J.FEED_URL:
            since = datetime.strptime(headers["If-Modified-Since"], "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
            idx = next((i for i, it in enumerate(self.items) if J.parse_time(it["date_modified"]) > since), None)
            if idx is None:
                return 304, None, None
            return 200, self.page_from(idx, canonical=False), None
        pid = url.rsplit("/", 1)[-1]
        return 200, self.page_from(int(pid[1:]), canonical=True), None


@pytest.mark.parametrize("step_h", [5, 24, 1000])
def test_crawl_feed_segments_cover_every_item_once(step_h):
    feed = FakeFeed()
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    res = J.crawl_feed(feed.get_json, "tok", start_time=start, now=now, step=timedelta(hours=step_h), workers=4)
    assert res.complete, res.errors
    assert res.items == len(feed.items)
    expected = {}
    for it in feed.items:
        expected[it["_feed_entry"]["uuid"]] = it["_feed_entry"]["status"]
    assert {u: h["status"] for u, h in res.latest.items()} == expected
    assert res.cursor_time == feed.items[-1]["date_modified"]


def test_crawl_feed_from_cursor_after_end_is_empty():
    feed = FakeFeed()
    res = J.crawl_feed(feed.get_json, "tok", start_time=datetime(2026, 12, 1, tzinfo=timezone.utc),
                       now=datetime(2026, 12, 2, tzinfo=timezone.utc))
    assert res.items == 0 and not res.errors


def test_crawl_feed_page_cap_marks_incomplete():
    feed = FakeFeed()
    res = J.crawl_feed(feed.get_json, "tok", start_time=datetime(2026, 10, 31, tzinfo=timezone.utc),
                       now=datetime(2026, 11, 7, tzinfo=timezone.utc), step=timedelta(days=30), max_pages=3)
    assert not res.complete
    assert res.items < len(feed.items)
