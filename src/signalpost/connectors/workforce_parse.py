"""Pure text rules for the workforce figure in a filed annual account (no I/O, unit-tested offline).

Two kinds of statement are recognised:

* the structured form field Brønnøysund renders on its "NOTEOPPLYSNINGER - SELSKAP" page,
  ``Antall årsverk i regnskapsåret`` / ``Tal på årsverk i rekneskapsåret`` followed by a number
  such as ``32.00``;
* free-text note sentences, used for PDFs with a text layer: "Selskapet har i 2025 sysselsatt 3 årsverk",
  "Gjennomsnittlig antall årsverk: 12", "Selskapet har ikke hatt ansatte", "The company had 4 employees".

Rules that protect precision: group (konsern) wording is never read as the company's figure, different
numbers for the same year are a conflict (abstain), a zero is accepted only when the text says so
explicitly, and OCR text that is not a clean number is never "repaired" into one.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

A = "[åaäáàâ]"  # OCR renders å as a/ä/á now and then

STRUCT_LABEL_RE = re.compile(
    rf"^\s*(?:antall|tal\s*p{A}|tall\s*p{A})\s*{A}rsverk\s*i\s*re(?:gn|kne|g)skaps?{A}ret\s*:?\s*$", re.I)
STRUCT_VALUE_RE = re.compile(r"^\s*(\d{1,6})(?:[.,](\d{1,2}))?\s*$")

GROUP_RE = re.compile(r"\b(konsern\w*|group|groups|consolidated|morselskap\w*|datterselskap\w*|subsidiar\w*)\b", re.I)
SECTION_RE = re.compile(r"NOTEOPPLYSNINGER\s*[-–:]?\s*(SELSKAP|KONSERN)", re.I)


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("­", "").replace("‐", "-").replace("‑", "-")
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)  # de-hyphenate line breaks
    return re.sub(r"[ \t ]+", " ", text)


_TITLE_NOTES_RE = re.compile(r"^NOTEOPPLYSNINGER(?:\s*[-–:]?\s*(SELSKAP|KONSERN))?$")
_TITLE_ACCOUNTS_RE = re.compile(r"^(?:RESULTATREGNSKAP|BALANSE|KONTANTSTR\S*)(?:\s*[-–:]?\s*(SELSKAP|KONSERN))?$")


def classify_header(header: str) -> tuple[str, str | None]:
    """Page kind from the top text boxes of a page (joined with " | ").

    Brønnøysund's rendered pages carry a title box of their own ("NOTEOPPLYSNINGER - SELSKAP",
    "RESULTATREGNSKAP", "BALANSE"). Titles must fill a whole box, so running text such as
    "Spesifisering av resultatregnskapet" on a continuation page is not mistaken for a title.
    Returns (notes|accounts|cover|brreg|other, SELSKAP|KONSERN|None).
    """
    segments = [norm(s).strip() for s in (header or "").split("|")]
    for seg in segments:
        m = _TITLE_NOTES_RE.match(seg)
        if m:
            return "notes", (m.group(1) or None)
    for seg in segments:
        m = _TITLE_ACCOUNTS_RE.match(seg)
        if m:
            return "accounts", (m.group(1) or None)
    joined = " ".join(segments).upper()
    if "GENERELL INFORMASJON" in joined and "REGNSKAP" in joined:
        return "cover", None
    if any(seg.startswith("Organisasjonsnr") for seg in segments):  # a brreg-rendered page we do not know
        return "brreg", None
    return "other", None


def is_structured_label(text: str) -> bool:
    return bool(STRUCT_LABEL_RE.match(norm(text)))


def parse_structured_value(text: str | None) -> float | None:
    """'32.00' -> 32.0. Anything that is not a clean number (e.g. 'O.00', '3Z') -> None."""
    if not text:
        return None
    m = STRUCT_VALUE_RE.match(norm(text))
    if not m:
        return None
    return float(m.group(1) + ("." + m.group(2) if m.group(2) else ""))


def _to_number(intpart: str, frac: str | None) -> float:
    return float(re.sub(r"[  ]", "", intpart) + ("." + frac if frac else ""))


@dataclass
class Statement:
    value: float
    measure: str          # full_time_equivalents | average_employees | employees
    span: str             # exact quoted text
    start: int
    group: bool = False   # group-level wording
    zero_phrase: bool = False


# Free-text patterns: (regex, measure). The number group is named "n" (+ "f" for decimals).
_N = r"(?P<n>\d{1,3}(?:[  ]\d{3})+|\d{1,6})(?:[.,](?P<f>\d{1,2}))?"
_SUBJ = r"(?:selskapet|foretaket|virksomheten|stiftelsen|foreningen|laget|sameiet|borettslaget|bedriften|vi)"
_YEAR = r"(?:i\s*(?:\d{4}|år|regnskapsåret|rekneskapsåret)\s*)?"
FREE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b{_SUBJ}\s+har\s+{_YEAR}(?:hatt|sysselsatt|hatt\s+sysselsatt)\s+(?:gjennomsnittlig\s+|i\s+gjennomsnitt\s+)?"
                rf"{_N}\s*{A}rsverk", re.I), "full_time_equivalents"),
    (re.compile(rf"\b{_SUBJ}\s+har\s+{_YEAR}(?:hatt|sysselsatt)\s+(?:gjennomsnittlig\s+|i\s+gjennomsnitt\s+)?{_N}\s+"
                rf"(?:ansatte|ansatt|medarbeidere|tilsette)\b", re.I), "employees"),
    (re.compile(rf"\bgjennomsnittlig\s+antall\s+{A}rsverk(?:\s+sysselsatt)?(?:\s+i\s+(?:regnskaps|rekneskaps){A}ret)?"
                rf"(?:\s+\d{{4}})?\s*(?:var|er|utgjorde|:|-|=)?\s*{_N}\b(?!\s*\d)", re.I), "full_time_equivalents"),
    (re.compile(rf"\bgjennomsnittlig\s+antall\s+(?:ansatte|tilsette|sysselsatte)(?:\s+i\s+(?:regnskaps|rekneskaps){A}ret)?"
                rf"(?:\s+\d{{4}})?\s*(?:var|er|utgjorde|:|-|=)?\s*{_N}\b(?!\s*\d)", re.I), "average_employees"),
    (re.compile(rf"\b(?:antall|tal\s+p{A})\s+{A}rsverk(?:\s+sysselsatt)?(?:\s+i\s+(?:regnskaps|rekneskaps){A}ret)?"
                rf"(?:\s+\d{{4}})?\s*(?:var|er|utgjorde|:|-|=)?\s*{_N}\b(?!\s*\d)", re.I), "full_time_equivalents"),
    (re.compile(rf"\b(?:{_N})\s+{A}rsverk\s+(?:i|var|ble)\s", re.I), "full_time_equivalents"),
    (re.compile(rf"\baverage\s+number\s+of\s+(?:employees|staff)(?:\s+during\s+the\s+year)?(?:\s+\d{{4}})?\s*(?:was|is|:|-)?\s*{_N}\b(?!\s*\d)", re.I),
     "average_employees"),
    (re.compile(rf"\b(?:number\s+of\s+)?(?:full[- ]time\s+equivalents|man[- ]years|FTEs?)(?:\s+in\s+\d{{4}})?\s*(?:was|were|:|-)?\s*{_N}\b(?!\s*\d)", re.I),
     "full_time_equivalents"),
    (re.compile(rf"\bthe\s+company\s+(?:had|employed)\s+(?:an\s+average\s+of\s+)?{_N}\s+(?:employees|full[- ]time\s+equivalents|FTEs?)\b", re.I),
     "employees"),
]
ZERO_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b{_SUBJ}\s+har\s+(?:ikke\s+hatt|ingen|ikkje\s+hatt|ikkje)\s+(?:noen\s+|nokon\s+)?(?:ansatte|tilsette|{A}rsverk)\b", re.I), "employees"),
    (re.compile(rf"\b{_SUBJ}\s+har\s+{_YEAR}(?:ikke\s+hatt|ingen)\s+(?:noen\s+)?(?:ansatte|{A}rsverk)\b", re.I), "employees"),
    (re.compile(r"\bdet\s+(?:har\s+ikke\s+vært|er\s+ingen|var\s+ingen)\s+(?:noen\s+)?ansatte\b", re.I), "employees"),
    (re.compile(r"\bingen\s+ansatte\s+i\s+(?:regnskapsåret|\d{4}|selskapet)\b", re.I), "employees"),
    (re.compile(r"\bthe\s+company\s+(?:has|had)\s+no\s+employees\b", re.I), "employees"),
]


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start - 1), text.rfind("\n", 0, start), text.rfind("|", 0, start))
    right_candidates = [i for i in (text.find(".", end), text.find("\n", end), text.find("|", end)) if i != -1]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1:right]


def find_statements(text: str) -> list[Statement]:
    """All workforce statements in free text, each with its exact span."""
    t = norm(text)
    out: list[Statement] = []
    taken: list[tuple[int, int]] = []
    for rx, measure in FREE_PATTERNS:
        for m in rx.finditer(t):
            if any(a <= m.start() < b for a, b in taken):
                continue
            val = _to_number(m.group("n"), m.group("f"))
            ctx = _sentence_around(t, m.start(), m.end())
            span = t[m.start():m.end()].strip()
            out.append(Statement(val, measure, span, m.start(), group=bool(GROUP_RE.search(ctx))))
            taken.append((m.start(), m.end()))
    for rx, measure in ZERO_PATTERNS:
        for m in rx.finditer(t):
            if any(a <= m.start() < b for a, b in taken):
                continue
            ctx = _sentence_around(t, m.start(), m.end())
            out.append(Statement(0.0, measure, t[m.start():m.end()].strip(), m.start(),
                                 group=bool(GROUP_RE.search(ctx)), zero_phrase=True))
            taken.append((m.start(), m.end()))
    return sorted(out, key=lambda s: s.start)


@dataclass
class Decision:
    state: str                 # available | ambiguous | not_available
    reason: str
    value: float | None = None
    measure: str | None = None
    span: str | None = None
    page: int | None = None
    method: str | None = None
    confidence: float = 0.0
    locator: str | None = None


def decide_free_text(pages: dict[int, str]) -> Decision:
    """Pick the company-level figure from free-text statements, or abstain."""
    found: list[tuple[int, Statement]] = []
    for pno in sorted(pages):
        for st in find_statements(pages[pno]):
            found.append((pno, st))
    if not found:
        return Decision("not_available", "no workforce statement found in the filing text")
    company = [(p, s) for p, s in found if not s.group]
    if not company:
        return Decision("ambiguous", "only group-level (konsern) workforce wording found")
    values = {round(s.value, 2) for _, s in company}
    if len(values) > 1:
        return Decision("ambiguous", "conflicting workforce figures in the filing: "
                        + ", ".join(sorted({f'{v:g}' for v in values})))
    # prefer an FTE statement, then average employees, then a plain count
    rank = {"full_time_equivalents": 0, "average_employees": 1, "employees": 2}
    p, s = sorted(company, key=lambda ps: (rank.get(ps[1].measure, 9), ps[0], ps[1].start))[0]
    return Decision("available", "stated in the filing text", s.value, s.measure, s.span, p, "pdf_text_layer", 0.9,
                    f"page={p}")


def decide_structured(findings: list, min_score: float = 0.80) -> Decision:
    """Decide from structured-field findings (workforce_ocr.Finding)."""
    if not findings:
        return Decision("not_available", "workforce field not found on the filing's note pages")
    company = [f for f in findings if f.section == "SELSKAP"]
    group = [f for f in findings if f.section == "KONSERN"]
    unknown = [f for f in findings if f.section not in ("SELSKAP", "KONSERN")]
    if not company:
        if group and not unknown:
            return Decision("ambiguous", "only the group-level (konsern) workforce field was found")
        return Decision("ambiguous", "workforce field found but its section (company or group) could not be confirmed")
    vals = []
    for f in company:
        if not f.value_text:
            continue
        v1, v2 = parse_structured_value(f.value_text), parse_structured_value(f.value_check_text)
        if v1 is None or v2 is None or v1 != v2:
            return Decision("ambiguous", f"workforce value not read cleanly by OCR ({f.value_text!r} / {f.value_check_text!r})")
        if f.value_score < min_score:
            return Decision("ambiguous", f"low OCR confidence on workforce value ({f.value_score:.2f})")
        vals.append((v1, f))
    if not vals:
        return Decision("not_available", "the workforce field is present but left blank in the filing")
    if len({v for v, _ in vals}) > 1:
        return Decision("ambiguous", "conflicting company-level workforce values: " + ", ".join(f"{v:g}" for v, _ in vals))
    v, f = vals[0]
    span = f"{norm(f.label_text).strip()} {norm(f.value_text).strip()}"
    conf = round(min(0.99, 0.5 + 0.5 * min(f.value_score, f.label_score)), 3)
    vp = f.value_page or f.page
    locator = f"page={f.page}" if vp == f.page else f"page={f.page}-{vp}"
    return Decision("available", "structured field on the filing's company note page", v, "full_time_equivalents",
                    span, f.page, f.method, conf, locator)
