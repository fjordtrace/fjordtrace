"""Workforce (årsverk) from the official annual-account copy in Regnskapsregisteret (NLOD 2.0).

The copy is fetched by organisation number and the registry's latest filed year
(``sisteInnsendteAarsregnskap``), so the document is exact-by-construction for the input company:
``https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}`` (one request per
company; the years list is skipped).

Rate limit (measured 2026-11-06): the ``/kopi/`` routes answer with Bucket4j headers - a bucket of 30
requests that refills about every 30 s per client (≈60 requests/min); over the limit they return 429 with
``x-rate-limit-retry-after-seconds``. Other Brønnøysund endpoints are not limited this way. Requests from this
connector are paced globally (default one start per 1.1 s, ``SIGNALPOST_WORKFORCE_INTERVAL``) and back off on
429. Server-side generation takes 1-10 s per PDF (up to ~45 s for 10 MB reports), so several downloads run
concurrently in the connector's own lane.

Each download is regenerated with a fresh random trailer ``/ID``; evidence carries the SHA-256 of the PDF
with that ID blanked (``stable_pdf_sha256``), which is identical across downloads.

The PDFs are scans (no text layer). ``workforce_ocr`` reads only the structured
"Antall årsverk i regnskapsåret" field on Brønnøysund's company note page; ``workforce_parse`` holds the
rules (company vs group section, double-read digits, explicit zero). Companies not reached before the time
budget are reported as ``blocked``.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from typing import Any

from ..model import Contribution, make_claim, make_evidence
from . import workforce_ocr as wo
from . import workforce_parse as wp
from .base import BatchContext, CompanyContext, Connector

COPY_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}"
SOURCE_CLASS = "official_annual_account_copy"
MAX_PDF_BYTES = 40_000_000
_PDF_ID_RE = re.compile(rb"/ID\s*\[\s*<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*\]")


def stable_pdf_sha256(pdf: bytes) -> str:
    """SHA-256 of the PDF with its trailer ``/ID`` blanked.

    The copy endpoint regenerates the file per request; the only bytes that differ between downloads are the
    random document ID (verified 2026-11-06). Blanking it gives a hash that any re-fetch reproduces, so the
    evidence id stays stable across runs (idempotent refresh)."""
    return hashlib.sha256(_PDF_ID_RE.sub(b"/ID [<> <>]", pdf)).hexdigest()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class _Pacer:
    """Global request pacing for the rate-limited copy endpoint (one start per ``interval`` seconds)."""

    def __init__(self, interval: float):
        self.interval = interval
        self._next = 0.0
        self._lock = threading.Lock()
        self.throttled = 0

    def reserve(self, latest_start: float) -> float | None:
        """Reserve the next start slot; None when it would fall after ``latest_start`` (monotonic)."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            if slot > latest_start:
                return None
            self._next = slot + self.interval
        return slot

    def back_off(self, seconds: float) -> None:
        with self._lock:
            self.throttled += 1
            self._next = max(self._next, time.monotonic() + seconds)


def _year_of(ctx: CompanyContext) -> str | None:
    year = (ctx.profile.get("sisteInnsendteAarsregnskap") or "").strip()
    if re.fullmatch(r"\d{4}", year):
        return year
    # live entity rows may lack the field; fall back to the newest company accounts period
    ends = [str(a.get("period_end") or "")[:4] for a in (ctx.registry.facts.get("accounts") or [])
            if (a.get("account_type") or "").upper() == "SELSKAP"]
    ends = [e for e in ends if re.fullmatch(r"\d{4}", e)]
    return max(ends) if ends else None


def _period(ctx: CompanyContext, year: str) -> tuple[dict[str, str], str | None]:
    for a in ctx.registry.facts.get("accounts") or []:
        if (a.get("account_type") or "").upper() == "SELSKAP" and str(a.get("period_end") or "")[:4] == year:
            start, end = str(a.get("period_start") or "")[:10], str(a.get("period_end") or "")[:10]
            if start and end:
                return {"start": start, "end": end}, end
    return {"fiscal_year": year}, None


def _payroll(ctx: CompanyContext, year: str) -> float | None:
    for a in ctx.registry.facts.get("accounts") or []:
        if (a.get("account_type") or "").upper() == "SELSKAP" and str(a.get("period_end") or "")[:4] == year:
            v = (a.get("figures") or {}).get("payroll_cost")
            return float(v) if isinstance(v, (int, float)) else None
    return None


class WorkforceConnector(Connector):
    name = "workforce"
    families = ("workforce",)
    lane = "workforce"
    lane_workers = 6

    def __init__(self) -> None:
        self.lane_workers = max(1, int(_env_float("SIGNALPOST_WORKFORCE_WORKERS", 6)))
        self.pacer = _Pacer(_env_float("SIGNALPOST_WORKFORCE_INTERVAL", 1.1))
        self.max_retries = 3
        self.stats: dict[str, Any] = {"downloads": 0, "bytes": 0, "download_s": 0.0, "ocr_s": 0.0, "throttled": 0}
        self._stats_lock = threading.Lock()

    def prepare(self, batch: BatchContext) -> None:
        batch.shared["workforce.stats"] = self.stats
        if wo.ocr_enabled():
            # load the ONNX models while the first PDFs download
            threading.Thread(target=wo.get_engine, name="workforce-ocr-warmup", daemon=True).start()

    # ------------------------------------------------------------------ fetching
    def _fetch(self, ctx: CompanyContext, url: str):
        budget = ctx.budget
        margin = 90.0
        res = None
        for attempt in range(self.max_retries + 1):
            latest = time.monotonic() + max(0.0, budget.time_left() - margin)
            slot = self.pacer.reserve(latest)
            if slot is None:
                return None, "time budget reached before the filing copy could be requested"
            delay = slot - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if budget.soft_deadline_passed() or budget.time_left() < margin:
                return None, "time budget reached before the filing copy could be requested"
            t = time.monotonic()
            res = ctx.fetcher.get(url, phase="workforce", max_bytes=MAX_PDF_BYTES, timeout=60)
            with self._stats_lock:
                self.stats["download_s"] += time.monotonic() - t
            if res.status == 429:
                self.pacer.back_off(8.0 * (attempt + 1))
                with self._stats_lock:
                    self.stats["throttled"] += 1
                continue
            if res.status is None and res.error and "Timeout" in res.error and attempt < 1:
                continue
            return res, None
        return res, "Brønnøysund rate limit (HTTP 429) persisted after retries"

    # ------------------------------------------------------------------ main
    def run(self, ctx: CompanyContext) -> Contribution:
        out = Contribution()
        year = _year_of(ctx)
        if not year:
            out.state("workforce", "not_applicable", "no filed annual accounts in Regnskapsregisteret")
            return out
        url = COPY_URL.format(org=ctx.org, year=year)
        res, why = self._fetch(ctx, url)
        if res is None or (why and (res is None or res.status == 429)):
            out.state("workforce", "blocked", why or "not fetched", source_url=url)
            return out
        if res.status in (404, 410) or (res.status == 400):
            out.state("workforce", "not_available", f"no annual-account copy published for {year} (HTTP {res.status})",
                      source_url=url)
            return out
        if not res.ok:
            state = "blocked" if res.state_hint == "blocked" else "failed"
            out.state("workforce", state, f"filing copy fetch failed: {res.error}", source_url=url)
            out.error(self.name, res.error or "fetch failed", url)
            return out
        pdf = res.content
        with self._stats_lock:
            self.stats["downloads"] += 1
            self.stats["bytes"] += len(pdf)
        if not pdf.startswith(b"%PDF"):
            out.state("workforce", "failed", "filing copy is not a PDF", source_url=url)
            return out
        if len(pdf) >= MAX_PDF_BYTES:
            out.state("workforce", "failed", "filing copy larger than the download cap", source_url=url)
            return out

        engine = wo.get_engine()
        t = time.monotonic()
        deadline = time.monotonic() + max(5.0, min(180.0, ctx.budget.time_left() - 60.0))
        try:
            ext = wo.extract_structured(pdf, engine, deadline=deadline)
        except Exception as exc:  # a malformed PDF must not sink the company
            out.state("workforce", "failed", f"filing copy could not be parsed: {type(exc).__name__}", source_url=url)
            out.error(self.name, f"{type(exc).__name__}: {exc}", url)
            return out
        with self._stats_lock:
            self.stats["ocr_s"] += time.monotonic() - t

        diag = {"source_url": url, "pages": ext.pages_total, "pages_read": ext.pages_ocr}
        if ext.text_layer:
            dec = wp.decide_free_text(ext.page_texts)
        elif ext.note and ext.note.startswith("pdf unreadable"):
            out.state("workforce", "failed", ext.note, **diag)
            return out
        elif ext.note and ext.note.startswith("no Brønnøysund-rendered"):
            out.state("workforce", "not_available", "the filing copy has no structured form pages to read "
                      "the workforce field from", **diag)
            return out
        elif engine is None:
            out.state("workforce", "blocked", ext.note or "OCR unavailable; the filing copy is a scanned image", **diag)
            return out
        elif ext.note and "deadline" in ext.note and not any(f.section == "SELSKAP" for f in ext.findings):
            out.state("workforce", "blocked", "time budget reached while reading the filing copy", **diag)
            return out
        else:
            dec = wp.decide_structured(ext.findings)
            if dec.state == "not_available" and not ext.findings and not any(
                    wp.classify_header(t)[0] == "notes" for _, t in ext.titles):
                dec.reason = ("the filing has no structured note section (full-report format); the workforce "
                              "figure is only in attached documents, which are not OCR-read")
        if dec.state != "available" or dec.value is None:
            out.state("workforce", dec.state, dec.reason, **diag)
            return out

        period, period_end = _period(ctx, year)
        value_num: float | int = int(dec.value) if float(dec.value).is_integer() else dec.value
        value = {"value": value_num, "measure": dec.measure, "year": year, "scope": "company"}
        note = None
        payroll = _payroll(ctx, year)
        if payroll is not None:
            if dec.value == 0 and payroll >= 1_000_000:
                note = f"filing states zero årsverk while reported payroll cost is {payroll:,.0f}"
            elif dec.value > 0 and payroll == 0:
                note = "filing states årsverk while reported payroll cost is zero"
        ev = make_evidence(url, SOURCE_CLASS, res.retrieved_at, stable_pdf_sha256(pdf), dec.span or "",
                           dec.method or "pdf_ocr_rapidocr", final_url=res.final_url, effective_at=period_end,
                           locator=dec.locator or f"page={dec.page}", http_status=res.status, access_policy="NLOD-2.0")
        out.add(make_claim("workforce_fte", value, [ev], discriminator=year, confidence=dec.confidence,
                           reporting_period=period, effective_at=period_end, note=note), ev)
        out.state("workforce", "available", dec.reason, **diag)
        out.facts["workforce"] = value
        return out
