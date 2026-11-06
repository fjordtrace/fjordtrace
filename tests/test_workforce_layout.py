"""Offline tests for the page-layout logic of the workforce extractor (synthetic pages, fake OCR)."""
from __future__ import annotations

import numpy as np
import pytest

from signalpost.connectors import workforce_ocr as wo
from signalpost.connectors import workforce_parse as wp

W, H = wo.BRREG_SIZE
LABEL = "Antall årsverk i regnskapsåret"


def page(number: int, items: list[tuple[int, int, int, str]]):
    """A white Brønnøysund-sized page with a black bar per (x0, y0, width, text) phrase."""
    img = np.full((H, W), 255, np.uint8)
    for x0, y0, w, _ in items:
        img[y0:y0 + 24, x0:x0 + w] = 0
    p = wo.PageImage(number, img, "", W, H)
    p.texts = {y0: t for _, y0, _, t in items}
    return p


class FakeEngine:
    """Returns the text planted at a phrase's top edge."""

    def __init__(self, pages):
        self.pages = {id(p.image): p for p in pages}
        self.calls = 0

    def recognize(self, img, box, pad=6):
        self.calls += 1
        p = self.pages[id(img)]
        return p.texts.get(int(box.y0), "?"), 0.99


def run(monkeypatch, pages):
    class Reader:
        def __init__(self):
            self.pages = [None] * max(p.number for p in pages)

    monkeypatch.setattr(wo, "open_pdf", lambda pdf: Reader())
    monkeypatch.setattr(wo, "brreg_page_count", lambda reader: max(p.number for p in pages))

    def fake_iter(reader, nums=None, want_image=True):
        for p in pages:
            if nums is None or p.number in nums:
                yield p

    monkeypatch.setattr(wo, "iter_pages", fake_iter)
    eng = FakeEngine(pages)
    res = wo.extract_structured(b"%PDF-fake", eng)
    return res, wp.decide_structured(res.findings)


def test_phrases_split_lines_and_wide_gaps():
    p = page(5, [(207, 391, 519, "t"), (902, 391, 560, "- alle poster"), (207, 544, 499, LABEL), (207, 573, 93, "32.00")])
    boxes = wo.phrases(p.image)
    assert [(int(b.x0), int(b.y0), int(b.w)) for b in boxes] == [(207, 391, 519), (902, 391, 560), (207, 544, 499), (207, 573, 93)]
    cands = wo.candidates(p, boxes)
    assert [int(b.y0) for b in cands] == [391, 544]          # title and label, not the right column or value
    assert int(wo.value_below(p, boxes, cands[1]).y0) == 573


def test_value_below_is_none_when_next_left_line_is_text():
    p = page(5, [(207, 544, 499, LABEL), (207, 580, 400, "Note")])
    boxes = wo.phrases(p.image)
    assert wo.value_below(p, boxes, boxes[0]) is None


def test_company_value_read_and_group_section_ignored(monkeypatch):
    pages = [
        page(2, [(207, 246, 300, "Organisasjonsnr: 1"), (207, 391, 330, "RESULTATREGNSKAP")]),
        page(3, [(207, 391, 519, "NOTEOPPLYSNINGER - SELSKAP"), (207, 544, 499, LABEL), (207, 573, 93, "12.00")]),
        page(4, [(207, 391, 520, "NOTEOPPLYSNINGER - KONSERN"), (207, 544, 499, LABEL), (207, 573, 120, "1200.00")]),
    ]
    res, dec = run(monkeypatch, pages)
    assert dec.state == "available" and dec.value == 12.0 and dec.page == 3
    assert dec.span == f"{LABEL} 12.00"
    assert res.pages_ocr == [2, 3]  # stops after the company figure; group pages are not read


def test_value_carried_to_next_page(monkeypatch):
    pages = [
        page(5, [(207, 391, 519, "NOTEOPPLYSNINGER - SELSKAP"), (207, 2250, 499, LABEL)]),
        page(6, [(207, 60, 70, "0.00"), (207, 140, 82, "Note")]),
    ]
    _, dec = run(monkeypatch, pages)
    assert dec.state == "available" and dec.value == 0.0 and dec.page == 5 and dec.locator == "page=5-6"


def test_label_on_continuation_page_keeps_section(monkeypatch):
    pages = [
        page(5, [(207, 391, 519, "NOTEOPPLYSNINGER - SELSKAP"), (207, 600, 900, "Regnskapsprinsipper ...")]),
        page(6, [(207, 146, 499, LABEL), (207, 175, 70, "2.00"), (207, 358, 580, "Spesifisering av resultatregnskapet")]),
    ]
    _, dec = run(monkeypatch, pages)
    assert dec.state == "available" and dec.value == 2.0


def test_group_label_without_company_title_abstains(monkeypatch):
    pages = [
        page(5, [(207, 391, 519, "N0TEOPPLYS??"), (207, 544, 499, LABEL), (207, 573, 93, "5.00")]),  # title misread
    ]
    _, dec = run(monkeypatch, pages)
    assert dec.state == "ambiguous" and dec.value is None


def test_second_label_needs_a_fresh_title(monkeypatch):
    # company field blank, then a group field whose title was misread: must not become the company's
    pages = [
        page(5, [(207, 391, 519, "NOTEOPPLYSNINGER - SELSKAP"), (207, 544, 499, LABEL), (207, 580, 400, "Note")]),
        page(6, [(207, 391, 520, "garbled"), (207, 544, 499, LABEL), (207, 573, 120, "1200.00")]),
    ]
    _, dec = run(monkeypatch, pages)
    assert dec.value is None and dec.state in ("not_available", "ambiguous")


def test_no_note_section_is_not_available(monkeypatch):
    pages = [page(2, [(207, 391, 330, "RESULTATREGNSKAP")]), page(3, [(207, 391, 150, "BALANSE")])]
    res, dec = run(monkeypatch, pages)
    assert dec.state == "not_available" and not res.findings


@pytest.mark.parametrize("env,expected", [("0", False), ("off", False), ("1", True), ("", True)])
def test_ocr_switch(monkeypatch, env, expected):
    monkeypatch.setenv("SIGNALPOST_OCR", env)
    assert wo.ocr_enabled() is expected


def _plant(p, x0, y0, kind_or_text):
    tpl = next(t for t in wo._templates() if t[0] == kind_or_text or t[1] == kind_or_text)
    bm = tpl[2]
    p.image[y0:y0 + bm.shape[0], x0:x0 + bm.shape[1]] = np.where(bm, 0, 255).astype(np.uint8)
    return tpl[0]


def test_templates_decode_and_match_without_ocr(monkeypatch):
    p3 = page(3, [(207, 1000, 505, "stray paragraph line of label width"), (207, 573, 93, "7.00")])
    _plant(p3, 207, 391, "title:SELSKAP")
    label = _plant(p3, 207, 544, "Antall årsverk i regnskapsåret")
    assert wo.match_template(p3, wo.Box(207, 544, 207 + 499, 544 + 24)) == (label, "label")
    res, dec = run(monkeypatch, [page(2, [(207, 391, 330, "RESULTATREGNSKAP")]), p3])
    assert dec.state == "available" and dec.value == 7.0 and dec.span == "Antall årsverk i regnskapsåret 7.00"
    assert res.template_hits == 2
    assert res.rec_calls == 2  # only the two value reads; the stray candidate is not OCR-read


def test_group_template_title_blocks_company_attribution(monkeypatch):
    p5 = page(5, [(207, 573, 120, "1200.00")])
    _plant(p5, 207, 391, "title:KONSERN")
    _plant(p5, 207, 544, "label")
    _, dec = run(monkeypatch, [p5])
    assert dec.state == "ambiguous" and dec.value is None
