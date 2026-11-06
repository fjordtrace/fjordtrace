"""Offline tests for the Wikidata connector (no network)."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from signalpost.budget import Budget
from signalpost.connectors import wikidata as W
from signalpost.connectors.base import BatchContext, CompanyContext
from signalpost.http import FetchResult, utcnow
from signalpost.model import Contribution

HYDRO = "914778271"
TWINS = "951103217"  # two items carry it
NONE = "923609016"


def snak(value, rank="normal", qualifiers=None):
    st = {"mainsnak": {"snaktype": "value", "datavalue": {"value": value}}, "rank": rank}
    if qualifiers:
        st["qualifiers"] = qualifiers
    return st


def hydro_entity():
    return {
        "id": "Q511031",
        "labels": {"en": {"value": "Norsk Hydro"}, "nb": {"value": "Norsk Hydro"}},
        "descriptions": {"nb": {"value": "norsk industrikonsern"}},
        "claims": {
            "P2333": [snak(HYDRO)],
            "P856": [snak("https://hydro.com")],
            "P571": [snak({"time": "+1905-12-02T00:00:00Z", "precision": 11})],
            "P1128": [
                snak({"amount": "+30000"}, qualifiers={"P585": [{"datavalue": {"value": {"time": "+2020-01-01T00:00:00Z", "precision": 9}}}]}),
                snak({"amount": "+31000"}, qualifiers={"P585": [{"datavalue": {"value": {"time": "+2022-01-01T00:00:00Z", "precision": 9}}}]}),
            ],
            "P154": [snak("Norsk Hydro.svg")],
            "P2013": [snak("norskhydroasa")],
            "P2003": [snak("someoneelse")],  # does not carry the name -> not attributed
            "P4264": [snak("norsk-hydro")],
            "P2397": [snak("UCEOwf5jNlgcSFdjxEZootow")],
            "P2002": [snak("oldhandle", rank="deprecated"), snak("norskhydroasa")],
        },
    }


def twin(qid, label):
    return {"id": qid, "labels": {"nb": {"value": label}}, "claims": {"P2333": [snak(TWINS)]}}


def sparql_body(pairs):
    return {"results": {"bindings": [{"o": {"value": o}, "item": {"value": f"http://www.wikidata.org/entity/{q}"}}
                                     for o, q in pairs]}}


class FakeFetcher:
    def __init__(self, entities, fail_sparql=False):
        self.entities = entities
        self.fail_sparql = fail_sparql
        self.calls = []

    def get(self, url, phase, **kw):
        self.calls.append(url)
        if url.startswith(W.SPARQL_URL):
            if self.fail_sparql:
                return FetchResult(url, url, 503, utcnow(), b"", error="http_503")
            q = parse_qs(urlsplit(url).query)["query"][0]
            pairs = [(o, e["id"]) for e in self.entities.values() for o in
                     [s["mainsnak"]["datavalue"]["value"] for s in e["claims"]["P2333"]] if f'"{o}"' in q]
            body = sparql_body(pairs)
        else:
            qid = url.rsplit("/", 1)[-1].removesuffix(".json")
            body = {"entities": {qid: self.entities[qid]}}
        return FetchResult(url, url, 200, utcnow(), json.dumps(body).encode(), "application/json")


def make_batch(fetcher, names):
    reg = {}
    for org, name in names.items():
        c = Contribution()
        c.facts["profile"] = {"navn": name}
        reg[org] = c
    return BatchContext("t", list(names), fetcher, Budget(1000, 600), None, reg)


def run(fetcher, names):
    batch = make_batch(fetcher, names)
    conn = W.WikidataConnector()
    conn.prepare(batch)
    return batch, {o: conn.run(CompanyContext(o, batch, batch.registry[o])) for o in names}


def test_parse_entity_fields():
    p = W.parse_entity(hydro_entity())
    assert p["item"] == "Q511031" and p["label"] == "Norsk Hydro"
    assert p["website"] == "https://hydro.com"
    assert p["inception"] == "1905-12-02"
    assert p["employees"] == 31000 and p["employees_point_in_time"] == "2022"
    assert p["logo"].endswith("Norsk_Hydro.svg")
    handles = {(s["platform"], s["handle"]) for s in p["socials"]}
    assert ("x", "oldhandle") not in handles and ("x", "norskhydroasa") in handles


def test_parse_sparql():
    assert W.parse_sparql(sparql_body([(HYDRO, "Q1"), (HYDRO, "Q2"), ("bad", "Q3")])) == {HYDRO: ["Q1", "Q2"]}


def test_connector_publishes_exact_item_and_name_bearing_handles():
    fetcher = FakeFetcher({"Q511031": hydro_entity()})
    batch, out = run(fetcher, {HYDRO: "NORSK HYDRO ASA", NONE: "TOM BEDRIFT AS"})
    c = out[HYDRO]
    keys = {cl["key"] for cl in c.claims}
    assert "wikidata_item:Q511031" in keys
    assert {"social_profile:facebook:norskhydroasa", "social_profile:linkedin:norsk-hydro",
            "social_profile:youtube:uceowf5jnlgcsfdjxezootow", "social_profile:x:norskhydroasa"} <= keys
    assert not any("someoneelse" in k for k in keys)
    assert c.family_states["wikidata"]["social_handles_not_attributed"] == ["instagram:someoneelse"]
    item = next(cl for cl in c.claims if cl["field"] == "wikidata_item")["value"]
    assert item["employees"] == 31000 and item["website"] == "https://hydro.com"
    ev = c.evidence[0]
    assert ev["source_class"] == "open_knowledge_base" and ev["access_policy"] == "CC0"
    assert f"P2333={HYDRO}" in ev["claim_span"] and ev["final_url"] == "https://www.wikidata.org/wiki/Q511031"
    assert out[NONE].family_states["wikidata"]["state"] == "not_available"
    assert batch.shared["wikidata"]["by_org"][HYDRO]["website"] == "https://hydro.com"
    # one SPARQL request for the batch, one entity fetch per hit
    assert sum(1 for u in fetcher.calls if u.startswith(W.SPARQL_URL)) == 1
    assert len(fetcher.calls) == 2


def test_multiple_items_resolved_by_legal_name_or_ambiguous():
    ents = {"Q1": twin("Q1", "Bergen Kino"), "Q2": twin("Q2", "Bergen kinematografer forening")}
    _, out = run(FakeFetcher(ents), {TWINS: "BERGEN KINO AS"})
    assert {c["key"] for c in out[TWINS].claims} == {"wikidata_item:Q1"}
    ents = {"Q1": twin("Q1", "Oslo Sporveier"), "Q2": twin("Q2", "Kollektivtrafikk i Oslo")}
    _, out = run(FakeFetcher(ents), {TWINS: "SPORVEIEN AS"})
    assert out[TWINS].claims == []
    assert out[TWINS].family_states["wikidata"]["state"] == "ambiguous"


def test_entity_without_org_number_is_not_published():
    ent = hydro_entity()
    sparql_ent = json.loads(json.dumps(ent))
    fetcher = FakeFetcher({"Q511031": sparql_ent})
    batch = make_batch(fetcher, {HYDRO: "NORSK HYDRO ASA"})
    conn = W.WikidataConnector()
    orig = fetcher.get

    def get(url, phase, **kw):  # SPARQL says yes, the live entity no longer carries P2333
        res = orig(url, phase, **kw)
        if "EntityData" in url:
            body = res.json()
            body["entities"]["Q511031"]["claims"]["P2333"] = [snak("999999999")]
            res.content = json.dumps(body).encode()
        return res

    fetcher.get = get
    conn.prepare(batch)
    out = conn.run(CompanyContext(HYDRO, batch, batch.registry[HYDRO]))
    assert out.claims == []
    assert out.family_states["wikidata"]["state"] == "not_available"


def test_sparql_failure_is_failed_not_absent():
    _, out = run(FakeFetcher({}, fail_sparql=True), {HYDRO: "NORSK HYDRO ASA"})
    assert out[HYDRO].family_states["wikidata"]["state"] == "failed"


def test_handle_matching():
    keys = W.name_keys("BERGEN KINO AS", "Bergen Kino", "https://www.bergenkino.no")
    assert W.handle_matches("bergenkino", keys)
    assert W.handle_matches("BergenKinoOfficial", keys)
    assert not W.handle_matches("telenorsverige", keys)
