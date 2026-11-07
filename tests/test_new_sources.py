"""Offline tests for the GLEIF (LEI) and Støtteregisteret (public support) connectors."""
from __future__ import annotations

import json

from signalpost.budget import Budget
from signalpost.connectors import lei as L
from signalpost.connectors import public_support as P
from signalpost.connectors.base import BatchContext, CompanyContext
from signalpost.http import FetchResult, utcnow
from signalpost.model import Contribution
from signalpost import synthesis as syn

ORG = "923609016"
OTHER = "914778271"


def res(url, body, status=200):
    return FetchResult(url, url, status, utcnow(), json.dumps(body).encode(), "application/json",
                       error=None if status == 200 else f"http_{status}")


class Fake:
    def __init__(self, body, status=200):
        self.body, self.status, self.calls = body, status, []
        self.budget = Budget(1000, 600)

    def get(self, url, phase, **kw):
        self.calls.append((url, kw))
        return res(url, self.body, self.status)


def ctx(fetcher, org=ORG):
    batch = BatchContext("t", [org], fetcher, fetcher.budget, None, {})
    return batch, CompanyContext(org, batch, Contribution())


def lei_rec(lei, registered_as, country="NO", status="ISSUED"):
    return {"id": lei, "attributes": {
        "entity": {"registeredAs": registered_as, "legalName": {"name": "EQUINOR ASA"}, "status": "ACTIVE",
                   "jurisdiction": country, "legalAddress": {"country": country, "city": "Stavanger"},
                   "legalForm": {"id": "3C7U"}},
        "registration": {"status": status, "initialRegistrationDate": "2012-06-06T00:00:00Z",
                         "lastUpdateDate": "2026-05-01T00:00:00Z", "nextRenewalDate": "2027-11-09T00:00:00Z",
                         "managingLou": "5493001KJTIIGC8Y1R12"}}}


def test_lei_query_sends_both_spellings_in_one_request():
    url = L.query_url([ORG, OTHER])
    assert "923609016%2C923+609+016%2C914778271%2C914+778+271" in url


def test_lei_matches_exact_norwegian_number_only():
    body = {"data": [lei_rec("OW6OFBNCKXC4US5C7523", "923 609 016"),
                     lei_rec("XXXXXXXXXXXXXXXXXX99", "923609016", country="SE")],  # same digits, Swedish register
            "meta": {"pagination": {"lastPage": 1}}}
    f = Fake(body)
    batch, c = ctx(f)
    conn = L.LeiConnector()
    conn.prepare(batch)
    out = conn.run(c)
    leis = [cl["value"]["lei"] for cl in out.claims]
    assert leis == ["OW6OFBNCKXC4US5C7523"]
    assert out.claims[0]["evidence_ids"] and out.family_states["lei"]["state"] == "available"
    assert len(f.calls) == 1


def test_lei_not_available_and_failed_states():
    f = Fake({"data": [], "meta": {"pagination": {"lastPage": 1}}})
    batch, c = ctx(f)
    conn = L.LeiConnector()
    conn.prepare(batch)
    assert conn.run(c).family_states["lei"]["state"] == "not_available"
    f = Fake({}, status=503)
    batch, c = ctx(f)
    conn.prepare(batch)
    assert conn.run(c).family_states["lei"]["state"] == "failed"


def award(aid, recipient, date="2026-10-05", amount=2716.63):
    return {"stoettetiltaksnummer": aid, "status": "stoettetiltaksstatus.registrert", "tildelingsdato": date,
            "stoetteinstrument": "stoetteinstrumenttype.skatteEllerAvgiftsFritak",
            "tildeltBeloep": {"beloep": amount, "valuta": "NOK"}, "tilknyttetStoetteordning": "1000029830",
            "eoesRettsligGrunnlag": {"statsstoettekategori": "statsstoettekategori.gruppeunntak",
                                     "primaerrettsligGrunnlag": {"formaal": "formaalstype.regionalstoette"}},
            "rolle": [{"type": "rolletype.stoettegiver", "rolleinnehaverVirksomhet": {"organisasjonsnummer": "974761076", "navn": "SKATTEETATEN"}},
                      {"type": "rolletype.stoettemottaker", "rolleinnehaverVirksomhet": {"organisasjonsnummer": recipient, "navn": "X"}}]}


def test_public_support_publishes_only_awards_to_this_organisation():
    f = Fake({"stoettetildeling": [award("1", ORG, "2026-01-01"), award("2", OTHER), award("3", ORG, "2026-10-05")]})
    _, c = ctx(f)
    out = P.PublicSupportConnector().run(c)
    assert [cl["value"]["award_id"] for cl in out.claims] == ["3", "1"]  # most recent first, other recipient dropped
    v = out.claims[0]["value"]
    assert v["instrument_en"] == "tax or duty exemption" and v["granting_body"] == "SKATTEETATEN"
    assert v["aid_category_en"] == "block-exempted aid" and v["amount"] == 2716.63
    url, kw = f.calls[0]
    assert kw["method"] == "POST" and kw["json_body"][0]["value"] == ORG
    assert out.family_states["public_support"]["state"] == "available"


def test_public_support_none_and_failure():
    _, c = ctx(Fake({"stoettetildeling": []}))
    assert P.PublicSupportConnector().run(c).family_states["public_support"]["state"] == "not_available"
    _, c = ctx(Fake({}, status=500))
    assert P.PublicSupportConnector().run(c).family_states["public_support"]["state"] == "failed"


def test_public_support_caps_published_awards():
    f = Fake({"stoettetildeling": [award(str(i), ORG, f"2026-01-{i % 28 + 1:02d}") for i in range(40)]})
    _, c = ctx(f)
    out = P.PublicSupportConnector().run(c)
    assert len(out.claims) == P.MAX_AWARDS
    assert "40 award(s)" in out.family_states["public_support"]["reason"]


def test_synthesis_mentions_lei_and_support():
    from test_synthesis import envelope, put
    env = envelope([put("legal_name", "EQUINOR ASA"),
                    put("lei_record", {"lei": "OW6OFBNCKXC4US5C7523", "registration_status": "ISSUED",
                                       "initial_registration": "2012-06-06", "next_renewal": "2027-11-09"}, "OW6"),
                    put("public_support", {"award_id": "1", "granted_date": "2026-10-05", "amount": 1000.0,
                                           "currency": "NOK", "instrument_en": "grant", "granting_body": "ENOVA SF"}, "1")])
    texts = [s["text"] for s in syn.summarise(env)["statements"]]
    assert any("Legal Entity Identifier OW6OFBNCKXC4US5C7523" in t for t in texts)
    assert any(t.startswith("Støtteregisteret lists 1 public support award totalling NOK 1,000") for t in texts)


DIBK_HTML = """<html><body><main><h2>SUBMARA SURVEY VEST AS</h2>
<div><b>Organisasjonsnummer</b><p>996871592</p><b>Webside</b><p><a href="http://www.vesethas.no">www.vesethas.no</a></p>
<b>Antall lønnsmottakere</b><p>19</p></div>
<div><h3>Foretaket har sentral godkjenning</h3><b>Godkjenningsperiode:</b> <span>23.07.2024&ndash;23.07.2027</span></div>
<div class="bg-dibk-green p-5"><p>Yrkesskadeforsikring</p><p>Ansvarsforsikring</p></div>
<table><tr><th>Funksjon</th><th>Fagområde</th><th>Tiltaksklasse</th><th></th></tr>
<tr><td>Prosjekterende</td><td>Oppmålingsteknisk prosjektering</td><td>3</td><td>Vis betydning</td></tr>
<tr><td colspan="4">Prosjekterende, tiltaksklasse 3 Kravene ...</td></tr>
<tr><td>Utførende</td><td>Innmåling og utstikking av tiltak</td><td>3</td><td>Vis betydning</td></tr></table>
</main></body></html>"""


def test_central_approval_parse_and_exact_number():
    from signalpost.connectors.central_approval import parse_page
    v = parse_page(DIBK_HTML.encode(), "996871592")
    assert v["approved"] and v["valid_from"] == "2024-07-23" and v["valid_to"] == "2027-07-23"
    assert v["wage_earners"] == 19 and v["website_in_register"] == "www.vesethas.no"
    assert v["areas"] == [{"function": "Prosjekterende", "subject_area": "Oppmålingsteknisk prosjektering", "class": 3},
                          {"function": "Utførende", "subject_area": "Innmåling og utstikking av tiltak", "class": 3}]
    assert parse_page(DIBK_HTML.encode(), "923609016") is None  # page about another organisation


def test_central_approval_connector_states():
    from signalpost.connectors.central_approval import CentralApprovalConnector

    class F(Fake):
        def get(self, url, phase, **kw):
            self.calls.append((url, kw))
            if url.endswith("996871592"):
                return FetchResult(url, url, 200, utcnow(), DIBK_HTML.encode(), "text/html")
            return FetchResult(url, url, 404, utcnow(), b"", "text/html", error="http_404")

    f = F({})
    _, c = ctx(f, "996871592")
    out = CentralApprovalConnector().run(c)
    assert out.claims[0]["field"] == "central_approval" and out.claims[0]["evidence_ids"]
    assert f.calls[0][1]["check_robots"] is True
    _, c = ctx(f, ORG)
    assert CentralApprovalConnector().run(c).family_states["central_approval"]["state"] == "not_available"


def test_procurement_attributes_by_exact_norwegian_winner_id():
    from signalpost.connectors import procurement as T
    body = {"notices": [
        {"publication-number": "43081-2024", "publication-date": "2024-01-22Z", "notice-type": "can-standard",
         "notice-title": {"eng": ["Norway – Construction work – New fire station"]}, "buyer-name": {"eng": ["NTNU"]},
         "winner-identifier": ["923 609 016", "999999999"], "winner-country": ["NOR"]},
        {"publication-number": "1-2024", "publication-date": "2024-02-01Z", "winner-identifier": ["923609016"],
         "winner-country": ["SWE"]},  # same digits, foreign winner: not attributed
    ]}
    f = Fake(body)
    batch, c = ctx(f)
    conn = T.ProcurementConnector()
    conn.prepare(batch)
    out = conn.run(c)
    assert [cl["value"]["publication_number"] for cl in out.claims] == ["43081-2024"]
    v = out.claims[0]["value"]
    assert v["title"] == "New fire station" and v["buyer"] == "NTNU" and v["published"] == "2024-01-22"
    assert f.calls[0][1]["method"] == "POST" and "923609016" in f.calls[0][1]["json_body"]["query"]
    batch, c = ctx(Fake({"notices": []}), OTHER)
    conn.prepare(batch)
    assert conn.run(c).family_states["procurement"]["state"] == "not_available"


SMILE = ("tilsynsobjektid;orgnummer;navn;adrlinje1;adrlinje2;postnr;poststed;tilsynid;sakref;status;dato;total_karakter;"
         "tilsynsbesoektype;tema1_no;tema1_nn;karakter1;tema2_no;tema2_nn;karakter2;tema3_no;tema3_nn;karakter3;tema4_no;tema4_nn;karakter4\n"
         "a;974760673;Egon Restaurant;Karl Johans gate 37;;0162;Oslo;T1;2016/1;0;05012016;1;0;Rutiner og ledelse;x;0;Lokaler;x;0;Mat;x;0;Merking;x;1\n"
         "b;974760673;Egon Restaurant;Karl Johans gate 37;;0162;Oslo;T2;2017/1;0;03022017;2;0;Rutiner og ledelse;x;2;Lokaler;x;0;Mat;x;0;Merking;x;0\n"
         "c;999999999;Other;Gate 1;;0001;Oslo;T3;2017/2;0;03022017;0;0;;;;;;;;;;;;\n")


def test_food_inspections_match_company_or_its_workplaces():
    from signalpost.connectors.food_inspections import FoodInspectionsConnector

    class F(Fake):
        def get(self, url, phase, **kw):
            self.calls.append((url, kw))
            return FetchResult(url, url, 200, utcnow(), SMILE.encode(), "text/csv")

    f = F({})
    reg = Contribution()
    reg.facts["workplaces"] = [{"organisation_number": "974760673"}]
    batch = BatchContext("t", [ORG], f, f.budget, None, {ORG: reg})
    conn = FoodInspectionsConnector()
    conn.prepare(batch)
    out = conn.run(CompanyContext(ORG, batch, reg))
    assert [c["value"]["inspection_id"] for c in out.claims] == ["T2", "T1"]
    v = out.claims[0]["value"]
    assert v["date"] == "2017-02-03" and v["grade"] == 2 and v["workplace_orgnr"] == "974760673"
    assert len(f.calls) == 1
    other = Contribution()
    assert conn.run(CompanyContext(OTHER, batch, other)).family_states["food_inspections"]["state"] == "not_available"
