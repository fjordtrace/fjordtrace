"""NAV job vacancy feed (pam-stilling-feed.nav.no): active job ads matched to the company by organisation number.

Source and terms
----------------
* Feed: ``GET https://pam-stilling-feed.nav.no/api/v1/feed`` (pages of 1,000 items, chained by ``next_url``)
  and ``GET /api/v1/feedentry/{uuid}`` (the ad, with ``ad_content.employer.orgnr``). Bearer JWT required:
  ``SIGNALPOST_NAV_TOKEN`` if set (registered consumer), otherwise the public token NAV publishes at
  ``/api/publicToken`` for experiments (it rotates).
* Terms (https://arbeidsplassen.nav.no/vilkar-api): consumers may republish ads, must remove an ad as soon as it is
  inactive or deleted, must reflect updates, and must deep-link the application to the source. So: every ad we
  publish is re-fetched live in this run (status ACTIVE, employer orgnr = the company or one of its subunits),
  contact persons are never stored or published, and the application URL is passed through unchanged.

How it works
------------
1. A declared cache (``data/nav_index.json.gz``, built by ``tools/build_nav_index.py``) lists the ads that were
   ACTIVE at build time, keyed by employer orgnr, plus the feed cursor where the build stopped and a
   subunit -> parent map from Brønnøysund's subunit bulk file.
2. ``prepare`` loads it and, in a background thread, reads the feed pages newer than the cursor (segmented with
   ``If-Modified-Since`` so a stale cache costs seconds, not minutes), drops ads that went inactive, and fetches
   the entry of new ACTIVE ads whose business name plausibly matches a batch company or subunit (name match only
   nominates; the orgnr decides).
3. ``run`` live-verifies up to ``SIGNALPOST_NAV_VERIFY_CAP`` ads per company and publishes those whose live entry
   is ACTIVE and whose employer orgnr is the company or one of its registered subunits.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Callable

from ..model import Contribution, make_claim, make_evidence
from .base import BatchContext, CompanyContext, Connector

BASE = "https://pam-stilling-feed.nav.no"
FEED_URL = BASE + "/api/v1/feed"
PAGE_URL = BASE + "/api/v1/feed/{page_id}"
ENTRY_URL = BASE + "/api/v1/feedentry/{uuid}"
TOKEN_URL = BASE + "/api/publicToken"
PUBLIC_AD_URL = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
TERMS_URL = "https://arbeidsplassen.nav.no/vilkar-api"
SUBUNIT_URL = "https://data.brreg.no/enhetsregisteret/api/underenheter/{org}"
INDEX_PATH = Path(__file__).resolve().parent.parent / "data" / "nav_index.json.gz"
PHASE = "nav_jobs"
ORG_RE = re.compile(r"^\d{9}$")

# get_json(url, headers) -> (status, parsed json or None, error or None)
GetJson = Callable[[str, dict[str, str]], tuple[int | None, Any, str | None]]


# ------------------------------------------------------------------ small helpers
def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # NAV dates without offset are Norwegian local dates; treat as UTC (day precision)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def http_date(dt: datetime) -> str:
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def parse_token(text: str) -> str | None:
    """``/api/publicToken`` returns a sentence and the JWT on the last line."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.count(".") == 2 and line.startswith("ey"):
            return line
    return None


def compact_ad(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Index record for an ACTIVE entry (no contact persons, no description)."""
    ad = entry.get("ad_content") or {}
    if entry.get("status") != "ACTIVE" or not ad:
        return None
    emp = ad.get("employer") or {}
    locs = []
    for loc in ad.get("workLocations") or []:
        loc = {k: loc.get(k) for k in ("city", "municipal", "postalCode", "county", "country") if loc.get(k)}
        if loc and loc not in locs:
            locs.append(loc)
    rec = {
        "uuid": entry.get("uuid") or ad.get("uuid"),
        "orgnr": (emp.get("orgnr") or "").strip() or None,
        "title": ad.get("title"),
        "employer_name": emp.get("name"),
        "homepage": (emp.get("homepage") or "").strip() or None,
        "published": ad.get("published"),
        "expires": ad.get("expires"),
        "updated": ad.get("updated") or entry.get("sistEndret"),
        "application_due": ad.get("applicationDue"),
        "work_locations": locs[:5],
    }
    return {k: v for k, v in rec.items() if v not in (None, "", [])}


def is_expired(rec: dict[str, Any], now: datetime) -> bool:
    exp = parse_time(rec.get("expires"))
    return exp is not None and exp + timedelta(days=1) < now  # expires is often a midnight date; keep the last day


# ------------------------------------------------------------------ feed crawl (shared with tools/build_nav_index.py)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _item_time(it: dict[str, Any]) -> datetime:
    return parse_time(it.get("date_modified") or (it.get("_feed_entry") or {}).get("sistEndret")) or _EPOCH


class CrawlResult:
    def __init__(self) -> None:
        self.latest: dict[str, dict[str, Any]] = {}  # uuid -> latest feed item header (in feed order)
        self.pages = 0
        self.items = 0
        self.cursor_page_id: str | None = None
        self.cursor_time: str | None = None
        self.complete = False  # reached the end of the feed with no gap
        self.errors: list[str] = []

    def apply_item(self, it: dict[str, Any]) -> None:
        fe = it.get("_feed_entry") or {}
        uuid = fe.get("uuid") or it.get("id")
        if not uuid:
            return
        self.items += 1
        self.latest.pop(uuid, None)  # keep dict order = order of each ad's latest change
        self.latest[uuid] = {
            "status": fe.get("status"),
            "businessName": fe.get("businessName"),
            "municipal": fe.get("municipal"),
            "title": fe.get("title") or it.get("title"),
            "sistEndret": fe.get("sistEndret") or it.get("date_modified"),
        }
        t = it.get("date_modified") or fe.get("sistEndret")
        if t and (self.cursor_time is None or _item_time(it) >= (parse_time(self.cursor_time) or _EPOCH)):
            self.cursor_time = t


def crawl_feed(
    get_json: GetJson,
    token: str,
    *,
    start_time: datetime,
    now: datetime | None = None,
    step: timedelta = timedelta(hours=24),
    max_pages: int = 2000,
    workers: int = 8,
    should_stop: Callable[[], bool] = lambda: False,
    log: Callable[[str], None] = lambda s: None,
) -> CrawlResult:
    """Read every feed item modified after ``start_time``, to the end of the feed.

    The range is split into segments of ``step``: one ``If-Modified-Since`` request per segment returns the
    page holding the first item modified after the segment start (NAV builds such a page on the fly, so page
    ids are not shared between segments). Each segment then follows ``next_url`` until its items reach the
    first item time of the next segment. Segments run in parallel, so a stale cursor costs the wall time of
    one segment's chain (4-6 pages per day) instead of the whole range. Items are applied in time order;
    the result stops at the first gap so the cursor never skips items.
    """
    now = now or datetime.now(timezone.utc)
    auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    result = CrawlResult()
    times = []
    t = start_time
    while t < now:
        times.append(t)
        t += step

    def first_page(when: datetime) -> tuple[dict | None, str | None]:
        if should_stop():
            return None, "stopped"
        status, body, err = get_json(FEED_URL, {**auth, "If-Modified-Since": http_date(when)})
        if status == 304:
            return None, None  # nothing modified after this time
        if status != 200 or not isinstance(body, dict):
            return None, err or f"http_{status}"
        return body, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        seeds = list(pool.map(first_page, times))
    segs: list[dict] = []  # first pages, in time order, each starting later than the previous one
    for k, (body, err) in enumerate(seeds):
        if err:
            result.errors.append(f"segment {k} seed: {err}")
            break  # a missing seed would leave a gap; the previous segment cannot know where to stop
        if body is None:
            break  # 304: nothing after this time
        items = body.get("items") or []
        if not items:
            continue
        if segs and _item_time(items[0]) <= _item_time(segs[-1]["items"][0]):
            continue  # resolved to the same start as the previous segment
        segs.append(body)
    if not segs:
        return result
    stops = [_item_time(p["items"][0]) for p in segs[1:]] + [None]
    per_seg = max(2, math.ceil(max(0, max_pages - len(segs)) / len(segs)) + 1)
    used = [len(segs)]
    lock = threading.Lock()

    def follow(k: int) -> tuple[list[dict], bool, str | None]:
        chain, page, stop = [segs[k]], segs[k], stops[k]
        while True:
            items = page.get("items") or []
            if stop is not None and items and _item_time(items[-1]) >= stop:
                return chain, True, None
            nxt = page.get("next_id")
            if not nxt:
                return chain, True, None  # end of the feed
            with lock:
                if len(chain) >= per_seg or used[0] >= max_pages:
                    return chain, False, "page cap"
                used[0] += 1
            if should_stop():
                return chain, False, "stopped"
            status, body, err = get_json(PAGE_URL.format(page_id=nxt), auth)
            if status != 200 or not isinstance(body, dict):
                return chain, False, err or f"http_{status}"
            chain.append(body)
            page = body

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        chains = list(pool.map(follow, range(len(segs))))
    complete = not result.errors
    for k, (chain, ok, err) in enumerate(chains):
        stop = stops[k]
        for page in chain:
            result.pages += 1
            for it in page.get("items") or []:
                if stop is None or _item_time(it) < stop:
                    result.apply_item(it)
            result.cursor_page_id = page.get("id")
        if not ok:
            complete = False
            result.errors.append(f"segment {k} chain: {err}")
            break
    result.complete = complete
    log(f"nav feed: {result.pages} pages, {result.items} items, {len(result.latest)} ads, "
        f"cursor {result.cursor_time}, complete={result.complete}")
    return result


# ------------------------------------------------------------------ name matching (nominates entry fetches only)
LEGAL_TOKENS = {"as", "asa", "sa", "ba", "ans", "da", "enk", "nuf", "ks", "sf", "iks", "hf", "kf", "fkf", "bbl", "stiftelsen",
                "aksjeselskap", "the", "og", "and", "i", "avd", "avdeling"}


def name_tokens(name: str | None) -> tuple[str, ...]:
    s = unicodedata.normalize("NFKC", (name or "")).lower()
    s = re.sub(r"[^0-9a-zæøåäöüé]+", " ", s)
    return tuple(t for t in s.split() if t and t not in LEGAL_TOKENS)


def build_name_matcher(names: dict[str, set[str]]) -> Callable[[str | None], set[str]]:
    """names: org -> set of names (company + subunits). Returns business name -> nominated orgs."""
    by_first: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    for org, ns in names.items():
        for n in ns:
            toks = name_tokens(n)
            if not toks or len("".join(toks)) < 4:
                continue
            by_first.setdefault(toks[0], []).append((toks, org))

    def match(business: str | None) -> set[str]:
        toks = name_tokens(business)
        out: set[str] = set()
        for i, t in enumerate(toks):
            for cand, org in by_first.get(t, ()):
                if toks[i:i + len(cand)] == cand:
                    out.add(org)
        return out

    return match


# ------------------------------------------------------------------ index
def load_index(path: Path | str | None = None) -> dict[str, Any] | None:
    p = Path(path or os.environ.get("SIGNALPOST_NAV_INDEX") or INDEX_PATH)
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


# ------------------------------------------------------------------ connector
class NavJobsConnector(Connector):
    name = "nav_jobs"
    families = ("jobs",)
    lane = "nav"
    lane_workers = 6

    def __init__(self, index_path: str | Path | None = None) -> None:
        self.index_path = index_path
        self.verify_cap = _int_env("SIGNALPOST_NAV_VERIFY_CAP", 6)
        self.max_pages = _int_env("SIGNALPOST_NAV_MAX_PAGES", 150)
        self.update_timeout_s = _int_env("SIGNALPOST_NAV_UPDATE_TIMEOUT_S", 240)

    # ---------------------------------------------------------------- batch phase
    def prepare(self, batch: BatchContext) -> None:
        shared: dict[str, Any] = {"by_org": {}, "homepage_by_org": {}, "parent_of": {}, "token": None,
                                  "as_of": None, "index_built_at": None, "update": {}, "error": None}
        batch.shared["nav_jobs"] = shared
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._subunit_checks: dict[tuple[str, str], tuple[bool, Any]] = {}
        try:
            index = load_index(self.index_path)
        except (OSError, ValueError) as exc:
            index = None
            shared["error"] = f"index unreadable: {exc}"
        index = index or {}
        batch_orgs = set(batch.orgs)
        subs_of: dict[str, set[str]] = {o: set() for o in batch.orgs}
        for org in batch.orgs:
            reg = batch.registry.get(org)
            for w in (reg.facts.get("workplaces") if reg else None) or []:
                if w.get("organisation_number"):
                    subs_of[org].add(w["organisation_number"])
        parent_of = {s: p for s, p in (index.get("parent_of") or {}).items() if p in batch_orgs}
        for s, p in parent_of.items():
            subs_of[p].add(s)
        wanted = {}
        for org, subs in subs_of.items():
            wanted[org] = org
            for s in subs:
                wanted.setdefault(s, org)
        by_org: dict[str, dict[str, dict]] = {o: {} for o in batch.orgs}
        now = datetime.now(timezone.utc)
        for ad_org, ads in (index.get("by_org") or {}).items():
            owner = wanted.get(ad_org)
            if not owner:
                continue
            for ad in ads:
                if is_expired(ad, now):
                    continue
                by_org[owner][ad["uuid"]] = {**ad, "orgnr": ad_org}
        shared.update(by_org=by_org, parent_of=parent_of, subs_of={k: sorted(v) for k, v in subs_of.items()},
                      wanted=wanted, index_built_at=index.get("built_at"),
                      as_of=(index.get("feed_cursor") or {}).get("item_time") or index.get("built_at"))
        self._refresh_homepages(shared)
        if os.environ.get("SIGNALPOST_NAV_OFFLINE") == "1":
            shared["token"] = os.environ.get("SIGNALPOST_NAV_TOKEN") or None
            self._ready.set()
            return
        threading.Thread(target=self._update, args=(batch, index), name="nav-feed-update", daemon=True).start()

    def _refresh_homepages(self, shared: dict[str, Any]) -> None:
        hp = shared["homepage_by_org"]
        for org, ads in shared["by_org"].items():
            if org in hp:
                continue
            # prefer the company's own orgnr, then the most recently updated ad
            cands = sorted((a for a in ads.values() if a.get("homepage")), key=lambda a: a.get("updated") or "", reverse=True)
            cands.sort(key=lambda a: a.get("orgnr") != org)  # stable: own orgnr first, newest first within
            if cands:
                hp[org] = cands[0]["homepage"]

    def _get_json(self, batch: BatchContext) -> GetJson:
        def get(url: str, headers: dict[str, str]) -> tuple[int | None, Any, str | None]:
            res = batch.fetcher.get(url, PHASE, headers=headers, timeout=30.0)
            if res.status != 200:
                return res.status, None, res.error
            try:
                return res.status, res.json(), None
            except ValueError as exc:
                return res.status, None, f"bad json: {exc}"
        return get

    def _token(self, batch: BatchContext) -> tuple[str | None, str]:
        env = os.environ.get("SIGNALPOST_NAV_TOKEN", "").strip()
        if env:
            return env, "env:SIGNALPOST_NAV_TOKEN"
        res = batch.fetcher.get(TOKEN_URL, PHASE, essential=True)
        tok = parse_token(res.text) if res.ok else None
        return tok, "public token (/api/publicToken)" if tok else (res.error or "token fetch failed")

    def _update(self, batch: BatchContext, index: dict[str, Any]) -> None:
        shared = batch.shared["nav_jobs"]
        info: dict[str, Any] = {}
        shared["update"] = info
        try:
            token, how = self._token(batch)
            info["token_source"] = how
            if not token:
                shared["error"] = f"no NAV token: {how}"
                return
            shared["token"] = token
            cursor = index.get("feed_cursor") or {}
            start_time = parse_time(cursor.get("item_time")) or (datetime.now(timezone.utc) - timedelta(days=183))
            page_cap = max(10, min(self.max_pages, int(batch.budget.max_requests * 0.03)))
            t0 = time.monotonic()
            crawl = crawl_feed(
                self._get_json(batch), token, start_time=start_time - timedelta(minutes=1),
                max_pages=page_cap, workers=8,
                should_stop=lambda: batch.budget.soft_deadline_passed() or time.monotonic() - t0 > self.update_timeout_s,
            )
            info.update(pages=crawl.pages, complete=crawl.complete, cursor_time=crawl.cursor_time,
                        errors=crawl.errors[:5], seconds=round(time.monotonic() - t0, 1))
            if crawl.pages:
                shared["as_of"] = crawl.cursor_time or shared["as_of"]
            self._apply_crawl(batch, crawl, token, info)
        except Exception as exc:  # never sink the run
            shared["error"] = f"feed update failed: {type(exc).__name__}: {exc}"
        finally:
            self._ready.set()

    def _apply_crawl(self, batch: BatchContext, crawl: CrawlResult, token: str, info: dict[str, Any]) -> None:
        shared = batch.shared["nav_jobs"]
        by_org = shared["by_org"]
        removed = 0
        with self._lock:
            for ads in by_org.values():
                for uuid in [u for u, ad in ads.items() if (crawl.latest.get(u) or {}).get("status") == "INACTIVE"]:
                    del ads[uuid]
                    removed += 1
        info["removed_inactive"] = removed
        known = {u for ads in by_org.values() for u in ads}
        names: dict[str, set[str]] = {}
        for org in batch.orgs:
            reg = batch.registry.get(org)
            prof = (reg.facts.get("profile") if reg else None) or {}
            ns = {prof.get("navn")} | {w.get("name") for w in ((reg.facts.get("workplaces") if reg else None) or [])}
            names[org] = {n for n in ns if n}
        match = build_name_matcher(names)
        nominated = []
        for uuid, head in crawl.latest.items():
            if head.get("status") != "ACTIVE" or uuid in known:
                continue
            orgs = match(head.get("businessName"))
            if orgs:
                nominated.append(uuid)
        cap = max(30, min(len(batch.orgs) // 3, int(batch.budget.max_requests * 0.02)))
        info["new_active_ads"] = sum(1 for h in crawl.latest.values() if h.get("status") == "ACTIVE")
        info["name_nominated"] = len(nominated)
        nominated = nominated[:cap]
        auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        get = self._get_json(batch)
        wanted = shared["wanted"]

        def fetch(uuid: str) -> dict | None:
            if batch.budget.soft_deadline_passed():
                return None
            status, body, _ = get(ENTRY_URL.format(uuid=uuid), auth)
            return compact_ad(body) if status == 200 and isinstance(body, dict) else None

        added = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            for rec in pool.map(fetch, nominated):
                if not rec or not rec.get("orgnr"):
                    continue
                owner = wanted.get(rec["orgnr"])
                if owner:
                    with self._lock:
                        by_org[owner][rec["uuid"]] = rec
                    added += 1
        info["name_fetched"] = len(nominated)
        info["added"] = added
        self._refresh_homepages(shared)

    # ---------------------------------------------------------------- company phase
    def _check_subunit(self, ctx: CompanyContext, sub: str) -> tuple[bool, Any]:
        """Confirm live that ``sub`` is a subunit of the company (only for subunits not in the registry facts)."""
        key = (ctx.org, sub)
        if key in self._subunit_checks:
            return self._subunit_checks[key]
        res = ctx.fetcher.get(SUBUNIT_URL.format(org=sub), PHASE)
        ok = False
        if res.ok:
            try:
                ok = (res.json().get("overordnetEnhet") == ctx.org)
            except ValueError:
                ok = False
        self._subunit_checks[key] = (ok, res)
        return ok, res

    def run(self, ctx: CompanyContext) -> Contribution:
        out = Contribution()
        shared = ctx.batch.shared.get("nav_jobs")
        if shared is None:
            out.state("jobs", "failed", "NAV job index was not prepared")
            return out
        self._ready.wait(timeout=max(5.0, min(self.update_timeout_s + 30, ctx.budget.time_left() - 120)))
        with self._lock:
            ads = list((shared["by_org"].get(ctx.org) or {}).values())
        as_of = shared.get("as_of") or "unknown"
        registry_subs = {w.get("organisation_number"): w.get("name")
                         for w in (ctx.registry.facts.get("workplaces") or []) if w.get("organisation_number")}
        index_subs = {s for s, p in shared.get("parent_of", {}).items() if p == ctx.org}
        token = shared.get("token")
        if not ads:
            if not token and shared.get("error"):
                out.state("jobs", "not_available",
                          f"no active NAV ads for this organisation or its subunits in the declared NAV feed cache "
                          f"(as of {as_of}); live feed update unavailable: {shared['error']}", as_of=as_of)
            else:
                out.state("jobs", "not_available",
                          f"no active NAV ads for this organisation or its subunits as of {as_of}", as_of=as_of)
            return out
        if not token:
            out.state("jobs", "blocked", f"NAV feed token unavailable, {len(ads)} cached ads could not be live-verified: "
                                         f"{shared.get('error')}", as_of=as_of, cached_ads=len(ads))
            return out
        # own-orgnr ads first, then most recently updated
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        ads.sort(key=lambda a: (a.get("orgnr") != ctx.org,
                                -(parse_time(a.get("updated") or a.get("published")) or epoch).timestamp(), a["uuid"]))
        auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        published = inactive = mismatched = errors = 0
        blocked_reason = None
        for ad in ads:
            if published >= self.verify_cap:
                break
            uuid = ad["uuid"]
            res = ctx.fetcher.get(ENTRY_URL.format(uuid=uuid), PHASE, headers=auth, timeout=20.0)
            if res.state_hint == "blocked" or res.status in (401, 403):
                blocked_reason = res.error
                break
            if not res.ok:
                errors += 1
                out.error(self.name, res.error or "entry fetch failed", res.url)
                continue
            try:
                entry = res.json()
            except ValueError:
                errors += 1
                continue
            claim = self._claim(ctx, entry, res, registry_subs, index_subs)
            if claim == "inactive":
                inactive += 1
            elif claim == "mismatch":
                mismatched += 1
            elif claim is not None:
                c, evs = claim
                out.add(c, *evs)
                published += 1
        extra = {"as_of": as_of, "cached_ads": len(ads), "verified": published, "inactive_on_verify": inactive,
                 "orgnr_mismatch": mismatched}
        if published:
            more = len(ads) - published - inactive - mismatched
            note = f"{published} active NAV ads live-verified"
            if more > 0:
                note += f"; {more} more cached ads not verified (cap {self.verify_cap} per company)"
            out.state("jobs", "available", note, **extra)
        elif blocked_reason:
            out.state("jobs", "blocked", f"NAV entry fetch refused ({blocked_reason})", **extra)
        elif errors and not (inactive or mismatched):
            out.state("jobs", "failed", "NAV entry fetches failed", **extra)
        else:
            out.state("jobs", "not_available",
                      f"no active NAV ads for this organisation or its subunits as of {as_of} "
                      f"(cached ads were inactive or not matched on live check)", **extra)
        return out

    def _claim(self, ctx: CompanyContext, entry: dict[str, Any], res, registry_subs: dict[str, Any],
               index_subs: set[str]):
        ad = entry.get("ad_content") or {}
        if entry.get("status") != "ACTIVE" or not ad:
            return "inactive"
        if is_expired(ad, datetime.now(timezone.utc)):
            return "inactive"
        emp = ad.get("employer") or {}
        orgnr = (emp.get("orgnr") or "").strip()
        uuid = entry.get("uuid") or ad.get("uuid")
        evidence = []
        sub = None
        if orgnr == ctx.org:
            pass
        elif orgnr in registry_subs:
            sub = {"organisation_number": orgnr, "name": registry_subs[orgnr], "proof": "registry subunit list"}
        elif orgnr in index_subs:
            ok, sres = self._check_subunit(ctx, orgnr)
            if not ok:
                return "mismatch"
            sname = None
            try:
                sname = sres.json().get("navn")
            except ValueError:
                pass
            sub = {"organisation_number": orgnr, "name": sname, "proof": "Brønnøysund subunit record"}
            evidence.append(make_evidence(
                sres.url, "official_subunits", sres.retrieved_at, sres.content_sha256,
                f"underenhet {orgnr} overordnetEnhet={ctx.org}", "brreg_subunit_api_json",
                http_status=sres.status, access_policy="NLOD-2.0", locator=f"underenhet={orgnr}"))
        else:
            return "mismatch"
        if not uuid or not ORG_RE.match(orgnr):
            return "mismatch"
        locs = []
        for loc in ad.get("workLocations") or []:
            loc = {k: loc.get(k) for k in ("address", "postalCode", "city", "municipal", "county", "country") if loc.get(k)}
            if loc and loc not in locs:
                locs.append(loc)
        occ = [o.get("level2") or o.get("level1") for o in ad.get("occupationCategories") or [] if o.get("level2") or o.get("level1")]
        public_url = PUBLIC_AD_URL.format(uuid=uuid)
        value = {
            "title": ad.get("title"),
            "job_title": ad.get("jobtitle"),
            "uuid": uuid,
            "status": "ACTIVE",
            "published": ad.get("published"),
            "expires": ad.get("expires"),
            "application_due": ad.get("applicationDue"),
            "updated": ad.get("updated") or entry.get("sistEndret"),
            "work_locations": locs[:5],
            "employer_name": emp.get("name"),
            "employer_orgnr": orgnr,
            "posted_under_subunit": {k: v for k, v in (sub or {}).items() if v} or None,
            "occupation": occ[:3] or None,
            "engagement_type": ad.get("engagementtype") or None,
            "extent": ad.get("extent") or None,
            "position_count": ad.get("positioncount") or None,
            "application_url": ad.get("applicationUrl") or None,
            "public_url": public_url,
            "source": "NAV arbeidsplassen job feed",
        }
        value = {k: v for k, v in value.items() if v not in (None, "", [])}
        span = (f"employer.orgnr={orgnr}; employer.name={emp.get('name')}; title={ad.get('title')}; status=ACTIVE; "
                f"published={ad.get('published')}; expires={ad.get('expires')}")
        ev = make_evidence(
            res.url, "official_job_feed", res.retrieved_at, res.content_sha256, span, "nav_feed_entry_json",
            final_url=public_url, effective_at=ad.get("published"), locator=f"feedentry={uuid}",
            http_status=res.status, access_policy=TERMS_URL)
        evidence.insert(0, ev)
        conf = 1.0 if sub is None else 0.97
        claim = make_claim("job_posting", value, evidence, discriminator=uuid, confidence=conf,
                           effective_at=ad.get("published"))
        return claim, evidence
