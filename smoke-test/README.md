# Smoke test

All runs used the one evaluator command (`signalpost --input <batch> --output-dir out --bulk <snapshot>`) on a home connection in Australia, with Windows 11, 16 workers and the default budget (45 min, 18 requests/company).
Companies were drawn at random from the public 411,160-company universe with the starter kit's `select_entry_batch.py`: seed 101 for the 100, seed 1000 for the first 1,000, and seed 2026 for an unseen 1,000 used only for validation.
The 100-company smoke test was run at commit `6ffeb4a`. The seed-1000 run predates the retry-after fix (35 workforce lookups were rate-limited); the unseen run includes that fix. Both predate the payroll-contradiction rule.

| | 100 (smoke) | 1,000 (seed 1000) | 1,000 unseen (seed 2026) |
|---|---|---|---|
| Envelopes / inputs | 100 / 100 | 1000 / 1000 | 1000 / 1000 |
| Contract validation | passed | passed | passed |
| Runtime | 3.6 min | 30.1 min | 31.5 min |
| Requests (per company) | 626 (6.3) | 6406 (6.4) | 6376 (6.4) |
| Third-party cost | $0 | $0 | $0 |
| Companies with ≥1 external claim | 98 | 951 | 989 |
| Peak memory (agent process) | | 437 MB | |
| Refresh re-run on the same companies | 0 changes, 0 carried forward | | |

Companies whose family is `available`:

| Family | 100 | 1,000 | 1,000 unseen |
|---|---|---|---|
| legal_identity | 100 | 1000 | 1000 |
| annual_accounts | 100 | 1000 | 1000 |
| roles | 100 | 998 | 1000 |
| workplaces | 75 | 766 | 780 |
| group_relation | 9 | 53 | 68 |
| workforce | 97 | 943 | 984 |
| official_website | 10 | 98 | 124 |
| social_profiles | 6 | 42 | 69 |
| news_activity | 1 | 19 | 19 |
| jobs | 0 | 4 | 5 |
| wikidata | 1 | 12 | 16 |

Every family that is not available carries an explicit state and reason (`not_available`, `blocked`, `ambiguous`, `failed` or `not_applicable`). See `family_states` in each run report.

Files: `companies-100.jsonl` (input), `envelopes-100.jsonl` (output), `run-report-100.json`, `report-100.html` (open in a browser), `run-report-1000.json`, `run-report-1000-unseen.json`.
