"""GLEIF Legal Entity Identifier records for the company's exact organisation number (CC0).

``prepare`` looks the whole batch up with GLEIF's public API, 100 organisation numbers per request
(``filter[entity.registeredAs]`` takes a comma-separated list; GLEIF stores Norwegian numbers either as
``923609016`` or ``923 609 016``, so both spellings are sent). A record is attributed only when its
``registeredAs`` is this exact organisation number and the entity is Norwegian (legal address or
jurisdiction NO), so a same-digit identifier from another country's register is never matched.

Published: ``lei_record`` (LEI, legal name as filed with GLEIF, entity and registration status, initial
registration, last update and next renewal dates, managing LOU, legal-address city). A lapsed LEI is
published with its status: it is a fact about the company's identifier, not a claim that it is current.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from ..model import Contribution, make_claim, make_evidence
from .base import BatchContext, CompanyContext, Connector

API_URL = "https://api.gleif.org/api/v1/lei-records"
RECORD_PAGE = "https://search.gleif.org/#/record/{lei}"
PHASE = "lei"
CHUNK = 100
ORG_RE = re.compile(r"^\d{9}$")
LEI_RE = re.compile(r"^[A-Z0-9]{18}\d{2}$")


def spaced(org: str) -> str:
    return f"{org[:3]} {org[3:6]} {org[6:]}"


def query_url(orgs: list[str], page: int = 1) -> str:
    vals = []
    for o in orgs:
        vals += [o, spaced(o)]
    return API_URL + "?" + urlencode({"filter[entity.registeredAs]": ",".join(vals), "page[size]": 200,
                                      "page[number]": page})


def parse_records(body: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for rec in (body or {}).get("data") or []:
        lei = rec.get("id") or ""
        a = rec.get("attributes") or {}
        e, r = a.get("entity") or {}, a.get("registration") or {}
        org = re.sub(r"\D", "", e.get("registeredAs") or "")
        addr = e.get("legalAddress") or {}
        norwegian = (addr.get("country") == "NO") or str(e.get("jurisdiction") or "").upper().startswith("NO")
        if not (LEI_RE.match(lei) and ORG_RE.match(org) and norwegian):
            continue
        out.append({
            "org": org, "lei": lei, "legal_name": (e.get("legalName") or {}).get("name"),
            "entity_status": e.get("status"), "registration_status": r.get("status"),
            "initial_registration": (r.get("initialRegistrationDate") or "")[:10] or None,
            "last_update": (r.get("lastUpdateDate") or "")[:10] or None,
            "next_renewal": (r.get("nextRenewalDate") or "")[:10] or None,
            "managing_lou": r.get("managingLou"), "legal_address_city": addr.get("city"),
            "legal_form_code": (e.get("legalForm") or {}).get("id"),
            "registered_as": e.get("registeredAs"),
        })
    return out


class LeiConnector(Connector):
    name = "lei"
    families = ("lei",)
    lane = "lei"
    lane_workers = 2

    def prepare(self, batch: BatchContext) -> None:
        shared: dict[str, Any] = {"by_org": {}, "queries": [], "errors": []}
        batch.shared["lei"] = shared
        orgs = [o for o in batch.orgs if ORG_RE.match(o)]
        for i in range(0, len(orgs), CHUNK):
            chunk = orgs[i:i + CHUNK]
            page, ok, res = 1, True, None
            while True:
                res = batch.fetcher.get(query_url(chunk, page), PHASE, headers={"Accept": "application/vnd.api+json"},
                                        timeout=60.0)
                if not res.ok:
                    ok = False
                    shared["errors"].append(f"GLEIF chunk {i // CHUNK}: {res.error}")
                    break
                try:
                    body = res.json()
                except ValueError as exc:
                    ok = False
                    shared["errors"].append(f"GLEIF chunk {i // CHUNK}: bad json {exc}")
                    break
                for rec in parse_records(body):
                    if rec["org"] in chunk:
                        rec["res"] = res
                        shared["by_org"].setdefault(rec["org"], []).append(rec)
                last = ((body.get("meta") or {}).get("pagination") or {}).get("lastPage") or 1
                if page >= last or page >= 5:
                    break
                page += 1
            shared["queries"].append({"orgs": set(chunk), "ok": ok, "retrieved_at": res.retrieved_at if res else None})

    def run(self, ctx: CompanyContext) -> Contribution:
        out = Contribution()
        shared = ctx.batch.shared.get("lei")
        if shared is None:
            out.state("lei", "failed", "GLEIF lookup was not prepared")
            return out
        query = next((q for q in shared["queries"] if ctx.org in q["orgs"]), None)
        recs = shared["by_org"].get(ctx.org) or []
        if not recs:
            if query and query["ok"]:
                out.state("lei", "not_available", "no LEI record in GLEIF carries this organisation number",
                          checked_at=query["retrieved_at"])
            else:
                out.state("lei", "blocked" if query is None else "failed",
                          "GLEIF lookup did not complete: " + "; ".join(shared["errors"][:2]))
            return out
        for rec in recs:
            res = rec["res"]
            value = {k: rec[k] for k in ("lei", "legal_name", "entity_status", "registration_status",
                                         "initial_registration", "last_update", "next_renewal", "managing_lou",
                                         "legal_address_city", "legal_form_code") if rec.get(k)}
            value["url"] = RECORD_PAGE.format(lei=rec["lei"])
            span = (f"LEI {rec['lei']}; registeredAs={rec['registered_as']}; legalName={rec['legal_name']}; "
                    f"entity status={rec['entity_status']}; registration status={rec['registration_status']}; "
                    f"next renewal={rec['next_renewal']}")
            e = make_evidence(res.url, "official_identifier_registry", res.retrieved_at, res.content_sha256, span,
                              "gleif_api_json", final_url=value["url"], locator=f"data[id={rec['lei']}]",
                              http_status=res.status, access_policy="CC0 (GLEIF open data)")
            note = None if rec.get("registration_status") == "ISSUED" else (
                f"registration status {rec.get('registration_status')}: the LEI has not been renewed")
            out.add(make_claim("lei_record", value, [e], discriminator=rec["lei"],
                               effective_at=rec.get("last_update"), note=note), e)
        out.state("lei", "available", f"GLEIF has {len(recs)} LEI record(s) registered to this organisation number")
        return out
