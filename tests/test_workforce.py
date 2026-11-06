"""Offline tests for the workforce rules (no network, no OCR models needed)."""
from __future__ import annotations

import pytest

from signalpost.connectors import workforce_parse as wp
from signalpost.connectors.workforce_ocr import Finding


# ---------------------------------------------------------------- structured field
@pytest.mark.parametrize("label", [
    "Antall årsverk i regnskapsåret",
    "Tal på årsverk i rekneskapsåret",
    "Antall arsverk i regnskapsaret",      # OCR dropped the ring
    "Antall årsverk i regnskapsåret:",
])
def test_structured_label_variants(label):
    assert wp.is_structured_label(label)


@pytest.mark.parametrize("text", [
    "Gjennomsnittlig antall årsverk",
    "Antall årsverk i regnskapsåret 32.00",  # label box never contains the number
    "NOTEOPPLYSNINGER - SELSKAP",
    "Note 2 - Antall årsverk",
])
def test_structured_label_rejects_other_lines(text):
    assert not wp.is_structured_label(text)


@pytest.mark.parametrize("raw,expected", [
    ("32.00", 32.0), ("0.00", 0.0), ("0.50", 0.5), ("2,5", 2.5), ("1250.00", 1250.0), (" 3.00 ", 3.0),
    ("O.00", None), ("3Z.00", None), ("", None), (None, None), ("1.000.00", None), ("12 3", None),
])
def test_structured_value_parsing_never_repairs_digits(raw, expected):
    assert wp.parse_structured_value(raw) == expected


@pytest.mark.parametrize("header,kind,section", [
    ("Organisasjonsnr: 815 846 192 | ASTERO KOMPETANSE AS | NOTEOPPLYSNINGER - SELSKAP | - alle poster", "notes", "SELSKAP"),
    ("Organisasjonsnr: 1 | X AS | NOTEOPPLYSNINGER - KONSERN", "notes", "KONSERN"),
    ("Organisasjonsnr: 919 992 360 | RENATA BARROS AS | RESULTATREGNSKAP | Beløp i: NOK", "accounts", None),
    ("Organisasjonsnr: 1 | X AS | BALANSE", "accounts", None),
    ("Brønnøysundregistrene | ÅRSREGNSKAP FOR REGNSKAPSÅRET 2025 - GENERELL INFORMASJON", "cover", None),
    ("1.00 | Note | 1", "other", None),
    # continuation page: running text mentioning the income statement is not a title
    ("Antall årsverk i regnskapsåret | 2.00 | Note | Spesifisering av resultatregnskapet | Lonnkostnader", "other", None),
    ("Organisasjonsnr: 1 | X AS | RESULTATREGNSKAP - KONSERN", "accounts", "KONSERN"),
])
def test_classify_header(header, kind, section):
    assert wp.classify_header(header) == (kind, section)


def _f(section, value, check=None, score=0.99, page=5):
    return Finding(page=page, section=section, label_text="Antall årsverk i regnskapsåret", label_score=0.99,
                   value_text=value, value_score=score, value_check_text=value if check is None else check,
                   value_page=page)


def test_decide_structured_company_value():
    d = wp.decide_structured([_f("SELSKAP", "32.00")])
    assert d.state == "available" and d.value == 32.0 and d.measure == "full_time_equivalents"
    assert d.span == "Antall årsverk i regnskapsåret 32.00" and d.page == 5


def test_decide_structured_explicit_zero_is_a_value():
    d = wp.decide_structured([_f("SELSKAP", "0.00")])
    assert d.state == "available" and d.value == 0.0


def test_decide_structured_blank_field_is_not_zero():
    d = wp.decide_structured([_f("SELSKAP", None)])
    assert d.state == "not_available" and d.value is None


def test_decide_structured_group_only_abstains():
    d = wp.decide_structured([_f("KONSERN", "1200.00")])
    assert d.state == "ambiguous" and "konsern" in d.reason


def test_decide_structured_prefers_company_over_group():
    d = wp.decide_structured([_f("SELSKAP", "12.00"), _f("KONSERN", "450.00", page=9)])
    assert d.state == "available" and d.value == 12.0


def test_decide_structured_unknown_section_abstains():
    assert wp.decide_structured([_f(None, "5.00")]).state == "ambiguous"


def test_decide_structured_double_read_disagreement_abstains():
    d = wp.decide_structured([_f("SELSKAP", "38.00", check="36.00")])
    assert d.state == "ambiguous" and "OCR" in d.reason


def test_decide_structured_unclean_digits_abstain():
    assert wp.decide_structured([_f("SELSKAP", "3B.00")]).state == "ambiguous"


def test_decide_structured_low_confidence_abstains():
    assert wp.decide_structured([_f("SELSKAP", "4.00", score=0.5)]).state == "ambiguous"


def test_decide_structured_conflicting_company_values_abstain():
    d = wp.decide_structured([_f("SELSKAP", "4.00"), _f("SELSKAP", "5.00", page=6)])
    assert d.state == "ambiguous" and "conflicting" in d.reason


def test_decide_structured_nothing_found():
    assert wp.decide_structured([]).state == "not_available"


# ---------------------------------------------------------------- free text (text-layer PDFs)
@pytest.mark.parametrize("text,value,measure", [
    ("Selskapet har i 2025 sysselsatt 3 årsverk.", 3.0, "full_time_equivalents"),
    ("Selskapet har i 2025 sysselsatt 0,5 årsverk.", 0.5, "full_time_equivalents"),
    ("Selskapet har hatt 14 årsverk i løpet av året.", 14.0, "full_time_equivalents"),
    ("Gjennomsnittlig antall årsverk: 12", 12.0, "full_time_equivalents"),
    ("Gjennomsnittlig antall ansatte i regnskapsåret 7", 7.0, "average_employees"),
    ("Antall årsverk sysselsatt i regnskapsåret: 2", 2.0, "full_time_equivalents"),
    ("Antall årsverk er 4.", 4.0, "full_time_equivalents"),
    ("Selskapet har sysselsatt 6 ansatte.", 6.0, "employees"),
    ("The average number of employees was 25.", 25.0, "average_employees"),
    ("The company had 4 employees during 2025.", 4.0, "employees"),
    ("Number of full-time equivalents: 1 250", 1250.0, "full_time_equivalents"),
])
def test_free_text_phrases(text, value, measure):
    d = wp.decide_free_text({7: text})
    assert d.state == "available", d.reason
    assert d.value == value and d.measure == measure and d.page == 7
    assert d.span and d.span in wp.norm(text)


@pytest.mark.parametrize("text", [
    "Selskapet har ikke hatt ansatte i løpet av regnskapsåret.",
    "Selskapet har ingen ansatte.",
    "Det har ikke vært ansatte i selskapet.",
    "The company has no employees.",
])
def test_free_text_explicit_zero(text):
    d = wp.decide_free_text({3: text})
    assert d.state == "available" and d.value == 0.0


@pytest.mark.parametrize("text", [
    "Lønnskostnader 1 045 396",                      # payroll is not a headcount
    "Selskapet har avtale om innskuddsbasert pensjonsordning for alle nåværende ansatte.",
    "Note 2 Lønnskostnader, antall ansatte, godtgjørelser",
    "Daglig leder er ansatt i morselskapet.",
])
def test_free_text_never_infers(text):
    d = wp.decide_free_text({1: text})
    assert d.state == "not_available" and d.value is None


def test_free_text_group_wording_abstains():
    d = wp.decide_free_text({4: "Konsernet har i 2025 sysselsatt 1 200 årsverk."})
    assert d.state in ("ambiguous", "not_available")
    assert d.value is None
    d = wp.decide_free_text({4: "Gjennomsnittlig antall årsverk i konsernet var 450."})
    assert d.value is None


def test_free_text_group_and_company_keeps_company():
    d = wp.decide_free_text({4: "Selskapet har i 2025 sysselsatt 12 årsverk. Konsernet har i 2025 sysselsatt 450 årsverk."})
    assert d.state == "available" and d.value == 12.0


def test_free_text_conflict_abstains():
    d = wp.decide_free_text({4: "Selskapet har i 2025 sysselsatt 3 årsverk.", 9: "Gjennomsnittlig antall årsverk: 5"})
    assert d.state == "ambiguous" and "conflicting" in d.reason


def test_free_text_same_value_twice_is_fine():
    d = wp.decide_free_text({4: "Selskapet har i 2025 sysselsatt 3 årsverk.", 9: "Antall årsverk: 3"})
    assert d.state == "available" and d.value == 3.0


def test_free_text_table_with_two_years_is_not_guessed():
    # "Gjennomsnittlig antall årsverk | 32 | 26" - column order unknown; the trailing digit guard rejects it
    d = wp.decide_free_text({14: "Gjennomsnittlig antall årsverk 32 26"})
    assert d.value is None


# ---------------------------------------------------------------- connector wiring (no network)
def test_connector_not_applicable_without_filing():
    from signalpost.connectors.workforce import WorkforceConnector
    from signalpost.model import Contribution

    class Ctx:
        org = "919992360"
        profile = {"sisteInnsendteAarsregnskap": ""}
        registry = Contribution()

    out = WorkforceConnector().run(Ctx())  # returns before any fetch
    assert out.family_states["workforce"]["state"] == "not_applicable"
    assert not out.claims


def test_stable_pdf_hash_ignores_regenerated_id():
    from signalpost.connectors.workforce import stable_pdf_sha256

    a = b"%PDF-1.6\n...trailer\n<< /Size 9 /ID [<A5A783A75D179228E6D6C89B5F997AD0> <A5A783A75D179228E6D6C89B5F997AD0>] >>"
    b = b"%PDF-1.6\n...trailer\n<< /Size 9 /ID [<637898CAD85E85AA0C0BED06A05485CA> <637898CAD85E85AA0C0BED06A05485CA>] >>"
    assert stable_pdf_sha256(a) == stable_pdf_sha256(b)
    assert stable_pdf_sha256(a) != stable_pdf_sha256(a.replace(b"/Size 9", b"/Size 8"))


@pytest.mark.parametrize("payroll,state", [(3_381_320.0, "ambiguous"), (0.0, "available"), (None, "available")])
def test_stated_zero_contradicted_by_payroll_abstains(monkeypatch, payroll, state):
    from signalpost.budget import Budget
    from signalpost.connectors import workforce as wf
    from signalpost.connectors import workforce_ocr as wo
    from signalpost.http import FetchResult
    from signalpost.model import Contribution

    figures = {} if payroll is None else {"payroll_cost": payroll}

    class Batch:
        budget = Budget(max_requests=10, time_budget_s=1000)

    class Ctx:
        org = "989760939"
        profile = {"sisteInnsendteAarsregnskap": "2025"}
        registry = Contribution(facts={"accounts": [{"account_type": "SELSKAP", "period_start": "2025-01-01",
                                                     "period_end": "2025-12-31", "figures": figures}]})
        budget = Batch.budget

    conn = wf.WorkforceConnector()
    pdf = b"%PDF-1.4 fake"
    monkeypatch.setattr(conn, "_fetch", lambda ctx, url: (FetchResult(url, url, 200, "t", pdf, "application/pdf"), None))
    monkeypatch.setattr(wo, "get_engine", lambda: object())
    monkeypatch.setattr(wo, "extract_structured", lambda *a, **k: wo.ExtractResult(findings=[_f("SELSKAP", "0.00")]))
    out = conn.run(Ctx())
    assert out.family_states["workforce"]["state"] == state
    claim = out.claims[0]
    assert claim["availability"] == state and claim["value"]["value"] == 0
    assert claim["evidence_ids"]  # the quoted statement is kept either way
