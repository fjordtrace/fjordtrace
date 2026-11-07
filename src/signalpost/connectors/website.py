"""Company website connector: find the official site, prove it belongs to this organisation, then read it.

Candidates come from the register (website, subunit websites, e-mail domain), NAV employer homepages,
Wikidata P856 and, as a last resort, two name-derived .no guesses. The identity gate in
``website_identity`` decides what may be published: site content (brand, description, contact details,
locations, social profiles, dated news, JSON-LD job postings) only for ``exact`` (org number on the site)
or ``strong`` proof; a URL-only claim for register-declared sites; ``shared_website`` for domains many
organisations list; and an ``ambiguous`` claim with the candidate URL when a different organisation shows.

Budget: at most ``SIGNALPOST_WEBSITE_MAX_REQUESTS`` (default 10) requests per company including robots.txt.
No browser: JavaScript-only sites are reported, not rendered.
"""
from __future__ import annotations

import os
import re
import traceback
from datetime import datetime, timezone
import urllib.robotparser
from collections import Counter
from typing import Any
from urllib.parse import urlsplit

from ..http import FetchResult, UnsafeURL, check_url_safe
from ..model import Contribution, make_claim, make_evidence
from .base import BatchContext, CompanyContext, Connector
from .website_identity import (
    CONTENT_LEVELS, URL_LEVELS, Candidate, Decision, IdentityEvidence, best, build_candidates, decide,
)
from .website_parse import (
    Page, brand_candidates, contact_candidates, description_candidates, iter_unique, job_candidates,
    location_candidates, location_key, news_candidates, parse_feed, parse_page, section_links, social_candidates,
)
from .website_text import (
    alias_phrases, compact, domain_label, extra_guess_domains, find_orgnrs, guess_domains, handle_matches, find_name_span, legal_name_phrase, mentions, name_core,
    name_domain_match, name_tokens, norm_space, copyright_entities, same_entity_name, phone_in_text, raw_contains_org, site_key, translit, window, GENERIC,
    DIRECTORY_DOMAINS, OTHER_SOCIAL, SOCIAL_DOMAINS,
)

PHASE = "website"
# Optional, evaluator-supplied search key (Builderr allows search to nominate candidates, never as evidence).
SEARCH_KEY = os.environ.get("BRAVE_SEARCH_API_KEY") or os.environ.get("SIGNALPOST_BRAVE_KEY") or ""
SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SEARCH_COST = float(os.environ.get("SIGNALPOST_SEARCH_COST_USD", "0.005") or 0.005)
SEARCH_MIN_REVENUE = float(os.environ.get("SIGNALPOST_SEARCH_MIN_REVENUE", "1000000") or 1e6)
MAX_REQ = int(os.environ.get("SIGNALPOST_WEBSITE_MAX_REQUESTS", "10") or 10)
TIMEOUT = float(os.environ.get("SIGNALPOST_WEBSITE_TIMEOUT", "10") or 10)
MAX_BYTES = 1_500_000
HEAVY_SHARED = 4          # register entries for other organisations above which a shared site is not fetched
MAX_NEWS = 6
MAX_SOCIAL_PER_PLATFORM = 2
SOURCE_CLASS = "company_owned"
REGISTRAR_DOMAINS = {"webhuset.no", "domeneshop.no", "one.com", "uniweb.no", "proisp.no", "godaddy.com", "sedo.com",
                     "dan.com", "afternic.com", "hugedomains.com", "namecheap.com", "domainnameshop.com", "loopia.no",
                     "loopia.com", "hostinger.com", "simply.com", "pro.no", "nettsjef.no", "netim.com", "gandi.net",
                     "bodis.com", "parkingcrew.net", "above.com", "squarespace-domains.com", "wix.com"}
ACCESS = "robots.txt allowed; public page"


class WebsiteConnector(Connector):
    name = "website"
    families = ("official_website", "social_profiles", "news_activity")
    lane = "website"
    lane_workers = 16

    def prepare(self, batch: BatchContext) -> None:
        batch.shared["website"] = _domain_counts(batch.snapshot)

    def run(self, ctx: CompanyContext) -> Contribution:
        out = Contribution()
        try:
            counts = ctx.batch.shared.get("website") or {}
            if "site" not in counts:
                counts = _domain_counts(ctx.batch.snapshot)
                ctx.batch.shared["website"] = counts
            _CompanyRun(ctx, out, counts).run()
        except Exception as exc:  # never raise: record and mark what is still open as failed
            out.error(self.name, traceback.format_exc(limit=4))
            for fam in self.families:
                if fam not in out.family_states:
                    out.state(fam, "failed", f"website connector error: {type(exc).__name__}")
        return out


def _domain_counts(snapshot: Any) -> dict[str, Counter]:
    site, email = Counter(), Counter()
    for host, n in (getattr(snapshot, "site_domain_count", None) or {}).items():
        k = site_key(host)
        if k:
            site[k] += n
    for dom, n in (getattr(snapshot, "email_domain_count", None) or {}).items():
        k = site_key(dom)
        if k:
            email[k] += n
    return {"site": site, "email": email}


def _err_kind(res: FetchResult) -> str:
    e = res.error or ""
    if e.startswith("unsafe_url: dns failure"):
        return "dns"
    if e.startswith("unsafe_url"):
        return "unsafe"
    if e == "robots_disallow":
        return "robots"
    if e == "budget_exhausted":
        return "budget"
    if res.state_hint == "blocked":
        return "blocked"
    if res.status and res.status >= 400:
        return "http"
    return "connect"


class _CompanyRun:
    def __init__(self, ctx: CompanyContext, out: Contribution, counts: dict[str, Counter]):
        self.ctx, self.out, self.counts = ctx, out, counts
        self.org = ctx.org
        self.profile = ctx.profile or {}
        facts = ctx.registry.facts or {}
        self.name = self.profile.get("navn") or ""
        self.roles = facts.get("roles") or []
        self.workplaces = facts.get("workplaces") or []
        self.group = facts.get("group") or []
        self.ours = {self.org} | {w.get("organisation_number") for w in self.workplaces if w.get("organisation_number")}
        self.related = {g.get("organisation_number"): f"{g.get('relation')} {g.get('name')}".strip()
                        for g in self.group if g.get("organisation_number")}
        self.used = 0
        self.probed: dict[str, str] = {}
        self.truncated = False
        self.cand_log: list[dict] = []
        self.fail_reasons: list[tuple[str, str]] = []  # (state, reason) for unreachable candidates
        self._news_pages = 0
        self._feed_items: list = []

    # ------------------------------------------------------------------ fetching
    def _can_spend(self, n: int = 1) -> bool:
        if self.used + n > MAX_REQ or self.ctx.budget.soft_deadline_passed() or self.ctx.budget.requests_left() < n:
            self.truncated = True
            return False
        return True

    def _probe(self, origin: str) -> str:
        """Fetch robots.txt once per origin (seeding the fetcher's robots cache) and detect dead hosts early."""
        f = self.ctx.fetcher
        cache, lock = getattr(f, "_robots", None), getattr(f, "_robots_lock", None)
        if cache is None or lock is None:
            return "ok"
        with lock:
            if origin in cache:
                return "ok"
        if not self._can_spend(2):
            return "budget"
        res = f.get(origin + "/robots.txt", phase=PHASE, check_robots=False, polite=True, max_bytes=500_000, timeout=TIMEOUT)
        kind = _err_kind(res) if res.error else ""
        if kind in ("budget",):
            self.truncated = True
            return "budget"
        if kind not in ("dns", "unsafe"):
            self.used += 1
        if res.status is None:
            return "dead:" + (res.error or "no response")[:160]
        parser = None
        if res.status == 200 and res.content:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(res.text.splitlines())
        elif res.status in (401, 403):
            parser = urllib.robotparser.RobotFileParser()
            parser.disallow_all = True
        with lock:
            cache.setdefault(origin, parser)
        return "ok"

    def fetch(self, url: str) -> FetchResult | None:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.probed:
            self.probed[origin] = self._probe(origin)
        st = self.probed[origin]
        if st == "budget":
            return None
        if st.startswith("dead:"):
            return FetchResult(url, url, None, "", error=st[5:])
        if not self._can_spend(1):
            return None
        res = self.ctx.fetcher.get(url, phase=PHASE, check_robots=True, polite=True, max_bytes=MAX_BYTES, timeout=TIMEOUT)
        if _err_kind(res) == "budget" and res.error:
            self.truncated = True
            return None
        if _err_kind(res) not in ("dns", "unsafe", "robots") or not res.error:
            self.used += 1
        return res

    def fetch_page(self, url: str) -> Page | None:
        res = self.fetch(url)
        if res is None or not res.ok:
            return None
        if not _is_html(res):
            return None
        return parse_page(res)

    def fetch_home(self, cand: Candidate) -> tuple[Page | None, str, str]:
        """Homepage of a candidate: https first, then http; the www/apex twin only when DNS fails.
        Returns (page, state, reason) where state is ok / not_available / blocked / failed."""
        p = urlsplit(cand.url)
        host = (p.hostname or "").lower()
        path = p.path or "/"
        twin = host[4:] if host.startswith("www.") else "www." + host
        last = ("not_available", "site did not respond")
        for h in (host, twin):
            try:
                check_url_safe(f"https://{h}/")
            except UnsafeURL as exc:
                last = ("not_available", f"domain does not resolve ({h})" if "dns" in str(exc) else f"unsafe address ({h})")
                continue
            for scheme in ("https", "http"):
                res = self.fetch(f"{scheme}://{h}{path}")
                if res is None:
                    return None, "blocked", "website request budget or time budget reached"
                if res.ok and _is_html(res):
                    return parse_page(res), "ok", ""
                kind = _err_kind(res)
                if res.ok:
                    return None, "not_available", f"homepage is not HTML ({res.content_type[:40]})"
                if kind == "robots":
                    return None, "blocked", f"robots.txt disallows {h}"
                if kind == "blocked":
                    return None, "blocked", f"site refused access (HTTP {res.status})"
                if kind == "http":
                    if res.status in (404, 410) and path != "/":
                        path = "/"
                        res2 = self.fetch(f"{scheme}://{h}/")
                        if res2 is not None and res2.ok and _is_html(res2):
                            return parse_page(res2), "ok", ""
                    last = ("failed" if (res.status or 0) >= 500 else "not_available", f"site returned HTTP {res.status}")
                    break
                err = (res.error or "").split(":")[0]
                last = ("failed", f"site unreachable: {err}")
            else:
                continue
            break
        return None, last[0], last[1]

    # ------------------------------------------------------------------ evidence helpers
    def ev(self, page: Page, span: str, method: str, locator: str | None = None) -> dict:
        r = page.res
        return make_evidence(r.url, SOURCE_CLASS, r.retrieved_at, r.content_sha256, norm_space(span), method,
                             final_url=r.final_url, http_status=r.status, locator=locator, access_policy=ACCESS)

    def registry_ev(self, field_value: str, origin: str, workplace_org: str | None = None) -> dict | None:
        """Re-use the registry evidence record that carried the website/e-mail field."""
        reg = self.ctx.registry
        evs = {e["id"]: e for e in reg.evidence}
        if origin == "subunit" and workplace_org:
            claim = next((c for c in reg.claims if c["field"] == "workplace"
                          and (c.get("value") or {}).get("organisation_number") == workplace_org), None)
            col = "underenhet.hjemmeside"
        else:
            claim = next((c for c in reg.claims if c["field"] == "registry_contact"), None)
            col = "hjemmeside" if origin == "registry" else "epostadresse"
        if not claim or not claim.get("evidence_ids") or claim["evidence_ids"][0] not in evs:
            return None
        src = evs[claim["evidence_ids"][0]]
        return make_evidence(src["source_url"], src["source_class"], src["retrieved_at"], src["content_sha256"],
                             f"organisasjonsnummer={self.org}; {col}={field_value}", "brreg_contact_field",
                             final_url=src.get("final_url"), locator=f"row:{col}", access_policy=src.get("access_policy"))

    # ------------------------------------------------------------------ identity
    def scan(self, page: Page, ev: IdentityEvidence) -> None:
        hits = find_orgnrs(page.text)
        found_ours = 0
        for h in hits:
            if h.org in self.ours:
                found_ours += 1
                if len(ev.ours_proofs) < 3:
                    ev.ours_proofs.append((page.url, window(page.text, h.start, h.end)))
            elif h.labelled:
                ev.others[h.org] += 1
                ev.other_proofs.setdefault(h.org, (page.url, window(page.text, h.start, h.end)))
        if not found_ours:
            for o in sorted(self.ours):
                n = raw_contains_org(page.raw, o)
                if n:
                    found_ours += 1
                    i = page.raw.find(o)
                    if len(ev.ours_proofs) < 3:
                        ev.ours_proofs.append((page.url, norm_space(page.raw[max(0, i - 80):i + 40])))
                    break
        ev.ours_count += found_ours
        core = name_core(self.name)
        if core and len(core.replace(" ", "")) >= 4 and mentions(page.text + " " + page.title, [core]):
            ev.name_in_text = True
            phrase = legal_name_phrase(self.name)
            legal = bool(phrase and mentions(page.text + " " + page.title, [phrase]))
            if legal and not ev.legal_name_in_text:
                ev.legal_name_in_text = True
                ev.name_proof = None  # prefer a snippet with the full legal name
            if ev.name_proof is None:
                for src in (page.text, page.title):
                    span = find_name_span(src, self.name, with_form=legal) or find_name_span(src, self.name)
                    if span:
                        ev.name_proof = (page.url, window(src, *span))
                        break
        ev.js_shell = ev.js_shell or page.js_shell
        if not ev.foreign_owner:
            for ent in copyright_entities(page.text):
                if not same_entity_name(ent, self.name):
                    ev.foreign_owner = ent
                    break
        self.corroborate(page, ev)

    def corroborate(self, page: Page, ev: IdentityEvidence) -> None:
        have = {c[0] for c in ev.corroborations}
        for col in ("telefon", "mobil"):
            v = self.profile.get(col)
            if v and f"phone:{col}" not in have:
                i = phone_in_text(page.digit_text, v)
                if i >= 0:
                    ev.corroborations.append((f"phone:{col}", page.url, page.digit_text[max(0, i - 50):i + 30]))
        email = (self.profile.get("epostadresse") or "").strip().lower()
        low = page.text.lower()
        if email and "email" not in have and (email in low or any(email in ln.url.lower() for ln in page.links if ln.url.startswith("mailto:"))):
            i = low.find(email)
            ev.corroborations.append(("email", page.url, page.text[max(0, i - 40):i + len(email) + 20] if i >= 0 else f"mailto:{email}"))
        street = (self.profile.get("forretningsadresse.adresse") or "").split(",")[0].strip()
        if street and "address" not in have and any(ch.isdigit() for ch in street) and len(street) >= 6:
            t = " " + " ".join(translit(page.text).split()) + " "
            s = " ".join(translit(street).split())
            i = t.find(" " + s)
            if i >= 0 and not t[i + 1 + len(s):i + 2 + len(s)].isdigit():
                ev.corroborations.append(("address", page.url, t[max(0, i - 40):i + len(s) + 40]))
        if "person" not in have:
            tl = " " + " ".join(translit(page.text).split()) + " "
            for r in self.roles:
                if r.get("holder_type") != "person":
                    continue
                toks = translit(r.get("name") or "").split()
                if len(toks) < 2:
                    continue
                for variant in {" ".join(toks), f"{toks[0]} {toks[-1]}"}:
                    i = tl.find(f" {variant} ")
                    if i >= 0:
                        ev.corroborations.append(("person", page.url, tl[max(0, i - 40):i + len(variant) + 40]))
                        break
                else:
                    continue
                break

    def evaluate(self, cand: Candidate) -> tuple[Decision, list[Page], IdentityEvidence]:
        ev = IdentityEvidence(domain_match=name_domain_match(self.name, cand.key),
                              domain_spells_full_name=domain_label(cand.key) == "".join(name_tokens(self.name)),
                              namesakes=(getattr(self.ctx.batch.snapshot, "namesakes", None) or {}).get(self.org))
        group_member = bool(self.group)
        if set(cand.origins) & {"registry", "subunit"} and cand.others_site >= HEAVY_SHARED:
            return decide(cand, ev, group_member, self.related), [], ev
        if set(cand.origins) <= {"email", "guess"} and cand.others_email + cand.others_site >= HEAVY_SHARED:
            return Decision("unverified", f"registry e-mail domain {cand.key} is shared by "
                            f"{cand.others_email + cand.others_site} other organisations (manager or provider)"), [], ev
        home, state, reason = self.fetch_home(cand)
        if home is None:
            self.fail_reasons.append((state, reason))
            lvl = "blocked" if state == "blocked" else "unreachable"
            return Decision(lvl, reason), [], ev
        final_path = translit(urlsplit(home.url).path)
        if not home.placeholder and home.key != cand.key and (
                home.key in REGISTRAR_DOMAINS or re.search(r"park(ering|ed|ing)|domain-?for-?sale|til-?salgs", final_path)):
            home.placeholder = f"redirects to a registrar or parking page ({home.key})"
        if home.placeholder:
            return Decision("placeholder", f"domain shows a parked or placeholder page ({home.placeholder})"), [home], ev
        pages = [home]
        final_key = home.key
        if final_key != cand.key:
            ev.redirected_to = final_key
            ev.redirect_declared_by_others = self.counts["site"].get(final_key, 0)
            ev.domain_match = ev.domain_match or name_domain_match(self.name, final_key)
        self.scan(home, ev)
        if not ev.ours_count:
            links = section_links(home, final_key)
            for kind in ("contact", "about"):
                if ev.ours_count or kind not in links:
                    continue
                pg = self.fetch_page(links[kind])
                if pg is not None and pg.key == final_key:
                    pages.append(pg)
                    self.scan(pg, ev)
        return decide(cand, ev, group_member, self.related), pages, ev

    # ------------------------------------------------------------------ main
    def run(self) -> None:
        nav = (self.ctx.batch.shared.get("nav_jobs") or {}).get("homepage_by_org", {}).get(self.org)
        wd = ((self.ctx.batch.shared.get("wikidata") or {}).get("by_org", {}).get(self.org) or {}).get("website")
        bulk_row = (getattr(self.ctx.batch.snapshot, "rows", None) or {}).get(self.org)
        cands, declared_socials, notes = build_candidates(self.org, self.profile, bulk_row, self.workplaces, nav, wd,
                                                          self.counts.get("site", Counter()), self.counts.get("email", Counter()))
        self.publish_declared_socials(declared_socials)
        guessed = False
        if not cands and self._guesses_allowed():
            cands = self.guess_candidates()
            guessed = bool(cands)
        results: list[tuple[Candidate, Decision, list[Page], IdentityEvidence]] = []
        fetched_cands = 0
        for cand in cands[:4]:
            if any(r[1].level in CONTENT_LEVELS for r in results):
                break
            if fetched_cands >= (3 if guessed else 2) or (results and not self._can_spend(3)):
                break
            before = self.used
            d, pages, ev = self.evaluate(cand)
            fetched_cands += self.used > before
            results.append((cand, d, pages, ev))
            self.cand_log.append({"url": cand.url, "key": cand.key, "origins": cand.origins, "level": d.level,
                                  "reason": d.reason, **({"others_listing": cand.others_site} if cand.others_site else {})})
        if not any(r[1].level in CONTENT_LEVELS for r in results) and self._search_allowed():
            tried = {c.key for c, _, _, _ in results}
            for cand in self.search_candidates()[:2]:
                if cand.key in tried or not self._can_spend(3):
                    continue
                d, pages, ev = self.evaluate(cand)
                results.append((cand, d, pages, ev))
                self.cand_log.append({"url": cand.url, "key": cand.key, "origins": cand.origins, "level": d.level,
                                      "reason": d.reason})
                if d.level in CONTENT_LEVELS:
                    guessed = False
                    break
        self.out.facts["website_candidates"] = self.cand_log
        pick = best([(c, d) for c, d, _, _ in results])
        if pick is None:
            why = "; ".join(notes) if notes else ""
            if guessed:
                reason = "no website candidate in registry, e-mail domain, NAV or Wikidata; name-derived .no guesses not proven"
            else:
                reason = "no website candidate in registry, e-mail domain, NAV or Wikidata" + (f" ({why})" if why else "")
            self.finish_without_site("not_available", reason)
            return
        cand, dec = pick
        _, _, pages, ev = next(r for r in results if r[0] is cand)
        if dec.level in CONTENT_LEVELS:
            self.publish_verified(cand, dec, pages, ev)
        elif dec.level in URL_LEVELS:
            self.publish_url_only(cand, dec, pages, ev)
        elif dec.level == "shared":
            self.publish_shared(cand, dec, pages)
        elif dec.level == "ambiguous":
            self.publish_ambiguous(cand, dec, pages, ev)
        else:
            if dec.level == "blocked":
                self.finish_without_site("blocked", dec.reason)
            elif dec.level == "unreachable":
                st = next((s for s, _ in self.fail_reasons if s == "failed"), "not_available")
                self.finish_without_site(st, dec.reason)
            else:
                self.finish_without_site("not_available", dec.reason)
        self.out.facts["website_requests"] = self.used

    # ------------------------------------------------------------------ optional search (candidates only)
    def _search_allowed(self) -> bool:
        """Search runs only with an evaluator-supplied key, for companies likely to have a site."""
        if not SEARCH_KEY or self.ctx.budget.soft_deadline_passed() or not self._can_spend(4):
            return False
        prof = self.profile or {}
        try:
            employees = int(float(prof.get("antallAnsatte") or 0))
        except ValueError:
            employees = 0
        revenue = 0.0
        for acc in self.ctx.registry.facts.get("accounts") or []:
            if acc.get("account_type") == "SELSKAP":
                revenue = max(revenue, float((acc.get("figures") or {}).get("revenue") or 0))
        return employees > 0 or revenue >= SEARCH_MIN_REVENUE

    def search_candidates(self) -> list[Candidate]:
        """Brave Search API, used transiently: results only nominate domains, which must then show this
        organisation number on the site (same gate as name guesses). Titles/snippets are not stored."""
        city = (self.profile or {}).get("forretningsadresse.poststed") or (self.profile or {}).get("forretningsadresse.kommune") or ""
        q = f"\"{name_core(self.name) or self.name}\" {city}".strip()
        res = self.ctx.fetcher.get(SEARCH_URL, phase="search", params={"q": q, "country": "NO", "count": 8},
                                   headers={"X-Subscription-Token": SEARCH_KEY, "Accept": "application/json"},
                                   timeout=TIMEOUT)
        self.out.facts["third_party_cost_usd"] = round(self.out.facts.get("third_party_cost_usd", 0.0) + SEARCH_COST, 6)
        if not res.ok:
            self.out.error("website.search", res.error or f"http_{res.status}")
            return []
        try:
            urls = [r.get("url", "") for r in (res.json().get("web") or {}).get("results", [])]
        except ValueError:
            return []
        out, seen = [], set()
        for u in urls:
            host = (urlsplit(u).hostname or "").lower()
            dom = host[4:] if host.startswith("www.") else host
            if not dom or dom in seen:
                continue
            seen.add(dom)
            if any(dom == d or dom.endswith("." + d) for d in set(DIRECTORY_DOMAINS) | set(SOCIAL_DOMAINS) | set(OTHER_SOCIAL)):
                continue
            if self.counts["site"].get(dom, 0) or self.counts["email"].get(dom, 0):
                continue  # registered to another organisation
            out.append(Candidate(dom, f"https://{host}/", ["search"], {"search": u}))
        return out

    def _guesses_allowed(self) -> bool:
        b = self.ctx.budget
        return not b.soft_deadline_passed() and b.requests_left() > 0.3 * b.max_requests

    def guess_candidates(self) -> list[Candidate]:
        out = []
        for dom in guess_domains(self.name) + extra_guess_domains(self.name):
            if self.counts["site"].get(dom, 0) or self.counts["email"].get(dom, 0):
                continue  # another organisation lists this domain: it is theirs
            try:
                check_url_safe(f"https://{dom}/")
            except UnsafeURL:
                try:
                    check_url_safe(f"https://www.{dom}/")
                except UnsafeURL:
                    continue
            out.append(Candidate(dom, f"https://{dom}/", ["guess"], {"guess": dom}))
        return out

    # ------------------------------------------------------------------ publication
    def _site_value(self, cand: Candidate, dec: Decision, home: Page | None, proofs: list[str]) -> dict:
        url = home.url if home is not None else cand.url
        v = {"url": url, "domain": home.key if home is not None else cand.key, "identity": dec.level,
             "origin": cand.primary, "proofs": sorted(set(proofs))}
        if len(cand.origins) > 1:
            v["also_listed_by"] = sorted(o for o in cand.origins if o != cand.primary)
        if home is not None and cand.url.rstrip("/") != url.rstrip("/") and site_key(cand.url) != home.key:
            v["declared_url"] = cand.url
        return v

    def _declaration_evs(self, cand: Candidate) -> list[dict]:
        evs = []
        if "registry" in cand.origins:
            e = self.registry_ev(cand.raw["registry"], "registry")
            if e:
                evs.append(e)
        if "subunit" in cand.origins:
            w = next((w for w in self.workplaces if cand.raw["subunit"] in (w.get("website") or "")), None)
            e = self.registry_ev(cand.raw["subunit"], "subunit", (w or {}).get("organisation_number"))
            if e:
                evs.append(e)
        if "email" in cand.origins and set(cand.origins) == {"email"}:
            e = self.registry_ev(self.profile.get("epostadresse", ""), "email")
            if e:
                evs.append(e)
        return evs

    def publish_url_only(self, cand: Candidate, dec: Decision, pages: list[Page], ev: IdentityEvidence) -> None:
        home = pages[0] if pages else None
        evs = self._declaration_evs(cand)
        proofs = [f"declared by {o}" for o in cand.origins]
        if home is not None:
            evs.append(self.ev(home, f"<title>{home.title}</title>" if home.title else home.text[:200], "http_fetch_homepage",
                               "html>head>title"))
        value = self._site_value(cand, dec, home, proofs)
        if value.get("declared_url"):
            # unproven site: publish what the source declared, not where it redirects (e.g. a foreign group domain)
            value["redirects_to"] = value["url"]
            value["url"] = value.pop("declared_url")
        note = dec.reason + ". Only the URL is published; no site content is attributed."
        if ev.js_shell:
            note += " The site is a JavaScript application shell and was not rendered."
        self.out.add(make_claim("official_website", value, evs, confidence=dec.confidence, note=note), *evs)
        self.out.state("official_website", "available", note)
        self.out.facts.update({"website_declared": value["url"], "website_identity": dec.level})
        why = "website not proven to belong to this organisation, so its links are not attributed"
        self._state_if_free("social_profiles", "not_available", why)
        self.out.state("news_activity", "not_available", why)

    def publish_shared(self, cand: Candidate, dec: Decision, pages: list[Page]) -> None:
        home = pages[0] if pages else None
        evs = self._declaration_evs(cand)
        if not evs and home is not None:
            evs = [self.ev(home, home.title or home.text[:200], "http_fetch_homepage", "html>head>title")]
        value = {"url": home.url if home is not None else cand.url, "domain": cand.key, "origin": cand.primary,
                 "other_organisations_listing": cand.others_site}
        note = (f"{dec.reason}; this is a shared site (manager, group or service provider) and none of its content is "
                "attributed to this organisation")
        # Not published as this organisation's website: a shared domain (housing-cooperative manager, accountant,
        # group) would read as a wrong-company website. Kept as an ambiguous candidate with the declaration evidence.
        self.out.add(make_claim("shared_website", value, evs, availability="ambiguous", confidence=0.4, note=note), *evs)
        self.out.state("official_website", "ambiguous", note)
        self.out.facts.update({"website_shared": value["url"], "website_identity": "shared"})
        why = "only a shared website is known; its links are not attributed to this organisation"
        self._state_if_free("social_profiles", "not_available", why)
        self.out.state("news_activity", "not_available", why)

    def publish_ambiguous(self, cand: Candidate, dec: Decision, pages: list[Page], ev: IdentityEvidence) -> None:
        home = pages[0] if pages else None
        url = home.url if home is not None and not urlsplit(home.url).query else cand.url
        evs = []
        for org, (purl, snip) in list(ev.other_proofs.items())[:2]:
            pg = next((p for p in pages if p.url == purl), None)
            if pg is not None:
                evs.append(self.ev(pg, snip, "regex_orgnr", "text"))
        value = {"candidate_url": url, "reason": dec.reason, "origin": cand.primary}
        self.out.add(make_claim("official_website", value, evs, availability="ambiguous", confidence=0.3,
                                note="candidate not tied to this exact organisation; nothing from the site is published"), *evs)
        self.out.state("official_website", "ambiguous", dec.reason, candidate_url=url)
        self.out.facts.update({"website_candidate": url, "website_identity": "ambiguous"})
        why = "website candidate is not tied to this organisation; its links are not attributed"
        self._state_if_free("social_profiles", "ambiguous", why)
        self.out.state("news_activity", "ambiguous", why)

    def finish_without_site(self, state: str, reason: str) -> None:
        if "official_website" not in self.out.family_states:
            self.out.state("official_website", state, reason)
        why = {"blocked": "website could not be read: " + reason, "failed": "website could not be read: " + reason}.get(
            state, "no verified company website to discover profiles and news from")
        self._state_if_free("social_profiles", state if state in ("blocked", "failed") else "not_available", why)
        self.out.state("news_activity", state if state in ("blocked", "failed") else "not_available", why)
        self.out.facts["website_requests"] = self.used

    def _state_if_free(self, fam: str, state: str, reason: str) -> None:
        """Set a family state unless a claim already made it available (e.g. a profile declared in the register)."""
        if fam == "social_profiles" and any(c["field"] == "social_profile" and c["availability"] == "available"
                                            for c in self.out.claims):
            return
        self.out.state(fam, state, reason)

    def publish_declared_socials(self, socials: list) -> None:
        seen = set()
        for s in socials:
            disc = f"{s.platform}:{s.handle.lower()}"
            if disc in seen:
                continue
            seen.add(disc)
            e = self.registry_ev(s.raw, s.origin)
            if not e:
                continue
            self.out.add(make_claim("social_profile", {"platform": s.platform, "handle": s.handle, "url": s.url,
                                                       "found_via": f"{s.origin} website field"},
                                    [e], discriminator=disc, confidence=0.85,
                                    note="profile URL the organisation registered as its website in Enhetsregisteret"), e)
        if seen:
            self.out.state("social_profiles", "available", "profile listed as the website in the register")

    # ------------------------------------------------------------------ verified site
    def publish_verified(self, cand: Candidate, dec: Decision, pages: list[Page], ev: IdentityEvidence) -> None:
        home = pages[0]
        key = home.key
        fetched = {p.url for p in pages}
        links = section_links(home, key)
        for kind in ("contact", "about"):
            u = links.get(kind)
            if u and u not in fetched and self._can_spend(2):
                pg = self.fetch_page(u)
                fetched.add(u)
                if pg is not None and pg.key == key:
                    pages.append(pg)

        # official_website
        evs, proofs = [], []
        if dec.level == "exact":
            for purl, snip in ev.ours_proofs[:2]:
                pg = next((p for p in pages if p.url == purl), home)
                evs.append(self.ev(pg, snip, "regex_orgnr", "text"))
                proofs.append(f"organisation number on {purl}")
        else:
            for what, purl, snip in ev.corroborations[:3]:
                pg = next((p for p in pages if p.url == purl), home)
                evs.append(self.ev(pg, snip, f"match_registry_{what.split(':')[0]}", "text"))
                proofs.append(f"registry {what.split(':')[0]} on {purl}")
            if ev.name_in_text:
                proofs.append("full legal name on site" if ev.legal_name_in_text else "legal name on site")
                if ev.name_proof:
                    purl, snip = ev.name_proof
                    pg = next((p for p in pages if p.url == purl), home)
                    evs.append(self.ev(pg, snip, "match_legal_name", "text"))
            if ev.domain_match:
                proofs.append(f"domain {ev.domain_match}ly matches legal name")
        evs.extend(self._declaration_evs(cand))
        value = self._site_value(cand, dec, home, proofs)
        self.out.add(make_claim("official_website", value, evs, confidence=dec.confidence, note=dec.reason), *evs)
        self.out.state("official_website", "available", f"official website verified ({dec.level}): {dec.reason}")
        self.out.facts.update({"website": value["url"], "website_identity": dec.level})

        brand = self.publish_brand(pages, key)
        phrases = alias_phrases([self.name] + ([brand] if brand else []))
        label = domain_label(key)
        if len(label) >= 4 and label not in [p.replace(" ", "") for p in phrases]:
            phrases.append(label)
        compacts = [compact(p) for p in phrases]
        distinct = "".join(t for t in name_tokens(self.name) if t not in GENERIC)
        if len(distinct) >= 4:
            compacts.append(distinct)
        self.out.facts["brand_aliases"] = sorted({brand} - {None, ""}) if brand else []
        self.publish_description(pages)
        self.publish_contacts(pages, key)
        self.publish_locations(pages)
        n_social = self.publish_socials(pages, compacts)
        if self.used < MAX_REQ:
            self.crawl_news_and_jobs(home, links, pages, key, phrases, fetched)
        n_news = self.publish_news(pages, key, phrases)
        self.publish_jobs(pages, phrases)
        if n_social == 0 and not any(c["field"] == "social_profile" for c in self.out.claims):
            self.out.state("social_profiles", "not_available",
                           "verified website links no social profile carrying the company's name, brand or domain")
        elif n_social:
            self.out.state("social_profiles", "available", "profiles linked from the verified website")
        if n_news == 0:
            if self.truncated and not self._news_pages:
                self.out.state("news_activity", "blocked", "website request budget reached before news pages were read")
            else:
                self.out.state("news_activity", "not_available",
                               "no dated news item naming the company on the verified website"
                               + ("" if self._news_pages else " (no news section or feed linked from the homepage)"))
        else:
            self.out.state("news_activity", "available", "dated posts on the verified website")
        if ev.js_shell:
            self.out.facts["website_js_shell"] = True

    def crawl_news_and_jobs(self, home: Page, links: dict[str, str], pages: list[Page], key: str,
                            phrases: list[str], fetched: set[str]) -> None:
        feeds = [f for p in pages for f in p.feeds if site_key(f) == key]
        listing = links.get("news")
        if feeds and self._can_spend(2):
            res = self.fetch(feeds[0])
            fetched.add(feeds[0])
            if res is not None and res.ok:
                self._feed_items = [(res, it) for it in parse_feed(res)]
                self._news_pages += 1
        if listing and listing not in fetched and self._can_spend(2):
            pg = self.fetch_page(listing)
            fetched.add(listing)
            if pg is not None and pg.key == key:
                pages.append(pg)
                self._news_pages += 1
        # read up to two article pages that the listing points at but whose excerpt does not prove a date+name
        cands = []
        for p in pages:
            for it in news_candidates(p):
                if it["url"] not in fetched and site_key(it["url"]) == key:
                    if not mentions(it["title"] + " " + it["body"], phrases):
                        cands.append(it)
        if listing and listing in fetched:
            lp = next((p for p in pages if p.url == listing), None)
            if lp is not None and not news_candidates(lp):
                # undated listing: follow the first two article-looking links under the news path
                base = urlsplit(listing).path.rstrip("/")
                for ln in lp.links:
                    pth = urlsplit(ln.url).path
                    if site_key(ln.url) == key and base and pth.startswith(base + "/") and len(pth) > len(base) + 3 \
                            and ln.url not in fetched and not ln.nav:
                        cands.append({"url": ln.url, "published": ""})
        cands = iter_unique(sorted(cands, key=lambda x: x.get("published") or "", reverse=True), lambda x: x["url"])
        for it in cands[:2]:
            if not self._can_spend(2):
                break
            pg = self.fetch_page(it["url"])
            fetched.add(it["url"])
            if pg is not None and pg.key == key:
                pg.is_article = True  # type: ignore[attr-defined]
                pages.append(pg)
        careers = links.get("careers")
        if careers and careers not in fetched and self._can_spend(2):
            pg = self.fetch_page(careers)
            fetched.add(careers)
            if pg is not None and pg.key == key:
                pages.append(pg)

    # -- individual fields
    def publish_brand(self, pages: list[Page], key: str) -> str | None:
        label = domain_label(key)
        core_c = compact(name_core(self.name))
        distinct = "".join(t for t in name_tokens(self.name) if t not in GENERIC)
        for p in pages[:1] + pages[1:]:
            for b in brand_candidates(p):
                bc = compact(b["value"])
                related = bool(bc) and (
                    (len(label) >= 3 and (label in bc or bc in label))
                    or (len(core_c) >= 3 and (core_c in bc or (bc in core_c and len(bc) >= 4)))
                    or (len(distinct) >= 4 and (distinct in bc or (bc in distinct and len(bc) >= 4))))
                if not related:
                    continue
                e = self.ev(p, b["span"], b["method"], b["locator"])
                self.out.add(make_claim("public_brand", b["value"], [e], confidence=0.9,
                                        note="name the verified website uses for the company"), e)
                return b["value"]
        return None

    def publish_description(self, pages: list[Page]) -> None:
        for p in pages[:3]:
            for d in description_candidates(p):
                e = self.ev(p, d["span"], d["method"], d["locator"])
                self.out.add(make_claim("company_description", d["value"], [e], confidence=0.8,
                                        note="company-owned promotional copy from the verified website; describes the "
                                             "business as the company presents it, not an independent assessment"), e)
                return

    def publish_contacts(self, pages: list[Page], key: str) -> None:
        n = {"phone": 0, "email": 0}
        seen = set()
        for p in pages:
            for c in contact_candidates(p, key, self.profile.get("epostadresse")):
                disc = f"{c['kind']}:{c['value']}"
                if disc in seen or n[c["kind"]] >= 4:
                    continue
                seen.add(disc)
                n[c["kind"]] += 1
                e = self.ev(p, c["span"], c["method"], c["locator"])
                self.out.add(make_claim("contact_detail", {"type": c["kind"], "value": c["value"]}, [e],
                                        discriminator=disc, confidence=0.9), e)

    def publish_locations(self, pages: list[Page]) -> None:
        zips = {self.profile.get(f"{p}.postnummer") for p in ("forretningsadresse", "postadresse")} - {None, ""}
        towns = {translit(self.profile.get(f"{p}.poststed") or "") for p in ("forretningsadresse", "postadresse")} - {""}
        for w in self.workplaces:
            a = w.get("address") or {}
            if a.get("postnummer"):
                zips.add(a["postnummer"])
            if a.get("poststed"):
                towns.add(translit(a["poststed"]))
        seen = set()
        for p in pages:
            for c in location_candidates(p, zips, towns):
                k = location_key(c["value"])
                if k in seen or len(seen) >= 5:
                    continue
                seen.add(k)
                e = self.ev(p, c["span"], c["method"], c["locator"])
                self.out.add(make_claim("site_location", c["value"], [e], discriminator=k, confidence=0.85,
                                        note="address shown on the verified website"), e)

    def publish_socials(self, pages: list[Page], compacts: list[str]) -> int:
        per: Counter = Counter()
        seen = {c["key"].split(":", 1)[1] for c in self.out.claims if c["field"] == "social_profile"}
        n = 0
        for p in pages:
            for s in social_candidates(p):
                disc = f"{s['platform']}:{s['handle'].lower()}"
                if disc in seen or per[s["platform"]] >= MAX_SOCIAL_PER_PLATFORM:
                    continue
                if not handle_matches(s["handle"], compacts):
                    continue
                seen.add(disc)
                per[s["platform"]] += 1
                e = self.ev(p, s["span"], s["method"], s["locator"])
                self.out.add(make_claim("social_profile", {"platform": s["platform"], "handle": s["handle"],
                                                           "url": s["url"], "found_via": "verified website"},
                                        [e], discriminator=disc, confidence=0.9,
                                        note="linked from the verified website; handle carries the company's name/brand/domain"), e)
                n += 1
        return n

    def publish_news(self, pages: list[Page], key: str, phrases: list[str]) -> int:
        # home, contact, about, listing and careers pages are not news items themselves
        section_urls = {p.url for p in pages if not getattr(p, "is_article", False)}
        items = []
        for p in pages:
            for it in news_candidates(p):
                if site_key(it["url"]) != key or urlsplit(it["url"]).path in ("", "/") or it["url"] in section_urls:
                    continue
                if not mentions(it["title"] + " " + it["body"], phrases):
                    continue
                items.append((p, it))
        for res, it in self._feed_items:
            if site_key(it["url"]) == key and mentions(it["title"] + " " + it["body"], phrases):
                items.append((res, it))
        items.sort(key=lambda x: x[1]["published"], reverse=True)
        n, seen = 0, set()
        for src, it in items:
            u = it["url"]
            if u in seen or n >= MAX_NEWS:
                continue
            seen.add(u)
            if isinstance(src, Page):
                e = self.ev(src, it["span"], it["method"], it["locator"])
            else:
                e = make_evidence(src.url, SOURCE_CLASS, src.retrieved_at, src.content_sha256, it["span"], it["method"],
                                  final_url=src.final_url, http_status=src.status, locator=it["locator"], access_policy=ACCESS)
            self.out.add(make_claim("news_item", {"title": it["title"], "url": u, "published": it["published"]}, [e],
                                    discriminator=u, effective_at=it["published"], confidence=0.85,
                                    note="dated post on the verified company website that names the company"), e)
            n += 1
        return n

    def publish_jobs(self, pages: list[Page], phrases: list[str]) -> None:
        today = _today()
        seen = set()
        for p in pages:
            for j in job_candidates(p):
                if not mentions(j["hiring_organization"], phrases):
                    continue
                if j.get("valid_through") and j["valid_through"] < today:
                    continue
                disc = j["url"] if j["url"] != p.url or len(job_candidates(p)) == 1 else f"{j['url']}#{compact(j['title'])}"
                if disc in seen:
                    continue
                seen.add(disc)
                e = self.ev(p, j["span"], j["method"], j["locator"])
                v = {k: j[k] for k in ("title", "url", "date_posted", "valid_through", "location", "hiring_organization") if j.get(k)}
                self.out.add(make_claim("job_posting", v, [e], discriminator=disc, confidence=0.85,
                                        effective_at=j.get("date_posted"),
                                        note="JobPosting structured data on the verified website"), e)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _is_html(res: FetchResult) -> bool:
    ct = (res.content_type or "").lower()
    if "html" in ct:
        return True
    if not ct or "text/plain" in ct:
        return b"<html" in res.content[:3000].lower() or b"<!doctype html" in res.content[:3000].lower()
    return False
