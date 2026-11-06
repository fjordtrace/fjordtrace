"""Refresh as a diff over stable claim keys.

State lives in ``<state_dir>/profiles/<org>.json``. Re-running on the same source snapshot produces no
changes (idempotent). When a source fails or is blocked this run, the previous supported value is
carried forward with its original evidence and flagged, never erased.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MATERIAL_FIELDS = {"legal_name", "registration_status", "registered_address", "annual_accounts", "role", "auditor",
                   "official_website", "workplace", "job_posting", "workforce_fte", "group_relation", "share_capital",
                   "industry", "registered_employees"}

ADDED = {"role": "new_role", "auditor": "new_auditor", "workplace": "new_location", "annual_accounts": "new_filing",
         "job_posting": "new_job", "news_item": "new_activity", "social_profile": "new_profile",
         "official_website": "new_website", "workforce_fte": "new_workforce_figure"}
REMOVED = {"role": "role_ended", "auditor": "auditor_ended", "workplace": "closed_location", "job_posting": "closed_job",
           "social_profile": "profile_removed", "official_website": "website_removed"}


def value_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class RefreshStore:
    def __init__(self, state_dir: str | None, previous_envelopes: str | None = None):
        self.dir = Path(state_dir) / "profiles" if state_dir else None
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
        self.previous: dict[str, dict] = {}
        if previous_envelopes and Path(previous_envelopes).exists():
            for line in Path(previous_envelopes).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    env = json.loads(line)
                    self.previous[env["organisation_number"]] = self._from_envelope(env)

    @staticmethod
    def _from_envelope(env: dict) -> dict:
        ev = {e["id"]: e for e in env.get("evidence", [])}
        claims = {}
        for c in env.get("claims", []):
            if c.get("availability") != "available":
                continue
            claims[c["key"]] = {"claim": c, "hash": value_hash(c.get("value")), "first_seen": c.get("first_seen"),
                                "evidence": [ev[i] for i in c.get("evidence_ids", []) if i in ev]}
        return {"run_id": (env.get("run") or {}).get("run_id"), "claims": claims}

    def load(self, org: str) -> dict | None:
        if org in self.previous:
            return self.previous[org]
        if self.dir:
            p = self.dir / f"{org}.json"
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        return None

    def save(self, org: str, env: dict) -> None:
        if not self.dir:
            return
        state = self._from_envelope(env)
        tmp = self.dir / f"{org}.json.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self.dir / f"{org}.json")

    def diff(self, org: str, claims: list[dict], evidence: list[dict], family_states: dict[str, dict],
             field_family: dict[str, str], run_started: str) -> tuple[list[dict], list[dict], list[dict], dict]:
        """Return (claims, evidence, changes, refresh_meta). Mutates claims with first/last_seen."""
        prev = self.load(org)
        changes: list[dict] = []
        if not prev:
            for c in claims:
                if c.get("availability") == "available":
                    c["first_seen"] = run_started
                    c["last_seen"] = run_started
            return claims, evidence, changes, {"previous_run_id": None, "baseline": True}
        prev_claims = prev.get("claims", {})
        current = {c["key"]: c for c in claims if c.get("availability") == "available"}
        for key, c in current.items():
            p = prev_claims.get(key)
            c["last_seen"] = run_started
            if p is None:
                c["first_seen"] = run_started
                changes.append(self._change(ADDED.get(c["field"], "added"), c, None, c))
            else:
                c["first_seen"] = p.get("first_seen") or run_started
                if p["hash"] != value_hash(c.get("value")):
                    changes.append(self._change("changed", c, p["claim"], c))
        ev_ids = {e["id"] for e in evidence}
        for key, p in prev_claims.items():
            if key in current:
                continue
            pc = p["claim"]
            fam = field_family.get(pc["field"], pc["field"])
            fam_state = (family_states.get(fam) or {}).get("state")
            if fam_state in ("blocked", "failed", None):
                # source not re-checked successfully: keep last supported value with its evidence
                carried = dict(pc)
                carried["carried_forward"] = True
                carried["note"] = f"source {fam_state or 'not re-checked'} this run; last supported value kept"
                claims.append(carried)
                for e in p.get("evidence", []):
                    if e["id"] not in ev_ids:
                        evidence.append(e)
                        ev_ids.add(e["id"])
            else:
                changes.append(self._change(REMOVED.get(pc["field"], "removed"), pc, pc, None))
        changes.sort(key=lambda ch: (ch["field"], ch["key"]))
        return claims, evidence, changes, {"previous_run_id": prev.get("run_id"), "baseline": False}

    @staticmethod
    def _change(kind: str, ref: dict, before: dict | None, after: dict | None) -> dict:
        return {
            "change_type": kind,
            "key": ref["key"],
            "field": ref["field"],
            "material": ref["field"] in MATERIAL_FIELDS,
            "previous_value": before.get("value") if before else None,
            "current_value": after.get("value") if after else None,
            "previous_evidence_ids": before.get("evidence_ids", []) if before else [],
            "current_evidence_ids": after.get("evidence_ids", []) if after else [],
        }
