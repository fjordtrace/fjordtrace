# Fjordtrace: Signalpost research agent

Give it a batch of Norwegian organisation numbers. It returns **exactly one terminal envelope per input
row**. Each envelope carries the legal identity, filed accounts, leadership, registered workplaces,
workforce stated in the official annual accounts, a verified website and company-owned profiles, active
job ads and dated company news. Every fact has a source, retrieval time, content hash, quoted span and
extraction method. A summary cites every sentence, and a refresh diff shows what changed since the
previous run. A self-contained `report.html` lets a person find, compare and verify any fact.

**No paid APIs, no LLM and no personal credentials are needed. Expected third-party cost per official run: $0.**

## Run it

Python 3.12 or 3.13. From a clean clone, use either route:

```bash
# uv (fetches the pinned Python if needed)
uv sync --frozen
uv run signalpost --input companies.jsonl --output-dir out

# or pip
python3 -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt                       # every version pinned, project included
signalpost --input companies.jsonl --output-dir out   # same as: python -m signalpost ...
```

**The evaluator command** is the last line. Add `--bulk <registry snapshot>` when the frozen Brønnøysund
snapshot is supplied, as CSV, CSV.gz or JSON(L). Without it, identity comes from the live register.

`--input` accepts JSONL (`{"organisation_number": "..."}` per line), a JSON list or `{"organisation_numbers": [...]}`,
CSV with an `organisation_number` column, or plain text with one number per line. Every input row yields one
envelope, in input order. Duplicates repeat the same envelope. Malformed numbers get state `failed` with
the reason. Starter-kit flag names also work: `--organisations`, `--output`, `--report`.

### Outputs (`--output-dir`)
| File | Contents |
|---|---|
| `envelopes.jsonl` | one terminal envelope per input ([DATA_SCHEMA.md](DATA_SCHEMA.md)); written provisionally right after the registry phase, so even a hard kill leaves one row per input |
| `run-report.json` | counts by state and family, claims by field, requests by phase, p50/p95 per company, runtime, third-party cost, contract validation |
| `report.html` | offline viewer: search and filter, company page with cited summary, every fact linked to its source with a verify panel (span, hash, method), side-by-side compare of 2–4 companies, what changed, what is unknown; works on phone and desktop |

A terminal view of the same output:

```bash
uv run python -m signalpost.show --envelopes out/envelopes.jsonl --find "elektro"
uv run python -m signalpost.show --envelopes out/envelopes.jsonl --org 810034882
uv run python -m signalpost.show --envelopes out/envelopes.jsonl --compare 810034882 971228113
uv run python -m signalpost.show --envelopes out/envelopes.jsonl --changes
```

### Refresh
Run the same command again with the same `--state-dir` (default `state/`). Claims keep stable keys,
changes are typed (`new_role`, `closed_job`, `new_filing`, ...) and the same snapshot produces no changes.
If a source fails, the last supported value is kept with its evidence. `--previous old/envelopes.jsonl`
diffs against an earlier output instead. See [REFRESH.md](REFRESH.md).

### Budgets (flag / environment variable)
| Option | Default | Env |
|---|---|---|
| `--time-budget-min` | 45; optional work stops at 85% of it, the rest is reserved for finalising | `SIGNALPOST_TIME_BUDGET_MIN` |
| `--max-requests` | `--requests-per-company` × companies | `SIGNALPOST_MAX_REQUESTS` |
| `--requests-per-company` | 18 | `SIGNALPOST_REQUESTS_PER_COMPANY` |
| `--soft-stop` | 0.85 | `SIGNALPOST_SOFT_STOP` |
| `--workers` | 16 | `SIGNALPOST_WORKERS` |
| `--connectors` | all (`website,nav_jobs,workforce,wikidata,lei,public_support,central_approval,procurement,food_inspections`) | `SIGNALPOST_CONNECTORS` |
| OCR on/off, threads | on, 2 | `SIGNALPOST_OCR`, `SIGNALPOST_OCR_THREADS` |

When time or requests run out, the remaining sources are reported as `blocked` with the reason, never
dropped. Measured on a random 1,000-company batch on a home connection: see `smoke-test/`.

Optional server-side keys (none required): `SIGNALPOST_NAV_TOKEN` (registered NAV job-feed consumer token),
and `BRAVE_SEARCH_API_KEY` (search only nominates candidate domains; a site is attributed only if it shows this
organisation number). See [SECURITY.md](SECURITY.md).

## How it works

```
batch ─► Brønnøysund: entity, roles, subunits, filed accounts (3 years), group links ─► provisional envelopes
      ├─► website lane: candidates (registry site, registry e-mail domain, subunit site, NAV employer homepage,
      │     Wikidata P856, name-derived .no domains, optional search) ─► identity gate ─► static crawl:
      │     brand, description, contacts, locations, socials, dated news, JobPosting
      ├─► workforce lane: official annual-account copy (by org number) ─► "Antall årsverk" field (template + OCR)
      ├─► NAV job feed: ads whose employer org number is the company or one of its subunits, verified live
      ├─► Wikidata: items with P2333 = organisation number
      ├─► GLEIF: LEI records registered to the organisation number (batched, 100 per request)
      ├─► Støtteregisteret: public support awards whose recipient is the organisation number
      ├─► DiBK: central approval for building work, from the public page keyed by organisation number
      ├─► TED: EU/EEA contract award notices whose winner identifier is the organisation number (batched)
      └─► Mattilsynet: food-safety inspections of the company or its registered workplaces (one open file)
      ─► dedupe ─► refresh diff ─► cited summary ─► envelopes.jsonl + run-report.json + report.html
```

**Exact-entity gate.** Site content is published only for `exact` (this org number on the site, not dominated
by another) or `strong` sites:
- declared for this organisation by the register or a subunit, with its registered phone, e-mail or address on
  the site, or with its legal name and nothing pointing elsewhere;
- an unshared registry e-mail domain, NAV or Wikidata, with the legal name on the site;
- a name-derived domain with the full legal name *including its company form* ("Krauman AS"), when no other
  entity in the register has a name containing this name (two companies cannot share a registered name); or
- a name-derived domain with the legal name and a registered role holder (board member, CEO) on the site.

Register-declared but unproven sites publish the URL only. Domains shared by several organisations
(managers, accountants, groups), sites that show another organisation number, and sites whose copyright
names another legal entity are `ambiguous`. Nothing from those sites is attributed. Details are in
[AGENT.md](AGENT.md).

## Tests

```bash
uv run --group dev pytest -q      # offline; no network needed
```

## Repository map
`src/signalpost/`: `cli.py`, `pipeline.py` (orchestration, envelope assembly), `registry.py`, `http.py`
(budgeted, robots-aware, safe-URL fetcher), `model.py` (claims and evidence), `refresh.py`,
`synthesis.py`, `report.py`, `show.py`, `connectors/` (website*, jobs_nav, workforce*, wikidata, lei,
public_support).
`tools/` rebuilds the shipped NAV index and the shared-domain table. Licence: MIT. Registry endpoint
normalisation follows Builderr's MIT starter kit (`NOTICE-starter-kit-LICENSE.txt`).
