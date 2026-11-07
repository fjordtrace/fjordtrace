# Smoke test

All runs used the one evaluator command (`signalpost --input <batch> --output-dir out --bulk <snapshot>`) on a
home connection in Australia (Windows 11, 12 logical cores), with the default budget (45 min, 18 requests/company).
Companies were drawn at random from the public 411,160-company universe: seed 101 for the 100 (the starter kit's
`select_entry_batch.py`), and fresh seeds for each 1,000-company batch, never reused for tuning.

## Version 2

| | 100 (smoke) | 1,000 (fresh batch, seed 4242) |
|---|---|---|
| Commit | `4fd08af` | `9a529e6` (before the parked-page fix in `4fd08af`; see note) |
| Envelopes / inputs | 100 / 100 | 1000 / 1000 |
| Contract validation | passed | passed |
| Runtime | 3.2 min | 30.8 min |
| Requests (per company) | 911 (9.1) | 9079 (9.1) |
| Third-party cost | $0 | $0 |
| Companies with ≥1 external claim | 99 | 985 |
| Refresh re-run on the same companies | 0 changes, 0 carried forward | |

Companies whose family is `available`:

| Family | Source | 100 | 1,000 |
|---|---|---|---|
| legal_identity | Enhetsregisteret | 100 | 1000 |
| annual_accounts | Regnskapsregisteret | 100 | 1000 |
| roles | Enhetsregisteret roles | 100 | 995 |
| workplaces | registered subunits | 75 | 755 |
| group_relation | konsernstruktur | 9 | 67 |
| workforce | annual-account copy (OCR) | 97 | 964 |
| official_website | company site, identity-gated | 11 | 138 |
| social_profiles | verified site / Wikidata | 6 | 67 |
| news_activity | verified site | 2 | 24 |
| jobs | NAV feed + site JobPosting | 0 | 8 |
| wikidata | Wikidata P2333 | 1 | 14 |
| lei | GLEIF | 7 | 134 |
| public_support | Støtteregisteret | 8 | 97 |
| central_approval | DiBK | 2 | 24 |
| procurement | TED award notices | 1 | 7 |
| food_inspections | Mattilsynet | 1 | 11 |

Note on the 1,000 run: hand-checking its website matches found four parked or placeholder pages
("aireiendom.no is parked") accepted because the bare domain text matched the legal name. Commit `4fd08af`
fixes this (a bare domain is never a name mention; more parked-page phrases), which removes those four.
Every other site verified by the new name rules was hand-checked against its proof snippet.

Every family that is not available carries an explicit state and reason (`not_available`, `blocked`, `ambiguous`,
`failed` or `not_applicable`). See `family_states` in each run report. Per-connector finish times are in
`connectors.*.finished_at_s`; the workforce lane (rate-limited PDF downloads) finishes last.

Files: `companies-100.jsonl` (input), `envelopes-100.jsonl` (output), `run-report-100.json`, `report-100.html`
(open in a browser), `run-report-1000-v2.json`. Version 1 reports: `run-report-1000.json`,
`run-report-1000-unseen.json`.
