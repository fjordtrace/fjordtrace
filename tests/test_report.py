"""Offline tests for the HTML viewer and the show CLI."""
from __future__ import annotations

import base64
import copy
import gzip
import json
import re

from signalpost import report, show
from test_synthesis import envelope, full_env, put


def payload(html: str) -> tuple[str, str]:
    m = re.search(r'<script id="data" type="([^"]+)">(.*?)</script>', html, re.S)
    assert m, "data block missing"
    return m.group(1), m.group(2)


def decode(html: str) -> dict:
    kind, body = payload(html)
    if kind == "application/json":
        return json.loads(body)
    return json.loads(gzip.decompress(base64.b64decode(body)).decode("utf-8"))


def many(n: int) -> list[dict]:
    base = full_env()
    out = []
    for i in range(n):
        env = copy.deepcopy(base)
        env["organisation_number"] = f"{900000000 + i}"
        out.append(env)
    return out


def test_report_is_self_contained_and_embeds_compact_data(tmp_path):
    envs = [full_env(), envelope([], availability={"legal_identity": {"state": "failed", "reason": "bad input"}}, state="failed")]
    envs[1]["organisation_number"] = "123"
    envs.append(envs[0])  # duplicate input row
    out = tmp_path / "report.html"
    report.write_report(envs, out, run_meta={"run_id": "t1", "started_at": "2026-11-06T10:00:00Z"})
    html = out.read_text(encoding="utf-8")
    assert "<script src" not in html and "<link" not in html  # no external assets
    data = decode(html)
    assert data["meta"]["run_id"] == "t1" and data["meta"]["companies"] == 2 and data["meta"]["inputs"] == 3
    a, b = data["companies"]
    assert a["n"] == "FJORDLYS TEKNIKK AS" and a["w"] == 1 and a["j"] == 1 and a["a"] == "2025"
    assert a["sum"]["st"] and all(st[2] and st[3] for st in a["sum"]["st"])  # statements carry claim + evidence refs
    assert all(0 <= i < len(data["E"]) for row in a["c"] for i in row[2])
    assert b["s"] == "failed" and b["sum"]["un"][0][1] == "failed"
    assert set(data["meta"]["families"]["jobs"]) == {"available", "failed"}
    assert all(len(e[6]) <= report.SPAN_MAX for e in data["E"])


def test_untrusted_text_cannot_break_out_of_the_data_block(tmp_path):
    env = full_env()
    env["legal_name"] = "</script><script>alert(1)</script><!--"
    out = tmp_path / "r.html"
    report.write_report([env], out)
    html = out.read_text(encoding="utf-8")
    kind, body = payload(html)
    assert "<" not in body
    assert decode(html)["companies"][0]["n"] == env["legal_name"]


def test_long_spans_are_clipped(tmp_path):
    env = full_env()
    env["evidence"][0]["claim_span"] = "x" * 590
    data = report.build_data([env])
    assert max(len(e[6]) for e in data["E"]) == report.SPAN_MAX


def test_large_runs_switch_to_gzip_and_stay_small(tmp_path, monkeypatch):
    out = tmp_path / "big.html"
    report.write_report(many(1000), out)
    html = out.read_text(encoding="utf-8")
    kind, _ = payload(html)
    assert kind == "application/gzip-base64"
    assert out.stat().st_size < 4_000_000
    data = decode(html)
    assert len(data["companies"]) == 1000
    monkeypatch.setattr(report, "GZIP_THRESHOLD", 10**9)
    assert payload(report.render_html(report.build_data(many(3))))[0] == "application/json"


def test_envelopes_without_summary_are_summarised(tmp_path):
    env = full_env()
    assert env["summary"] is None
    data = report.build_data([env])
    assert data["companies"][0]["sum"]["h"].startswith("FJORDLYS TEKNIKK AS")


# ------------------------------------------------------------------ show CLI
def write_jsonl(tmp_path, envs) -> str:
    p = tmp_path / "envelopes.jsonl"
    p.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in envs) + "\n", encoding="utf-8")
    return str(p)


def test_show_profile_find_compare_changes(tmp_path, capsys):
    other = envelope([put("legal_name", "BØLGE MARIN AS"),
                      put("registered_address", {"adresse": "Havna 2", "postnummer": "6002", "poststed": "ÅLESUND", "kommune": "ÅLESUND"})],
                     changes=[{"change_type": "new_role", "key": "role:DAGL:Y", "field": "role", "material": True,
                               "previous_value": None, "current_value": {"name": "Y Z", "role": "Daglig leder"}}],
                     baseline=False, previous="t0")
    other["organisation_number"] = "987654321"
    path = write_jsonl(tmp_path, [full_env(), other])

    assert show.main(["--envelopes", path, "--org", "923609016", "--width", "60"]) == 0
    out = capsys.readouterr().out
    assert "SUMMARY" in out and "WHAT CHANGED" in out and "WHAT IS UNKNOWN" in out and "SOURCES" in out
    assert "[1]" in out and "span:" in out and "retrieved 2026-11-06T10:00:00Z" in out
    assert max(len(line) for line in out.splitlines()) <= 60

    assert show.main(["--envelopes", path, "--find", "ålesund"]) == 0
    out = capsys.readouterr().out
    assert "987654321" in out and "1 of 2 companies" in out

    assert show.main(["--envelopes", path, "--compare", "923609016", "987654321", "--width", "100"]) == 0
    out = capsys.readouterr().out
    assert "Revenue" in out and "NOK 12.5M" in out and "(not available)" in out

    assert show.main(["--envelopes", path, "--compare", "923609016", "987654321", "--width", "40"]) == 0
    assert "REVENUE" in capsys.readouterr().out  # narrow terminal: one block per figure

    assert show.main(["--envelopes", path, "--changes", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [rows[0]] and rows[0]["organisation_number"] == "987654321" and "Y Z" in rows[0]["text"]

    assert show.main(["--envelopes", path, "--org", "923609016", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["statements"] and data["key_figures"] and data["evidence"]

    assert show.main(["--envelopes", path, "--org", "111111111"]) == 1
