"""Pure text helpers for the website connector: names, domains, org numbers, phones, dates.

Nothing here touches the network. tldextract runs from its bundled suffix-list snapshot so results are
offline-safe and deterministic.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import tldextract

from ..registry import valid_org

_TLD = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=(), include_psl_private_domains=True)

# Site builders / hosts where the registered domain is shared by unrelated customers: the key is the full host.
PLATFORM_DOMAINS = {
    "wixsite.com", "wix.com", "squarespace.com", "webnode.no", "webnode.com", "webnode.page", "wordpress.com",
    "weebly.com", "jimdo.com", "jimdofree.com", "jimdosite.com", "business.site", "godaddysites.com", "site123.me",
    "strikingly.com", "mozello.com", "carrd.co", "webflow.io", "netlify.app", "vercel.app", "github.io",
    "000webhostapp.com", "hjemmeside.no", "123hjemmeside.no", "one.com", "onepage.website", "simplesite.com",
    "mystrikingly.com", "framer.website", "shopify.com", "myshopify.com", "tictail.com", "wpengine.com",
    "kommune.no", "fylkeskommune.no", "idrettenonline.no", "klubbenonline.no", "spond.com", "minidrett.no",
    "rubic.no", "styreweb.com", "enkelexamen.no",
}
# Directories, marketplaces and portals: never a company's own website, even when listed as one.
DIRECTORY_DOMAINS = {
    "proff.no", "gulesider.no", "1881.no", "purehelp.no", "brreg.no", "finn.no", "google.com", "google.no",
    "goo.gl", "g.page", "bing.com", "mittanbud.no", "firmasok.no", "linktr.ee", "booking.com", "tripadvisor.com",
    "tripadvisor.no", "yelp.com", "altinn.no", "regjeringen.no", "nav.no", "arbeidsplassen.no", "bedrift.no",
    "kompass.com", "dnb.no", "vg.no", "nrk.no", "aftenposten.no", "e24.no", "dn.no", "wikipedia.org",
    "airbnb.com", "airbnb.no", "hotels.com", "expedia.com", "apple.com", "microsoft.com", "dropbox.com",
    "forhandler.no", "legelisten.no", "visitnorway.com", "visitnorway.no", "bookup.no", "timma.no",
}
SOCIAL_DOMAINS = {
    "facebook.com": "facebook", "fb.com": "facebook", "fb.me": "facebook", "instagram.com": "instagram",
    "linkedin.com": "linkedin", "youtube.com": "youtube", "youtu.be": "youtube", "twitter.com": "x", "x.com": "x",
    "tiktok.com": "tiktok",
}
OTHER_SOCIAL = {"pinterest.com", "snapchat.com", "vimeo.com", "flickr.com", "threads.net", "spotify.com"}

LEGAL_FORMS = {"as", "asa", "sa", "ba", "da", "ans", "nuf", "ks", "iks", "kf", "fkf", "sf", "enk", "bl", "bbl", "ab",
               "aps", "gmbh", "ltd", "limited", "inc", "llc", "oy", "konkursbo", "nuf"}
GENERIC = {"og", "and", "the", "i", "av", "for", "de", "co", "company", "norge", "norway", "norsk", "norske", "holding",
           "holdings", "group", "gruppen", "invest", "investment", "eiendom", "consulting", "service", "services",
           "drift", "stiftelsen", "stiftelse", "forening", "foreningen", "borettslag", "sameie", "selskap",
           "selskapet", "nordic", "scandinavia", "international", "solutions", "tjenester"}

_TR = str.maketrans({"æ": "ae", "ø": "o", "å": "a", "ä": "a", "ö": "o", "ü": "u", "ß": "ss", "œ": "oe", "ð": "d",
                     "þ": "th", " ": " "})


def translit(s: str) -> str:
    s = (s or "").lower().translate(_TR)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace(" ", " ")).strip()


def _all_name_tokens(name: str) -> list[str]:
    s = translit(name)
    s = re.sub(r"\ba\s*/\s*s\b", " as ", s)
    s = re.sub(r"\ba\.\s?s\b\.?", " as ", s)
    s = s.replace("&", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.split()


def name_tokens(name: str) -> list[str]:
    """Legal name -> lower-case ASCII tokens without the trailing legal-form suffix."""
    toks = _all_name_tokens(name)
    while len(toks) > 1 and toks[-1] in LEGAL_FORMS:
        toks.pop()
    return toks


COMPANY_FORMS = {"as", "asa", "sa", "ba", "da", "ans", "ks", "nuf"}


def legal_name_phrase(name: str) -> str | None:
    """Full legal name with its company form ("krauman as"). Two companies cannot share this exact name in the
    Norwegian register, so it identifies one entity. None when the name carries no company form."""
    toks = _all_name_tokens(name)
    if len(toks) < 2 or toks[-1] not in COMPANY_FORMS:
        return None
    return " ".join(toks)


def name_core(name: str) -> str:
    return " ".join(name_tokens(name))


def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", translit(s))


# ---------------------------------------------------------------- domains
def host_of(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "http://" + url
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host


def _idna(host: str) -> str:
    if "xn--" not in host:
        return host
    try:
        return host.encode("ascii").decode("idna")
    except (UnicodeError, ValueError):
        return host


def registered_domain(host: str) -> str:
    host = (host or "").lower().strip(".")
    if not host:
        return ""
    ext = _TLD(host)
    return ext.top_domain_under_public_suffix or host


def site_key(url_or_host: str) -> str:
    """Identity key for a site: the registered domain, or the full host on shared site builders."""
    host = host_of(url_or_host) if ("/" in (url_or_host or "") or ":" in (url_or_host or "")) else (url_or_host or "").lower()
    host = host[4:] if host.startswith("www.") else host
    rd = registered_domain(host)
    if not rd:
        return host
    if rd in PLATFORM_DOMAINS and host != rd:
        return host
    return rd


def domain_label(key: str) -> str:
    """Brand-bearing label of a site key: 'bergen-ror' for bergen-ror.no, the user part on site builders."""
    key = _idna(key or "")
    ext = _TLD(key)
    label = ext.domain
    rd = ext.top_domain_under_public_suffix
    if rd in PLATFORM_DOMAINS and ext.subdomain:
        label = ext.subdomain.split(".")[-1]
    return compact(label)


def looks_like_domain(s: str) -> bool:
    host = host_of(s)
    return bool(host and "." in host and _TLD(host).suffix and re.match(r"^[a-z0-9.-]+$", host))


def name_domain_match(name: str, key: str) -> str | None:
    """'exact' when the domain label spells the legal name, 'partial' when one clearly contains the other."""
    label = domain_label(key)
    toks = name_tokens(name)
    if not label or len(label) < 3 or not toks:
        return None
    full = "".join(toks)
    distinct = "".join(t for t in toks if t not in GENERIC)
    if label == full or (len(distinct) >= 4 and label == distinct):
        return "exact"
    if len(label) >= 5 and label in full and len(label) >= 0.5 * len(full):
        return "partial"
    if len(full) >= 5 and full in label:
        return "partial"
    if len(distinct) >= 5 and (distinct in label or (label in distinct and len(label) >= 0.6 * len(distinct))):
        return "partial"
    return None


def guess_domains(name: str) -> list[str]:
    """At most two .no guesses from the legal name (compact and hyphenated)."""
    toks = name_tokens(name)
    if not toks or len(toks) > 4:
        return []
    full = "".join(toks)
    if len(full) < 4 or len(full) > 40 or full.isdigit():
        return []
    if all(t in GENERIC for t in toks):
        return []
    out = [full + ".no"]
    if len(toks) > 1:
        out.append("-".join(toks) + ".no")
    return out


# ---------------------------------------------------------------- org numbers
_ORG_RE = re.compile(r"(?<!\d)((?<![A-Za-z])NO[\s.]?)?(\d{3})[ \t .]?(\d{3})[ \t .]?(\d{3})(?!\d)")
_LABEL_RE = re.compile(r"(org|foretak|organisa|mva|vat\b|reg\.?\s?n|bedriftsn|enhetsreg|company\s+(no|number|reg)|"
                       r"business\s+id|registration)", re.I)


@dataclass
class OrgHit:
    org: str
    labelled: bool
    start: int
    end: int


def find_orgnrs(text: str) -> list[OrgHit]:
    """Valid (mod-11) Norwegian organisation numbers in text, with whether a label sits next to them."""
    out = []
    for m in _ORG_RE.finditer(text or ""):
        num = m.group(2) + m.group(3) + m.group(4)
        if not valid_org(num):
            continue
        before = text[max(0, m.start() - 40):m.start()]
        after = text[m.end():m.end() + 8].lower()
        labelled = bool(m.group(1)) or bool(_LABEL_RE.search(before)) or "mva" in after
        out.append(OrgHit(num, labelled, m.start(), m.end()))
    return out


def raw_contains_org(raw: str, org: str) -> int:
    """Occurrences of the bare 9-digit number in raw HTML (links to brreg/proff, JSON-LD vatID, etc.)."""
    return len(re.findall(r"(?<!\d)" + re.escape(org) + r"(?!\d)", raw or ""))


def find_name_span(text: str, name: str, with_form: bool = False) -> tuple[int, int] | None:
    """Where the legal name (optionally with its company form) is written in the original page text, matched
    case-insensitively with flexible separators, so the evidence span is verbatim page text."""
    toks = [x for x in re.split(r"[^\w]+", (name or "").lower()) if x]
    if not with_form:
        while len(toks) > 1 and translit(toks[-1]) in LEGAL_FORMS:
            toks.pop()
    if not toks:
        return None
    pat = r"(?<!\w)" + r"[\s\-&./,]*".join(re.escape(x) for x in toks) + r"(?!\w)"
    m = re.search(pat, text or "", re.I)
    return (m.start(), m.end()) if m else None


def window(text: str, start: int, end: int, pad: int = 70) -> str:
    return norm_space(text[max(0, start - pad):min(len(text), end + pad)])


# ---------------------------------------------------------------- phones / emails
def norm_phone(s: str) -> str | None:
    d = re.sub(r"\D", "", s or "")
    if d.startswith("0047"):
        d = d[4:]
    elif d.startswith("47") and len(d) == 10:
        d = d[2:]
    if len(d) == 8 and d[0] in "2345679":
        return "+47" + d
    return None


_PHONE_RE = re.compile(r"(?<![\d+])(?:(?:\+|00)47[\s .]?)?(\d{2}[\s .]?\d{2}[\s .]?\d{2}[\s .]?\d{2}"
                       r"|\d{3}[\s .]?\d{2}[\s .]?\d{3})(?![\d])")
_PHONE_LABEL = re.compile(r"(tlf|telefon|phone|mob|tel\b|tel\.|ring oss|sentralbord|kontakt)", re.I)


def find_labelled_phones(text: str) -> list[tuple[str, int, int]]:
    out = []
    for m in _PHONE_RE.finditer(text or ""):
        before = text[max(0, m.start() - 25):m.start()]
        if not _PHONE_LABEL.search(before):
            continue
        p = norm_phone(m.group(0))
        if p:
            out.append((p, m.start(), m.end()))
    return out


def phone_in_text(digit_text: str, phone: str) -> int:
    """Position of an 8-digit Norwegian number in text whose digit groups were joined, or -1."""
    p = norm_phone(phone)
    if not p:
        return -1
    m = re.search(r"(?<!\d)(?:0047|47)?" + p[3:] + r"(?!\d)", digit_text)
    return m.start() if m else -1


def join_digit_groups(text: str) -> str:
    return re.sub(r"(?<=\d)[  .\-](?=\d)", "", text or "")


EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,10}(?![\w-])")


def decode_cfemail(hexstr: str) -> str | None:
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------- dates
def parse_date(s: str | None, today: datetime | None = None) -> str | None:
    """ISO 8601 or RFC 822 -> 'YYYY-MM-DD'. Rejects implausible dates (before 1995 or in the future)."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    dt = None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    else:
        try:
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError):
            return None
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    today = today or datetime.now(timezone.utc)
    if dt.year < 1995 or dt > today + timedelta(days=2):
        return None
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- aliases / mentions
def alias_phrases(names: list[str]) -> list[str]:
    """Distinct lower-case ASCII phrases (>=4 chars) that name the company."""
    out: list[str] = []
    for n in names:
        if not n:
            continue
        p = " ".join(re.sub(r"[^a-z0-9]+", " ", translit(n)).split())
        toks = name_tokens(n)
        cands = [p, " ".join(toks)]
        for c in cands:
            if len(c.replace(" ", "")) >= 4 and c not in out and not all(t in GENERIC for t in c.split()):
                out.append(c)
    return out


def mentions(text: str, phrases: list[str]) -> str | None:
    """First alias phrase that occurs as whole words in text (accent-folded), or None."""
    text = re.sub(r"(https?://|www\.)\S+|\S+@\S+", " ", text or "")  # a URL or address is not a mention
    t = " " + " ".join(re.sub(r"[^a-z0-9]+", " ", translit(text)).split()) + " "
    for p in phrases:
        if f" {p} " in t:
            return p
        # one-word brand written together or apart ("solli fjellet" vs "sollifjellet")
        pc = p.replace(" ", "")
        if " " in p and len(pc) >= 8 and f" {pc} " in t:
            return p
    return None


def handle_matches(handle: str, compacts: list[str]) -> bool:
    """A social handle carries the company's name, brand or domain."""
    h = compact(handle)
    if len(h) < 3:
        return False
    for a in compacts:
        if len(a) < 4:
            continue
        if a in h:
            return True
        if len(h) >= 5 and h in a and len(h) >= 0.6 * len(a):
            return True
    return False


_COPYRIGHT_RE = re.compile(
    r"(?i:©|&copy;|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?\s*[,:|-]?\s*"
    r"((?:[A-ZÆØÅ0-9][\w&.'\-]*\s+){1,5}?(?:AS|ASA|SA|DA|ANS|BA|NUF|A/S))(?![\w/])")


def copyright_entities(text: str) -> list[str]:
    """Legal-entity names in copyright notices ("© 2026 Salamander Consulting AS")."""
    return [norm_space(m.group(1)) for m in _COPYRIGHT_RE.finditer(text or "")]


def same_entity_name(a: str, b: str) -> bool:
    ca, cb = "".join(name_tokens(a)), "".join(name_tokens(b))
    return bool(ca and cb) and (ca == cb or (min(len(ca), len(cb)) >= 5 and (ca in cb or cb in ca)))
