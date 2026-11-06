# Smoke test

Both runs used the one evaluator command on a home connection (Australia), Windows 11, Python 3.13, 16 workers, default budget (45 min, 18 requests/company).
Companies were drawn at random from the public 411,160-company universe with the starter kit's `select_entry_batch.py` (seed 101 for 100, seed 1000 for 1,000).

| | 100 companies (smoke) | 1,000 companies (scale) |
|---|---|---|
| Envelopes emitted / inputs | 100 / 100 | 1000 / 1000 |
| Contract validation | passed | passed |
| Runtime | 3.8 min | 30.1 min |
| Requests (per company) | 629 (6.3) | 6406 (6.4) |
| Third-party cost | $0.0 | $0.0 |
| Companies with at least one external claim | 98 | 951 |
| Peak memory (agent process) | | 437 MB |

Companies with the family `available`:

| Family | 100 | 1,000 |
|---|---|---|
| legal_identity | 100 | 1000 |
| annual_accounts | 100 | 1000 |
| roles | 100 | 998 |
| workplaces | 75 | 766 |
| group_relation | 9 | 53 |
| workforce | 97 | 943 |
| official_website | 10 | 98 |
| social_profiles | 6 | 42 |
| news_activity | 1 | 19 |
| jobs | 0 | 4 |
| wikidata | 1 | 12 |

Every non-available family carries an explicit state and reason (`not_available`, `blocked`, `ambiguous`, `failed`, `not_applicable`); see `family_states` in the run reports.
In the 1,000 run, 35 workforce lookups were `blocked` by Brønnøysund's copy-endpoint rate limit; since then the connector honours the server's retry-after and keeps retrying within the time budget.

Files: `companies-100.jsonl` (input), `envelopes-100.jsonl` (output), `run-report-100.json`, `report-100.html` (open in a browser), `run-report-1000.json`.
