"""HTML parsing and extraction for the website connector (no network).

Each extractor returns candidates with the exact span it came from (anchor HTML, meta tag, JSON-LD
property or a text window) so every published claim can point at its evidence.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from lxml import etree

from .website_text import (
    EMAIL_RE, OTHER_SOCIAL, SOCIAL_DOMAINS, compact, decode_cfemail, find_labelled_phones, host_of,
    join_digit_groups, norm_phone, norm_space, parse_date, registered_domain, site_key, translit,
)

MAX_HTML_CHARS = 1_500_000

_PLACEHOLDER = (
    "domain is for sale", "domain may be for sale", "domenet er til salgs", "domenet kan vaere til salgs",
    "this domain is parked", "parked domain", "domeneparkering", "denne siden er parkert", "domenet er parkert",
    "buy this domain", "kjop dette domenet", "welcome to nginx", "apache2 ubuntu default page",
    "apache2 debian default page", "test page for the apache", "web server's default page", "default web site page",
    "account suspended", "this account has been suspended", "dette domenet er registrert",
    "this domain has been registered", "domenet er registrert hos", "future home of something quite cool",
    "index of /", "iis windows server", "this site is currently unavailable", "domain not configured",
    "nettstedet er ikke tilgjengelig", "denne nettsiden er ikke publisert", "site not found",
)


@dataclass
class Link:
    url: str
    text: str
    snippet: str
    nav: bool


@dataclass
class Page:
    res: Any  # http.FetchResult
    url: str
    raw: str
    text: str
    digit_text: str
    title: str
    metas: dict[str, tuple[str, str]] = field(default_factory=dict)  # key -> (content, tag html)
    jsonld: list[dict] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    articles: list[dict] = field(default_factory=list)
    cfemails: list[tuple[str, str]] = field(default_factory=list)
    js_shell: bool = False
    placeholder: str | None = None

    @property
    def key(self) -> str:
        return site_key(self.url)


def _strip_fragment(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))


def _jsonld_nodes(data: Any, out: list[dict], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(data, list):
        for x in data:
            _jsonld_nodes(x, out, depth + 1)
    elif isinstance(data, dict):
        if "@graph" in data:
            _jsonld_nodes(data["@graph"], out, depth + 1)
        if "@type" in data:
            out.append(data)
        for k, v in data.items():
            if k != "@graph" and isinstance(v, (dict, list)):
                _jsonld_nodes(v, out, depth + 1)


def _load_jsonld(s: str) -> Any:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except ValueError:
        pass
    s2 = re.sub(r",\s*([}\]])", r"\1", re.sub(r"[\x00-\x1f]", " ", s))
    try:
        return json.loads(s2)
    except ValueError:
        return None


def types_of(node: dict) -> set[str]:
    t = node.get("@type")
    ts = t if isinstance(t, list) else [t]
    return {str(x).lower().rsplit("/", 1)[-1] for x in ts if x}


ORG_TYPES = {
    "organization", "corporation", "localbusiness", "store", "restaurant", "hotel", "dentist", "medicalclinic",
    "medicalbusiness", "autorepair", "automotivebusiness", "autodealer", "homeandconstructionbusiness",
    "professionalservice", "legalservice", "accountingservice", "financialservice", "realestateagent",
    "travelagency", "lodgingbusiness", "foodestablishment", "cafeorcoffeeshop", "bakery", "beautysalon",
    "hairsalon", "healthandbeautybusiness", "plumber", "electrician", "generalcontractor", "roofingcontractor",
    "housepainter", "locksmith", "movingcompany", "ngo", "educationalorganization", "sportsorganization",
    "onlinestore", "onlinebusiness", "employmentagency", "insuranceagency", "attorney", "physician", "optician",
    "veterinarycare", "shoppingcenter", "clothingstore", "furniturestore", "hardwarestore", "homegoodsstore",
    "electronicsstore", "sportinggoodsstore", "bikestore", "petstore", "florist", "grocerystore",
    "entertainmentbusiness", "sportsactivitylocation", "exerciseGym".lower(), "healthclub", "campground",
    "skiresort", "bedandbreakfast", "touristattraction", "governmentorganization", "researchorganization",
    "newsmediaorganization", "airline", "consortium", "cooperative", "fundingscheme", "workersunion",
}


def is_org_node(node: dict) -> bool:
    return bool(types_of(node) & ORG_TYPES)


def parse_page(res: Any) -> Page:
    raw = res.text[:MAX_HTML_CHARS] if res.content else ""
    base = res.final_url or res.url
    soup = BeautifulSoup(raw, "lxml")
    nodes: list[dict] = []
    for s in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        data = _load_jsonld(s.string or s.get_text() or "")
        if data is not None:
            _jsonld_nodes(data, nodes)
    metas: dict[str, tuple[str, str]] = {}
    for m in soup.find_all("meta"):
        k = (m.get("property") or m.get("name") or m.get("itemprop") or "").strip().lower()
        c = m.get("content")
        if k and c and k not in metas:
            metas[k] = (norm_space(c), str(m)[:400])
    feeds = []
    for ln in soup.find_all("link", href=True):
        rel = " ".join(ln.get("rel") or []).lower()
        typ = (ln.get("type") or "").lower()
        if "alternate" in rel and ("rss" in typ or "atom" in typ):
            href = urljoin(base, ln["href"])
            if "comments" not in href.lower():
                feeds.append(href)
    title = norm_space(soup.title.get_text(" ")) if soup.title else ""
    cfemails = []
    for el in soup.find_all(attrs={"data-cfemail": True}):
        e = decode_cfemail(el.get("data-cfemail", ""))
        if e and "@" in e:
            cfemails.append((e.lower(), str(el)[:300]))
    links: list[Link] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "#")):
            continue
        if href.startswith("/cdn-cgi/l/email-protection#"):
            e = decode_cfemail(href.split("#", 1)[1])
            if e and "@" in e:
                cfemails.append((e.lower(), str(a)[:300]))
            continue
        try:
            absu = href if href.startswith(("mailto:", "tel:")) else _strip_fragment(urljoin(base, href))
        except ValueError:
            continue
        nav = a.find_parent(["nav", "header", "footer"]) is not None
        links.append(Link(absu, norm_space(a.get_text(" "))[:200], str(a)[:400], nav))
    articles = []
    for art in soup.find_all("article")[:40]:
        t = art.find("time", attrs={"datetime": True})
        h = art.find(["h1", "h2", "h3", "h4"])
        a = (h.find("a", href=True) if h else None) or art.find("a", href=True)
        articles.append({
            "datetime": t.get("datetime") if t else None,
            "time_html": str(t)[:200] if t else None,
            "title": norm_space(h.get_text(" ")) if h else "",
            "url": _strip_fragment(urljoin(base, a["href"])) if a and not a["href"].startswith(("mailto:", "tel:", "#")) else None,
            "text": norm_space(art.get_text(" "))[:4000],
        })
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = norm_space(soup.get_text(" "))
    low = raw[:200_000].lower()
    js_shell = len(text) < 250 and bool(
        re.search(r"id=[\"'](root|app|__next|__nuxt|svelte|q-app|main-app)[\"']", low)
        or "enable javascript" in low or "requires javascript" in low or "<app-root" in low)
    placeholder = None
    if len(text) < 4000:
        tl = translit(text)
        placeholder = next((p for p in _PLACEHOLDER if p in tl), None)
    return Page(res, base, raw, text, join_digit_groups(text), title, metas, nodes, links, feeds, articles,
                cfemails, js_shell, placeholder)


# ---------------------------------------------------------------- section links
SECTIONS = {
    "contact": re.compile(r"(^|[/_\-\s])(kontakt|contact|finn-oss|finn oss|hvor-er-vi)", re.I),
    "about": re.compile(r"(^|[/_\-\s])(om-oss|om oss|omoss|om-[a-z]+|about|hvem-er-vi|hvem er vi|var-historie|historikk|historie|selskapet)", re.I),
    "news": re.compile(r"(^|[/_\-\s])(nyheter|nyhet|aktuelt|news|blogg|blog|artikler|presse|siste-nytt|nytt|newsroom)", re.I),
    "careers": re.compile(r"(^|[/_\-\s])(karriere|jobb|stilling|ledige|career|jobs|vacanc|bli-med)", re.I),
}
_SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|pptx?|mp4|mp3)$", re.I)


def section_links(page: Page, key: str) -> dict[str, str]:
    """Best same-site link per section (contact/about/news/careers), from the page's own navigation."""
    best: dict[str, tuple[int, str]] = {}
    for ln in page.links:
        if ln.url.startswith(("mailto:", "tel:")) or site_key(ln.url) != key:
            continue
        p = urlsplit(ln.url)
        if _SKIP_EXT.search(p.path) or p.path in ("", "/"):
            continue
        path = translit(p.path).strip("/")
        segs = path.split("/")
        for kind, rx in SECTIONS.items():
            hit_path = bool(rx.search("/" + segs[-1])) if segs else False
            hit_text = bool(rx.search(" " + translit(ln.text).replace(" ", "-")))
            if not (hit_path or hit_text):
                continue
            score = len(segs) * 10 + len(path) + (0 if hit_path else 30) + (0 if ln.nav else 5) + (20 if p.query else 0)
            if kind not in best or score < best[kind][0]:
                best[kind] = (score, ln.url)
    return {k: v[1] for k, v in best.items()}


# ---------------------------------------------------------------- socials
_FB_REJECT = {"sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "login", "login.php", "groups",
              "events", "photo", "photo.php", "photos", "watch", "hashtag", "help", "policies", "privacy", "profile.php",
              "people", "permalink.php", "story.php", "home.php", "ads", "business", "gaming", "marketplace", "legal",
              "terms", "about", "settings", "notes", "media", "reel", "reels", "l.php", "v", "video.php", "search",
              "messages", "pg", "public", "fundraisers", "privacy_policy"}
_IG_REJECT = {"p", "reel", "reels", "explore", "stories", "accounts", "tv", "share", "direct", "about", "legal",
              "developer", "web"}
_X_REJECT = {"intent", "share", "home", "hashtag", "i", "search", "widgets", "widgets.js", "login", "signup", "tos",
             "privacy", "explore", "settings", "messages", "notifications", "compose"}
_PLATFORM_HANDLES = {"facebook", "instagram", "linkedin", "youtube", "twitter", "x", "tiktok", "wix", "wordpress",
                     "squarespace", "shopify", "onecom", "domeneshop", "webnode", "godaddy", "elementor", "themeforest",
                     "envato", "meta", "google", "jimdo", "weebly"}
_HANDLE_OK = re.compile(r"^[A-Za-z0-9._\-]{2,80}$")


def parse_social(url: str) -> tuple[str, str, str] | None:
    """URL -> (platform, handle, canonical profile URL), or None for share links, posts and non-profiles."""
    host = host_of(url)
    if not host:
        return None
    rd = registered_domain(host)
    platform = SOCIAL_DOMAINS.get(rd)
    if not platform or rd == "youtu.be":
        return None
    segs = [s for s in urlsplit(url if "://" in url else "https://" + url).path.split("/") if s]
    if not segs:
        return None
    s0 = segs[0]
    handle = canonical = None
    if platform == "facebook":
        if s0.lower() == "pages" and len(segs) >= 2:
            handle = segs[1]
            canonical = "https://www.facebook.com/pages/" + "/".join(segs[1:3])
        elif s0.lower() not in _FB_REJECT and not s0.endswith(".php"):
            handle = s0
            canonical = f"https://www.facebook.com/{handle}"
    elif platform == "instagram":
        if s0.lower() not in _IG_REJECT:
            handle, canonical = s0, f"https://www.instagram.com/{s0}"
    elif platform == "linkedin":
        if s0.lower() in ("company", "school", "showcase") and len(segs) >= 2:
            handle, canonical = segs[1], f"https://www.linkedin.com/{s0.lower()}/{segs[1]}"
    elif platform == "youtube":
        if s0.startswith("@"):
            handle, canonical = s0[1:], f"https://www.youtube.com/{s0}"
        elif s0.lower() in ("c", "user") and len(segs) >= 2:
            handle, canonical = segs[1], f"https://www.youtube.com/{s0.lower()}/{segs[1]}"
        elif s0.lower() == "channel" and len(segs) >= 2:
            handle, canonical = segs[1], f"https://www.youtube.com/channel/{segs[1]}"
    elif platform == "x":
        if s0.lower() not in _X_REJECT:
            handle, canonical = s0.lstrip("@"), f"https://x.com/{s0.lstrip('@')}"
    elif platform == "tiktok":
        if s0.startswith("@"):
            handle, canonical = s0[1:], f"https://www.tiktok.com/{s0}"
    if not handle or not _HANDLE_OK.match(handle) or handle.lower() in _PLATFORM_HANDLES:
        return None
    return platform, handle, canonical


def social_candidates(page: Page) -> list[dict]:
    out = []
    for ln in page.links:
        s = parse_social(ln.url)
        if s:
            out.append({"platform": s[0], "handle": s[1], "url": s[2], "span": ln.snippet,
                        "method": "html_anchor", "locator": "a[href]"})
    for node in page.jsonld:
        if not is_org_node(node):
            continue
        same = node.get("sameAs") or []
        for u in same if isinstance(same, list) else [same]:
            if isinstance(u, str):
                s = parse_social(u)
                if s:
                    out.append({"platform": s[0], "handle": s[1], "url": s[2],
                                "span": json.dumps({"@type": node.get("@type"), "sameAs": u}, ensure_ascii=False),
                                "method": "jsonld_sameas", "locator": "jsonld:Organization.sameAs"})
    return out


# ---------------------------------------------------------------- brand / description
_GENERIC_BRANDS = {"home", "hjem", "forside", "startside", "my site", "min side", "wordpress", "untitled", "website",
                   "nettside", "hjemmeside", "my wordpress blog", "just another wordpress site", "site title", "velkommen",
                   "welcome", "default", "blog", "shop", "nettbutikk"}


def brand_candidates(page: Page) -> list[dict]:
    out = []
    for node in page.jsonld:
        if is_org_node(node) and isinstance(node.get("name"), str):
            out.append({"value": norm_space(node["name"]), "method": "jsonld_organization",
                        "span": json.dumps({"@type": node.get("@type"), "name": node["name"]}, ensure_ascii=False),
                        "locator": "jsonld:Organization.name"})
    if "og:site_name" in page.metas:
        c, tag = page.metas["og:site_name"]
        out.append({"value": c, "method": "meta_og_site_name", "span": tag, "locator": "meta[property=og:site_name]"})
    for node in page.jsonld:
        if "website" in types_of(node) and isinstance(node.get("name"), str):
            out.append({"value": norm_space(node["name"]), "method": "jsonld_website",
                        "span": json.dumps({"@type": node.get("@type"), "name": node["name"]}, ensure_ascii=False),
                        "locator": "jsonld:WebSite.name"})
    good = []
    for c in out:
        v = c["value"]
        if not (2 <= len(v) <= 80) or v.lower() in _GENERIC_BRANDS or translit(v).startswith(("http", "www.")):
            continue
        good.append(c)
    return good


def description_candidates(page: Page) -> list[dict]:
    out = []
    for node in page.jsonld:
        if is_org_node(node) and isinstance(node.get("description"), str):
            out.append({"value": norm_space(node["description"]), "method": "jsonld_organization",
                        "span": json.dumps({"@type": node.get("@type"), "description": node["description"]}, ensure_ascii=False),
                        "locator": "jsonld:Organization.description"})
    for k, m in (("og:description", "meta_og_description"), ("description", "meta_description")):
        if k in page.metas:
            c, tag = page.metas[k]
            out.append({"value": c, "method": m, "span": tag, "locator": f"meta[{k}]"})
    good = []
    for c in out:
        v = c["value"]
        low = v.lower()
        if len(v) < 40 or any(b in low for b in ("wordpress", "lorem ipsum", "cookie", "javascript", "just another")):
            continue
        if v.strip() == page.title.strip():
            continue
        c["value"] = v[:600]
        good.append(c)
    return good


# ---------------------------------------------------------------- contacts / locations
def contact_candidates(page: Page, key: str, registry_email: str | None) -> list[dict]:
    out = []
    reg_email = (registry_email or "").strip().lower()

    def email_ok(e: str) -> bool:
        e = e.lower()
        return e == reg_email or site_key(e.rsplit("@", 1)[-1]) == key

    for ln in page.links:
        if ln.url.lower().startswith("tel:"):
            p = norm_phone(ln.url[4:])
            if p:
                out.append({"kind": "phone", "value": p, "span": ln.snippet, "method": "html_anchor_tel", "locator": "a[href^=tel:]"})
        elif ln.url.lower().startswith("mailto:"):
            e = ln.url[7:].split("?")[0].strip().lower()
            if EMAIL_RE.fullmatch(e) and email_ok(e):
                out.append({"kind": "email", "value": e, "span": ln.snippet, "method": "html_anchor_mailto", "locator": "a[href^=mailto:]"})
    for e, snip in page.cfemails:
        if EMAIL_RE.fullmatch(e) and email_ok(e):
            out.append({"kind": "email", "value": e, "span": snip, "method": "cloudflare_email_decode", "locator": "[data-cfemail]"})
    for m in EMAIL_RE.finditer(page.text):
        e = m.group(0).lower()
        if email_ok(e):
            out.append({"kind": "email", "value": e, "span": page.text[max(0, m.start() - 60):m.end() + 40],
                        "method": "regex_email", "locator": f"text:{m.start()}"})
    for p, s, e in find_labelled_phones(page.text):
        out.append({"kind": "phone", "value": p, "span": page.text[max(0, s - 40):e + 20], "method": "regex_phone_labelled",
                    "locator": f"text:{s}"})
    for node in page.jsonld:
        if not is_org_node(node):
            continue
        tel = node.get("telephone")
        for t in tel if isinstance(tel, list) else [tel]:
            p = norm_phone(t) if isinstance(t, str) else None
            if p:
                out.append({"kind": "phone", "value": p, "span": json.dumps({"telephone": t}, ensure_ascii=False),
                            "method": "jsonld_organization", "locator": "jsonld:Organization.telephone"})
        em = node.get("email")
        if isinstance(em, str):
            e = em.replace("mailto:", "").strip().lower()
            if EMAIL_RE.fullmatch(e) and email_ok(e):
                out.append({"kind": "email", "value": e, "span": json.dumps({"email": em}, ensure_ascii=False),
                            "method": "jsonld_organization", "locator": "jsonld:Organization.email"})
    return out


STREET_SUFFIX = ("veien", "vegen", "vei", "veg", "gata", "gaten", "gate", "plass", "plassen", "alle", "brygge", "bryggen",
                 "torget", "torg", "bakken", "stien", "sti", "kaia", "kaien", "tunet", "lia", "haugen", "svingen",
                 "ringen", "lokka", "berget", "vollen", "jordet", "parken", "senter", "senteret", "terrassen",
                 "kroken", "sletta", "moen", "marka", "odden", "holmen", "neset", "strand", "stranda", "vika",
                 "bukta", "garden", "tun", "road", "street", "tomta", "hagen", "bryggja", "kollen", "aasen", "asen")
_ADDR_STOP = {"telefon", "tlf", "mobil", "fax", "org", "orgnr", "mva", "postboks", "pb", "boks", "kl", "apent",
              "apningstider", "man", "tir", "ons", "tor", "fre", "lor", "son", "mandag", "tirsdag", "onsdag", "torsdag",
              "fredag", "lordag", "sondag", "copyright", "nr", "side", "uke", "ca", "kr", "nok", "pris", "over",
              "under", "med", "fra", "til", "pa", "og", "i", "for", "the", "kontonr", "konto", "bank", "pris"}
_ADDR_LABEL = {"besoksadresse", "postadresse", "adresse", "address", "besok", "kontor", "kontoradresse",
               "visit", "leveringsadresse", "fakturaadresse", "hovedkontor", "avdeling", "lokasjon", "her"}
_ADDR_RE = re.compile(
    r"\b([A-ZÆØÅ][A-Za-zÆØÅæøåÉéÜüÖöÄä'.\-]{1,}(?: [A-Za-zÆØÅæøåÉéÜüÖöÄä'.\-]{2,}){0,3}) (\d{1,4}(?: ?[A-Za-z])?)\b"
    r"[,|·•–\- ]{1,5}(?:N-|NO-)?(\d{4}) ([A-ZÆØÅ][A-Za-zÆØÅæøå\-]+(?: [A-ZÆØÅ][A-Za-zÆØÅæøå\-]+)?)")


_SEPARATE_SUFFIX = {"vei", "veg", "gate", "gata", "alle", "plass", "vn", "gt", "v", "road", "street", "torg", "brygge"}
_ADDR_NOISE = {"kontakt", "kontaktinfo", "prisliste", "meny", "hjem", "forside", "adresse", "besoksadresse", "postadresse",
               "address", "lokasjon", "her", "finn", "oss", "vi", "holder", "til", "pa", "ligger", "i"}


def _trim_street(words: list[str]) -> list[str]:
    """Keep only the street name: drop text before a legal-form token or label, and leading capitalised noise."""
    from .website_text import LEGAL_FORMS
    tl = [translit(w).strip(".,:") for w in words]
    cut = 0
    for i, t in enumerate(tl):
        if t in LEGAL_FORMS or t in _ADDR_NOISE or t in _ADDR_LABEL:
            cut = i + 1
    words, tl = words[cut:], tl[cut:]
    if not words:
        return []
    last = tl[-1]
    if last in _SEPARATE_SUFFIX:
        return words[-4:] if len(words) > 1 else []  # "Dronning Åsas vei", "Erling Skjalgssons gate"
    if last.endswith(STREET_SUFFIX) and len(last) > 4:
        return words[-1:]                              # "Glimmerveien", "Rådhusgata"
    return words[-2:]


def location_candidates(page: Page, registry_zips: set[str], registry_towns: set[str]) -> list[dict]:
    out = []
    for node in page.jsonld:
        if not is_org_node(node):
            continue
        addrs = node.get("address")
        for a in addrs if isinstance(addrs, list) else [addrs]:
            if isinstance(a, dict) and (a.get("streetAddress") or a.get("postalCode")):
                v = {"street": norm_space(str(a.get("streetAddress") or "")), "postal_code": norm_space(str(a.get("postalCode") or "")),
                     "city": norm_space(str(a.get("addressLocality") or ""))}
                if a.get("addressCountry") and isinstance(a.get("addressCountry"), str):
                    v["country"] = a["addressCountry"]
                v = {k: x for k, x in v.items() if x}
                if v.get("street"):
                    out.append({"value": v, "span": json.dumps({"address": a}, ensure_ascii=False)[:500],
                                "method": "jsonld_postaladdress", "locator": "jsonld:Organization.address"})
    for m in _ADDR_RE.finditer(page.text):
        words = m.group(1).split(" ")
        while words and translit(words[0]).strip(".:") in _ADDR_LABEL:
            words = words[1:]
        if not words or translit(words[0]).strip(".") in _ADDR_STOP or any(translit(w) in _ADDR_STOP for w in words):
            continue
        words = _trim_street(words)
        if not words:
            continue
        street = " ".join(words) + " " + m.group(2)
        last = translit(words[-1]).strip(".")
        zip_ = m.group(3)
        town_words = m.group(4).split(" ")
        town = " ".join(town_words) if translit(" ".join(town_words)) in registry_towns else town_words[0]
        streetish = last.endswith(STREET_SUFFIX) or last in ("gt", "v", "vn", "gt.")
        if not (streetish or zip_ in registry_zips or translit(town) in registry_towns):
            continue
        out.append({"value": {"street": street, "postal_code": zip_, "city": town},
                    "span": page.text[max(0, m.start() - 30):m.end() + 10], "method": "regex_address",
                    "locator": f"text:{m.start()}"})
    return out


def location_key(v: dict) -> str:
    return f"{v.get('postal_code', '')}:{compact(v.get('street', ''))}"


# ---------------------------------------------------------------- news / jobs
ARTICLE_TYPES = {"newsarticle", "article", "blogposting", "reportagenewsarticle", "analysisnewsarticle", "pressrelease",
                 "socialmediaposting", "techarticle"}


def _first_str(v: Any) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v and isinstance(v[0], str):
        return v[0]
    if isinstance(v, dict):
        return v.get("@id") or v.get("url")
    return None


def news_candidates(page: Page) -> list[dict]:
    """Dated items on a page: JSON-LD articles, the page itself as an article, and <article> cards with <time>."""
    out = []
    for node in page.jsonld:
        if not (types_of(node) & ARTICLE_TYPES):
            continue
        pub = parse_date(_first_str(node.get("datePublished")))
        title = _first_str(node.get("headline")) or _first_str(node.get("name"))
        if not pub or not title:
            continue
        url = _first_str(node.get("url")) or _first_str(node.get("mainEntityOfPage")) or page.url
        body = " ".join(x for x in (_first_str(node.get("articleBody")), _first_str(node.get("description"))) if x)
        if url == page.url or _strip_fragment(urljoin(page.url, url)) == page.url:
            body = body + " " + page.text[:6000]
        out.append({"title": norm_space(title)[:300], "url": _strip_fragment(urljoin(page.url, url)), "published": pub,
                    "body": body, "span": json.dumps({"@type": node.get("@type"), "headline": title,
                                                      "datePublished": node.get("datePublished")}, ensure_ascii=False),
                    "method": "jsonld_article", "locator": "jsonld:Article.datePublished"})
    if "article:published_time" in page.metas:
        pub = parse_date(page.metas["article:published_time"][0])
        title = (page.metas.get("og:title") or (page.title, ""))[0]
        og_type = (page.metas.get("og:type") or ("", ""))[0].lower()
        if pub and title and og_type in ("article", "") :
            out.append({"title": norm_space(title)[:300], "url": page.url, "published": pub,
                        "body": page.text[:8000], "span": page.metas["article:published_time"][1] + " " + title,
                        "method": "meta_article_published_time", "locator": "meta[property=article:published_time]"})
    for art in page.articles:
        pub = parse_date(art.get("datetime"))
        if not pub or not art.get("title"):
            continue
        url = art.get("url") or page.url
        if len(page.articles) == 1 and not art.get("url"):
            url = page.url
        out.append({"title": art["title"][:300], "url": url, "published": pub, "body": art["text"],
                    "span": f"{art['time_html']} {art['title']}", "method": "html_article_time",
                    "locator": "article time[datetime]"})
    return out


def parse_feed(res: Any) -> list[dict]:
    """RSS 2.0 / Atom items with their publication date (pubDate/published, never 'updated')."""
    try:
        root = etree.fromstring(res.content, parser=etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    except (etree.XMLSyntaxError, ValueError):
        return []
    if root is None:
        return []
    out = []

    def txt(el: Any, *names: str) -> str:
        for n in names:
            found = el.find(n)
            if found is not None and (found.text or "").strip():
                return found.text.strip()
        return ""

    atom = "{http://www.w3.org/2005/Atom}"
    for item in root.iter("item"):
        title, link, pub = txt(item, "title"), txt(item, "link"), txt(item, "pubDate")
        desc = txt(item, "description", "{http://purl.org/rss/1.0/modules/content/}encoded")
        out.append({"title": title, "url": link, "published_raw": pub, "body": re.sub(r"<[^>]+>", " ", desc),
                    "span": f"<item><title>{title}</title><link>{link}</link><pubDate>{pub}</pubDate></item>"})
    for entry in root.iter(atom + "entry"):
        title, pub = txt(entry, atom + "title"), txt(entry, atom + "published")
        link_el = entry.find(atom + "link")
        link = link_el.get("href") if link_el is not None else ""
        desc = txt(entry, atom + "summary", atom + "content")
        out.append({"title": title, "url": link, "published_raw": pub, "body": re.sub(r"<[^>]+>", " ", desc),
                    "span": f"<entry><title>{title}</title><published>{pub}</published></entry>"})
    for o in out:
        o["published"] = parse_date(o.pop("published_raw"))
        o["title"] = norm_space(o["title"])[:300]
        o["method"] = "feed_item_pubdate"
        o["locator"] = "rss:item/pubDate"
    return [o for o in out if o["published"] and o["title"] and o["url"]]


def job_candidates(page: Page) -> list[dict]:
    out = []
    for node in page.jsonld:
        if "jobposting" not in types_of(node):
            continue
        org = node.get("hiringOrganization")
        org_name = org.get("name") if isinstance(org, dict) else (org if isinstance(org, str) else None)
        title = _first_str(node.get("title"))
        if not title or not org_name:
            continue
        loc = node.get("jobLocation")
        loc = loc[0] if isinstance(loc, list) and loc else loc
        addr = loc.get("address") if isinstance(loc, dict) else None
        city = addr.get("addressLocality") if isinstance(addr, dict) else None
        out.append({"title": norm_space(title)[:200], "hiring_organization": org_name,
                    "url": _strip_fragment(urljoin(page.url, _first_str(node.get("url")) or page.url)),
                    "date_posted": parse_date(_first_str(node.get("datePosted"))),
                    "valid_through": (_first_str(node.get("validThrough")) or "")[:10] or None,
                    "location": city if isinstance(city, str) else None,
                    "span": json.dumps({"@type": "JobPosting", "title": title, "hiringOrganization": org_name,
                                        "datePosted": node.get("datePosted")}, ensure_ascii=False),
                    "method": "jsonld_jobposting", "locator": "jsonld:JobPosting"})
    return out


def iter_unique(items: Iterable[dict], key) -> list[dict]:
    seen, out = set(), []
    for it in items:
        k = key(it)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


__all__ = ["Page", "parse_page", "section_links", "parse_social", "social_candidates", "brand_candidates",
           "description_candidates", "contact_candidates", "location_candidates", "location_key", "news_candidates",
           "parse_feed", "job_candidates", "iter_unique", "OTHER_SOCIAL"]
