"""Public support awards from Brønnøysundregistrene's Støtteregisteret (register of public aid; NLOD 2.0).

One search per company on the register's public API (``STOETTEMOTTAKER_ORGNR`` = the exact organisation
number). Every award names its recipient by organisation number; an award is published only when that
recipient is this organisation. Awards include grants, loans, guarantees and tax or duty relief granted under
EEA state-aid rules, by authorities such as Innovasjon Norge, Skatteetaten (e.g. SkatteFUNN), Enova or
Forskningsrådet.

Published: ``public_support`` per award (award id, date granted, amount and currency, instrument, granting
body and its organisation number, aid category, purpose, scheme id). At most MAX_AWARDS are published
(most recent first); the family state says how many the register holds.
"""
from __future__ import annotations

import re
from typing import Any

from ..model import Contribution, make_claim, make_evidence
from .base import CompanyContext, Connector

API_URL = "https://stottetiltak-registerinfo-api.app.brreg.no/api/v1/soek/stoettetildeling"
PHASE = "public_support"
MAX_AWARDS = 25
ORG_RE = re.compile(r"^\d{9}$")

INSTRUMENT_EN = {
    "tilskudd": "grant", "laan": "loan", "laanMedRentefordel": "loan on favourable terms", "garanti": "guarantee",
    "skatteEllerAvgiftsFritak": "tax or duty exemption", "skattefradrag": "tax deduction",
    "redusertSatsPaaSkatteEllerAvgift": "reduced tax or duty rate", "egenkapital": "equity investment",
    "annet": "other",
}
CATEGORY_EN = {
    "bagatellmessigStoette": "de minimis aid", "gruppeunntak": "block-exempted aid",
    "notifisertStoette": "notified state aid", "tjenesterAvAllmennOekonomiskInteresse": "services of general economic interest",
}


def code(v: Any) -> str | None:
    """'stoetteinstrumenttype.tilskudd' -> 'tilskudd'."""
    return str(v).rsplit(".", 1)[-1] if v else None


def parse_award(a: dict[str, Any], org: str) -> dict[str, Any] | None:
    roles = a.get("rolle") or []
    recipient = [r for r in roles if code(r.get("type")) == "stoettemottaker"
                 and ((r.get("rolleinnehaverVirksomhet") or {}).get("organisasjonsnummer") == org)]
    if not recipient:
        return None
    giver = next((r.get("rolleinnehaverVirksomhet") or {} for r in roles if code(r.get("type")) == "stoettegiver"), {})
    amt = a.get("tildeltBeloep") or {}
    legal = a.get("eoesRettsligGrunnlag") or {}
    prim = legal.get("primaerrettsligGrunnlag") or {}
    instrument = code(a.get("stoetteinstrument"))
    category = code(legal.get("statsstoettekategori"))
    value = {
        "award_id": a.get("stoettetiltaksnummer"),
        "granted_date": a.get("tildelingsdato"),
        "amount": amt.get("beloep") if isinstance(amt.get("beloep"), (int, float)) else None,
        "currency": amt.get("valuta"),
        "instrument": instrument,
        "instrument_en": INSTRUMENT_EN.get(instrument or ""),
        "granting_body": giver.get("navn"),
        "granting_body_orgnr": giver.get("organisasjonsnummer"),
        "aid_category": category,
        "aid_category_en": CATEGORY_EN.get(category or ""),
        "purpose": code(prim.get("formaal")),
        "scheme_id": a.get("tilknyttetStoetteordning"),
        "reported_date": a.get("mottattDato"),
        "status": code(a.get("status")),
    }
    return {k: v for k, v in value.items() if v not in (None, "")}


class PublicSupportConnector(Connector):
    name = "public_support"
    families = ("public_support",)
    lane = "public_support"
    lane_workers = 6

    def run(self, ctx: CompanyContext) -> Contribution:
        out = Contribution()
        if not ORG_RE.match(ctx.org):
            out.state("public_support", "not_applicable", "not a valid organisation number")
            return out
        body = [{"field": "STOETTEMOTTAKER_ORGNR", "value": ctx.org, "matchType": "EXACT_MATCH"}]
        res = ctx.fetcher.get(API_URL, PHASE, method="POST", json_body=body, params={"pageSize": 200},
                              headers={"Accept": "application/json"}, timeout=30.0)
        if not res.ok:
            out.error("public_support", res.error or f"http_{res.status}")
            out.state("public_support", "blocked" if res.state_hint == "blocked" else "failed",
                      f"Støtteregisteret search failed: {res.error or res.status}")
            return out
        try:
            awards = res.json().get("stoettetildeling") or []
        except (ValueError, AttributeError) as exc:
            out.state("public_support", "failed", f"Støtteregisteret returned unreadable JSON: {exc}")
            return out
        parsed = [p for a in awards if (p := parse_award(a, ctx.org)) and p.get("award_id")]
        if not parsed:
            out.state("public_support", "not_available",
                      "Støtteregisteret lists no public support awarded to this organisation number",
                      checked_at=res.retrieved_at)
            return out
        parsed.sort(key=lambda p: (p.get("granted_date") or "", p["award_id"]), reverse=True)
        for p in parsed[:MAX_AWARDS]:
            span = (f"stoettetiltaksnummer={p['award_id']}; stoettemottaker={ctx.org}; "
                    f"tildelingsdato={p.get('granted_date')}; tildeltBeloep={p.get('amount')} {p.get('currency') or ''}; "
                    f"stoettegiver={p.get('granting_body')} ({p.get('granting_body_orgnr')}); "
                    f"stoetteinstrument={p.get('instrument')}")
            e = make_evidence(API_URL, "official_registry_live", res.retrieved_at, res.content_sha256, span,
                              "brreg_stotteregisteret_api_json", final_url=res.final_url,
                              locator=f"POST STOETTEMOTTAKER_ORGNR={ctx.org}; stoettetiltaksnummer={p['award_id']}",
                              http_status=res.status, access_policy="NLOD 2.0 (Brønnøysundregistrene)")
            out.add(make_claim("public_support", p, [e], discriminator=p["award_id"],
                               effective_at=p.get("granted_date")), e)
        more = f"; the {MAX_AWARDS} most recent are published" if len(parsed) > MAX_AWARDS else ""
        out.state("public_support", "available", f"Støtteregisteret lists {len(parsed)} award(s) to this organisation{more}",
                  awards_total=len(parsed))
        return out
