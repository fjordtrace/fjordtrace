"""Offline tests for the website connector: org-number detection, identity gate, socials, news rules."""
from __future__ import annotations

import threading
from collections import Counter

import pytest

from signalpost.budget import Budget
from signalpost.connectors import website as W
from signalpost.connectors.base import BatchContext, CompanyContext
from signalpost.connectors.website_identity import Candidate, IdentityEvidence, build_candidates, decide
from signalpost.connectors.website_parse import news_candidates, parse_feed, parse_page, parse_social, section_links
from signalpost.connectors.website_text import (
    find_orgnrs, guess_domains, handle_matches, mentions, name_domain_match, name_tokens, parse_date, site_key,
)
from signalpost.http import FetchResult
from signalpost.model import Contribution, make_evidence

ORG = "923609016"      # valid mod-11
PARENT = "914778271"   # valid mod-11
SUB = "974760673"      # valid mod-11


def res(url: str, html: str, status: int = 200, ct: str = "text/html; charset=utf-8") -> FetchResult:
    return FetchResult(url, url, status, "2026-11-06T00:00:00Z", html.encode("utf-8"), ct)


# ---------------------------------------------------------------- text helpers
@pytest.mark.parametrize("text", [
    f"Org.nr: {ORG[:3]} {ORG[3:6]} {ORG[6:]}",
    f"Foretaksregisteret NO {ORG} MVA",
    f"NO{ORG}MVA",
    f"Organisasjonsnummer {ORG[:3]}.{ORG[3:6]}.{ORG[6:]}",
    f"Org nr {ORG}",
])
def test_orgnr_formats_labelled(text):
    hits = find_orgnrs(text)
    assert [h.org for h in hits] == [ORG]
    assert hits[0].labelled


def test_orgnr_rejects_invalid_checksum_and_long_numbers():
    assert find_orgnrs("Org.nr 923 609 017") == []          # bad check digit
    assert find_orgnrs("Konto 1234 56 78901, ref 99236090161") == []
    unl = find_orgnrs(f"Ring oss, kode {ORG} i dag")
    assert unl and not unl[0].labelled


def test_name_tokens_and_domain_match():
    assert name_tokens("BERGEN RØR A/S") == ["bergen", "ror"]
    assert name_tokens("Sollifjellet Alpinsenter SA") == ["sollifjellet", "alpinsenter"]
    assert name_domain_match("BERGEN RØR AS", "bergenror.no") == "exact"
    assert name_domain_match("BERGEN RØR AS", "bergen-ror.no") == "exact"
    assert name_domain_match("SOLLIFJELLET ALPINSENTER SA", "sollifjellet.no") == "partial"
    assert name_domain_match("NORDMANN HOLDING AS", "bigconsulting.no") is None
    assert guess_domains("Bergen Rør AS") == ["bergenror.no", "bergen-ror.no"]
    assert guess_domains("Holding AS") == []


def test_site_key_platforms():
    assert site_key("https://www.firma.no/om") == "firma.no"
    assert site_key("user.wixsite.com") == "user.wixsite.com"
    assert site_key("bergen.kommune.no") == "bergen.kommune.no"


def test_parse_date_rules():
    assert parse_date("2025-03-04T10:00:00+01:00") == "2025-03-04"
    assert parse_date("Tue, 04 Mar 2025 10:00:00 +0000") == "2025-03-04"
    assert parse_date("2999-01-01") is None
    assert parse_date("1990-01-01") is None
    assert parse_date("yesterday") is None


# ---------------------------------------------------------------- socials
@pytest.mark.parametrize("url,expected", [
    ("https://www.facebook.com/bergenror/", ("facebook", "bergenror")),
    ("https://facebook.com/sharer/sharer.php?u=x", None),
    ("https://www.facebook.com/profile.php?id=123", None),
    ("https://www.instagram.com/p/abc123/", None),
    ("https://www.instagram.com/bergen_ror", ("instagram", "bergen_ror")),
    ("https://www.linkedin.com/company/bergen-ror-as/", ("linkedin", "bergen-ror-as")),
    ("https://www.linkedin.com/in/ola-nordmann", None),
    ("https://twitter.com/intent/tweet?text=x", None),
    ("https://x.com/bergenror", ("x", "bergenror")),
    ("https://www.youtube.com/@BergenRor", ("youtube", "BergenRor")),
    ("https://www.youtube.com/watch?v=abc", None),
    ("https://www.tiktok.com/@bergenror", ("tiktok", "bergenror")),
    ("https://www.facebook.com/wix", None),
])
def test_parse_social(url, expected):
    got = parse_social(url)
    assert (got[:2] if got else None) == expected


def test_handle_matching():
    aliases = ["bergenror", "bergenror"]
    assert handle_matches("bergen_ror_as", aliases)
    assert handle_matches("BergenRor", aliases)
    assert not handle_matches("webbyraaet", aliases)
    assert not handle_matches("envato", aliases)


# ---------------------------------------------------------------- identity gate
def cand(origins=("registry",), others_site=0, others_email=0, key="bergenror.no"):
    return Candidate(key, f"https://{key}/", list(origins), {o: key for o in origins}, others_site, others_email)


def test_gate_exact():
    ev = IdentityEvidence(ours_count=2)
    assert decide(cand(), ev, group_member=True).level == "exact"


def test_gate_parent_conflict_is_ambiguous():
    ev = IdentityEvidence(ours_count=0, others=Counter({PARENT: 2}))
    d = decide(cand(), ev, group_member=True, related={PARENT: "parent BERGEN GRUPPEN AS"})
    assert d.level == "ambiguous" and PARENT in d.reason and "BERGEN GRUPPEN" in d.reason


def test_gate_sister_tie_is_ambiguous():
    ev = IdentityEvidence(ours_count=1, others=Counter({PARENT: 1}))
    assert decide(cand(), ev, group_member=False).level == "ambiguous"


def test_gate_shared_registry_domain():
    assert decide(cand(others_site=3), IdentityEvidence(), False).level == "shared"


def test_gate_registry_declared_without_proof():
    d = decide(cand(), IdentityEvidence(), False)
    assert d.level == "registry_declared" and not d.content


def test_gate_strong_needs_name_and_corroboration_and_not_group():
    ev = IdentityEvidence(name_in_text=True, corroborations=[("phone:telefon", "u", "s")])
    assert decide(cand(), ev, group_member=False).level == "strong"
    assert decide(cand(), ev, group_member=True).level == "registry_declared"
    ev2 = IdentityEvidence(name_in_text=True)
    assert decide(cand(), ev2, group_member=False).level == "strong"  # declared + legal name, nothing against it
    assert decide(cand(), ev2, group_member=True).level == "registry_declared"
    assert decide(cand(), IdentityEvidence(), group_member=False).level == "registry_declared"
    ev3 = IdentityEvidence(corroborations=[("address", "u", "s")])  # declared + registered address, no name
    assert decide(cand(), ev3, group_member=False).level == "strong"
    assert decide(cand(), IdentityEvidence(corroborations=[("person", "u", "s")]), False).level == "registry_declared"


def test_gate_guess_needs_exact():
    ev = IdentityEvidence(name_in_text=True, domain_match="exact", corroborations=[("person", "u", "s")])
    assert decide(cand(origins=("guess",)), ev, False).level == "unverified"
    assert decide(cand(origins=("guess",)), IdentityEvidence(ours_count=1), False).level == "exact"


def test_gate_email_domain_rules():
    assert decide(cand(origins=("email",)), IdentityEvidence(domain_match="exact"), False).level == "email_declared"
    assert decide(cand(origins=("email",), others_email=2), IdentityEvidence(domain_match="exact"), False).level == "ambiguous"
    assert decide(cand(origins=("email",)), IdentityEvidence(), False).level == "ambiguous"


def test_gate_redirect_to_someone_elses_domain():
    ev = IdentityEvidence(redirected_to="bergengruppen.no", redirect_declared_by_others=1)
    assert decide(cand(), ev, False).level == "ambiguous"


def test_build_candidates_sources_and_sharing():
    profile = {"hjemmeside": "www.bergenror.no", "epostadresse": "post@bergenror.no"}
    bulk = dict(profile)
    cands, socials, notes = build_candidates(ORG, profile, bulk, [{"organisation_number": SUB, "website": "bergenror.no/bergen"}],
                                             "https://www.bergenror.no", None, Counter({"bergenror.no": 1}),
                                             Counter({"bergenror.no": 1}))
    assert len(cands) == 1
    c = cands[0]
    assert c.origins == ["registry", "subunit", "nav", "email"] and c.others_site == 0 and c.others_email == 0

    profile = {"hjemmeside": "https://www.facebook.com/bergenror", "epostadresse": "ola@gmail.com"}
    cands, socials, notes = build_candidates(ORG, profile, profile, [], None, None, Counter(), Counter())
    assert cands == [] and socials and socials[0].handle == "bergenror"
    assert any("free mail" in n for n in notes)

    profile = {"hjemmeside": "www.usbl.no"}
    cands, _, _ = build_candidates(ORG, profile, profile, [], None, None, Counter({"usbl.no": 900}), Counter())
    assert cands[0].others_site == 899


# ---------------------------------------------------------------- news rules
def test_news_needs_real_date():
    html = """<html><body>
      <article><h2><a href="/nyheter/a">Bergen Rør åpner nytt lager</a></h2><time datetime="2025-05-01">1. mai</time>
        <p>Bergen Rør har åpnet lager.</p></article>
      <article><h2><a href="/nyheter/b">Uten dato</a></h2><p>Bergen Rør tekst</p></article>
      <div><time datetime="2025-01-01"></time><a href="/nyheter/c">Utenfor artikkel</a></div>
    </body></html>"""
    p = parse_page(res("https://bergenror.no/nyheter", html))
    items = news_candidates(p)
    assert [i["url"] for i in items] == ["https://bergenror.no/nyheter/a"]
    assert items[0]["published"] == "2025-05-01"
    assert mentions(items[0]["title"] + " " + items[0]["body"], ["bergen ror"])


def test_news_jsonld_and_feed():
    html = """<html><head><script type="application/ld+json">
      {"@context":"https://schema.org","@type":"BlogPosting","headline":"Nytt fra Bergen Rør",
       "datePublished":"2024-11-02T08:00:00Z","url":"https://bergenror.no/blogg/nytt"}</script></head><body></body></html>"""
    items = news_candidates(parse_page(res("https://bergenror.no/blogg/nytt", html)))
    assert items and items[0]["published"] == "2024-11-02" and items[0]["method"] == "jsonld_article"
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
      <item><title>Bergen Rør vant pris</title><link>https://bergenror.no/vant</link>
      <pubDate>Mon, 03 Mar 2025 10:00:00 +0000</pubDate><description>Bergen Rør vant</description></item>
      <item><title>Udatert</title><link>https://bergenror.no/u</link></item></channel></rss>"""
    items = parse_feed(res("https://bergenror.no/feed", rss, ct="application/rss+xml"))
    assert [i["url"] for i in items] == ["https://bergenror.no/vant"] and items[0]["published"] == "2025-03-03"


def test_section_links_and_js_shell():
    html = """<html><body><nav><a href="/kontakt-oss">Kontakt</a><a href="/om-oss/">Om oss</a>
      <a href="/aktuelt">Aktuelt</a><a href="https://other.no/kontakt">x</a></nav></body></html>"""
    p = parse_page(res("https://bergenror.no/", html))
    links = section_links(p, "bergenror.no")
    assert links["contact"].endswith("/kontakt-oss") and links["about"].endswith("/om-oss/") and links["news"].endswith("/aktuelt")
    shell = parse_page(res("https://app.no/", '<html><body><div id="root"></div><script src="/a.js"></script></body></html>'))
    assert shell.js_shell and shell.placeholder is None
    parked = parse_page(res("https://x.no/", "<html><body>This domain is for sale! Buy this domain.</body></html>"))
    assert parked.placeholder


# ---------------------------------------------------------------- end-to-end with a fake fetcher
class FakeFetcher:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self._robots: dict = {}
        self._robots_lock = threading.Lock()
        self.calls: list[str] = []
        self.budget = Budget(1000, 3600)

    def get(self, url, phase, **kw):
        self.calls.append(url)
        self.budget.take(phase)
        if url.endswith("/robots.txt"):
            return res(url, "User-agent: *\nAllow: /\n", ct="text/plain")
        if url in self.pages:
            return res(url, self.pages[url])
        return FetchResult(url, url, 404, "2026-11-06T00:00:00Z", b"", "text/html", error="http_404")


def registry_contribution(profile: dict, roles=None, group=None, workplaces=None) -> Contribution:
    c = Contribution()
    e = make_evidence(f"https://data.brreg.no/enhetsregisteret/api/enheter/{ORG}", "official_registry_live",
                      "2026-11-06T00:00:00Z", "abc", "x", "brreg_entity_api_json")
    from signalpost.model import make_claim
    c.add(make_claim("registry_contact", {"website": profile.get("hjemmeside")}, [e]), e)
    c.facts.update({"profile": profile, "roles": roles or [], "group": group or [], "workplaces": workplaces or []})
    return c


def run_connector(monkeypatch, pages, profile, site_count=None, group=None, roles=None, namesakes=None):
    monkeypatch.setattr(W, "check_url_safe", lambda url: None)
    f = FakeFetcher(pages)

    class Snap:
        rows = {ORG: profile}
        site_domain_count = site_count or Counter({"bergenror.no": 1})
        email_domain_count = Counter()
    Snap.namesakes = {ORG: namesakes} if namesakes is not None else {}

    batch = BatchContext("t", [ORG], f, f.budget, Snap(), {})
    conn = W.WebsiteConnector()
    conn.prepare(batch)
    reg = registry_contribution(profile, roles=roles, group=group)
    out = conn.run(CompanyContext(ORG, batch, reg))
    return out, f


HOME = f"""<html><head><title>Bergen Rør</title>
<meta property="og:site_name" content="Bergen Rør">
<meta name="description" content="Bergen Rør leverer rørleggertjenester i hele Bergen og omegn siden 1987.">
<link rel="alternate" type="application/rss+xml" href="https://bergenror.no/feed/">
<script type="application/ld+json">{{"@type":"Plumber","name":"Bergen Rør AS","sameAs":["https://www.instagram.com/bergenror"]}}</script>
</head><body><nav><a href="/kontakt">Kontakt</a><a href="/nyheter">Nyheter</a></nav>
<p>Velkommen</p>
<footer>Bergen Rør AS · Storgata 12, 5003 Bergen · <a href="tel:+4755123456">55 12 34 56</a>
<a href="mailto:post@bergenror.no">post@bergenror.no</a> · Org.nr {ORG[:3]} {ORG[3:6]} {ORG[6:]}
<a href="https://www.facebook.com/bergenror">Facebook</a> <a href="https://www.facebook.com/sharer.php?u=x">Del</a>
<a href="https://www.linkedin.com/company/webbyraaet">Laget av Webbyrået</a></footer></body></html>"""


def test_end_to_end_exact_site(monkeypatch):
    pages = {
        "https://www.bergenror.no/": HOME,
        "https://bergenror.no/kontakt": "<html><body>Kontakt oss</body></html>",
        "https://bergenror.no/nyheter": "<html><body><p>Ingen datoer her</p></body></html>",
    }
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>Bergen Rør utvider</title>
      <link>https://bergenror.no/utvider</link><pubDate>Mon, 03 Mar 2025 10:00:00 +0000</pubDate>
      <description>Nyheter fra oss</description></item></channel></rss>"""
    pages["https://bergenror.no/feed/"] = feed
    profile = {"navn": "BERGEN RØR AS", "hjemmeside": "www.bergenror.no", "telefon": "55 12 34 56"}
    out, f = run_connector(monkeypatch, pages, profile)
    by = {}
    for c in out.claims:
        by.setdefault(c["field"], []).append(c)
    assert by["official_website"][0]["value"]["identity"] == "exact"
    assert by["public_brand"][0]["value"] in ("Bergen Rør AS", "Bergen Rør")
    socials = {c["key"] for c in by["social_profile"]}
    assert socials == {"social_profile:facebook:bergenror", "social_profile:instagram:bergenror"}
    assert {c["value"]["value"] for c in by["contact_detail"]} == {"+4755123456", "post@bergenror.no"}
    assert by["site_location"][0]["value"]["postal_code"] == "5003"
    assert by["news_item"][0]["value"]["published"] == "2025-03-03"
    assert "company_description" in by
    ev_ids = {e["id"] for e in out.evidence}
    for c in out.claims:
        if c["availability"] == "available":
            assert c["evidence_ids"] and set(c["evidence_ids"]) <= ev_ids
    assert out.family_states["official_website"]["state"] == "available"
    assert len(f.calls) <= W.MAX_REQ


def test_end_to_end_parent_site_is_ambiguous(monkeypatch):
    html = f"<html><body><h1>Bergen Gruppen</h1><footer>Bergen Gruppen AS org.nr {PARENT}</footer>" \
           f"<a href='https://www.facebook.com/bergenror'>fb</a></body></html>"
    profile = {"navn": "BERGEN RØR AS", "hjemmeside": "www.bergenror.no"}
    out, _ = run_connector(monkeypatch, {"https://www.bergenror.no/": html}, profile,
                           group=[{"organisation_number": PARENT, "name": "BERGEN GRUPPEN AS", "relation": "parent"}])
    fields = {c["field"]: c for c in out.claims}
    assert fields["official_website"]["availability"] == "ambiguous"
    assert "social_profile" not in fields and "public_brand" not in fields
    assert out.family_states["official_website"]["state"] == "ambiguous"


def test_end_to_end_registry_declared_publishes_url_only(monkeypatch):
    html = "<html><head><title>Velkommen</title><meta property='og:site_name' content='Rørleggeren'></head>" \
           "<body><a href='https://www.facebook.com/bergenror'>fb</a></body></html>"
    profile = {"navn": "BERGEN RØR AS", "hjemmeside": "www.bergenror.no"}
    out, _ = run_connector(monkeypatch, {"https://www.bergenror.no/": html}, profile)
    fields = [c["field"] for c in out.claims]
    assert fields == ["official_website"]
    assert out.claims[0]["value"]["identity"] == "registry_declared"
    assert out.family_states["social_profiles"]["state"] == "not_available"


def test_end_to_end_shared_domain(monkeypatch):
    profile = {"navn": "SOLBAKKEN BORETTSLAG", "hjemmeside": "www.usbl.no"}
    out, f = run_connector(monkeypatch, {}, profile, site_count=Counter({"usbl.no": 500}))
    assert [c["field"] for c in out.claims] == ["shared_website"]
    assert f.calls == []  # heavily shared sites are not fetched


def test_end_to_end_no_candidates_never_raises(monkeypatch):
    profile = {"navn": "AB", "epostadresse": "ola@gmail.com"}
    out, _ = run_connector(monkeypatch, {}, profile)
    assert out.claims == []
    for fam in ("official_website", "social_profiles", "news_activity"):
        assert out.family_states[fam]["state"] == "not_available"


def test_address_trimming():
    html = """<html><body><p>DEHN NORGE AS Rådhusgata 11, 3211 Sandefjord E-post: x</p>
      <p>Prisliste Kontakt Welhavensgate 1 2821 Gjøvik Adresse</p><p>Besøk oss i Dronning Åsas vei 10, 3117 Tønsberg</p></body></html>"""
    from signalpost.connectors.website_parse import location_candidates
    got = {(c["value"]["street"], c["value"]["postal_code"], c["value"]["city"])
           for c in location_candidates(parse_page(res("https://x.no/", html)), set(), set())}
    assert ("Rådhusgata 11", "3211", "Sandefjord") in got
    assert ("Welhavensgate 1", "2821", "Gjøvik") in got
    assert any(s.endswith("Åsas vei 10") for s, _, _ in got)


def test_guess_conflict_is_silent():
    ev = IdentityEvidence(others=Counter({PARENT: 1}))
    assert decide(cand(origins=("guess",)), ev, False).level == "unverified"
    ev = IdentityEvidence(ours_count=1, redirected_to="other.no")
    assert decide(cand(origins=("guess",)), ev, False).level == "unverified"


def test_copyright_owner_conflict():
    from signalpost.connectors.website_text import copyright_entities, same_entity_name
    ents = copyright_entities("Ta kontakt marius@x.mobi © 2026 Salamander Consulting AS blog")
    assert ents == ["Salamander Consulting AS"]
    assert not same_entity_name(ents[0], "SALAMANDER FINANS AS")
    assert same_entity_name("Bergen Rør AS", "BERGEN RØR AS")
    assert copyright_entities("© 2024 alle rettigheter, sa han") == []
    ev = IdentityEvidence(foreign_owner="Salamander Consulting AS", domain_match="partial",
                          corroborations=[("person", "u", "s")])
    assert decide(cand(), ev, False).level == "ambiguous"
    # partial domain match plus one person is not strong any more
    ev = IdentityEvidence(domain_match="partial", corroborations=[("person", "u", "s")])
    assert decide(cand(), ev, False).level == "registry_declared"


def test_url_is_not_a_mention():
    assert mentions("Les mer https://sollifjellet.no/wp-content/Sollifjellet_LOGO.png", ["sollifjellet"]) is None
    assert mentions("Velkommen til Sollifjellet i vinter", ["sollifjellet"]) == "sollifjellet"


def test_heavily_shared_email_domain_is_not_fetched(monkeypatch):
    profile = {"navn": "MIDT GUL BRL", "epostadresse": "styret@styrerommet.no"}
    monkeypatch.setattr(W, "check_url_safe", lambda url: None)
    f = FakeFetcher({})

    class Snap:
        rows = {ORG: profile}
        site_domain_count = Counter()
        email_domain_count = Counter({"styrerommet.no": 4000})

    batch = BatchContext("t", [ORG], f, f.budget, Snap(), {})
    conn = W.WebsiteConnector()
    conn.prepare(batch)
    out = conn.run(CompanyContext(ORG, batch, registry_contribution(profile)))
    assert out.claims == [] and f.calls == []
    assert out.family_states["official_website"]["state"] == "not_available"


def test_end_to_end_registry_declared_with_name_is_strong(monkeypatch):
    html = ("<html><head><title>Bergen Rør</title></head>"
            "<body><p>Bergen Rør AS leverer rørtjenester.</p><a href='https://www.facebook.com/bergenror'>fb</a></body></html>")
    profile = {"navn": "BERGEN RØR AS", "hjemmeside": "www.bergenror.no"}
    out, _ = run_connector(monkeypatch, {"https://www.bergenror.no/": html}, profile)
    site = next(c for c in out.claims if c["field"] == "official_website")
    assert site["value"]["identity"] == "strong"
    assert any(c["field"] == "social_profile" for c in out.claims)


def test_gate_guess_legal_name_with_form_and_unique_name():
    base = dict(name_in_text=True, legal_name_in_text=True, domain_match="exact")
    g = cand(origins=("guess",))
    assert decide(g, IdentityEvidence(**base, namesakes=0), False).level == "strong"
    assert decide(g, IdentityEvidence(**base, namesakes=1), False).level == "unverified"    # a similar name exists
    assert decide(g, IdentityEvidence(**base, namesakes=None), False).level == "unverified"  # unknown without bulk
    assert decide(g, IdentityEvidence(**base, namesakes=0), True).level == "unverified"     # group member
    assert decide(g, IdentityEvidence(**base, namesakes=0, redirected_to="x.no"), False).level == "unverified"
    assert decide(g, IdentityEvidence(**base, namesakes=0, foreign_owner="Other AS"), False).level == "unverified"
    assert decide(g, IdentityEvidence(name_in_text=True, domain_match="exact", namesakes=0), False).level == "unverified"
    person = IdentityEvidence(name_in_text=True, namesakes=0, corroborations=[("person", "u", "s")])
    assert decide(g, person, False).level == "strong"
    assert decide(g, IdentityEvidence(name_in_text=True, namesakes=2, corroborations=[("person", "u", "s")]),
                  False).level == "unverified"


def test_namesake_counts():
    from signalpost.registry import namesake_counts
    names = ["KRAUMAN AS", "NYBØ AS", "NYBØ MEDIA AS", "HANSEN OG NYBØ AS", "TUNNCON AS", "TUNNCONSULT AS"]
    got = namesake_counts(names, {"1": "KRAUMAN AS", "2": "NYBØ AS", "3": "TUNNCON AS"})
    assert got == {"1": 0, "2": 2, "3": 0}


def test_legal_name_phrase():
    from signalpost.connectors.website_text import legal_name_phrase
    assert legal_name_phrase("KRAUMAN AS") == "krauman as"
    assert legal_name_phrase("NYBØ A/S") == "nybo as"
    assert legal_name_phrase("Hansen Tømrer Ola Hansen") is None


def test_end_to_end_guess_with_full_legal_name(monkeypatch):
    html = ("<html><head><title>Hjem | Krauman AS</title></head>"
            "<body><p>Vi bygger hus.</p><a href='https://www.facebook.com/krauman'>fb</a></body></html>")
    profile = {"navn": "KRAUMAN AS"}
    out, _ = run_connector(monkeypatch, {"https://krauman.no/": html}, profile, site_count=Counter(), namesakes=0)
    site = next(c for c in out.claims if c["field"] == "official_website")
    assert site["value"]["identity"] == "strong"
    assert site["evidence_ids"], "a published site needs evidence"
    spans = [e["claim_span"] for e in out.evidence if e["id"] in site["evidence_ids"]]
    assert any("Krauman AS" in s for s in spans)
    # same page, but a similar name exists in the register: not published
    out2, _ = run_connector(monkeypatch, {"https://krauman.no/": html}, profile, site_count=Counter(), namesakes=3)
    assert not any(c["field"] == "official_website" and c["availability"] == "available" for c in out2.claims)


def test_gate_declared_by_email_nav_wikidata_with_legal_name():
    legal = dict(name_in_text=True, legal_name_in_text=True, namesakes=0)
    assert decide(cand(origins=("email",)), IdentityEvidence(**legal), False).level == "strong"
    assert decide(cand(origins=("email",), others_email=1), IdentityEvidence(**legal), False).level == "ambiguous"
    assert decide(cand(origins=("email",)), IdentityEvidence(**legal), True).level != "strong"
    assert decide(cand(origins=("nav",)), IdentityEvidence(name_in_text=True), False).level == "strong"
    assert decide(cand(origins=("wikidata",)), IdentityEvidence(name_in_text=True), True).level == "source_declared"
    assert decide(cand(origins=("nav",)), IdentityEvidence(domain_match="exact"), False).level == "source_declared"
