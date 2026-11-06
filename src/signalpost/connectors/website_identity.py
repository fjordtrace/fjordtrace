"""Website candidates and the identity gate (pure functions, no network).

Levels, strongest first:

* ``exact``             this organisation number (or one of its subunits') is on the site and no other
                        labelled organisation number appears as often. Site content may be attributed.
* ``strong``            declared by an official/open source, legal name or domain matches, and a registry
                        detail (phone, e-mail, address, role holder) is on the site. Not for group members.
* ``registry_declared`` the register lists the site and nothing contradicts it: URL only, no content.
* ``source_declared``   NAV or Wikidata lists it, name/domain match, nothing contradicts: URL only.
* ``email_declared``    unshared e-mail domain whose label spells the legal name: URL only.
* ``shared``            the register lists the domain for other organisations too: shared_website only.
* ``ambiguous``         a different organisation number dominates, or the candidate is not tied to us.
* ``unverified`` / ``placeholder`` / ``unreachable`` / ``blocked``  nothing is published from the site.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..registry import FREE_MAIL
from .website_parse import parse_social
from .website_text import (
    DIRECTORY_DOMAINS, OTHER_SOCIAL, SOCIAL_DOMAINS, host_of, looks_like_domain, registered_domain, site_key,
)

ORIGIN_RANK = {"registry": 0, "subunit": 1, "wikidata": 2, "nav": 3, "email": 4, "guess": 5}
DECLARED = {"registry", "subunit", "wikidata", "nav", "email"}
LEVEL_RANK = {"exact": 0, "strong": 1, "registry_declared": 2, "source_declared": 3, "email_declared": 4,
              "shared": 5, "ambiguous": 6, "placeholder": 7, "unverified": 8, "blocked": 9, "unreachable": 10,
              "skipped": 11}
CONTENT_LEVELS = {"exact", "strong"}
URL_LEVELS = {"exact", "strong", "registry_declared", "source_declared", "email_declared"}
CONFIDENCE = {"exact": 0.97, "strong": 0.9, "registry_declared": 0.7, "source_declared": 0.6, "email_declared": 0.6,
              "shared": 0.4}


@dataclass
class Candidate:
    key: str
    url: str
    origins: list[str] = field(default_factory=list)
    raw: dict[str, str] = field(default_factory=dict)
    others_site: int = 0   # other organisations listing this site in the register
    others_email: int = 0  # other organisations with an e-mail address on this domain

    @property
    def primary(self) -> str:
        return min(self.origins, key=lambda o: ORIGIN_RANK.get(o, 9))

    @property
    def rank(self) -> int:
        return ORIGIN_RANK.get(self.primary, 9)


@dataclass
class DeclaredSocial:
    origin: str
    raw: str
    platform: str
    handle: str
    url: str


def _as_list(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return _as_list(v.get("url") or v.get("homepage") or v.get("website") or v.get("value"))
    if isinstance(v, (list, tuple, set)):
        out = []
        for x in v:
            out.extend(_as_list(x))
        return out
    return []


def split_urls(raw: str) -> list[str]:
    return [t.strip().strip("'\"<>()") for t in re.split(r"[\s,;]+", raw or "") if t.strip()]


def normalise_url(raw: str) -> str:
    raw = raw.strip()
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw.lstrip("/")
    return raw


def build_candidates(org: str, profile: dict, bulk_row: dict | None, workplaces: list[dict], nav_home: Any,
                     wikidata_site: Any, site_count: Counter, email_count: Counter
                     ) -> tuple[list[Candidate], list[DeclaredSocial], list[str]]:
    """Website candidates (merged by site key, strongest origin first), declared social URLs, and notes."""
    cands: dict[str, Candidate] = {}
    socials: list[DeclaredSocial] = []
    notes: list[str] = []
    bulk_row = bulk_row or {}
    own_site_keys = {site_key(u) for u in split_urls(bulk_row.get("hjemmeside", "")) if looks_like_domain(u)}
    own_email = (bulk_row.get("epostadresse") or "").strip().lower()
    own_email_key = site_key(own_email.rsplit("@", 1)[-1]) if "@" in own_email else ""

    def add(origin: str, raw: str) -> None:
        if not looks_like_domain(raw):
            return
        url = normalise_url(raw)
        host = host_of(url)
        rd = registered_domain(host)
        if rd in SOCIAL_DOMAINS or rd in OTHER_SOCIAL:
            s = parse_social(url)
            if s and origin in ("registry", "subunit"):
                socials.append(DeclaredSocial(origin, raw, s[0], s[1], s[2]))
            else:
                notes.append(f"{origin} website is a social or video platform page ({raw})")
            return
        if rd in DIRECTORY_DOMAINS:
            notes.append(f"{origin} website is a directory or portal page, not a company site ({raw})")
            return
        key = site_key(url)
        if not key:
            return
        c = cands.get(key)
        if c is None:
            c = cands[key] = Candidate(key, url)
            c.others_site = max(0, site_count.get(key, 0) - (1 if key in own_site_keys else 0))
            c.others_email = max(0, email_count.get(key, 0) - (1 if key == own_email_key else 0))
        if origin not in c.origins:
            if c.origins and ORIGIN_RANK[origin] < c.rank:
                c.url = url
            c.origins.append(origin)
            c.raw[origin] = raw

    for u in split_urls(profile.get("hjemmeside", "")):
        add("registry", u)
    for w in workplaces or []:
        for u in split_urls(w.get("website") or ""):
            add("subunit", u)
    for u in _as_list(wikidata_site):
        add("wikidata", u)
    for u in _as_list(nav_home):
        add("nav", u)
    email = (profile.get("epostadresse") or "").strip().lower()
    if "@" in email:
        dom = email.rsplit("@", 1)[-1]
        if dom in FREE_MAIL or registered_domain(dom) in FREE_MAIL:
            notes.append("registry e-mail uses a free mail provider")
        elif registered_domain(dom) in DIRECTORY_DOMAINS or registered_domain(dom) in SOCIAL_DOMAINS:
            pass
        else:
            k = site_key(dom)
            c = cands.get(k)
            if c is None:
                add("email", dom[5:] if dom.startswith("mail.") else dom)
            elif "email" not in c.origins:
                c.origins.append("email")
                c.raw["email"] = dom
    out = sorted(cands.values(), key=lambda c: (c.rank, c.key))
    return out, socials, notes


# ---------------------------------------------------------------- the gate
@dataclass
class IdentityEvidence:
    ours_count: int = 0
    ours_proofs: list[tuple[str, str]] = field(default_factory=list)  # (page url, snippet)
    others: Counter = field(default_factory=Counter)  # labelled foreign org numbers -> count
    other_proofs: dict[str, tuple[str, str]] = field(default_factory=dict)
    name_in_text: bool = False
    domain_match: str | None = None
    corroborations: list[tuple[str, str, str]] = field(default_factory=list)  # (what, page url, snippet)
    redirected_to: str | None = None          # final site key when it differs from the candidate's
    redirect_declared_by_others: int = 0      # register entries listing that final site
    js_shell: bool = False
    foreign_owner: str | None = None          # a different legal entity named in the copyright line


@dataclass
class Decision:
    level: str
    reason: str

    @property
    def content(self) -> bool:
        return self.level in CONTENT_LEVELS

    @property
    def confidence(self) -> float:
        return CONFIDENCE.get(self.level, 0.3)


def decide(cand: Candidate, ev: IdentityEvidence, group_member: bool, related: dict[str, str] | None = None) -> Decision:
    related = related or {}
    if set(cand.origins) == {"guess"}:
        # a name-derived domain is only ever a candidate: exact proof or nothing (never an ambiguous claim)
        if ev.ours_count and not ev.others and not ev.redirected_to:
            return Decision("exact", "organisation number shown on the site")
        if ev.ours_count and ev.others and ev.others.most_common(1)[0][1] < ev.ours_count and not ev.redirected_to:
            return Decision("exact", "organisation number shown on the site")
        return Decision("unverified", "name-derived domain without this organisation number on the site")
    if ev.ours_count:
        top_org, top = (ev.others.most_common(1)[0] if ev.others else (None, 0))
        if top >= ev.ours_count:
            who = f" ({related[top_org]})" if top_org in related else ""
            return Decision("ambiguous", f"site shows this organisation number but also {top_org}{who} at least as often")
        return Decision("exact", "organisation number shown on the site")
    if ev.others:
        top_org = ev.others.most_common(1)[0][0]
        who = f" ({related[top_org]})" if top_org in related else ""
        return Decision("ambiguous", f"site shows organisation number {top_org}{who}, not this organisation's")
    if ev.foreign_owner and not ev.name_in_text:
        return Decision("ambiguous", f"site names {ev.foreign_owner} as its owner, not this organisation")
    foreign_redirect = bool(ev.redirected_to and ev.redirect_declared_by_others > 0)
    declared = set(cand.origins) & {"registry", "subunit"}
    if declared and cand.others_site:
        return Decision("shared", f"domain is listed as website by {cand.others_site} other organisation(s) in the register")
    name_ok = ev.name_in_text or bool(ev.domain_match)
    strong_name = ev.name_in_text or ev.domain_match == "exact"
    email_unshared = "email" not in cand.origins or (not cand.others_email and not cand.others_site)
    if (set(cand.origins) & DECLARED and strong_name and ev.corroborations and not group_member and not foreign_redirect
            and not cand.others_site and email_unshared):
        return Decision("strong", "declared by " + "/".join(sorted(set(cand.origins) & DECLARED))
                        + "; name and registry details found on the site")
    if foreign_redirect:
        return Decision("ambiguous", f"redirects to {ev.redirected_to}, which other organisations list as their website")
    if declared:
        return Decision("registry_declared", "listed in the register; ownership not proven on the site")
    if set(cand.origins) & {"wikidata", "nav"}:
        if cand.others_site:
            return Decision("ambiguous", "candidate domain is listed as website by another organisation in the register")
        if name_ok:
            return Decision("source_declared", "listed by " + "/".join(sorted(set(cand.origins) & {"wikidata", "nav"}))
                            + " and the name matches; ownership not proven on the site")
        return Decision("ambiguous", "declared by NAV/Wikidata but neither name nor domain match the legal name")
    if "email" in cand.origins:
        if cand.others_email or cand.others_site:
            return Decision("ambiguous", "e-mail domain is shared with other organisations in the register")
        if ev.domain_match:
            return Decision("email_declared", "unshared registry e-mail domain spells the legal name; ownership not proven on the site")
        return Decision("ambiguous", "site at the registry e-mail domain is not tied to this organisation")
    return Decision("unverified", "candidate not tied to this organisation")


def best(decisions: list[tuple[Candidate, Decision]]) -> tuple[Candidate, Decision] | None:
    if not decisions:
        return None
    return min(decisions, key=lambda cd: (LEVEL_RANK.get(cd[1].level, 99), cd[0].rank))
