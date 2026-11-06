"""Self-contained HTML viewer for a run: find, compare and verify company profiles.

``write_report(envs, path, run_meta)`` writes one offline HTML file (inline CSS and JS, no CDN). The run's
envelopes are embedded as compact JSON: strings that repeat across facts (source URLs, content hashes,
retrieval times, extraction methods) are interned once, evidence is deduplicated by id across companies,
and claim spans are clipped to 300 characters. Large runs are gzip+base64 encoded and inflated in the
browser with ``DecompressionStream``.
"""
from __future__ import annotations

import base64
import gzip
import html
import json
from pathlib import Path
from typing import Any

from . import synthesis as syn
from .model import FIELDS, SECTIONS

SPAN_MAX = 300
GZIP_THRESHOLD = 3_000_000  # bytes of JSON above which the payload is gzip+base64 encoded


class _Strings:
    """Intern table: repeated strings are stored once and referenced by index (-1 = missing)."""

    def __init__(self) -> None:
        self.items: list[str] = []
        self.index: dict[str, int] = {}

    def __call__(self, s: Any) -> int:
        if s is None or s == "":
            return -1
        s = str(s)
        i = self.index.get(s)
        if i is None:
            i = self.index[s] = len(self.items)
            self.items.append(s)
        return i


def _claim_extra(c: dict) -> dict:
    extra: dict[str, Any] = {}
    u = syn.value_url(c)
    if u:
        extra["u"] = u
    if c.get("carried_forward"):
        extra["cf"] = 1
    if c.get("effective_at"):
        extra["e"] = str(c["effective_at"])[:10]
    v = c.get("value")
    if c.get("field") == "annual_accounts" and isinstance(v, dict):
        extra.update({"at": v.get("account_type"), "fy": syn.fiscal_label(v), "pe": v.get("period_end"),
                      "cur": v.get("currency") or "NOK", "fig": v.get("figures") or {}})
        tags = [t for t, on in (("audit opt-out", v.get("audit_opt_out")), ("unaudited", v.get("audited") is False and not v.get("audit_opt_out")),
                                ("liquidation accounts", v.get("liquidation_accounts")), ("small-company rules", v.get("small_company_rules"))) if on]
        if tags:
            extra["tags"] = tags
    if c.get("availability") != "available":
        extra["av"] = c.get("availability")
        if c.get("note"):
            extra["note"] = c["note"]
    return extra


def _company(env: dict, S: _Strings, E: list, ev_index: dict[str, int]) -> dict:
    ev_by_id = {e["id"]: e for e in env.get("evidence") or []}

    def ref(eid: str) -> int | None:
        if eid in ev_index:
            return ev_index[eid]
        e = ev_by_id.get(eid)
        if not e:
            return None
        span = e.get("claim_span") or ""
        row = [S(e.get("source_url")), S(e.get("final_url")) if e.get("final_url") != e.get("source_url") else -1,
               S(e.get("source_class")), S(e.get("retrieved_at")), S(e.get("effective_at")), S(e.get("content_sha256")),
               span if len(span) <= SPAN_MAX else span[: SPAN_MAX - 1] + "…", S(e.get("locator")),
               S(e.get("extraction_method")), e.get("http_status") or 0, S(e.get("access_policy"))]
        ev_index[eid] = len(E)
        E.append(row)
        return ev_index[eid]

    def refs(ids: list[str]) -> list[int]:
        return [i for i in (ref(x) for x in ids or []) if i is not None]

    summary = env.get("summary") or syn.summarise(env)
    claims = sorted(env.get("claims") or [], key=lambda c: (SECTIONS.index(c["section"]) if c.get("section") in SECTIONS else 99,
                                                            syn.field_rank(c.get("field", "")), c.get("key", "")))
    key_pos = {c["key"]: i for i, c in enumerate(claims)}
    cl = []
    for c in claims:
        row: list[Any] = [c.get("field"), syn.describe_claim(c), refs(c.get("evidence_ids"))]
        extra = _claim_extra(c)
        if extra:
            row.append(extra)
        cl.append(row)
    av = syn.available(env)
    addr = syn.first(av, "registered_address")
    ind = sorted(syn.by_field(av, "industry"), key=lambda c: (c.get("value") or {}).get("rank", 9))
    sel = syn.accounts(av, "SELSKAP")
    jobs, _ = syn.active_jobs(av, syn.ref_date(env))
    emp = syn.first(av, "registered_employees")
    refresh = (env.get("sections") or {}).get("refresh") or {}
    fam = env.get("availability") or {}
    rec = {
        "o": env.get("organisation_number"),
        "n": env.get("legal_name") or (syn.first(av, "legal_name") or {}).get("value") or "",
        "s": env.get("state"),
        "m": syn.municipality((addr or {}).get("value")),
        "i": syn.describe_claim(ind[0]).replace(" (primary)", "") if ind else "",
        "c": cl,
        "av": {k: [v.get("state"), v.get("reason") or ""] for k, v in fam.items()},
        "sec": {s: (env.get("sections") or {}).get(s, {}).get("state") for s in SECTIONS},
        "sum": {
            "h": summary.get("headline"),
            "st": [[s["topic"], s["text"], [key_pos[k] for k in s.get("claim_keys", []) if k in key_pos], refs(s.get("evidence_ids"))]
                   for s in summary.get("statements") or []],
            "ch": [[c["text"], 1 if c.get("material") else 0, refs(c.get("evidence_ids")), c.get("change_type") or ""]
                   for c in summary.get("changes") or []],
            "un": [[u["family"], u["state"], u["text"]] for u in summary.get("unknowns") or []],
            "n": summary.get("confidence_note"),
        },
        "kf": [[k["text"], k["state"], refs(k.get("evidence_ids"))] for k in syn.key_figures(env)],
        "rf": {"b": 1 if refresh.get("baseline") else 0, "p": refresh.get("previous_run_id"),
               "ch": len(env.get("changes") or []), "mc": sum(1 for c in env.get("changes") or [] if c.get("material")),
               "fs": min((c.get("first_seen") for c in av if c.get("first_seen")), default=None),
               "cf": sum(1 for c in av if c.get("carried_forward"))},
        "ev": refs([e["id"] for e in env.get("evidence") or []]),
        "t": (env.get("run") or {}).get("completed_at"),
        "ts": (env.get("run") or {}).get("terminal_status"),
        "er": len(env.get("errors") or []),
        # list / filter helpers
        "w": 1 if (fam.get("official_website") or {}).get("state") == "available" and syn.first(av, "official_website") else 0,
        "j": len(jobs),
        "a": sel[-1]["value"].get("period_end", "")[:4] if sel else "",
        "rv": (sel[-1]["value"].get("figures") or {}).get("revenue") if sel else None,
        "em": emp.get("value") if emp else None,
    }
    if env.get("public_brand"):
        rec["b"] = env["public_brand"]
    return {k: v for k, v in rec.items() if v is not None}


def build_data(envs: list[dict], run_meta: dict | None = None) -> dict:
    run_meta = dict(run_meta or {})
    S, E, ev_index = _Strings(), [], {}
    companies, seen = [], set()
    fam_counts: dict[str, dict[str, int]] = {}
    states: dict[str, int] = {}
    n_claims = n_changes = n_material = 0
    for env in envs:
        org = env.get("organisation_number")
        if org in seen:
            continue  # duplicate input rows carry the same envelope
        seen.add(org)
        companies.append(_company(env, S, E, ev_index))
        states[env.get("state")] = states.get(env.get("state"), 0) + 1
        fams = env.get("availability") or {}
        for f in list(syn.EXPECTED_FAMILIES) + [f for f in fams if f not in syn.EXPECTED_FAMILIES]:
            st = (fams.get(f) or {}).get("state") or ("not_checked" if env.get("state") == "available" else env.get("state"))
            fam_counts.setdefault(f, {}).setdefault(st, 0)
            fam_counts[f][st] += 1
        n_claims += sum(1 for c in env.get("claims") or [] if c.get("availability") == "available")
        n_changes += len(env.get("changes") or [])
        n_material += sum(1 for c in env.get("changes") or [] if c.get("material"))
    runs = [e.get("run") or {} for e in envs]
    meta = {
        "run_id": run_meta.get("run_id") or next((r.get("run_id") for r in runs if r.get("run_id")), "run"),
        "started_at": run_meta.get("started_at") or min((r.get("started_at") for r in runs if r.get("started_at")), default=None),
        "completed_at": max((r.get("completed_at") for r in runs if r.get("completed_at")), default=None),
        "agent_version": next((r.get("agent_version") for r in runs if r.get("agent_version")), None),
        "inputs": len(envs), "companies": len(companies), "states": states, "families": fam_counts,
        "claims": n_claims, "evidence": len(E), "changes": n_changes, "material_changes": n_material,
    }
    return {
        "meta": meta,
        "labels": {
            "fields": {f: [FIELDS[f][0], syn.FIELD_LABEL.get(f, f)] for f in FIELDS},
            "sections": [[s, syn.SECTION_LABEL.get(s, s)] for s in SECTIONS],
            "families": {f: [syn.FAMILY_LABEL[f], syn.FAMILY_SECTION.get(f)] for f in syn.FAMILY_LABEL},
            "topics": syn.TOPIC_LABEL,
            "kf": [list(k) for k in syn.KEY_FIGURES],
            "figures": [[k, syn.FIGURE_LABEL[k]] for k in syn.FIGURE_ORDER],
        },
        "S": S.items,
        "E": E,
        "companies": companies,
    }


def render_html(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > GZIP_THRESHOLD:
        payload = base64.b64encode(gzip.compress(raw.encode("utf-8"), 9, mtime=0)).decode("ascii")
        data_type = "application/gzip-base64"
    else:
        payload = raw.replace("<", "\\u003c")  # keeps "</script>" and "<!--" out of the inline block
        data_type = "application/json"
    title = f"Signalpost profiles · {data['meta'].get('run_id')}"
    return (TEMPLATE.replace("__TITLE__", html.escape(title))
            .replace("__DATA_TYPE__", data_type)
            .replace("__CSS__", CSS)
            .replace("__JS__", JS)
            .replace("__DATA__", payload))


def write_report(envs: list[dict], path: str | Path, run_meta: dict | None = None) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(render_html(build_data(envs, run_meta)), encoding="utf-8")
    tmp.replace(out)


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m signalpost.report", description="Write the offline HTML viewer for a run.")
    p.add_argument("--envelopes", default="out/envelopes.jsonl")
    p.add_argument("--out", default=None, help="default: report.html next to the envelopes file")
    a = p.parse_args(argv)
    envs = [json.loads(line) for line in Path(a.envelopes).read_text(encoding="utf-8").splitlines() if line.strip()]
    out = Path(a.out) if a.out else Path(a.envelopes).with_name("report.html")
    write_report(envs, out)
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KiB, {len(envs)} envelopes)")
    return 0


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>__TITLE__</title>
<style>__CSS__</style>
</head>
<body>
<a class="skip" href="#q">Skip to search</a>
<header class="top">
  <a class="brand" href="#">Signalpost <span>company profiles</span></a>
  <span id="runinfo" class="runinfo"></span>
</header>
<div class="layout">
  <aside id="listpane" class="listpane" aria-label="Find companies">
    <div class="search">
      <label for="q" class="vh">Search companies</label>
      <input id="q" type="search" autocomplete="off" spellcheck="false" placeholder="Name, org no., municipality, industry  ( / )">
      <div class="filters">
        <label>State <select id="fstate"><option value="">any</option></select></label>
        <label>Sort <select id="fsort"><option value="name">name</option><option value="rv">revenue</option><option value="em">employees</option><option value="j">job ads</option></select></label>
        <label class="ck"><input type="checkbox" id="fweb"> website</label>
        <label class="ck"><input type="checkbox" id="fjobs"> job ads</label>
        <label class="ck"><input type="checkbox" id="facc"> accounts</label>
        <label class="ck"><input type="checkbox" id="fchg"> changes</label>
      </div>
      <p id="count" class="count" aria-live="polite"></p>
    </div>
    <ul id="results" class="results"></ul>
    <button id="more" class="btn ghost" hidden>Show more</button>
  </aside>
  <main id="view" class="view" tabindex="-1"></main>
</div>
<div id="cmpbar" class="cmpbar" hidden>
  <span id="cmplist" class="cmplist"></span>
  <a id="cmpgo" class="btn" href="#">Compare</a>
  <button id="cmpclear" class="btn ghost" type="button">Clear</button>
</div>
<dialog id="dlg" aria-labelledby="dlg-title">
  <form method="dialog" class="dlg-close"><button class="btn ghost" aria-label="Close">Close ✕</button></form>
  <div id="dlg-body"></div>
</dialog>
<noscript><p class="card">This report needs JavaScript to search and display the embedded run data.</p></noscript>
<script id="data" type="__DATA_TYPE__">__DATA__</script>
<script>__JS__</script>
</body>
</html>
"""

CSS = r"""
:root{--bg:#f6f6f3;--fg:#1c1c1e;--muted:#5b5b62;--card:#fff;--line:#deded8;--accent:#0a58a8;--accent-bg:#e5effa;
--ok:#17663a;--ok-bg:#e3f4e8;--warn:#8a5a00;--warn-bg:#fdf1d8;--bad:#b3261e;--bad-bg:#fbe5e3;--na:#4d5560;--na-bg:#eceef1;
--chip:#eef1f6;--mark:#fff3b0;--radius:10px;--shadow:0 1px 2px rgba(0,0,0,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--fg:#e9e9ec;--muted:#a3a3ad;--card:#1c1c20;--line:#33333a;--accent:#7ab4ff;
--accent-bg:#1b2a3d;--ok:#7ad69a;--ok-bg:#16301f;--warn:#f0c46b;--warn-bg:#3a2d10;--bad:#ff8f86;--bad-bg:#3d1916;--na:#b4bcc6;
--na-bg:#2a2d33;--chip:#262a33;--mark:#5a4b00;--shadow:none}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;overflow-wrap:anywhere}
a{color:var(--accent)}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.skip{position:absolute;left:-999px;top:0;background:var(--card);padding:8px;z-index:9}.skip:focus{left:8px}
.top{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:baseline;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--card)}
.brand{font-weight:700;font-size:17px;text-decoration:none;color:var(--fg)}.brand span{font-weight:400;color:var(--muted)}
.runinfo{color:var(--muted);font-size:13px}
.layout{display:block}
.listpane{padding:12px 16px}
.view{padding:12px 16px 96px;min-width:0}
.view:focus{outline:none}
body.has-view .listpane{display:none}
@media (min-width:960px){
 .layout{display:grid;grid-template-columns:minmax(300px,380px) 1fr;align-items:start}
 .listpane{position:sticky;top:0;max-height:100vh;overflow:auto;border-right:1px solid var(--line)}
 body.has-view .listpane{display:block}
 .view{padding:16px 24px 96px;max-width:1100px}
 .back{display:none}
}
input[type=search]{width:100%;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:var(--radius);background:var(--card);color:var(--fg)}
select{font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);max-width:100%}
.filters{display:flex;flex-wrap:wrap;gap:6px 12px;margin:8px 0;font-size:14px;color:var(--muted)}
.filters label{display:inline-flex;gap:4px;align-items:center}
.count{margin:4px 0 8px;color:var(--muted);font-size:13px}
.results{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.item{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);display:flex;align-items:stretch}
.item.cur{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.item-main{flex:1;min-width:0;padding:10px 12px;text-decoration:none;color:inherit;display:block}
.item-name{font-weight:600;display:block}
.item-meta{display:block;color:var(--muted);font-size:13px}
.item-tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.cmp-toggle{border:0;border-left:1px solid var(--line);background:none;color:var(--accent);font:inherit;font-size:13px;padding:0 10px;cursor:pointer;border-radius:0 var(--radius) var(--radius) 0;min-width:76px}
.cmp-toggle[aria-pressed=true]{background:var(--accent-bg);font-weight:600}
.btn{display:inline-block;font:inherit;font-size:14px;padding:7px 12px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:#fff;text-decoration:none;cursor:pointer}
@media (prefers-color-scheme:dark){.btn{color:#0b1320}}
.btn.ghost{background:transparent;color:var(--accent)}
.btn:disabled{opacity:.5;cursor:default}
.badge{display:inline-block;font-size:12px;line-height:1.4;padding:1px 7px;border-radius:999px;white-space:nowrap;background:var(--na-bg);color:var(--na);font-weight:600}
.st-available{background:var(--ok-bg);color:var(--ok)}
.st-blocked,.st-ambiguous,.st-not_checked{background:var(--warn-bg);color:var(--warn)}
.st-failed,.badge.bad{background:var(--bad-bg);color:var(--bad)}
.badge.info{background:var(--accent-bg);color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;margin:0 0 14px;box-shadow:var(--shadow)}
.card>h3,.card>summary h3{margin:0 0 8px;font-size:17px;display:inline}
.card>summary{cursor:pointer;list-style-position:outside}
.card>summary .badge{margin-left:8px;vertical-align:2px}
h2{margin:4px 0 2px;font-size:22px;line-height:1.25}
.sub{color:var(--muted);margin:0 0 10px;font-size:14px}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 14px}
.headline{font-weight:600;margin:0 0 8px}
.stmts{list-style:none;padding:0;margin:0}
.stmts li{padding:6px 0;border-top:1px solid var(--line)}
.stmts li:first-child{border-top:0}
.topic{display:inline-block;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em;margin-right:6px}
.chip{display:inline-block;font:600 12px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;padding:0 6px;margin:0 2px;border-radius:6px;border:1px solid var(--line);background:var(--chip);color:var(--accent);cursor:pointer;vertical-align:1px}
.chip:hover{border-color:var(--accent)}
.note{color:var(--muted);font-size:13px;margin:10px 0 0}
.glance{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin:0}
.glance div{border:1px solid var(--line);border-radius:8px;padding:6px 10px}
.glance dt{font-size:12px;color:var(--muted)}.glance dd{margin:0;font-weight:600}
.plain{list-style:none;padding:0;margin:0}.plain li{padding:4px 0}
.fams{list-style:none;padding:0;margin:4px 0 10px;font-size:14px}.fams li{padding:2px 0}
.fact{border-top:1px solid var(--line);padding:8px 0}
.fact.hl{background:var(--mark);border-radius:6px}
.f-label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.f-val{margin:1px 0}
.f-meta{font-size:13px;color:var(--muted);display:flex;flex-wrap:wrap;gap:2px 10px;align-items:center}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;vertical-align:top;padding:6px 8px;border-bottom:1px solid var(--line)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.cmp th[scope=row],table.cmp .rowh{position:sticky;left:0;background:var(--card);min-width:110px;z-index:1}
table.cmp td,table.cmp thead th{min-width:150px}
@media (max-width:600px){
 .card.cmpcard{padding:6px}
 table.cmp{font-size:13px}table.cmp th,table.cmp td{padding:6px 5px}
 table.cmp th[scope=row],table.cmp .rowh{min-width:78px;max-width:96px}
 table.cmp td,table.cmp thead th{min-width:104px}
}
.bar{height:8px;background:var(--na-bg);border-radius:4px;overflow:hidden;min-width:60px}.bar i{display:block;height:100%;background:var(--ok)}
.cmpbar{position:fixed;left:0;right:0;bottom:0;display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 16px;background:var(--card);border-top:1px solid var(--line);box-shadow:0 -2px 8px rgba(0,0,0,.08);z-index:5}
.cmplist{flex:1 1 200px;font-size:13px;color:var(--muted);min-width:0}
dialog{border:1px solid var(--line);border-radius:12px;padding:16px;width:min(680px,calc(100vw - 24px));max-height:calc(100vh - 32px);background:var(--card);color:var(--fg)}
dialog::backdrop{background:rgba(0,0,0,.45)}
.dlg-close{float:right;margin:0 0 8px 8px}
dl.kv{display:grid;grid-template-columns:minmax(90px,max-content) 1fr;gap:4px 12px;margin:8px 0;font-size:14px}
dl.kv dt{color:var(--muted)}dl.kv dd{margin:0}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12.5px}
blockquote.span{margin:8px 0;padding:8px 12px;border-left:4px solid var(--accent);background:var(--chip);border-radius:0 8px 8px 0;white-space:pre-wrap}
.back{margin:0 0 8px}
.neg{color:var(--bad)}
.empty{color:var(--muted)}
@media print{
 .top .runinfo,.listpane,.cmpbar,.actions,.back,.cmp-toggle,dialog,.skip{display:none!important}
 body{background:#fff;color:#000;font-size:12px}.layout{display:block}.view{padding:0}
 .card{box-shadow:none;break-inside:avoid-page}.chip{border:0;background:none;color:#000}
 a[href^="http"]::after{content:" <" attr(href) ">";font-size:10px;color:#555}
}
"""

JS = r"""
(function(){'use strict';
var D,S,E,L,byOrg=new Map(),cmp=[],limit=60,cur=null;
var $=function(s,r){return (r||document).querySelector(s)};
var esc=function(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})};
var safe=function(u){return /^https?:\/\//i.test(u||'')?u:null};
var SL={available:'Available',not_available:'Not available',blocked:'Blocked',not_applicable:'Not applicable',ambiguous:'Ambiguous',failed:'Failed',not_checked:'Not checked'};
function badge(st,txt){return '<span class="badge st-'+esc(st)+'">'+esc(txt||SL[st]||st)+'</span>'}
function str(i){return i>=0?S[i]:null}
function ev(i){var r=E[i];return {url:str(r[0]),fin:str(r[1]),cls:str(r[2]),at:str(r[3]),eff:str(r[4]),sha:str(r[5]),span:r[6],loc:str(r[7]),method:str(r[8]),http:r[9]||null,pol:str(r[10])}}
function host(u){try{return new URL(u).hostname.replace(/^www\./,'')}catch(e){return u||''}}
function fmtOrg(o){return /^\d{9}$/.test(o)?o.slice(0,3)+' '+o.slice(3,6)+' '+o.slice(6):o}
function fmtFull(v,cur){if(v==null)return '<span class="empty">–</span>';return esc((cur||'NOK')+' '+Number(v).toLocaleString('en-US',{maximumFractionDigits:2}))}
function fmtShort(v){if(v==null)return '';var a=Math.abs(v),s=v<0?'-':'';if(a>=1e9)return 'NOK '+s+(a/1e9).toFixed(2).replace(/\.?0+$/,'')+'bn';if(a>=1e6)return 'NOK '+s+(a/1e6).toFixed(1).replace(/\.0$/,'')+'M';if(a>=1e4)return 'NOK '+s+Math.round(a/1e3)+'k';return 'NOK '+s+Math.round(a).toLocaleString('en-US')}
function day(t){return t?String(t).slice(0,10):''}
function flabel(f){var x=L.fields[f];return x?x[1]:f}
function fsection(f){var x=L.fields[f];return x?x[0]:'claim_evidence'}

/* ---------- loading ---------- */
function load(){var el=$('#data');
 if(el.type==='application/json')return Promise.resolve(JSON.parse(el.textContent));
 if(typeof DecompressionStream==='undefined')return Promise.reject(new Error('This browser cannot inflate the embedded data (needs DecompressionStream). Use a current Chrome, Edge, Firefox or Safari.'));
 var bin=Uint8Array.from(atob(el.textContent.trim()),function(c){return c.charCodeAt(0)});
 return new Response(new Blob([bin]).stream().pipeThrough(new DecompressionStream('gzip'))).text().then(JSON.parse)}

/* ---------- list ---------- */
function prep(){D.companies.forEach(function(c){byOrg.set(c.o,c);
 var munis=c.c.filter(function(r){return r[0]==='workplace'||r[0]==='registered_address'}).map(function(r){return r[1]}).join(' ');
 c._h=[c.n,c.b,c.o,c.m,c.i,munis].join(' ').toLowerCase()})}
function filtered(){var q=$('#q').value.trim().toLowerCase(),st=$('#fstate').value,sort=$('#fsort').value;
 var terms=q?q.split(/\s+/):[],digits=q.replace(/\s+/g,'');
 var out=D.companies.filter(function(c){
  if(st&&c.s!==st)return false;
  if($('#fweb').checked&&!c.w)return false;
  if($('#fjobs').checked&&!c.j)return false;
  if($('#facc').checked&&!c.a)return false;
  if($('#fchg').checked&&!(c.rf&&c.rf.mc))return false;
  if(!terms.length)return true;
  if(/^\d+$/.test(digits)&&c.o.indexOf(digits)>=0)return true;
  return terms.every(function(t){return c._h.indexOf(t)>=0})});
 if(sort==='name')out.sort(function(a,b){return a.n.localeCompare(b.n,'nb')});
 else out.sort(function(a,b){return (b[sort]==null?-Infinity:b[sort])-(a[sort]==null?-Infinity:a[sort])});
 return out}
function renderList(){var res=filtered();
 $('#count').textContent=res.length+' of '+D.companies.length+' companies';
 $('#results').innerHTML=res.slice(0,limit).map(function(c){
  var tags=[];if(c.s!=='available')tags.push(badge(c.s));
  if(c.a)tags.push('<span class="badge info">accounts '+esc(c.a)+'</span>');
  if(c.rv!=null)tags.push('<span class="badge">'+esc(fmtShort(c.rv))+'</span>');
  if(c.w)tags.push('<span class="badge info">website</span>');
  if(c.j)tags.push('<span class="badge info">'+c.j+' job ad'+(c.j>1?'s':'')+'</span>');
  if(c.rf&&c.rf.mc)tags.push('<span class="badge st-blocked">'+c.rf.mc+' change'+(c.rf.mc>1?'s':'')+'</span>');
  var on=cmp.indexOf(c.o)>=0;
  return '<li class="item'+(cur===c.o?' cur':'')+'"><a class="item-main" href="#org='+esc(c.o)+'"><span class="item-name">'+esc(c.n||c.o)+'</span><span class="item-meta">'+esc([fmtOrg(c.o),c.m,c.i].filter(Boolean).join(' · '))+'</span><span class="item-tags">'+tags.join('')+'</span></a>'+
   '<button type="button" class="cmp-toggle" data-cmp="'+esc(c.o)+'" aria-pressed="'+on+'" aria-label="'+(on?'Remove ':'Add ')+esc(c.n)+(on?' from':' to')+' comparison">'+(on?'✓ Compare':'+ Compare')+'</button></li>'}).join('');
 $('#more').hidden=res.length<=limit}

/* ---------- compare selection ---------- */
function toggleCmp(o){var i=cmp.indexOf(o);if(i>=0)cmp.splice(i,1);else{if(cmp.length>=4){alert('You can compare up to 4 companies. Remove one first.');return}cmp.push(o)}syncCmp();renderList()}
function syncCmp(){var bar=$('#cmpbar');bar.hidden=!cmp.length;
 $('#cmplist').textContent=cmp.length+' selected: '+cmp.map(function(o){return (byOrg.get(o)||{}).n||o}).join(', ')+(cmp.length<2?' (pick at least 2)':'');
 var go=$('#cmpgo');go.href='#compare='+cmp.join(',');go.setAttribute('aria-disabled',cmp.length<2?'true':'false');go.style.pointerEvents=cmp.length<2?'none':'';go.style.opacity=cmp.length<2?.5:1;
 document.querySelectorAll('[data-cmp]').forEach(function(b){var on=cmp.indexOf(b.getAttribute('data-cmp'))>=0;b.setAttribute('aria-pressed',on);if(b.classList.contains('btn'))b.textContent=on?'✓ In comparison':'+ Add to compare'})}

/* ---------- citations ---------- */
var notes=null;
function numberer(){var m=new Map();return {n:function(i){if(!m.has(i))m.set(i,m.size+1);return m.get(i)},all:function(){return Array.from(m.keys())}}}
function chips(evs,o,max){if(!evs||!evs.length)return '';max=max||3;
 var h=evs.slice(0,max).map(function(i){return '<button type="button" class="chip" data-ev="'+i+'" data-o="'+esc(o)+'" aria-label="Verify source '+notes.n(i)+'">'+notes.n(i)+'</button>'}).join('');
 evs.slice(max).forEach(function(i){notes.n(i)});
 if(evs.length>max)h+='<button type="button" class="chip" data-evs="'+evs.join(',')+'" data-o="'+esc(o)+'" aria-label="Show all '+evs.length+' sources">+'+(evs.length-max)+'</button>';
 return '<span class="cites">'+h+'</span>'}

function openDlg(h){$('#dlg-body').innerHTML=h;var d=$('#dlg');if(d.showModal){if(!d.open)d.showModal()}else d.setAttribute('open','');var t=$('#dlg-title');if(t)t.focus()}
function verifyHtml(i,o){var e=ev(i),c=byOrg.get(o),n=notes?notes.n(i):'';
 var sup=c?c.c.filter(function(r){return r[2].indexOf(i)>=0}).map(function(r){return '<li><b>'+esc(flabel(r[0]))+':</b> '+esc(r[1])+'</li>'}).join(''):'';
 var u=safe(e.url),pub='';
 if(/(^|\.)brreg\.no$/.test(host(e.url))&&c)pub=' · <a href="https://virksomhet.brreg.no/nb/oppslag/enheter/'+esc(c.o)+'" target="_blank" rel="noopener">human-readable Brønnøysund page</a>';
 return '<h3 id="dlg-title" tabindex="-1">Source ['+n+']</h3>'+
 '<p>'+(u?'<a href="'+esc(u)+'" target="_blank" rel="noopener noreferrer">Open original source ↗</a>':esc(e.url))+pub+'</p>'+
 '<p class="f-label">Exact text the claim was extracted from</p><blockquote class="span">'+esc(e.span||'(no span recorded)')+'</blockquote>'+
 '<dl class="kv"><dt>Source URL</dt><dd class="mono">'+esc(e.url)+'</dd>'+(e.fin?'<dt>Final URL</dt><dd class="mono">'+esc(e.fin)+'</dd>':'')+
 '<dt>Source class</dt><dd>'+esc(e.cls)+'</dd><dt>Retrieved</dt><dd>'+esc(e.at)+'</dd>'+(e.eff?'<dt>Effective</dt><dd>'+esc(e.eff)+'</dd>':'')+
 (e.http?'<dt>HTTP status</dt><dd>'+e.http+'</dd>':'')+'<dt>Extraction</dt><dd class="mono">'+esc(e.method)+'</dd>'+(e.loc?'<dt>Locator</dt><dd class="mono">'+esc(e.loc)+'</dd>':'')+
 '<dt>Content SHA-256</dt><dd class="mono">'+esc(e.sha)+'</dd>'+(e.pol?'<dt>Licence</dt><dd>'+esc(e.pol)+'</dd>':'')+'</dl>'+
 (sup?'<p class="f-label">Facts supported by this source</p><ul>'+sup+'</ul>':'')+
 '<p class="note">To verify: open the source and find the quoted text. The SHA-256 identifies the exact response that was retrieved; the locator points to the record inside it.</p>'}
function listHtml(evs,o){return '<h3 id="dlg-title" tabindex="-1">'+evs.length+' sources</h3><ul class="plain">'+evs.map(function(i){var e=ev(i);
 return '<li><button type="button" class="chip" data-ev="'+i+'" data-o="'+esc(o)+'">'+notes.n(i)+'</button> '+esc(host(e.url))+' · '+esc(e.cls)+' · '+esc(day(e.at))+'<br><span class="mono">'+esc((e.span||'').slice(0,140))+'</span></li>'}).join('')+'</ul>'}

/* ---------- views ---------- */
function setView(h,hasView){var v=$('#view');v.innerHTML=h;document.body.classList.toggle('has-view',!!hasView);renderList();if(hasView){window.scrollTo(0,0);var t=v.querySelector('h2');if(t){t.setAttribute('tabindex','-1');t.focus({preventScroll:true})}}}
function back(){return '<p class="back"><a href="#" class="btn ghost">← All companies</a></p>'}

function home(){cur=null;var m=D.meta;
 var fams=Object.keys(m.families).map(function(f){var c=m.families[f],tot=0,ok=c.available||0;Object.keys(c).forEach(function(k){tot+=c[k]});
  var other=Object.keys(c).filter(function(k){return k!=='available'}).map(function(k){return badge(k,(SL[k]||k)+' '+c[k])}).join(' ');
  var lab=L.families[f]?L.families[f][0]:f;
  return '<tr><th scope="row">'+esc(lab)+'</th><td class="num">'+ok+' / '+tot+'</td><td><div class="bar" role="img" aria-label="'+Math.round(100*ok/Math.max(tot,1))+'% available"><i style="width:'+(100*ok/Math.max(tot,1))+'%"></i></div></td><td>'+other+'</td></tr>'}).join('');
 var states=Object.keys(m.states).map(function(k){return badge(k,(SL[k]||k)+' '+m.states[k])}).join(' ');
 setView('<section class="card"><h2>Run '+esc(m.run_id)+'</h2><p class="sub">'+esc(m.companies)+' companies'+(m.inputs!==m.companies?' ('+m.inputs+' input rows)':'')+' · started '+esc(m.started_at||'?')+' · completed '+esc(m.completed_at||'?')+(m.agent_version?' · agent '+esc(m.agent_version):'')+'</p>'+
  '<p>'+states+'</p><dl class="glance"><div><dt>Sourced claims</dt><dd>'+m.claims.toLocaleString('en-US')+'</dd></div><div><dt>Evidence records</dt><dd>'+m.evidence.toLocaleString('en-US')+'</dd></div><div><dt>Changes</dt><dd>'+m.changes+' ('+m.material_changes+' material)</dd></div></dl></section>'+
  '<section class="card"><h3>Coverage by source family</h3><div class="scroll"><table><thead><tr><th scope="col">Family</th><th scope="col" class="num">Available</th><th scope="col">Share</th><th scope="col">Other states</th></tr></thead><tbody>'+fams+'</tbody></table></div></section>'+
  '<section class="card"><h3>How to use</h3><ul><li><b>Find:</b> type a name, organisation number, municipality or industry in the search box (press <kbd>/</kbd> to jump there). Filters narrow by state, website, job ads, accounts and changes.</li>'+
  '<li><b>Read:</b> open a company for a sourced summary, what changed, what is unknown, then every fact by section.</li>'+
  '<li><b>Verify:</b> every number in brackets is a source. Click it to see the original URL, retrieval time, the exact text the fact came from and the content hash.</li>'+
  '<li><b>Compare:</b> tick “+ Compare” on 2–4 companies, then press Compare. The page address can be shared (e.g. <code>#compare=…</code>, <code>#org=…</code>).</li></ul></section>',false)}

function factHtml(r,o,idx){var x=r[3]||{},evs=r[2],e=evs.length?ev(evs[0]):null,u=x.u&&safe(x.u);
 var val=esc(r[1])+(u?' <a href="'+esc(u)+'" target="_blank" rel="noopener noreferrer" aria-label="Open '+esc(host(u))+'">↗</a>':'');
 if(x.cf)val+=' <span class="badge st-blocked" title="'+esc(x.note||'')+'">carried forward</span>';
 if(x.av)val+=' '+badge(x.av);
 var meta=e?('<a href="'+esc(safe(e.url)||'#')+'" target="_blank" rel="noopener noreferrer">'+esc(host(e.url))+'</a><span>'+esc(e.cls)+'</span><span>retrieved '+esc(day(e.at))+'</span>'+(x.e?'<span>effective '+esc(x.e)+'</span>':'')+
  '<button type="button" class="chip" data-ev="'+evs[0]+'" data-o="'+esc(o)+'">verify '+notes.n(evs[0])+'</button>'+chips(evs.slice(1),o)):'<span>no evidence</span>';
 return '<div class="fact" id="f-'+idx+'"><div class="f-label">'+esc(flabel(r[0]))+'</div><div class="f-val">'+val+'</div><div class="f-meta">'+meta+'</div></div>'}

function accountsHtml(c){var rows=c.c.map(function(r,i){return [r,i]}).filter(function(p){return p[0][0]==='annual_accounts'&&p[0][3]&&p[0][3].fig});
 if(!rows.length)return '';
 var types=[];rows.forEach(function(p){var t=p[0][3].at||'?';if(types.indexOf(t)<0)types.push(t)});
 types.sort(function(a,b){return a==='SELSKAP'?-1:b==='SELSKAP'?1:0});
 return types.map(function(t){var cols=rows.filter(function(p){return (p[0][3].at||'?')===t}).sort(function(a,b){return (b[0][3].pe||'').localeCompare(a[0][3].pe||'')}).slice(0,6);
  var head=cols.map(function(p){var x=p[0][3];return '<th scope="col" class="num">'+esc(x.fy)+'<br>'+(p[0][2].length?'<button type="button" class="chip" data-ev="'+p[0][2][0]+'" data-o="'+esc(c.o)+'">verify '+notes.n(p[0][2][0])+'</button>':'')+(x.tags?'<br><small>'+esc(x.tags.join(', '))+'</small>':'')+'</th>'}).join('');
  var body=L.figures.filter(function(f){return cols.some(function(p){return p[0][3].fig[f[0]]!=null})}).map(function(f){
   return '<tr><th scope="row">'+esc(f[1])+'</th>'+cols.map(function(p){var v=p[0][3].fig[f[0]];return '<td class="num'+(f[0]==='equity'&&v<0?' neg':'')+'">'+fmtFull(v,p[0][3].cur)+'</td>'}).join('')+'</tr>'}).join('');
  return '<p class="f-label">'+(t==='SELSKAP'?'Company accounts (SELSKAP)':t==='KONSERN'?'Group accounts (KONSERN), whole group, not comparable with company figures':esc(t))+'</p><div class="scroll"><table><thead><tr><th scope="col">Figure</th>'+head+'</tr></thead><tbody>'+body+'</tbody></table></div>'}).join('')+'<p class="note">“–” means the figure was not reported in the filing. Missing values are never treated as zero.</p>'}

function detail(o){var c=byOrg.get(o);if(!c){setView(back()+'<section class="card"><h2>Not in this run</h2><p>No company with organisation number '+esc(o)+' is in this report.</p></section>',true);return}
 cur=o;notes=numberer();var s=c.sum||{};
 var stm=(s.st||[]).map(function(t){return '<li><span class="topic">'+esc(L.topics[t[0]]||t[0])+'</span>'+esc(t[1])+' '+chips(t[3],o)+'</li>'}).join('');
 var glance=c.kf.map(function(k,i){return k[0]==null?'':'<div><dt>'+esc(L.kf[i][1])+'</dt><dd>'+esc(k[0])+' '+chips(k[2],o,1)+'</dd></div>'}).join('');
 var changes=(s.ch||[]).map(function(x){return '<li>'+(x[1]?'<span class="badge st-blocked">material</span> ':'')+esc(x[0])+' '+chips(x[2],o)+'</li>'}).join('');
 var unk=(s.un||[]).map(function(u){return '<li>'+badge(u[1])+' '+esc(u[2])+'</li>'}).join('');
 var inCmp=cmp.indexOf(o)>=0;
 var h=back()+'<h2>'+esc(c.n||c.o)+'</h2><p class="sub">Org. no. '+esc(fmtOrg(c.o))+(c.b?' · brand “'+esc(c.b)+'”':'')+(c.m?' · '+esc(c.m):'')+' · '+badge(c.s)+(c.ts==='provisional'?' '+badge('blocked','provisional'):'')+'</p>'+
 '<div class="actions"><button type="button" class="btn" data-cmp="'+esc(o)+'" aria-pressed="'+inCmp+'">'+(inCmp?'✓ In comparison':'+ Add to compare')+'</button>'+
 '<button type="button" class="btn ghost" id="copylink">Copy link</button><a class="btn ghost" href="https://virksomhet.brreg.no/nb/oppslag/enheter/'+esc(o)+'" target="_blank" rel="noopener">Brønnøysund ↗</a></div>'+
 '<section class="card" aria-labelledby="h-sum"><h3 id="h-sum">Summary</h3><p class="headline">'+esc(s.h||'')+'</p>'+(stm?'<ul class="stmts">'+stm+'</ul>':'<p class="empty">No supported statements for this company.</p>')+'<p class="note">'+esc(s.n||'')+'</p></section>'+
 (glance?'<section class="card"><h3>At a glance</h3><dl class="glance">'+glance+'</dl></section>':'')+
 '<section class="card"><h3>What changed</h3><ul class="plain">'+(changes||'<li class="empty">No change information.</li>')+'</ul></section>'+
 '<section class="card"><h3>What is unknown</h3>'+(unk?'<ul class="plain">'+unk+'</ul>':'<p class="empty">Every source family returned data.</p>')+'</section>';
 L.sections.forEach(function(sec){var id=sec[0];
  var fams=Object.keys(c.av).filter(function(f){return (L.families[f]?L.families[f][1]:null)===id}).map(function(f){var a=c.av[f];return '<li>'+badge(a[0])+' <b>'+esc(L.families[f][0])+'</b>'+(a[0]!=='available'&&a[1]?' — '+esc(a[1]):'')+'</li>'}).join('');
  var body='';
  if(id==='annual_accounts')body+=accountsHtml(c);
  if(id==='claim_evidence'){var nc=c.c.length,cited=c.c.filter(function(r){return r[2].length}).length;
   c.ev.forEach(function(i){notes.n(i)});
   body+='<p>'+cited+' of '+nc+' claims cite evidence; '+c.ev.length+' evidence records'+(c.er?'; '+c.er+' source error'+(c.er>1?'s':'')+' recorded':'')+'.</p><details><summary>All sources ('+c.ev.length+')</summary><ol class="plain">'+
    notes.all().filter(function(i){return c.ev.indexOf(i)>=0}).map(function(i){var e=ev(i);return '<li><button type="button" class="chip" data-ev="'+i+'" data-o="'+esc(o)+'">'+notes.n(i)+'</button> <a href="'+esc(safe(e.url)||'#')+'" target="_blank" rel="noopener noreferrer">'+esc(host(e.url))+'</a> · '+esc(e.cls)+' · '+esc(day(e.at))+' · <span class="mono">'+esc(e.method)+'</span></li>'}).join('')+'</ol></details>'}
  if(id==='refresh'){var r=c.rf||{};body+='<dl class="kv"><dt>Run status</dt><dd>'+esc(c.ts||'?')+(c.t?', completed '+esc(c.t):'')+'</dd><dt>Compared with</dt><dd>'+(r.b?'nothing (baseline run)':esc(r.p||'?'))+'</dd><dt>Changes</dt><dd>'+(r.ch||0)+' ('+(r.mc||0)+' material)</dd>'+(r.fs?'<dt>First seen</dt><dd>'+esc(r.fs)+'</dd>':'')+(r.cf?'<dt>Carried forward</dt><dd>'+r.cf+' values kept from the last successful check</dd>':'')+'</dl>'}
  var facts=c.c.map(function(r,i){return [r,i]}).filter(function(p){return fsection(p[0][0])===id&&!(p[0][0]==='annual_accounts'&&p[0][3]&&p[0][3].fig)}).map(function(p){return factHtml(p[0],o,p[1])}).join('');
  var st=c.sec[id]||'not_available';
  h+='<details class="card" open id="sec-'+id+'"><summary><h3>'+esc(sec[1])+'</h3>'+badge(st)+'</summary>'+(fams?'<ul class="fams">'+fams+'</ul>':'')+body+(facts||(body?'':'<p class="empty">No facts in this section.</p>'))+'</details>'});
 setView(h,true)}

function compare(list){var cs=list.map(function(o){return byOrg.get(o)}).filter(Boolean).slice(0,4);
 cmp=cs.map(function(c){return c.o});syncCmp();cur=null;notes=numberer();
 if(cs.length<2){setView(back()+'<section class="card"><h2>Compare</h2><p>Select at least two companies with “+ Compare” in the list.</p></section>',true);return}
 var head=cs.map(function(c){return '<th scope="col"><a href="#org='+esc(c.o)+'">'+esc(c.n)+'</a><br><small>'+esc(fmtOrg(c.o))+'</small> '+badge(c.s)+'<br><button type="button" class="btn ghost" data-rm="'+esc(c.o)+'" aria-label="Remove '+esc(c.n)+' from comparison">Remove</button></th>'}).join('');
 var rows=L.kf.map(function(k,i){return '<tr><th scope="row">'+esc(k[1])+'</th>'+cs.map(function(c){var v=c.kf[i];return '<td>'+(v[0]==null?badge(v[1]):esc(v[0])+' '+chips(v[2],c.o,2))+'</td>'}).join('')+'</tr>'}).join('');
 var sums=cs.map(function(c){return '<section class="card"><h3><a href="#org='+esc(c.o)+'">'+esc(c.n)+'</a></h3><p>'+esc((c.sum||{}).h||'')+'</p></section>'}).join('');
 setView(back()+'<h2>Compare '+cs.length+' companies</h2><p class="sub">Company-level (SELSKAP) figures from each company\'s latest filing. Numbers in brackets open the source.</p>'+
  '<div class="actions"><button type="button" class="btn ghost" id="copylink">Copy link</button></div>'+
  '<section class="card cmpcard"><div class="scroll"><table class="cmp"><thead><tr><th scope="col" class="rowh">Company</th>'+head+'</tr></thead><tbody>'+rows+'</tbody></table></div></section>'+sums,true)}

function route(){var p=new URLSearchParams(location.hash.replace(/^#/,''));
 if(p.get('org'))detail(p.get('org').replace(/\s/g,''));
 else if(p.get('compare'))compare(p.get('compare').split(',').map(function(s){return s.trim()}));
 else home()}

/* ---------- events ---------- */
function init(){S=D.S;E=D.E;L=D.labels;prep();
 $('#runinfo').textContent='run '+D.meta.run_id+' · '+D.meta.companies+' companies';
 var states=Object.keys(D.meta.states);states.forEach(function(s){var o=document.createElement('option');o.value=s;o.textContent=(SL[s]||s)+' ('+D.meta.states[s]+')';$('#fstate').appendChild(o)});
 ['#q','#fstate','#fsort','#fweb','#fjobs','#facc','#fchg'].forEach(function(s){$(s).addEventListener('input',function(){limit=60;renderList()})});
 $('#more').addEventListener('click',function(){limit+=100;renderList()});
 $('#q').addEventListener('keydown',function(e){if(e.key==='Enter'){var r=filtered();if(r.length===1||(r.length&&/^\d{9}$/.test(this.value.replace(/\s/g,''))))location.hash='org='+r[0].o}});
 document.addEventListener('keydown',function(e){if(e.key==='/'&&!/^(INPUT|SELECT|TEXTAREA)$/.test((document.activeElement||{}).tagName||'')&&!$('#dlg').open){e.preventDefault();if(document.body.classList.contains('has-view')&&innerWidth<960)location.hash='';$('#q').focus()}});
 document.addEventListener('click',function(e){var t=e.target.closest('button,a');if(!t)return;
  if(t.hasAttribute('data-cmp')){e.preventDefault();toggleCmp(t.getAttribute('data-cmp'));return}
  if(t.hasAttribute('data-rm')){var l=cmp.filter(function(o){return o!==t.getAttribute('data-rm')});location.hash=l.length?'compare='+l.join(','):'';if(l.length<2){cmp=l;syncCmp()}return}
  if(t.hasAttribute('data-ev')){openDlg(verifyHtml(+t.getAttribute('data-ev'),t.getAttribute('data-o')));return}
  if(t.hasAttribute('data-evs')){openDlg(listHtml(t.getAttribute('data-evs').split(',').map(Number),t.getAttribute('data-o')));return}
  if(t.id==='copylink'){var done=function(){t.textContent='Link copied'};if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(location.href).then(done,function(){prompt('Copy this link',location.href)});else prompt('Copy this link',location.href);return}});
 $('#cmpclear').addEventListener('click',function(){cmp=[];syncCmp();renderList()});
 $('#dlg').addEventListener('click',function(e){if(e.target===this)this.close()});
 window.addEventListener('hashchange',route);
 window.addEventListener('beforeprint',function(){document.querySelectorAll('details').forEach(function(d){d.open=true})});
 route()}
load().then(function(d){D=d;init()}).catch(function(err){$('#view').innerHTML='<section class="card"><h2>Could not load data</h2><p>'+esc(err.message)+'</p></section>'})
})();
"""

if __name__ == "__main__":
    raise SystemExit(main())
