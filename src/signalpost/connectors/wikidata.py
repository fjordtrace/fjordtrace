"""Wikidata: items that carry the company's exact Norwegian organisation number (P2333). CC0.

``prepare`` finds the items for the whole batch with one SPARQL query per chunk of organisation numbers
(``VALUES ?o {...} ?item wdt:P2333 ?o``; about 1 in 100 companies has one), then fetches each hit's entity JSON
(``Special:EntityData/Q….json``) once. The entity JSON is the evidence: it is re-checked for P2333 = the org
number before anything is published, and every value comes from it.

Published:
* ``wikidata_item``: item id, label, description, inception (P571), employees (P1128, with point in time P585),
  official website (P856) and logo (P154).
* ``social_profile``: Facebook P2013, Instagram P2003, LinkedIn company P4264, YouTube channel P2397, X P2002.
  Name-bearing handles (all but YouTube channel ids) are published only when they carry the company's name,
  Wikidata label or website domain, so a subsidiary's or campaign's handle on a group item is not attributed.

Several items with the same org number: the one whose label matches the legal name is used; if that does not
single one out, the family is ``ambiguous`` with the candidates and nothing is published.
"""
from __future__ import annotations

import os
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from ..model import Contribution, make_claim, make_evidence
from .base import BatchContext, CompanyContext, Connector

SPARQL_URL = "https://query.wikidata.org/sparql"
ENTITY_JSON_URL = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
ENTITY_PAGE_URL = "https://www.wikidata.org/wiki/{qid}"
COMMONS_FILE_URL = "https://commons.wikimedia.org/wiki/Special:FilePath/{name}"
PHASE = "wikidata"
CHUNK = 150  # keeps the GET query URL well under common URL-length limits
ORG_RE = re.compile(r"^\d{9}$")
QID_RE = re.compile(r"^Q\d+$")

SOCIAL = {  # property -> (platform, url template, name-bearing handle?)
    "P2013": ("facebook", "https://www.facebook.com/{h}", True),
    "P2003": ("instagram", "https://www.instagram.com/{h}/", True),
    "P4264": ("linkedin", "https://www.linkedin.com/company/{h}/", True),
    "P2397": ("youtube", "https://www.youtube.com/channel/{h}", False),
    "P2002": ("x", "https://x.com/{h}", True),
}
GENERIC = {"norge", "norway", "norsk", "norske", "group", "gruppen", "konsern", "company", "holding", "kommune",
           "selskap", "official", "invest", "eiendom", "service", "services", "senter", "stiftelsen", "forening"}
LEGAL = {"as", "asa", "sa", "ba", "ans", "da", "enk", "nuf", "ks", "sf", "iks", "hf", "kf", "the", "og", "and"}


def sparql_query(orgs: list[str]) -> str:
    values = " ".join(f'"{o}"' for o in orgs if ORG_RE.match(o))
    return f"SELECT ?o ?item WHERE {{ VALUES ?o {{ {values} }} ?item wdt:P2333 ?o . }}"


def parse_sparql(body: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for b in ((body or {}).get("results") or {}).get("bindings") or []:
        org = (b.get("o") or {}).get("value", "")
        qid = (b.get("item") or {}).get("value", "").rsplit("/", 1)[-1]
        if ORG_RE.match(org) and QID_RE.match(qid) and qid not in out.setdefault(org, []):
            out[org].append(qid)
    return out


def _statements(entity: dict[str, Any], prop: str) -> list[dict[str, Any]]:
    sts = [s for s in (entity.get("claims") or {}).get(prop) or [] if s.get("rank") != "deprecated"]
    preferred = [s for s in sts if s.get("rank") == "preferred"]
    return preferred or sts


def _value(st: dict[str, Any]) -> Any:
    snak = st.get("mainsnak") or {}
    if snak.get("snaktype") != "value":
        return None
    return (snak.get("datavalue") or {}).get("value")


def _time(v: Any) -> str | None:
    if not isinstance(v, dict) or not v.get("time"):
        return None
    t = v["time"].lstrip("+")
    prec = v.get("precision", 11)
    date = t[:10]
    if prec <= 9:
        return date[:4]
    if prec == 10:
        return date[:7]
    return date


def _lang(d: dict[str, Any] | None) -> str | None:
    for lang in ("nb", "no", "nn", "en"):
        if (d or {}).get(lang, {}).get("value"):
            return d[lang]["value"]
    return next((v.get("value") for v in (d or {}).values() if v.get("value")), None)


def parse_entity(entity: dict[str, Any]) -> dict[str, Any]:
    """Fields we publish, from one Wikidata entity JSON object."""
    out: dict[str, Any] = {"item": entity.get("id"), "label": _lang(entity.get("labels")),
                           "description": _lang(entity.get("descriptions"))}
    out["orgnrs"] = [v for s in _statements(entity, "P2333") if isinstance(v := _value(s), str)]
    sites = [v for s in _statements(entity, "P856") if isinstance(v := _value(s), str)]
    if sites:
        out["website"] = sites[0]
        if len(sites) > 1:
            out["websites"] = sites[:5]
    inc = [_time(_value(s)) for s in _statements(entity, "P571")]
    if any(inc):
        out["inception"] = next(i for i in inc if i)
    emps = []
    for s in _statements(entity, "P1128"):
        v = _value(s)
        if not isinstance(v, dict) or "amount" not in v:
            continue
        try:
            n = int(float(v["amount"]))
        except ValueError:
            continue
        when = None
        for q in ((s.get("qualifiers") or {}).get("P585") or []):
            when = _time((q.get("datavalue") or {}).get("value")) or when
        emps.append({"employees": n, "point_in_time": when})
    if emps:
        emps.sort(key=lambda e: e["point_in_time"] or "", reverse=True)
        out["employees"] = emps[0]["employees"]
        if emps[0]["point_in_time"]:
            out["employees_point_in_time"] = emps[0]["point_in_time"]
    logos = [v for s in _statements(entity, "P154") if isinstance(v := _value(s), str)]
    if logos:
        out["logo"] = COMMONS_FILE_URL.format(name=quote(logos[0].replace(" ", "_")))
    socials = []
    for prop, (platform, tpl, _) in SOCIAL.items():
        for s in _statements(entity, prop):
            h = _value(s)
            if isinstance(h, str) and h.strip():
                h = h.strip()
                socials.append({"platform": platform, "handle": h, "url": tpl.format(h=h), "property": prop})
    out["socials"] = socials
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = s.replace("ø", "o").replace("æ", "ae").replace("å", "a")
    return "".join(ch for ch in s if ch.isalnum() and ord(ch) < 128)


def _tokens(s: str | None) -> list[str]:
    s = unicodedata.normalize("NFKC", (s or "").lower())
    return [t for t in re.split(r"[^0-9a-zæøåäöüé]+", s) if t and t not in LEGAL]


def name_keys(legal_name: str | None, label: str | None, website: str | None) -> set[str]:
    keys: set[str] = set()
    for name in (legal_name, label):
        toks = _tokens(name)
        if toks:
            keys.add(_norm("".join(toks)))
        keys |= {_norm(t) for t in toks if len(_norm(t)) >= 4 and t not in GENERIC}
    if website:
        host = (urlsplit(website if "//" in website else "//" + website).hostname or "").lower()
        host = host[4:] if host.startswith("www.") else host
        sld = host.split(".")[-2] if host.count(".") >= 1 else host
        if len(sld) >= 3:
            keys.add(_norm(sld))
    return {k for k in keys if len(k) >= 3}


def handle_matches(handle: str, keys: set[str]) -> bool:
    h = _norm(handle)
    if len(h) < 3:
        return False
    return any(k in h or (len(h) >= 4 and h in k) for k in keys)


def label_matches(label: str | None, legal_name: str | None) -> bool:
    a, b = set(_tokens(label)), set(_tokens(legal_name))
    return bool(a and b) and (a <= b or b <= a)


class WikidataConnector(Connector):
    name = "wikidata"
    families = ("wikidata",)
    lane = "wikidata"
    lane_workers = 4

    def prepare(self, batch: BatchContext) -> None:
        shared: dict[str, Any] = {"by_org": {}, "candidates": {}, "entities": {}, "queries": [], "errors": []}
        batch.shared["wikidata"] = shared
        if os.environ.get("SIGNALPOST_WIKIDATA_OFFLINE") == "1":
            shared["errors"].append("disabled by SIGNALPOST_WIKIDATA_OFFLINE")
            return
        orgs = [o for o in batch.orgs if ORG_RE.match(o)]
        hits: dict[str, list[str]] = {}
        for i in range(0, len(orgs), CHUNK):
            chunk = orgs[i:i + CHUNK]
            url = SPARQL_URL + "?" + urlencode({"query": sparql_query(chunk), "format": "json"})
            res = batch.fetcher.get(url, PHASE, headers={"Accept": "application/sparql-results+json"}, timeout=60.0)
            q = {"url": res.url, "retrieved_at": res.retrieved_at, "sha256": res.content_sha256,
                 "status": res.status, "orgs": set(chunk), "ok": res.ok}
            shared["queries"].append(q)
            if not res.ok:
                shared["errors"].append(f"sparql chunk {i // CHUNK}: {res.error}")
                continue
            try:
                for org, qids in parse_sparql(res.json()).items():
                    if org in q["orgs"]:
                        hits[org] = qids
                        shared["candidates"][org] = {"qids": qids, "query": q}
            except ValueError as exc:
                q["ok"] = False
                shared["errors"].append(f"sparql chunk {i // CHUNK}: bad json {exc}")
        qids = sorted({q for qs in hits.values() for q in qs})
        lock = threading.Lock()

        def fetch(qid: str) -> None:
            res = batch.fetcher.get(ENTITY_JSON_URL.format(qid=qid), PHASE, timeout=30.0)
            rec: dict[str, Any] = {"res": res, "parsed": None}
            if res.ok:
                try:
                    ents = (res.json().get("entities") or {})
                    ent = ents.get(qid) or next(iter(ents.values()), None)
                    if ent:
                        rec["parsed"] = parse_entity(ent)
                except (ValueError, StopIteration):
                    pass
            with lock:
                shared["entities"][qid] = rec

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(fetch, qids))
        for org, qs in hits.items():
            parsed = [shared["entities"].get(q, {}).get("parsed") for q in qs]
            parsed = [p for p in parsed if p and org in p.get("orgnrs", [])]
            if not parsed:
                continue
            chosen = parsed[0] if len(parsed) == 1 else None
            if chosen is None:
                legal = ((batch.registry.get(org).facts.get("profile") or {}).get("navn")
                         if batch.registry.get(org) else None)
                matching = [p for p in parsed if label_matches(p.get("label"), legal)]
                chosen = matching[0] if len(matching) == 1 else None
            if chosen:
                shared["by_org"][org] = {k: v for k, v in chosen.items() if k not in ("orgnrs",)}

    def run(self, ctx: CompanyContext) -> Contribution:
        out = Contribution()
        shared = ctx.batch.shared.get("wikidata")
        if shared is None:
            out.state("wikidata", "failed", "Wikidata lookup was not prepared")
            return out
        cand = shared["candidates"].get(ctx.org)
        query = next((q for q in shared["queries"] if ctx.org in q["orgs"]), None)
        if not cand:
            if query and query["ok"]:
                out.state("wikidata", "not_available", "no Wikidata item carries this organisation number (P2333)",
                          checked_at=query["retrieved_at"])
            else:
                out.state("wikidata", "blocked" if query is None else "failed",
                          "Wikidata query did not complete: " + "; ".join(shared["errors"][:2]))
            return out
        item = shared["by_org"].get(ctx.org)
        if not item:
            qids = cand["qids"]
            fetched = [q for q in qids if (shared["entities"].get(q) or {}).get("parsed")]
            if len(fetched) < len(qids):
                out.state("wikidata", "failed", "Wikidata entity fetch failed for " + ", ".join(qids), candidates=qids)
            elif len(qids) > 1:
                out.state("wikidata", "ambiguous", f"{len(qids)} Wikidata items carry this organisation number and "
                          "none is singled out by the legal name", candidates=qids)
            else:
                out.state("wikidata", "not_available", "the Wikidata item no longer carries this organisation number",
                          candidates=qids)
            return out
        qid = item["item"]
        res = shared["entities"][qid]["res"]
        page = ENTITY_PAGE_URL.format(qid=qid)

        def ev(prop: str, span: str) -> dict[str, Any]:
            return make_evidence(res.url, "open_knowledge_base", res.retrieved_at, res.content_sha256,
                                 f"{qid} P2333={ctx.org}; {span}", "wikidata_entity_json", final_url=page,
                                 locator=f"{qid}#{prop}", http_status=res.status, access_policy="CC0")

        value = {k: item[k] for k in ("item", "label", "description", "inception", "employees",
                                       "employees_point_in_time", "website", "logo") if item.get(k) is not None}
        value["url"] = page
        span = "; ".join(f"{k}={value[k]}" for k in ("label", "inception", "employees", "website") if k in value)
        e = ev("P2333", span)
        out.add(make_claim("wikidata_item", value, [e], discriminator=qid), e)
        legal = ctx.profile.get("navn")
        keys = name_keys(legal, item.get("label"), item.get("website"))
        skipped = []
        for s in item.get("socials") or []:
            platform, _, name_bearing = SOCIAL[s["property"]]
            if name_bearing and not handle_matches(s["handle"], keys):
                skipped.append(f"{platform}:{s['handle']}")
                continue
            e = ev(s["property"], f"{s['property']}={s['handle']}")
            val = {"platform": platform, "handle": s["handle"], "url": s["url"], "source": "wikidata",
                   "wikidata_item": qid, "wikidata_property": s["property"]}
            out.add(make_claim("social_profile", val, [e], discriminator=f"{platform}:{s['handle'].lower()}",
                               confidence=0.95), e)
        out.state("wikidata", "available", f"Wikidata item {qid} carries P2333={ctx.org}", item=qid,
                  **({"social_handles_not_attributed": skipped} if skipped else {}))
        return out
