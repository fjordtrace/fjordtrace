# Source rights, secrets and URL policy

## Sources and rights
| Source | Access | Licence / terms | Use |
|---|---|---|---|
| Brønnøysundregistrene: Enhetsregisteret (bulk + API), roles, subunits, group structure | open API | NLOD 2.0 | identity anchor, roles, workplaces, group |
| Regnskapsregisteret: normalised accounts API, annual-account copies | open API | NLOD 2.0 | filed accounts, workforce statement |
| Company-owned websites | static HTTP GET, robots.txt honoured, per-host rate limit | site's own terms | website, brand, contacts, socials, news, JobPosting |
| NAV arbeidsplassen job feed (pam-stilling-feed) | official API with token | NAV API terms (arbeidsplassen.nav.no/vilkar-api) | active job ads matched by employer org number; inactive ads are never shown |
| Wikidata SPARQL | official endpoint | CC0 | items with P2333 = organisation number |

No LinkedIn, Meta, Glassdoor, Indeed or Google scraping. No unofficial clients. Search engines are not used
as evidence.

## Secrets
None are required. Optional server-side environment variables:
- `SIGNALPOST_NAV_TOKEN`: a registered NAV feed consumer token. If it isn't set, the public token from NAV is used.
Secrets are never written to outputs, logs or evidence.

## Outbound URL policy (`src/signalpost/http.py`)
- Only `http`/`https`, on ports 80 and 443.
- Hosts must resolve only to public (global) IP addresses. Loopback, private, link-local and reserved ranges are
  refused, and the check is repeated on every redirect hop (max 5).
- Responses are capped at 4 MB, and the timeout is 12 s.
- Every request is charged to the run budget and counted per phase in `run-report.json`.
- The User-Agent identifies the agent.
