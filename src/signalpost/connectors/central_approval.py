"""Central approval for building work (sentral godkjenning) from the Directorate for Building Quality (DiBK).

One public page per organisation number: ``https://sgregister.dibk.no/enterprises/<orgnr>`` (HTML; robots.txt
keeps ``/enterprises/`` crawlable, while ``/api/`` and ``/search`` are disallowed and never used). The page is
keyed by the exact organisation number and repeats it; it is used only when the number on the page matches.
A 404 means the register has no approval record for the organisation.

Published: ``central_approval`` (whether the approval is valid, its period, approved areas as
function / subject area / class, insurance flags, and the number of wage earners stated by the register).
"""
from __future__ import annotations

import re
from typing import Any

import lxml.html

from ..model import Contribution, make_claim, make_evidence
from .base import CompanyContext, Connector

PAGE_URL = "https://sgregister.dibk.no/enterprises/{org}"
PHASE = "central_approval"
ORG_RE = re.compile(r"^\d{9}$")
_PERIOD = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s*[–-]\s*(\d{2})\.(\d{2})\.(\d{4})")


def _txt(el: Any) -> str:
    return " ".join(el.text_content().split()) if el is not None else ""


def parse_page(html: bytes, org: str) -> dict[str, Any] | None:
    """Fields from an enterprise page, or None when the page is not about this organisation number."""
    try:
        doc = lxml.html.fromstring(html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html)
    except (ValueError, lxml.etree.ParserError):
        return None
    pairs: dict[str, str] = {}
    for b in doc.xpath("//b"):
        nxt = b.getnext()
        label = _txt(b).rstrip(":")
        if nxt is not None and label:
            pairs.setdefault(label, _txt(nxt))
    if re.sub(r"\D", "", pairs.get("Organisasjonsnummer", "")) != org:
        return None
    heads = [_txt(h) for h in doc.xpath("//h3")]
    status = next((h for h in heads if "godkjenning" in h.lower()), "")
    valid = status.lower().startswith("foretaket har sentral godkjenning")
    out: dict[str, Any] = {"approved": valid, "status_text": status or None,
                           "name": _txt(next(iter(doc.xpath("//h2")), None)) or None}
    m = _PERIOD.search(pairs.get("Godkjenningsperiode", "").replace("–", "-"))
    if m:
        out["valid_from"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        out["valid_to"] = f"{m.group(6)}-{m.group(5)}-{m.group(4)}"
    try:
        out["wage_earners"] = int(pairs.get("Antall lønnsmottakere", "").replace(" ", ""))
    except ValueError:
        pass
    if pairs.get("Webside"):
        out["website_in_register"] = pairs["Webside"]
    areas = []
    for tab in doc.xpath("//table")[:1]:
        for tr in tab.xpath(".//tr"):
            cells = [_txt(td) for td in tr.xpath("./td")]
            if len(cells) >= 3 and cells[0] and cells[1] and re.fullmatch(r"\d", cells[2] or ""):
                areas.append({"function": cells[0], "subject_area": cells[1], "class": int(cells[2])})
    if areas:
        out["areas"] = areas
    flags = [_txt(p) for p in doc.xpath('//*[contains(@class,"bg-dibk-green")]//p')]
    if flags:
        out["insurance"] = [f for f in flags if f]
    if any("opplæringsbedrift" in h.lower() for h in heads + flags):
        out["apprentice_company"] = True
    return {k: v for k, v in out.items() if v not in (None, "", [])}


class CentralApprovalConnector(Connector):
    name = "central_approval"
    families = ("central_approval",)
    lane = "central_approval"
    lane_workers = 4

    def run(self, ctx: CompanyContext) -> Contribution:
        out = Contribution()
        if not ORG_RE.match(ctx.org):
            out.state("central_approval", "not_applicable", "not a valid organisation number")
            return out
        url = PAGE_URL.format(org=ctx.org)
        res = ctx.fetcher.get(url, PHASE, check_robots=True, polite=True, timeout=20.0)
        if res.status == 404:
            out.state("central_approval", "not_available",
                      "DiBK's register of centrally approved building firms has no record for this organisation",
                      checked_at=res.retrieved_at)
            return out
        if not res.ok:
            out.error("central_approval", res.error or f"http_{res.status}", url=url)
            out.state("central_approval", "blocked" if res.state_hint == "blocked" else "failed",
                      f"DiBK register page could not be read: {res.error or res.status}")
            return out
        v = parse_page(res.content, ctx.org)
        if v is None:
            out.state("central_approval", "failed", "DiBK page did not show this organisation number")
            return out
        if not v.get("approved"):
            out.state("central_approval", "not_available",
                      "DiBK lists the organisation but without a valid central approval"
                      + (f" ({v['status_text']})" if v.get("status_text") else ""), checked_at=res.retrieved_at)
            return out
        areas = "; ".join(f"{a['function']} / {a['subject_area']} / class {a['class']}" for a in v.get("areas", [])[:6])
        span = (f"Organisasjonsnummer {ctx.org}; {v.get('status_text')}; Godkjenningsperiode "
                f"{v.get('valid_from')}–{v.get('valid_to')}; {areas}")
        e = make_evidence(url, "official_register_page", res.retrieved_at, res.content_sha256, span,
                          "dibk_sentral_godkjenning_html", final_url=res.final_url, locator="main",
                          http_status=res.status, access_policy="public register page (DiBK); robots.txt allows /enterprises/")
        value = {k: v[k] for k in ("approved", "valid_from", "valid_to", "areas", "insurance", "wage_earners",
                                   "apprentice_company", "website_in_register") if k in v}
        value["url"] = url
        out.add(make_claim("central_approval", value, [e], discriminator="dibk", effective_at=v.get("valid_from")), e)
        out.state("central_approval", "available", "valid central approval for building work (DiBK)")
        return out
