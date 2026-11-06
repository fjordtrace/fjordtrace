# Refresh

- **State:** `--state-dir` (default `state/`) keeps the last supported claims and their evidence per company
  (`state/profiles/<org>.json`, written atomically). `--previous old/envelopes.jsonl` diffs against an earlier
  output file instead.
- **Stable keys:** every claim has a deterministic key (field plus discriminator, e.g. `role:DAGL:Kari Nordmann`,
  `job_posting:<uuid>`, `annual_accounts:SELSKAP:2025-12-31`). Evidence ids are content hashes of URL, response
  hash, span and locator. Retrieval time is not part of the id.
- **Idempotent:** re-running on the same source snapshot gives the same keys and value hashes, so no duplicates
  and no change events. This is covered by `tests/test_core.py::test_refresh_idempotent_added_removed_and_carry_forward`.
- **Typed changes:** `new_role`, `role_ended`, `new_auditor`, `new_location`, `closed_location`, `new_filing`,
  `new_job`, `closed_job`, `new_activity`, `new_profile`, `new_website`, `new_workforce_figure`, `changed`,
  `added`, `removed`. Each has previous/current values and the evidence ids on both sides. `material` marks
  identity, finance, leadership, location, website and job changes.
- **Failed refresh:** when a family's source is `blocked` or `failed` this run, or was never re-checked, the
  previous value is carried forward with its original evidence and `carried_forward: true`. A removal is
  reported only when the source was checked successfully this run.
- **No reuse needed:** a cold run with an empty state dir is a valid baseline. Builderr said not to assume
  output reuse, so nothing depends on prior state.
