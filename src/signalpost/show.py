"""Terminal viewer for a run: find, read, compare and verify company profiles.

    python -m signalpost.show --envelopes out/envelopes.jsonl --find "bergen"
    python -m signalpost.show --envelopes out/envelopes.jsonl --org 971228113
    python -m signalpost.show --envelopes out/envelopes.jsonl --compare 971228113 988963119
    python -m signalpost.show --envelopes out/envelopes.jsonl --changes
    add --json for machine-readable output

Profiles print the summary with [n] citations, then every fact by section, then numbered footnotes with
the source URL, retrieval time, extraction method and the exact span each fact was taken from.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any

from . import synthesis as syn
from .model import SECTIONS


def load(path: str | Path) -> list[dict]:
    envs, seen = [], set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            env = json.loads(line)
            if env.get("organisation_number") not in seen:
                seen.add(env.get("organisation_number"))
                envs.append(env)
    return envs


def summary_of(env: dict) -> dict:
    return env.get("summary") or syn.summarise(env)


def _name(env: dict) -> str:
    return env.get("legal_name") or (syn.first(syn.available(env), "legal_name") or {}).get("value") or "?"


def _haystack(env: dict) -> str:
    cl = syn.available(env)
    parts = [env.get("organisation_number"), _name(env), env.get("public_brand")]
    for c in syn.by_field(cl, "registered_address", "workplace", "industry"):
        parts.append(syn.describe_claim(c))
    return " ".join(p for p in parts if p).lower()


def find(envs: list[dict], text: str) -> list[dict]:
    q = text.strip().lower()
    digits = q.replace(" ", "")
    terms = q.split()
    out = []
    for env in envs:
        if digits.isdigit() and digits in (env.get("organisation_number") or ""):
            out.append(env)
        elif terms and all(t in _haystack(env) for t in terms):
            out.append(env)
    return out


class Notes:
    """Footnote numbers for evidence ids, in order of first citation."""

    def __init__(self) -> None:
        self.order: dict[str, int] = {}

    def cite(self, ids: list[str], limit: int = 4) -> str:
        nums = [self.order.setdefault(i, len(self.order) + 1) for i in ids or []]
        if not nums:
            return ""
        shown = "".join(f"[{n}]" for n in nums[:limit])
        return " " + shown + (f"[+{len(nums) - limit}]" if len(nums) > limit else "")


class Out:
    def __init__(self, width: int) -> None:
        self.width = width
        self.lines: list[str] = []

    def p(self, text: str = "", indent: int = 0, hang: int = 0) -> None:
        if not text:
            self.lines.append("")
            return
        self.lines.extend(textwrap.wrap(text, self.width, initial_indent=" " * indent,
                                        subsequent_indent=" " * (indent + hang), break_long_words=True,
                                        break_on_hyphens=False) or [""])

    def h(self, text: str) -> None:
        self.lines += ["", text.upper(), "-" * min(len(text), self.width)]

    def text(self) -> str:
        return "\n".join(self.lines)


def profile_text(env: dict, width: int) -> str:
    o, n, s = Out(width), Notes(), summary_of(env)
    run = env.get("run") or {}
    o.p(f"{_name(env)}  (org {env.get('organisation_number')})")
    o.p(f"state: {env.get('state')} · run {run.get('run_id')} · {run.get('terminal_status')} {run.get('completed_at') or ''}".strip())
    o.h("Summary")
    o.p(s.get("headline") or "")
    for st in s.get("statements") or []:
        o.p(f"- {st['text']}{n.cite(st.get('evidence_ids'))}", 0, 2)
    o.p()
    o.p(s.get("confidence_note") or "")
    o.h("What changed")
    for ch in s.get("changes") or []:
        o.p(f"- {'[material] ' if ch.get('material') else ''}{ch['text']}{n.cite(ch.get('evidence_ids'))}", 0, 2)
    o.h("What is unknown")
    unknowns = s.get("unknowns") or []
    for u in unknowns:
        o.p(f"- ({syn.STATE_WORDS.get(u['state'], u['state'])}) {u['text']}", 0, 2)
    if not unknowns:
        o.p("Every source family returned data.")
    facts = syn.facts_by_section(env)
    av = env.get("availability") or {}
    for sec in SECTIONS:
        state = ((env.get("sections") or {}).get(sec) or {}).get("state") or "?"
        o.h(f"{syn.SECTION_LABEL[sec]} [{syn.STATE_WORDS.get(state, state)}]")
        for fam, info in av.items():
            if syn.FAMILY_SECTION.get(fam) == sec and info.get("state") != "available":
                o.p(f"! {syn.FAMILY_LABEL.get(fam, fam)}: {syn.STATE_WORDS.get(info.get('state'), info.get('state'))}"
                    + (f" ({info['reason']})" if info.get("reason") else ""), 0, 2)
        if sec == "claim_evidence":
            ev = env.get("evidence") or []
            o.p(f"{sum(1 for c in env.get('claims') or [] if c.get('evidence_ids'))} of {len(env.get('claims') or [])} "
                f"claims cite evidence; {len(ev)} evidence records (sources listed below).")
            continue
        if sec == "refresh":
            r = (env.get("sections") or {}).get("refresh") or {}
            o.p("Baseline run (nothing earlier to compare with)." if r.get("baseline") else f"Compared with run {r.get('previous_run_id')}.")
            o.p(f"{r.get('changes', 0)} changes, {r.get('material_changes', 0)} material.")
            carried = [c for c in syn.available(env) if c.get("carried_forward")]
            if carried:
                o.p(f"{len(carried)} values carried forward from the last successful check.")
            continue
        for c in facts.get(sec, []):
            label = syn.FIELD_LABEL.get(c["field"], c["field"])
            flag = "" if c.get("availability") == "available" else f" ({c.get('availability')})"
            if c.get("carried_forward"):
                flag += " (carried forward)"
            o.p(f"{label}: {syn.describe_claim(c)}{flag}{n.cite(c.get('evidence_ids'), 2)}", 2, 4)
    o.h("Sources")
    ev_by_id = {e["id"]: e for e in env.get("evidence") or []}
    for e in env.get("evidence") or []:
        n.order.setdefault(e["id"], len(n.order) + 1)
    for eid, num in sorted(n.order.items(), key=lambda kv: kv[1]):
        e = ev_by_id.get(eid)
        if not e:
            continue
        o.p(f"[{num}] {e.get('source_url')}", 0, 5)
        meta = f"retrieved {e.get('retrieved_at')} · {e.get('source_class')} · {e.get('extraction_method')}"
        if e.get("locator"):
            meta += f" · {e['locator']}"
        o.p(meta, 5, 0)
        o.p(f"span: \"{syn.clip(e.get('claim_span'), 300)}\"", 5, 0)
        o.p(f"sha256 {e.get('content_sha256')}", 5, 0)
    return o.text()


def compare_text(envs: list[dict], width: int) -> str:
    rows = [syn.key_figures(e) for e in envs]
    label_w = max(12, min(22, width // 5))
    col_w = max(10, (width - label_w - 1) // max(1, len(envs)) - 2)
    narrow = col_w < 14

    def cell(r: dict) -> str:
        return r["text"] if r["text"] is not None else f"({syn.STATE_WORDS.get(r['state'], r['state'])})"

    o = Out(width)
    if narrow:  # too tight for columns: one block per figure
        for i, (fid, label) in enumerate(syn.KEY_FIGURES):
            o.p(label.upper())
            for e, r in zip(envs, rows):
                o.p(f"{_name(e)[:24]}: {cell(r[i])}", 2, 4)
        return o.text()

    def line(cols: list[str]) -> list[str]:
        wrapped = [textwrap.wrap(c, w, break_long_words=True) or [""] for c, w in zip(cols, [label_w] + [col_w] * len(envs))]
        out = []
        for k in range(max(len(w) for w in wrapped)):
            parts = [(w[k] if k < len(w) else "").ljust(wd) for w, wd in zip(wrapped, [label_w] + [col_w] * len(envs))]
            out.append(" | ".join(parts).rstrip())
        return out

    o.lines += line([""] + [f"{_name(e)} ({e.get('organisation_number')})" for e in envs])
    o.lines.append("-" * min(width, label_w + (col_w + 3) * len(envs)))
    for i, (fid, label) in enumerate(syn.KEY_FIGURES):
        o.lines += line([label] + [cell(r[i]) for r in rows])
    o.p()
    o.p("Figures are company-level (SELSKAP) from the latest filing; use --org for sources of each value.")
    return o.text()


def changes_list(envs: list[dict], include_all: bool) -> list[dict]:
    out = []
    for env in envs:
        for ch in summary_of(env).get("changes") or []:
            if ch.get("change_type") in ("baseline", "none"):
                continue
            if include_all or ch.get("material"):
                out.append({"organisation_number": env.get("organisation_number"), "legal_name": _name(env), **ch})
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="python -m signalpost.show", description="Find, read, compare and verify company profiles from a run.")
    p.add_argument("--envelopes", default="out/envelopes.jsonl", help="envelopes.jsonl written by a run")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--find", metavar="TEXT", help="list companies matching name, org number, municipality or industry")
    g.add_argument("--org", metavar="ORGNR", help="readable profile with citations and sources")
    g.add_argument("--compare", nargs="+", metavar="ORGNR", help="2-4 organisation numbers side by side")
    g.add_argument("--changes", action="store_true", help="material changes across the run")
    g.add_argument("--list", action="store_true", help="every company in the run")
    p.add_argument("--all", action="store_true", help="with --changes: include non-material changes")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--width", type=int, default=None, help="wrap width (default: terminal width, max 100)")
    a = p.parse_args(argv)
    width = a.width or min(100, max(40, shutil.get_terminal_size((80, 24)).columns - 1))
    envs = load(a.envelopes)
    by_org = {e.get("organisation_number"): e for e in envs}

    def emit(obj: Any, text: str) -> None:
        print(json.dumps(obj, ensure_ascii=False, indent=2) if a.json else text)

    if a.find is not None or a.list:
        hits = envs if a.list else find(envs, a.find)
        rows = []
        for e in hits:
            kf = {r["id"]: r for r in syn.key_figures(e)}
            rows.append({"organisation_number": e.get("organisation_number"), "legal_name": _name(e), "state": e.get("state"),
                         "municipality": kf["municipality"]["text"], "industry": kf["industry"]["text"],
                         "revenue": kf["revenue"]["text"]})
        text = "\n".join(textwrap.shorten(f"{r['organisation_number']}  {r['legal_name']} · {r['municipality'] or '-'} · "
                                          f"{r['industry'] or '-'}" + (f" · {r['revenue']}" if r["revenue"] else "")
                                          + ("" if r["state"] == "available" else f" [{r['state']}]"), width, placeholder="…")
                         for r in rows)
        emit(rows, (text + "\n" if text else "") + f"{len(rows)} of {len(envs)} companies")
        return 0
    if a.org:
        env = by_org.get(a.org.replace(" ", ""))
        if not env:
            print(f"{a.org} is not in {a.envelopes}", file=sys.stderr)
            return 1
        emit({"organisation_number": env.get("organisation_number"), "legal_name": _name(env), "state": env.get("state"),
              "summary": summary_of(env), "key_figures": syn.key_figures(env),
              "facts": [{"key": c["key"], "field": c["field"], "section": c.get("section"), "text": syn.describe_claim(c),
                         "availability": c.get("availability"), "evidence_ids": c.get("evidence_ids", [])} for c in env.get("claims") or []],
              "evidence": env.get("evidence") or []}, profile_text(env, width))
        return 0
    if a.compare:
        orgs = [x.replace(" ", "") for x in a.compare]
        missing = [x for x in orgs if x not in by_org]
        if missing or not 2 <= len(orgs) <= 4:
            print("--compare needs 2-4 organisation numbers from the run" + (f"; not found: {', '.join(missing)}" if missing else ""), file=sys.stderr)
            return 1
        sel = [by_org[x] for x in orgs]
        emit([{"organisation_number": e.get("organisation_number"), "legal_name": _name(e), "key_figures": syn.key_figures(e)} for e in sel],
             compare_text(sel, width))
        return 0
    rows = changes_list(envs, a.all)
    text = "\n".join(textwrap.fill(f"{r['organisation_number']} {r['legal_name']}: {r['text']}", width, subsequent_indent="    ") for r in rows)
    emit(rows, text or "No " + ("" if a.all else "material ") + "changes in this run (a baseline run has nothing to compare with).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
