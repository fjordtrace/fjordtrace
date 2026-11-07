"""Batch orchestration: registry pass -> provisional envelopes -> connector lanes -> synthesis -> final."""
from __future__ import annotations

import json
import os
import statistics
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from typing import Any

from . import __version__, registry
from .budget import Budget
from .connectors import BatchContext, CompanyContext, load_connectors
from .http import Fetcher, utcnow
from .model import Contribution, SECTIONS, dedupe, validate_envelope
from .refresh import RefreshStore

FAMILY_OF_FIELD = {
    **{f: "legal_identity" for f in ("legal_name", "legal_form", "registration_status", "registered_address",
                                     "postal_address", "industry", "founded_date", "registered_date",
                                     "statutory_purpose", "activity_description", "share_capital",
                                     "historical_name", "registry_contact", "registered_employees")},
    "annual_accounts": "annual_accounts", "annual_account_filing": "annual_accounts",
    "role": "roles", "auditor": "roles", "workplace": "workplaces", "group_relation": "group_relation",
    **{f: "official_website" for f in ("official_website", "shared_website", "public_brand", "company_description",
                                       "contact_detail", "site_location", "site_person")},
    "social_profile": "social_profiles", "job_posting": "jobs", "news_item": "news_activity",
    "public_mention": "news_activity", "workforce_fte": "workforce", "wikidata_item": "wikidata",
    "lei_record": "lei", "public_support": "public_support", "central_approval": "central_approval",
    "procurement_award": "procurement", "food_inspection": "food_inspections",
}
FAMILY_SECTION = {
    "legal_identity": "legal_identity_and_brand", "annual_accounts": "annual_accounts",
    "workforce": "annual_accounts", "roles": "leadership_and_workplaces", "workplaces": "leadership_and_workplaces",
    "group_relation": "leadership_and_workplaces", "official_website": "website_and_profiles",
    "social_profiles": "website_and_profiles", "wikidata": "website_and_profiles", "jobs": "hiring_and_activity",
    "news_activity": "hiring_and_activity", "lei": "legal_identity_and_brand", "public_support": "annual_accounts",
    "central_approval": "legal_identity_and_brand", "procurement": "hiring_and_activity",
    "food_inspections": "hiring_and_activity",
}
STATE_RANK = {"available": 0, "blocked": 1, "failed": 2, "ambiguous": 3, "not_available": 4, "not_applicable": 5}


class Pipeline:
    def __init__(self, args: Any):
        self.args = args
        self.run_id = args.run_id or time.strftime("run-%Y%m%dT%H%M%SZ", time.gmtime())
        self.started_at = utcnow()
        self.t0 = time.monotonic()
        self.inputs = registry.read_batch(args.input)
        valid = [o for o in dict.fromkeys(self.inputs) if registry.valid_org(o)]
        self.orgs = valid
        n = max(1, len(valid))
        self.budget = Budget(
            max_requests=args.max_requests or int(args.requests_per_company * n),
            time_budget_s=args.time_budget_min * 60,
            soft_stop_fraction=args.soft_stop,
        )
        self.fetcher = Fetcher(self.budget)
        self.out_dir = Path(args.output_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.envelopes_path = Path(args.output) if args.output else self.out_dir / "envelopes.jsonl"
        self.report_path = Path(args.report) if args.report else self.out_dir / "run-report.json"
        self.store = RefreshStore(args.state_dir, args.previous)
        self.registry: dict[str, Contribution] = {}
        self.contrib: dict[str, Contribution] = {}
        self.company_ms: dict[str, float] = {}
        self.connector_stats: dict[str, dict[str, int]] = {}
        self.prepare_s: dict[str, float] = {}
        self.log = (lambda *a: print(*a, flush=True)) if not args.quiet else (lambda *a: None)

    # ------------------------------------------------------------------ phases
    def run(self) -> dict:
        self.log(f"[signalpost] run {self.run_id}: {len(self.inputs)} inputs, {len(self.orgs)} valid, "
                 f"budget {self.budget.max_requests} requests / {self.budget.time_budget_s/60:.0f} min")
        snapshot = registry.BulkSnapshot(self.args.bulk, self.orgs)
        self.snapshot = snapshot
        self.log(f"[signalpost] bulk snapshot: {snapshot.rows_scanned} rows scanned, {len(snapshot.rows)} batch rows "
                 f"({time.monotonic()-self.t0:.0f}s)")
        self._registry_pass(snapshot)
        self._write_envelopes(provisional=True)
        self.log(f"[signalpost] registry pass done ({time.monotonic()-self.t0:.0f}s, {self.budget.used} requests); provisional envelopes written")
        self._connector_pass(snapshot)
        envs = self._write_envelopes(provisional=False)
        self._write_report(envs)
        if not self.args.no_html:
            try:
                from .report import write_report
                write_report(envs, self.out_dir / "report.html", run_meta={"run_id": self.run_id, "started_at": self.started_at})
            except ModuleNotFoundError:
                pass
            except Exception as exc:  # the viewer must never sink a run
                self.log(f"[signalpost] report.html failed: {exc}")
        self.log(f"[signalpost] done in {time.monotonic()-self.t0:.0f}s, {self.budget.used} requests -> {self.envelopes_path}")
        return {"envelopes": len(envs)}

    def _registry_pass(self, snapshot: registry.BulkSnapshot) -> None:
        def one(org: str) -> None:
            t = time.monotonic()
            try:
                self.registry[org] = registry.collect(org, snapshot.rows.get(org), self.fetcher, snapshot.file_sha256,
                                                      live_entity=not self.args.offline_registry)
            except Exception as exc:
                c = Contribution()
                c.state("legal_identity", "failed", f"registry error: {type(exc).__name__}")
                c.error("registry", traceback.format_exc(limit=3))
                c.facts["profile"] = snapshot.rows.get(org) or {}
                self.registry[org] = c
            self.company_ms[org] = (time.monotonic() - t) * 1000

        with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
            list(pool.map(one, self.orgs))

    def _connector_pass(self, snapshot: registry.BulkSnapshot) -> None:
        enabled = set(self.args.connectors.split(",")) if self.args.connectors else None
        connectors = load_connectors(enabled)
        batch = BatchContext(self.run_id, self.orgs, self.fetcher, self.budget, snapshot, self.registry,
                             options=vars(self.args))
        def prepare(c) -> None:
            t = time.monotonic()
            try:
                c.prepare(batch)
            except Exception as exc:
                self.log(f"[signalpost] {c.name}.prepare failed: {exc}")
                batch.shared[f"{c.name}.prepare_error"] = str(exc)
            self.prepare_s[c.name] = round(time.monotonic() - t, 1)

        for c in connectors:
            self.connector_stats[c.name] = {"ok": 0, "error": 0, "skipped": 0}
        self.connector_stats_started_s = round(self.budget.elapsed(), 1)
        # batch-level preparations (index updates, batched lookups, open-data downloads) are independent
        with ThreadPoolExecutor(max_workers=max(1, len(connectors))) as prep_pool:
            list(prep_pool.map(prepare, connectors))
        self.log(f"[signalpost] connector preparation done: {self.prepare_s}")
        for org in self.orgs:
            self.contrib[org] = Contribution()
        active = [o for o in self.orgs if not self.registry[o].facts.get("deleted")]
        lanes: dict[str, list] = {}
        for c in connectors:
            lanes.setdefault(c.lane, []).append(c)
        pools = {lane: ThreadPoolExecutor(max_workers=max(c.lane_workers for c in cs)) for lane, cs in lanes.items()}
        futures = {}

        def task(conn, org):
            t = time.monotonic()
            if self.budget.soft_deadline_passed():
                return conn, org, None, "skipped", 0.0
            try:
                res = conn.run(CompanyContext(org, batch, self.registry[org]))
                return conn, org, res, "ok", (time.monotonic() - t) * 1000
            except Exception:
                c = Contribution()
                c.error(conn.name, traceback.format_exc(limit=4))
                for fam in conn.families:
                    c.state(fam, "failed", f"{conn.name} connector error")
                return conn, org, c, "error", (time.monotonic() - t) * 1000

        for lane, cs in lanes.items():
            for org in active:
                for conn in cs:
                    futures[pools[lane].submit(task, conn, org)] = (conn, org)
        hard_stop = self.budget.time_budget_s - max(30.0, self.budget.time_budget_s * (1 - self.args.soft_stop) / 2)
        pending = set(futures)
        while pending:
            remaining = hard_stop - self.budget.elapsed()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=min(remaining, 5.0), return_when=FIRST_COMPLETED)
            for f in done:
                conn, org, res, status, ms = f.result()
                self.connector_stats[conn.name][status] += 1
                self.connector_stats[conn.name]["finished_at_s"] = round(self.budget.elapsed(), 1)
                if res is not None:
                    self.contrib[org].merge(res)
                self.company_ms[org] = self.company_ms.get(org, 0.0) + ms
        for f in pending:
            f.cancel()
            conn, org = futures[f]
            self.connector_stats[conn.name]["skipped"] += 1
        for pool in pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
        # anything a connector never finished is reported as blocked by the time budget
        for c in connectors:
            for org in active:
                for fam in c.families:
                    if fam not in self.contrib[org].family_states and not any(
                            FAMILY_OF_FIELD.get(cl["field"]) == fam for cl in self.contrib[org].claims):
                        self.contrib[org].state(fam, "blocked", "time or request budget reached before this source was checked")

    # ------------------------------------------------------------------ envelopes
    def build_envelope(self, org: str, provisional: bool) -> dict:
        reg = self.registry.get(org)
        merged = Contribution()
        if reg:
            merged.merge(reg)
        if not provisional and org in self.contrib:
            merged.merge(self.contrib[org])
        merged = dedupe(merged)
        profile = (reg.facts.get("profile") if reg else None) or {}
        # family states: explicit ones, plus 'available' for any family with an available claim
        fam = dict(merged.family_states)
        for c in merged.claims:
            f = FAMILY_OF_FIELD.get(c["field"], c["field"])
            if c.get("availability") == "available":
                fam[f] = {"state": "available", "reason": "supported claims found"}
        if reg and reg.facts.get("deleted"):
            state = "not_available"
        elif "legal_name" not in {c["field"] for c in merged.claims}:
            state = fam.get("legal_identity", {}).get("state", "failed")
            state = state if state != "available" else "failed"
        else:
            state = "available"
        claims, evidence, changes, refresh_meta = merged.claims, merged.evidence, [], {}
        if not provisional:
            claims, evidence, changes, refresh_meta = self.store.diff(
                org, claims, evidence, fam, FAMILY_OF_FIELD, self.started_at)
        sections = {}
        for s in SECTIONS:
            fams = {k: v for k, v in fam.items() if FAMILY_SECTION.get(k) == s}
            n_claims = sum(1 for c in claims if c.get("section") == s and c.get("availability") == "available")
            if s == "claim_evidence":
                st = "available" if evidence else "not_available"
            elif s == "refresh":
                st = "available" if not provisional else "not_available"
            elif n_claims:
                st = "available"
            elif fams:
                st = min((v["state"] for v in fams.values()), key=lambda x: STATE_RANK.get(x, 9))
            else:
                st = "blocked" if provisional else "not_available"
            sections[s] = {"state": st, "claims": n_claims, "families": fams}
        sections["claim_evidence"].update({"evidence_records": len(evidence),
                                           "claims_with_evidence": sum(1 for c in claims if c.get("evidence_ids"))})
        sections["refresh"].update({"changes": len(changes), "material_changes": sum(1 for c in changes if c["material"]),
                                    **refresh_meta})
        env = {
            "organisation_number": org,
            "legal_name": profile.get("navn"),
            "public_brand": next((c["value"] for c in claims if c["field"] == "public_brand"), None),
            "state": state,
            "run": {"run_id": self.run_id, "agent_version": __version__, "started_at": self.started_at,
                    "completed_at": utcnow(), "terminal_status": "provisional" if provisional else "completed",
                    "registry_snapshot_sha256": getattr(self.snapshot, "file_sha256", None)},
            "sections": sections,
            "availability": fam,
            "summary": None,
            "claims": claims,
            "evidence": evidence,
            "changes": changes,
            "errors": merged.errors,
            "operations": {"runtime_ms": int(self.company_ms.get(org, 0)),
                           "third_party_cost_usd": round(float(merged.facts.get("third_party_cost_usd", 0.0)), 6)},
        }
        if not provisional:
            try:
                from .synthesis import summarise
                env["summary"] = summarise(env)
            except ModuleNotFoundError:
                pass
            except Exception as exc:
                env["errors"].append({"connector": "synthesis", "message": f"{type(exc).__name__}: {exc}"[:300]})
        return env

    def _invalid_envelope(self, raw: str, reason: str) -> dict:
        return {
            "organisation_number": raw, "legal_name": None, "state": "failed",
            "run": {"run_id": self.run_id, "agent_version": __version__, "started_at": self.started_at,
                    "completed_at": utcnow(), "terminal_status": "completed"},
            "sections": {s: {"state": "failed", "claims": 0, "families": {}} for s in SECTIONS},
            "availability": {"legal_identity": {"state": "failed", "reason": reason}},
            "summary": None, "claims": [], "evidence": [], "changes": [],
            "errors": [{"connector": "input", "message": reason}],
            "operations": {"runtime_ms": 0, "third_party_cost_usd": 0.0},
        }

    def _write_envelopes(self, provisional: bool) -> list[dict]:
        envs, seen = [], {}
        for raw in self.inputs:
            if raw in seen:
                envs.append(seen[raw])  # duplicate input: same envelope, one row per input
                continue
            if not registry.valid_org(raw):
                env = self._invalid_envelope(raw, "input is not a valid Norwegian organisation number (9 digits, mod-11)")
            else:
                try:
                    env = self.build_envelope(raw, provisional)
                except Exception as exc:
                    env = self._invalid_envelope(raw, f"envelope assembly failed: {type(exc).__name__}: {exc}")
                    env["organisation_number"] = raw
            seen[raw] = env
            envs.append(env)
        tmp = self.envelopes_path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for env in envs:
                fh.write(json.dumps(env, ensure_ascii=False, sort_keys=False) + "\n")
        os.replace(tmp, self.envelopes_path)
        if not provisional:
            for env in envs:
                if env["state"] != "failed" or env["claims"]:
                    self.store.save(env["organisation_number"], env)
        return envs

    def _write_report(self, envs: list[dict]) -> None:
        problems = {e["organisation_number"]: p for e in envs if (p := validate_envelope(e))}
        ms = sorted(self.company_ms.values()) or [0]
        states: dict[str, int] = {}
        fam_states: dict[str, dict[str, int]] = {}
        for e in envs:
            states[e["state"]] = states.get(e["state"], 0) + 1
            for f, v in (e.get("availability") or {}).items():
                fam_states.setdefault(f, {}).setdefault(v["state"], 0)
                fam_states[f][v["state"]] += 1
        claims = [c for e in envs for c in e["claims"] if c.get("availability") == "available"]
        report = {
            "run_id": self.run_id, "agent_version": __version__, "started_at": self.started_at, "completed_at": utcnow(),
            "inputs": len(self.inputs), "unique_valid_inputs": len(self.orgs), "emitted_envelopes": len(envs),
            "envelope_states": states, "family_states": fam_states,
            "claims_available": len(claims),
            "claims_by_field": _count(c["field"] for c in claims),
            "companies_with_external_claim": sum(1 for e in envs if any(c.get("external") and c.get("availability") == "available" for c in e["claims"])),
            "changes": sum(len(e["changes"]) for e in envs),
            "operations": {**self.budget.report(), "http_responses": self.fetcher.count, "bytes": self.fetcher.bytes,
                           "p50_ms": int(statistics.median(ms)), "p95_ms": int(ms[int(0.95 * (len(ms) - 1))]),
                           "third_party_cost_usd": round(sum(e["operations"].get("third_party_cost_usd", 0.0) for e in envs), 4)},
            "connectors": self.connector_stats,
            "connector_pass_started_s": getattr(self, "connector_stats_started_s", None),
            "connector_prepare_s": self.prepare_s,
            "registry_snapshot": {"path": self.args.bulk, "sha256": getattr(self.snapshot, "file_sha256", None),
                                  "rows_scanned": getattr(self.snapshot, "rows_scanned", 0)},
            "validation": {"passed": not problems and len(envs) == len(self.inputs),
                           "exact_count": len(envs) == len(self.inputs), "contract_problems": problems},
        }
        self.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _count(it) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
