# Envelope schema

`envelopes.jsonl` holds exactly one JSON object per input row, in input order. Duplicate inputs repeat
the same envelope, and malformed numbers get a `failed` envelope with the reason. The envelope is a
superset of Builderr's `OUTPUT_CONTRACT.md`.

```jsonc
{
  "organisation_number": "810034882",
  "legal_name": "SANDNES ELEKTRISKE AS",
  "public_brand": null,                       // from the verified site, when proven
  "state": "available",                       // available | not_available | blocked | not_applicable | ambiguous | failed
  "run": {"run_id": "...", "agent_version": "0.1.0", "started_at": "...", "completed_at": "...",
          "terminal_status": "completed", "registry_snapshot_sha256": "..."},
  "sections": {                               // the seven required sections, each with a state
    "legal_identity_and_brand": {"state": "available", "claims": 12, "families": {...}},
    "annual_accounts": {...}, "leadership_and_workplaces": {...}, "website_and_profiles": {...},
    "hiring_and_activity": {...},
    "claim_evidence": {"state": "available", "evidence_records": 30, "claims_with_evidence": 30},
    "refresh": {"state": "available", "changes": 0, "material_changes": 0, "previous_run_id": "...", "baseline": false}
  },
  "availability": {                           // every field family checked, with an explicit state and reason
    "annual_accounts": {"state": "available", "reason": "supported claims found"},
    "jobs": {"state": "not_available", "reason": "no active NAV ads for this organisation or its subunits"}
  },
  "summary": {"headline": "...", "statements": [{"topic": "...", "text": "...", "claim_keys": [], "evidence_ids": []}],
              "changes": [], "unknowns": [], "generated_by": "deterministic_templates_v1"},
  "claims": [{
    "key": "annual_accounts:SELSKAP:2025-12-31",   // stable; refresh diffs on it
    "field": "annual_accounts", "section": "annual_accounts", "external": false, "signal_type": "financials",
    "value": {"account_type": "SELSKAP", "period_start": "2025-01-01", "period_end": "2025-12-31",
              "currency": "NOK", "figures": {"revenue": 8681606.0}},
    "availability": "available", "confidence": 1.0, "evidence_ids": ["ev-..."],
    "reporting_period": {"start": "2025-01-01", "end": "2025-12-31"},
    "first_seen": "...", "last_seen": "..."
  }],
  "evidence": [{
    "id": "ev-...", "source_url": "https://data.brreg.no/regnskapsregisteret/regnskap/810034882",
    "final_url": "...", "source_class": "official_annual_accounts", "retrieved_at": "...",
    "effective_at": "2025-12-31", "content_sha256": "...", "claim_span": "regnskapstype=SELSKAP; ...",
    "locator": "id=...;journalnr=...", "extraction_method": "brreg_regnskap_api_json",
    "http_status": 200, "access_policy": "NLOD-2.0"
  }],
  "changes": [{"change_type": "new_role", "key": "...", "field": "role", "material": true,
               "previous_value": null, "current_value": {...}, "previous_evidence_ids": [], "current_evidence_ids": []}],
  "errors": [{"connector": "...", "message": "...", "url": "..."}],
  "operations": {"runtime_ms": 1530, "third_party_cost_usd": 0.0}
}
```

## Rules
- An `available` claim always references at least one evidence record in the same envelope.
- A missing value is never zero: absent figures are left out of `figures`, and absent families carry a state
  with a reason.
- A claim carried forward after a failed refresh has `carried_forward: true`, keeps its original evidence,
  and has a note saying why.
- Birth dates from the roles register are discarded.
- Field names are defined in `src/signalpost/model.py` (`FIELDS`), and the family/section mapping is in
  `src/signalpost/pipeline.py`.
