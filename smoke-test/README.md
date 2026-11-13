# Smoke test

All runs used the one evaluator command (`signalpost --input <batch> --output-dir out --bulk <snapshot>`) on a
home connection in Australia (Windows 11, 12 logical cores), with the default budget (45 min, 18 requests/company).
Companies were drawn at random from the public 411,160-company universe: seed 101 for the 100 (the starter kit's
`select_entry_batch.py`), and fresh seeds for each 1,000-company batch, never reused for tuning.

## Version 3 (0.3.0)

Version 3 adds the request plan for tight caps (see the main README). Below, the default cap first, then
the same runs with a cap of 2 requests per company (the earlier listing's 2,000 for 1,000). In tight runs the
two Brønnøysund bulk files (192 MB together) were downloaded live; on this connection that took 6–7 minutes
of the runtime.

| | 100 (smoke, default cap) | 100, cap 200 | 1,000 fresh (seed 20261113), cap 2,000 |
|---|---|---|---|
| Commit | `004c52b` | `004c52b` | `004c52b` code before its last two edits (see note) |
| Envelopes / inputs | 100 / 100 | 100 / 100 | 1000 / 1000 |
| Contract validation | passed | passed | passed |
| Runtime | 3.9 min | 10.7 min | 35.8 min |
| Requests (per company) | 933 (9.3) | 200 (2.0) | 2000 (2.0) |
| Third-party cost | $0 | $0 | $0 |
| Companies with ≥1 external claim | 98 | 73 | 859 |
| Refresh re-run on the same companies | 0 changes | | |

Companies whose family is `available`:

| Family | 100 | 100, cap 200 | 1,000, cap 2,000 |
|---|---|---|---|
| legal_identity | 100 | 100 | 1000 |
| annual_accounts | 100 | 100 | 1000 |
| roles | 100 | 100 | 994 |
| workplaces | 75 | 75 | 751 |
| group_relation | 11 | 9 | 72 |
| workforce | 97 | 70 | 815 |
| official_website | 11 | 0 (blocked by cap) | 0 (blocked by cap) |
| lei | 6 | 6 | 133 |
| public_support / central_approval | 8 / 2 | blocked by cap | blocked by cap |

Note on the 1,000 run: it ran before two final edits in `004c52b`, a deadline on the bulk downloads and
the version strings, so its report says `agent_version` 0.2.0. Neither edit changes what a run collects.
For comparison, version 2's code with the same 100-company cap of 200 (and no bulk snapshot) left 45 of the
100 companies `blocked` and none with an external claim, because the registry pass spent the whole cap on the
first companies.

Files: `envelopes-100.jsonl`, `run-report-100.json`, `report-100.html` (default cap),
`run-report-100-tight200.json`, `run-report-1000-tight2000.json`.

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

Files: `companies-100.jsonl` (input), `run-report-1000-v2.json`. Version 2's own 100-company outputs were
replaced by version 3's; they remain in the git history at `7da0ec2`. Version 1 reports: `run-report-1000.json`,
`run-report-1000-unseen.json`.
