"""Food-safety inspections (smilefjesordningen) from the Norwegian Food Safety Authority, Mattilsynet (NLOD 2.0).

``prepare`` downloads Mattilsynet's open inspection file once per run (one request, about 16 MB, updated
daily). Each inspection names the inspected business by organisation number, usually the registered workplace
(underenhet). A company's inspections are those whose organisation number is the company itself or one of its
registered workplaces from Enhetsregisteret, so the match is exact by construction.

Published: ``food_inspection`` per inspection (date, overall grade 0-3 with its meaning, inspected premises
name and address, workplace organisation number, theme grades). At most MAX_INSPECTIONS, most recent first.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any

from ..model import Contribution, make_claim, make_evidence
from .base import BatchContext, CompanyContext, Connector

CSV_URL = "https://smilefjes.mattilsynet.no/api/tilsyn.csv"
PHASE = "food_inspections"
MAX_INSPECTIONS = 10
ORG_RE = re.compile(r"^\d{9}$")
GRADE = {"0": "no remarks (smiling face)", "1": "minor remarks (smiling face)",
         "2": "orders issued (straight face)", "3": "serious breaches (sad face)",
         "4": "theme not inspected", "5": "theme not relevant"}


def _date(ddmmyyyy: str) -> str | None:
    s = re.sub(r"\D", "", ddmmyyyy or "")
    return f"{s[4:8]}-{s[2:4]}-{s[0:2]}" if len(s) == 8 else None


def parse_rows(text: str, wanted: set[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in csv.DictReader(io.StringIO(text), delimiter=";"):
        org = (row.get("orgnummer") or "").strip()
        if org not in wanted:
            continue
        themes = []
        for i in range(1, 5):
            t, g = row.get(f"tema{i}_no"), (row.get(f"karakter{i}") or "").strip()
            if t and g:
                themes.append({"theme": t, "grade": int(g) if g.isdigit() else g})
        g = (row.get("total_karakter") or "").strip()
        out.setdefault(org, []).append({
            "inspection_id": row.get("tilsynid"), "date": _date(row.get("dato") or ""),
            "grade": int(g) if g.isdigit() else None, "grade_meaning": GRADE.get(g),
            "premises": row.get("navn"), "address": ", ".join(x for x in (row.get("adrlinje1"), row.get("postnr"),
                                                                          row.get("poststed")) if x),
            "workplace_orgnr": org, "themes": themes,
            "raw": ";".join(str(row.get(k) or "") for k in ("orgnummer", "navn", "dato", "total_karakter", "tilsynid")),
        })
    return out


class FoodInspectionsConnector(Connector):
    name = "food_inspections"
    families = ("food_inspections",)
    lane = "food_inspections"
    lane_workers = 2

    def prepare(self, batch: BatchContext) -> None:
        shared: dict[str, Any] = {"by_org": {}, "res": None, "error": None}
        batch.shared["food_inspections"] = shared
        wanted: set[str] = set()
        for org in batch.orgs:
            wanted.add(org)
            reg = batch.registry.get(org)
            for w in ((reg.facts.get("workplaces") if reg else None) or []):
                if w.get("organisation_number"):
                    wanted.add(w["organisation_number"])
        res = batch.fetcher.get(CSV_URL, PHASE, timeout=90.0, max_bytes=60_000_000)
        shared["res"] = res
        if not res.ok:
            shared["error"] = res.error or f"http_{res.status}"
            return
        try:
            shared["by_org"] = parse_rows(res.content.decode("utf-8-sig", errors="replace"), wanted)
        except csv.Error as exc:
            shared["error"] = f"unreadable CSV: {exc}"

    def run(self, ctx: CompanyContext) -> Contribution:
        out = Contribution()
        shared = ctx.batch.shared.get("food_inspections")
        if shared is None or shared.get("res") is None:
            out.state("food_inspections", "failed", "Mattilsynet inspection file was not loaded")
            return out
        if shared.get("error"):
            res = shared["res"]
            out.state("food_inspections", "blocked" if res.state_hint == "blocked" else "failed",
                      f"Mattilsynet inspection file could not be read: {shared['error']}")
            return out
        res = shared["res"]
        ours = {ctx.org} | {w.get("organisation_number") for w in (ctx.registry.facts.get("workplaces") or [])
                            if w.get("organisation_number")}
        rows = [r for o in ours for r in shared["by_org"].get(o, [])]
        if not rows:
            out.state("food_inspections", "not_available",
                      "Mattilsynet's inspection register has no inspection of this organisation or its workplaces",
                      checked_at=res.retrieved_at)
            return out
        rows.sort(key=lambda r: (r.get("date") or "", r.get("inspection_id") or ""), reverse=True)
        for r in rows[:MAX_INSPECTIONS]:
            e = make_evidence(CSV_URL, "official_inspection_register", res.retrieved_at, res.content_sha256, r["raw"],
                              "mattilsynet_smilefjes_csv", final_url=res.final_url,
                              locator=f"tilsynid={r['inspection_id']}", http_status=res.status,
                              access_policy="NLOD 2.0 (Mattilsynet)")
            value = {k: v for k, v in r.items() if k != "raw" and v not in (None, "", [])}
            out.add(make_claim("food_inspection", value, [e], discriminator=r["inspection_id"], effective_at=r.get("date")), e)
        out.state("food_inspections", "available", f"{len(rows)} food-safety inspection(s) of this organisation's premises")
        return out
