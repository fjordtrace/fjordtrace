"""Page decoding and targeted OCR for Brønnøysund annual-account copies.

The copy endpoint serves image-only PDFs (no text layer). Brønnøysundregistrene renders the electronically
filed form first - cover ("GENERELL INFORMASJON"), RESULTATREGNSKAP, BALANSE and, for filers that use the
form's note module, "NOTEOPPLYSNINGER - SELSKAP" - as clean 1-bit pages of exactly 1728x2312 px, then appends
the filer's own documents (other page sizes). The note module carries a fixed field::

    Antall årsverk i regnskapsåret        (Nynorsk: "Tal på årsverk i rekneskapsåret")
    32.00

with the value printed directly below the label at the same left margin (or at the top of the next page when
the label is the page's last line).

Because those pages are digital renders, text lines and phrases are found with numpy projection profiles
(a few ms per page) instead of a detection network. Phrases that start at the left margin and are as wide as
the label or a section title are compared with stored bitmaps of those fixed phrases (``workforce_templates``:
every occurrence in 112 sampled filings was bit-identical); only when a document's render matches no
template are such phrases OCR-read instead. Pages are read in order so the section title (SELSKAP vs
KONSERN) in force at the label is known. The value itself is recognised by OCR (RapidOCR's PP-OCR
recogniser on onnxruntime, ~0.2 CPU-s per crop) from two different crops, which must read identically.

OCR is optional: this module imports without the OCR wheels, and ``get_engine()`` returns None (with
``engine_error()``) when OCR is disabled (``SIGNALPOST_OCR=0``) or cannot load.
"""
from __future__ import annotations

import io
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from . import workforce_parse as wp

# Geometry of Brønnøysund's rendering (scaled if a render ever differs in width).
REF_WIDTH = 1728
BRREG_SIZE = (1728, 2312)
LEFT_MARGIN_MAX = 300           # label and titles start at x≈200-210
CAND_MIN_W, CAND_MAX_W = 470, 560   # measured ink widths: label 499 (nynorsk 515), "NOTEOPPLYSNINGER - SELSKAP/KONSERN" 519-520
MAX_LINE_H = 60
PHRASE_GAP = 36                 # a horizontal gap this wide separates phrases (word spaces are ~10-15 px)
VALUE_MAX_W = 260
MAX_BRREG_PAGES = 14


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0


@dataclass
class PageImage:
    number: int                      # 1-based
    image: Any = None                # numpy uint8 grayscale (0 = black), or None
    text: str = ""                   # text layer, when the page has one
    width: int = 0
    height: int = 0


@dataclass
class Finding:
    """One occurrence of the structured workforce field."""

    page: int
    section: str | None              # "SELSKAP" / "KONSERN" / None (unknown)
    label_text: str
    label_score: float
    value_text: str | None = None
    value_score: float = 0.0
    value_check_text: str | None = None
    value_page: int | None = None
    header_text: str = ""
    method: str = "pdf_ocr_rapidocr"
    label_box: tuple[int, int, int, int] | None = None
    value_box: tuple[int, int, int, int] | None = None


@dataclass
class ExtractResult:
    findings: list[Finding] = field(default_factory=list)
    pages_total: int = 0
    brreg_pages: int = 0
    pages_ocr: list[int] = field(default_factory=list)
    titles: list[tuple[int, str]] = field(default_factory=list)
    text_layer: bool = False
    page_texts: dict[int, str] = field(default_factory=dict)   # text-layer pages (for free-text parsing)
    rec_calls: int = 0
    template_hits: int = 0
    ocr_ms: int = 0
    note: str | None = None


# --------------------------------------------------------------------------- PDF pages
def open_pdf(pdf: bytes):
    from pypdf import PdfReader

    return PdfReader(io.BytesIO(pdf))


def _page_image_obj(page: Any) -> Any:
    try:
        xobjs = page["/Resources"]["/XObject"]
    except Exception:
        return None
    best = None
    for key in xobjs:
        obj = xobjs[key].get_object()
        if obj.get("/Subtype") != "/Image":
            continue
        area = int(obj.get("/Width", 0)) * int(obj.get("/Height", 0))
        if best is None or area > best[0]:
            best = (area, obj)
    return best[1] if best else None


def image_dims(page: Any) -> tuple[int, int, int] | None:
    """(width, height, bits) of the page's main image, read from the dictionary without decoding."""
    obj = _page_image_obj(page)
    if obj is None:
        return None
    return int(obj.get("/Width", 0)), int(obj.get("/Height", 0)), int(obj.get("/BitsPerComponent", 0))


def _decode_image_xobject(obj: Any):
    import numpy as np

    w, h = int(obj["/Width"]), int(obj["/Height"])
    bpc = int(obj.get("/BitsPerComponent", 8))
    filt = obj.get("/Filter")
    filters = [str(f) for f in (filt if isinstance(filt, list) else [filt])] if filt is not None else []
    if any(f in ("/DCTDecode", "/JPXDecode", "/CCITTFaxDecode", "/JBIG2Decode") for f in filters):
        return None  # not Brønnøysund's own render (those are Flate 1-bit); never needed for the form field
    data = obj.get_data()
    if bpc == 1:
        rowb = (w + 7) // 8
        raw = np.frombuffer(data, np.uint8)
        if raw.size < rowb * h:
            return None
        bits = np.unpackbits(raw[: rowb * h].reshape(h, rowb), axis=1)[:, :w]
        dec = obj.get("/Decode")
        inverted = bool(dec) and float(dec[0]) == 1.0
        img = (1 - bits) if inverted else bits
        return (img * 255).astype(np.uint8)
    if bpc == 8:
        comps = 3 if str(obj.get("/ColorSpace")) in ("/DeviceRGB", "/CalRGB") else 1
        raw = np.frombuffer(data, np.uint8)
        if raw.size < w * h * comps:
            return None
        if comps > 1:
            return raw[: w * h * comps].reshape(h, w, comps).mean(axis=2).astype(np.uint8)
        return raw[: w * h].reshape(h, w).copy()
    return None


def iter_pages(pdf: Any, pages: list[int] | None = None, want_image: bool = True) -> Iterator[PageImage]:
    """Yield requested pages (1-based) with their text layer or decoded page image.

    ``pdf`` is the raw bytes or a reader from ``open_pdf``."""
    reader = open_pdf(pdf) if isinstance(pdf, (bytes, bytearray)) else pdf
    n = len(reader.pages)
    for num in (pages if pages is not None else range(1, n + 1)):
        if not 1 <= num <= n:
            continue
        page = reader.pages[num - 1]
        out = PageImage(num)
        try:
            out.text = page.extract_text() or ""
        except Exception:
            out.text = ""
        if len(out.text.strip()) < 40:
            out.text = ""
            if want_image:
                obj = _page_image_obj(page)
                try:
                    out.image = _decode_image_xobject(obj) if obj is not None else None
                except Exception:
                    out.image = None
                if out.image is not None:
                    out.height, out.width = out.image.shape[:2]
        yield out


# --------------------------------------------------------------------------- layout (no OCR)
def _runs(mask, min_gap: int = 1) -> list[list[int]]:
    """[start, end) of True runs in a 1-D bool array, merging gaps shorter than ``min_gap``."""
    import numpy as np

    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.astype(np.int8), [0]))))
    out: list[list[int]] = []
    for s, e in zip(edges[0::2].tolist(), edges[1::2].tolist()):
        if out and s - out[-1][1] < min_gap:
            out[-1][1] = e
        else:
            out.append([s, e])
    return out


def phrases(img, gap: int = PHRASE_GAP) -> list[Box]:
    """Text phrases on a clean render: ink row-runs (lines) split at wide horizontal gaps. Reading order."""
    import numpy as np

    ink = img < 128
    out: list[Box] = []
    for y0, y1 in _runs(ink.any(axis=1), 2):
        if y1 - y0 < 6:
            continue
        for x0, x1 in _runs(ink[y0:y1].any(axis=0), gap):
            if x1 - x0 < 4:
                continue
            rows = np.flatnonzero(ink[y0:y1, x0:x1].any(axis=1))  # tight: ignore neighbours' descenders
            out.append(Box(x0, y0 + int(rows[0]), x1, y0 + int(rows[-1]) + 1))
    return out


def _scale(page: PageImage) -> float:
    return (page.width / REF_WIDTH) if page.width else 1.0


def candidates(page: PageImage, boxes: list[Box]) -> list[Box]:
    """Phrases that can be the workforce label or a section title."""
    s = _scale(page)
    return [b for b in boxes if b.x0 <= LEFT_MARGIN_MAX * s and CAND_MIN_W * s <= b.w <= CAND_MAX_W * s
            and b.h <= MAX_LINE_H * s]


def value_below(page: PageImage, boxes: list[Box], label: Box) -> Box | None:
    s = _scale(page)
    for b in boxes:
        if b.y0 < label.y1 or b.y0 > label.y1 + 30 * s:
            continue
        if abs(b.x0 - label.x0) <= 40 * s and b.w <= VALUE_MAX_W * s and b.h <= MAX_LINE_H * s:
            return b
        if b.x0 <= LEFT_MARGIN_MAX * s:
            return None  # another left-margin line comes first: the value is blank
    return None


def value_at_top(page: PageImage, boxes: list[Box], label_x0: float) -> Box | None:
    s = _scale(page)
    for b in boxes:
        if b.y0 > 260 * s:
            return None
        if b.x0 <= LEFT_MARGIN_MAX * s:
            ok = abs(b.x0 - label_x0) <= 40 * s and b.w <= VALUE_MAX_W * s and b.h <= MAX_LINE_H * s
            return b if ok else None
    return None


# --------------------------------------------------------------------------- OCR engine
_engine_lock = threading.Lock()
_engine_singleton: "OcrEngine | None" = None
_engine_error: str | None = None


def ocr_enabled() -> bool:
    return os.environ.get("SIGNALPOST_OCR", "1").strip().lower() not in ("0", "false", "no", "off")


def default_threads() -> int:
    env = os.environ.get("SIGNALPOST_OCR_THREADS", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    return 2 if (os.cpu_count() or 1) > 2 else 1


def _disable_ort_spinning() -> None:
    """Idle onnxruntime worker threads spin by default and burn CPU other lanes could use; turn that off.

    RapidOCR (pinned 3.10.0) builds its SessionOptions in ``OrtInferSession._init_sess_opts``; wrap it once.
    Measured on 25 filings: same wall time per filing, ~25% less CPU with 2 threads."""
    try:
        from rapidocr.inference_engine.onnxruntime import main as ort_main

        cls = ort_main.OrtInferSession
        if getattr(cls, "_signalpost_nospin", False):
            return
        original = cls._init_sess_opts

        def patched(cfg):
            opts = original(cfg)
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
            return opts

        cls._init_sess_opts = staticmethod(patched)
        cls._signalpost_nospin = True
    except Exception:  # an internal API change must never disable OCR
        pass


class OcrEngine:
    """Recognition-only RapidOCR wrapper. RapidOCR mutates its params per call, so calls are serialised."""

    name = "rapidocr"

    def __init__(self, threads: int | None = None):
        from rapidocr import RapidOCR  # heavy import, deferred

        _disable_ort_spinning()
        threads = threads or default_threads()
        params = {
            "Global.use_det": False,
            "Global.use_cls": False,
            "Global.log_level": "critical",
            "Global.text_score": 0.3,
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
        self._ocr = RapidOCR(params=params)
        self._lock = threading.Lock()
        self.threads = threads
        self.busy_ms = 0.0
        self.calls = 0

    def recognize(self, img, box: Box, pad: int = 6) -> tuple[str, float]:
        import numpy as np

        h, w = img.shape[:2]
        x0, y0 = max(0, int(box.x0) - pad), max(0, int(box.y0) - pad)
        x1, y1 = min(w, int(box.x1) + pad), min(h, int(box.y1) + pad)
        if x1 - x0 < 4 or y1 - y0 < 4:
            return "", 0.0
        crop = np.ascontiguousarray(img[y0:y1, x0:x1])
        with self._lock:
            t = time.monotonic()
            res = self._ocr(crop, use_det=False, use_cls=False, use_rec=True)
            self.busy_ms += (time.monotonic() - t) * 1000
            self.calls += 1
        txts = getattr(res, "txts", None) or ()
        scores = getattr(res, "scores", None) or ()
        if not txts:
            return "", 0.0
        return str(txts[0]), float(scores[0]) if scores else 0.0


def get_engine() -> "OcrEngine | None":
    """Process-wide engine, created on first use. None (see ``engine_error()``) when OCR cannot run."""
    global _engine_singleton, _engine_error
    if not ocr_enabled():
        _engine_error = "OCR disabled (SIGNALPOST_OCR=0)"
        return None
    with _engine_lock:
        if _engine_singleton is None and _engine_error is None:
            try:
                _engine_singleton = OcrEngine()
            except Exception as exc:  # missing wheel, unsupported platform, model load failure
                _engine_error = f"OCR engine unavailable: {type(exc).__name__}: {exc}"[:200]
        return _engine_singleton


def engine_error() -> str | None:
    return _engine_error


# --------------------------------------------------------------------------- extraction
def brreg_page_count(reader: Any) -> int:
    """Number of leading pages that are Brønnøysund's own 1-bit render (1728x2312)."""
    k = 0
    for page in reader.pages[:MAX_BRREG_PAGES]:
        dims = image_dims(page)
        if not dims or (dims[0], dims[1]) != BRREG_SIZE or dims[2] != 1:
            break
        k += 1
    return k


_templates_cache: list | None = None


def _templates() -> list:
    """Decoded phrase templates: (text, kind, ink bitmap)."""
    global _templates_cache
    if _templates_cache is None:
        import base64
        import zlib

        import numpy as np

        from .workforce_templates import TEMPLATES

        out = []
        for text, kind, w, h, b64 in TEMPLATES:
            bits = np.unpackbits(np.frombuffer(zlib.decompress(base64.b64decode(b64)), np.uint8))[: w * h]
            bm = bits.reshape(h, w).astype(bool)
            rows, cols = np.flatnonzero(bm.any(axis=1)), np.flatnonzero(bm.any(axis=0))
            out.append((text, kind, bm[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]))  # tight, like phrases()
        _templates_cache = out
    return _templates_cache


def match_template(page: PageImage, box: Box, max_diff: float = 0.02) -> tuple[str, str] | None:
    """(text, kind) when the phrase is pixel-identical (within ``max_diff`` of its ink) to a known render."""
    if page.image is None or abs(_scale(page) - 1.0) > 1e-6:
        return None
    w, h = int(box.w), int(box.h)
    for text, kind, tpl in _templates():
        th, tw = tpl.shape
        if abs(tw - w) > 2 or abs(th - h) > 2:
            continue
        crop = page.image[int(box.y0):int(box.y0) + th, int(box.x0):int(box.x0) + tw] < 128
        if crop.shape != tpl.shape:
            continue
        if (crop ^ tpl).sum() <= max_diff * tpl.sum():
            return text, kind
    return None


def _ibox(b: Box) -> tuple[int, int, int, int]:
    return int(b.x0), int(b.y0), int(b.x1), int(b.y1)


def _read_value(engine: OcrEngine, page: PageImage, box: Box) -> tuple[str, float, str]:
    t1, s1 = engine.recognize(page.image, box, pad=4)
    t2, _ = engine.recognize(page.image, box, pad=12)
    return t1.strip(), s1, t2.strip()


def extract_structured(pdf: bytes, engine: OcrEngine | None, *, deadline: float | None = None) -> ExtractResult:
    """Read the structured "Antall årsverk i regnskapsåret" field from Brønnøysund's rendered pages."""
    res = ExtractResult()
    t0 = time.monotonic()
    try:
        reader = open_pdf(pdf)
        res.pages_total = len(reader.pages)
    except Exception as exc:
        res.note = f"pdf unreadable: {type(exc).__name__}"
        return res
    # Text-layer PDFs (rare): hand the text to the free-text rules.
    probe = next(iter_pages(reader, [min(res.pages_total, 2)], want_image=False), None)
    if probe is not None and probe.text:
        res.text_layer = True
        for page in iter_pages(reader, want_image=False):
            if page.text:
                res.page_texts[page.number] = page.text
        return res
    res.brreg_pages = brreg_page_count(reader)
    if res.brreg_pages < 2:
        res.note = "no Brønnøysund-rendered form pages in the copy"
        return res
    if engine is None:
        res.note = engine_error() or "OCR unavailable"
        return res

    section: str | None = None
    pending: Finding | None = None
    pending_x0 = 0.0
    calls0 = engine.calls
    pages = list(range(2, res.brreg_pages + 1))
    # Pass 1 (no OCR): does this document use the known render? Then non-template phrases need no OCR.
    known_render = False
    for page in iter_pages(reader, pages):
        if page.image is not None and any(match_template(page, c) for c in candidates(page, phrases(page.image))):
            known_render = True
            break
    for page in iter_pages(reader, pages):
        if deadline is not None and time.monotonic() > deadline:
            res.note = "OCR deadline reached"
            break
        if page.image is None:
            pending = None
            continue
        res.pages_ocr.append(page.number)
        boxes = phrases(page.image)
        if pending is not None:
            vb = value_at_top(page, boxes, pending_x0)
            if vb is not None:
                pending.value_text, pending.value_score, pending.value_check_text = _read_value(engine, page, vb)
                pending.value_page, pending.value_box = page.number, _ibox(vb)
            pending = None
        s = _scale(page)
        for cand in candidates(page, boxes):
            hit = match_template(page, cand)
            if hit is not None:
                text, score = hit[0], 1.0
                res.template_hits += 1
            elif known_render:
                continue  # known render and not one of its fixed phrases: no need to OCR it
            else:
                text, score = engine.recognize(page.image, cand)  # unknown render: fall back to OCR
            kind, sec = wp.classify_header(text)
            if kind == "notes":
                section = sec
                res.titles.append((page.number, text))
                continue
            if kind == "accounts":
                section = None
                res.titles.append((page.number, text))
                continue
            if not wp.is_structured_label(text):
                continue
            f = Finding(page=page.number, section=section, label_text=text, label_score=score,
                        label_box=_ibox(cand))
            vb = value_below(page, boxes, cand)
            if vb is not None:
                f.value_text, f.value_score, f.value_check_text = _read_value(engine, page, vb)
                f.value_page, f.value_box = page.number, _ibox(vb)
            elif cand.y1 > page.height - 260 * s and boxes and boxes[-1] is cand:
                pending, pending_x0 = f, cand.x0
            res.findings.append(f)
            # one workforce field per note section: a later label needs a fresh section title, so a
            # misread "NOTEOPPLYSNINGER - KONSERN" can never pass a group figure off as the company's
            section = None
        if pending is None and any(f.section == "SELSKAP" and f.value_text for f in res.findings):
            break  # the company figure is read; group pages (if any) come later
    res.rec_calls = engine.calls - calls0
    res.ocr_ms = int((time.monotonic() - t0) * 1000)
    return res
