"""Public procurement contract awards from TED (Tenders Electronic Daily, the EU/EEA procurement journal).

``prepare`` searches TED's public API for award notices whose winner identifier is one of the batch's
organisation numbers, 100 organisations per request (both ``923609016`` and ``923 609 016`` spellings).
A notice is attributed only when one of its winner identifiers is this exact organisation number and a
winner is Norwegian. Winner names are not used: TED's name and identifier lists are not aligned.

Published: ``procurement_award`` per notice (publication number, publication date, notice title, buyer,
notice type, link to the official notice). Reuse of TED data is free (Commission Decision 2011/833/EU).
"""
from __future__ import annotations

import re
from typing import Any

from ..model import Contribution, make_claim, make_evidence
from .base import BatchContext, CompanyContext, Connector

API_URL = "https://api.ted.europa.eu/v3/notices/search"
NOTICE_URL = "https://ted.europa.eu/en/notice/-/detail/{pub}"
PHASE = "procurement"
CHUNK = 100
MAX_PAGES = 4
MAX_AWARDS = 15
ORG_RE = re.compile(r"^\d{9}$")
FIELDS = ["publication-number", "publication-date", "notice-title", "buyer-name", "notice-type",
          "winner-identifier", "winner-country"]


def query_body(orgs: list[str], page: int) -> dict[str, Any]:
    vals = " ".join(orgs + [f'"{o[:3]} {o[3:6]} {o[6:]}"' for o in orgs])
    return {"query": f"winner-identifier IN ({vals})", "fields": FIELDS, "limit": 250, "page": page, "scope": "ALL"}


def _lang(v: Any) -> str | None:
    if isinstance(v, dict):
        for k in ("eng", "nor", "nob", "nno"):
            x = v.get(k)
            if x:
                return x[0] if isinstance(x, list) else str(x)
        for x in v.values():
            if x:
                return x[0] if isinstance(x, list) else str(x)
    if isinstance(v, list) and v:
        return str(v[0])
    return str(v) if v else None


def parse_notice(n: dict[str, Any]) -> dict[str, Any] | None:
    pub = n.get("publication-number")
    if not pub:
        return None
    winners = {re.sub(r"\D", "", str(w)) for w in n.get("winner-identifier") or []}
    countries = {str(c).upper() for c in n.get("winner-country") or []}
    title = _lang(n.get("notice-title"))
    if title and " – " in title:  # "Norway – Construction work – New fire station" -> keep the specific part
        title = title.split(" – ")[-1].strip() or title
    return {"publication_number": pub, "published": str(n.get("publication-date") or "")[:10] or None,
            "title": title, "buyer": _lang(n.get("buyer-name")), "notice_type": n.get("notice-type"),
            "winners": winners, "norwegian_winner": "NOR" in countries or "NO" in countries,
            "url": NOTICE_URL.format(pub=pub)}


class ProcurementConnector(Connector):
    name = "procurement"
    families = ("procurement",)
    lane = "procurement"
    lane_workers = 2

    def prepare(self, batch: BatchContext) -> None:
        shared: dict[str, Any] = {"by_org": {}, "queries": [], "errors": []}
        batch.shared["procurement"] = shared
        orgs = [o for o in batch.orgs if ORG_RE.match(o)]
        for i in range(0, len(orgs), CHUNK):
            chunk = orgs[i:i + CHUNK]
            ok, res = True, None
            for page in range(1, MAX_PAGES + 1):
                res = batch.fetcher.get(API_URL, PHASE, method="POST", json_body=query_body(chunk, page), polite=True,
                                        headers={"Accept": "application/json"}, timeout=60.0)
                if not res.ok:
                    ok = False
                    shared["errors"].append(f"TED chunk {i // CHUNK}: {res.error}")
                    break
                try:
                    body = res.json()
                except ValueError as exc:
                    ok = False
                    shared["errors"].append(f"TED chunk {i // CHUNK}: bad json {exc}")
                    break
                notices = body.get("notices") or []
                for n in notices:
                    p = parse_notice(n)
                    if not p or not p["norwegian_winner"]:
                        continue
                    for org in p["winners"] & set(chunk):
                        shared["by_org"].setdefault(org, []).append({**p, "res": res})
                if len(notices) < 250:
                    break
            shared["queries"].append({"orgs": set(chunk), "ok": ok, "retrieved_at": res.retrieved_at if res else None})

    def run(self, ctx: CompanyContext) -> Contribution:
        out = Contribution()
        shared = ctx.batch.shared.get("procurement")
        if shared is None:
            out.state("procurement", "failed", "TED lookup was not prepared")
            return out
        query = next((q for q in shared["queries"] if ctx.org in q["orgs"]), None)
        notices = shared["by_org"].get(ctx.org) or []
        if not notices:
            if query and query["ok"]:
                out.state("procurement", "not_available",
                          "no contract award notice in TED names this organisation number as a winner",
                          checked_at=query["retrieved_at"])
            else:
                out.state("procurement", "blocked" if query is None else "failed",
                          "TED search did not complete: " + "; ".join(shared["errors"][:2]))
            return out
        seen, n = set(), 0
        for p in sorted(notices, key=lambda x: x.get("published") or "", reverse=True):
            if p["publication_number"] in seen or n >= MAX_AWARDS:
                continue
            seen.add(p["publication_number"])
            res = p["res"]
            span = (f"publication-number={p['publication_number']}; publication-date={p['published']}; "
                    f"winner-identifier contains {ctx.org}; buyer={p.get('buyer')}; title={p.get('title')}")
            e = make_evidence(API_URL, "official_procurement_journal", res.retrieved_at, res.content_sha256, span,
                              "ted_search_api_json", final_url=p["url"],
                              locator=f"notices[publication-number={p['publication_number']}]",
                              http_status=res.status, access_policy="TED reuse policy (Commission Decision 2011/833/EU)")
            value = {k: p[k] for k in ("publication_number", "published", "title", "buyer", "notice_type", "url") if p.get(k)}
            out.add(make_claim("procurement_award", value, [e], discriminator=p["publication_number"],
                               effective_at=p.get("published")), e)
            n += 1
        out.state("procurement", "available", f"{len(seen)} TED award notice(s) name this organisation number as a winner")
        return out
