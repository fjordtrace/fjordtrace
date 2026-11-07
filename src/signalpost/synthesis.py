"""Deterministic, evidence-cited profile summary (no LLM, no network, $0).

``summarise(env)`` turns one envelope into plain English sentences. Every statement lists the claim keys and
evidence ids it is built from, so nothing is said that a claim does not back. Norwegian register values
(industry names, role titles, place names) are quoted as published.

The helpers here (``describe_claim``, ``key_figures``, ``fmt_amount`` ...) are shared by ``report`` (HTML
viewer) and ``show`` (CLI) so all three surfaces word the same fact the same way.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable
from urllib.parse import urlparse

from .model import FIELDS, SECTIONS

GENERATED_BY = "deterministic_templates_v1"

SECTION_LABEL = {
    "legal_identity_and_brand": "Legal identity and brand",
    "annual_accounts": "Annual accounts",
    "leadership_and_workplaces": "Leadership and workplaces",
    "website_and_profiles": "Website and profiles",
    "hiring_and_activity": "Hiring and activity",
    "claim_evidence": "Claims and evidence",
    "refresh": "Refresh and history",
}

FIELD_LABEL = {
    "legal_name": "Legal name", "legal_form": "Legal form", "registration_status": "Registration status",
    "registered_address": "Business address", "postal_address": "Postal address", "industry": "Industry (NACE)",
    "founded_date": "Founded", "registered_date": "Registered in Enhetsregisteret",
    "statutory_purpose": "Statutory purpose", "activity_description": "Registered activity",
    "share_capital": "Share capital", "historical_name": "Former name", "registry_contact": "Contact in registry",
    "registered_employees": "Registered employees", "public_brand": "Public brand",
    "company_description": "Company's own description", "annual_accounts": "Annual accounts",
    "annual_account_filing": "Latest filed accounts", "workforce_fte": "Workforce (FTE)", "auditor": "Auditor / accountant",
    "role": "Role", "workplace": "Registered workplace", "group_relation": "Group relation",
    "official_website": "Official website", "shared_website": "Shared website", "social_profile": "Social profile",
    "contact_detail": "Contact detail (website)", "site_location": "Location (website)",
    "site_person": "Person named on website", "job_posting": "Job ad", "news_item": "News item",
    "public_mention": "Public mention", "wikidata_item": "Wikidata entry",
    "lei_record": "Legal Entity Identifier (LEI)", "public_support": "Public support award",
    "central_approval": "Central approval (building work)", "procurement_award": "Public contract award (TED)",
    "food_inspection": "Food-safety inspection (Mattilsynet)",
}

# availability family -> plain-words label. Mirrors pipeline.FAMILY_SECTION (kept local: pipeline is heavy to import).
FAMILY_LABEL = {
    "legal_identity": "registry identity",
    "annual_accounts": "annual accounts",
    "roles": "roles (CEO, board, auditor)",
    "workplaces": "registered workplaces",
    "group_relation": "group structure",
    "official_website": "official website",
    "social_profiles": "social media profiles",
    "wikidata": "Wikidata entry",
    "jobs": "job ads",
    "news_activity": "news and public activity",
    "workforce": "workforce (FTE) figures",
    "lei": "Legal Entity Identifier (LEI) record",
    "public_support": "public support awards (Støtteregisteret)",
    "central_approval": "central approval for building work (DiBK)",
    "procurement": "public contract awards (TED)",
    "food_inspections": "food-safety inspections (Mattilsynet)",
}
FAMILY_SECTION = {
    "legal_identity": "legal_identity_and_brand", "annual_accounts": "annual_accounts", "workforce": "annual_accounts",
    "roles": "leadership_and_workplaces", "workplaces": "leadership_and_workplaces",
    "group_relation": "leadership_and_workplaces", "official_website": "website_and_profiles",
    "social_profiles": "website_and_profiles", "wikidata": "website_and_profiles", "jobs": "hiring_and_activity",
    "news_activity": "hiring_and_activity", "lei": "legal_identity_and_brand", "public_support": "annual_accounts",
    "central_approval": "legal_identity_and_brand", "procurement": "hiring_and_activity",
    "food_inspections": "hiring_and_activity",
}
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
# families every full run checks; a family missing from env["availability"] was not checked at all
EXPECTED_FAMILIES = tuple(FAMILY_LABEL)

TOPIC_LABEL = {
    "assessment": "In short", "what_it_does": "What it does", "size_and_finances": "Size and finances", "people": "People",
    "footprint": "Footprint", "hiring_and_workforce": "Hiring and workforce", "group": "Group",
    "status": "Status", "recent_activity": "Recent activity",
}

FIGURE_LABEL = {
    "revenue": "Revenue", "operating_costs": "Operating costs", "payroll_cost": "Payroll cost",
    "operating_result": "Operating result", "net_financial": "Net financial items",
    "profit_before_tax": "Profit before tax", "annual_result": "Annual result", "total_assets": "Total assets",
    "cash": "Cash and bank", "equity": "Equity", "total_debt": "Total debt",
}
FIGURE_ORDER = tuple(FIGURE_LABEL)

BOARD_CODES = ("LEDE", "NEST", "MEDL")
SOCIAL_NAMES = {"linkedin": "LinkedIn", "facebook": "Facebook", "instagram": "Instagram", "x": "X", "twitter": "X",
                "youtube": "YouTube", "tiktok": "TikTok", "github": "GitHub", "pinterest": "Pinterest",
                "threads": "Threads", "bluesky": "Bluesky", "vimeo": "Vimeo", "snapchat": "Snapchat"}

STATE_WORDS = {"available": "available", "not_available": "not available", "blocked": "blocked",
               "not_applicable": "not applicable", "ambiguous": "ambiguous", "failed": "failed",
               "not_checked": "not checked"}


# ---------------------------------------------------------------------------------------------- formatting
def _thousands(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def fmt_amount(v: Any, currency: str | None = "NOK", full: bool = False) -> str:
    """NOK 121.5M / NOK 850k / NOK 9,500; ``full`` gives NOK 121,488,759. Missing stays 'not reported'."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return "not reported"
    cur = currency or "NOK"
    if full:
        return f"{cur} {_thousands(v)}"
    a, sign = abs(v), "-" if v < 0 else ""
    if a >= 1e9:
        s = f"{a / 1e9:.2f}".rstrip("0").rstrip(".") + "bn"
    elif a >= 1e6:
        s = f"{a / 1e6:.1f}".rstrip("0").rstrip(".") + "M"
    elif a >= 1e5:
        s = f"{a / 1e3:.0f}k"
    elif a >= 1e4:
        s = f"{a / 1e3:.1f}".rstrip("0").rstrip(".") + "k"
    else:
        s = _thousands(a)
    return f"{cur} {sign}{s}"


def fmt_number(v: Any) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    return _thousands(v)


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def join_words(items: list[str], limit: int = 4) -> str:
    items = [i for i in items if i]
    extra = len(items) - limit
    items = items[:limit]
    if extra > 0:
        items.append(f"{extra} more")
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def clip(text: Any, n: int = 220) -> str:
    s = " ".join(str(text or "").split()).replace(",,", ",")
    return s if len(s) <= n else s[: n - 1].rstrip(" ,;.") + "…"


def quote(text: Any, n: int = 220) -> str:
    return f"“{clip(text, n)}”"


def sentence(text: str) -> str:
    """End with exactly one full stop, also after a closing quote that already ends in punctuation."""
    t = text.rstrip()
    if t.endswith(("”", '"')) and t[:-1].rstrip().endswith((".", "!", "?", "…")):
        return t
    return t if t.endswith((".", "!", "?")) else t + "."


def cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def host(url: str | None) -> str:
    try:
        h = urlparse(url or "").hostname or ""
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def _date(s: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def fiscal_label(value: dict) -> str:
    """FY2025 for a calendar year, otherwise the explicit period."""
    start, end = value.get("period_start") or "", value.get("period_end") or ""
    if start[5:10] == "01-01" and end[5:10] == "12-31" and start[:4] == end[:4]:
        return f"FY{end[:4]}"
    if start and end:
        return f"{start[:10]} to {end[:10]}"
    return f"period ending {end[:10]}" if end else "an unknown period"


def _period_days(value: dict) -> int | None:
    s, e = _date(value.get("period_start")), _date(value.get("period_end"))
    return (e - s).days if s and e else None


# ---------------------------------------------------------------------------------------------- claim access
def available(env: dict) -> list[dict]:
    return [c for c in env.get("claims") or [] if c.get("availability") == "available"]


def by_field(claims: Iterable[dict], *fields: str) -> list[dict]:
    return [c for c in claims if c.get("field") in fields]


def first(claims: Iterable[dict], field: str) -> dict | None:
    return next((c for c in claims if c.get("field") == field), None)


def accounts(claims: Iterable[dict], account_type: str) -> list[dict]:
    """Annual-accounts claims of one type (SELSKAP or KONSERN), oldest first."""
    out = [c for c in claims if c.get("field") == "annual_accounts" and isinstance(c.get("value"), dict)
           and (c["value"].get("account_type") or "").upper() == account_type]
    return sorted(out, key=lambda c: c["value"].get("period_end") or "")


def preceding(series: list[dict], latest: dict) -> dict | None:
    """The period that ends right before ``latest`` starts and has the same length, else None."""
    ls, ldays = _date(latest["value"].get("period_start")), _period_days(latest["value"])
    if not ls or ldays is None:
        return None
    for c in series:
        e, days = _date(c["value"].get("period_end")), _period_days(c["value"])
        if e and days is not None and 0 < (ls - e).days <= 2 and abs(days - ldays) <= 2:
            return c
    return None


def active_jobs(claims: Iterable[dict], ref: dt.date) -> tuple[list[dict], list[dict]]:
    jobs = by_field(claims, "job_posting")
    act, expired = [], []
    for c in jobs:
        v = c.get("value") if isinstance(c.get("value"), dict) else {}
        exp = _date(v.get("expires"))
        (expired if exp and exp < ref else act).append(c)
    act.sort(key=lambda c: str((c.get("value") or {}).get("published") or ""), reverse=True)
    return act, expired


def municipality(addr: Any) -> str:
    if not isinstance(addr, dict):
        return ""
    return addr.get("kommune") or addr.get("poststed") or ""


def ref_date(env: dict) -> dt.date:
    return _date((env.get("run") or {}).get("started_at")) or dt.date.today()


# ---------------------------------------------------------------------------------------------- describing values
def _addr_text(a: Any) -> str:
    if not isinstance(a, dict):
        return str(a or "")
    line = ", ".join(filter(None, [a.get("adresse"), " ".join(filter(None, [a.get("postnummer"), a.get("poststed")]))]))
    kom = a.get("kommune")
    if kom and kom != a.get("poststed"):
        line += f" ({kom} municipality)"
    if a.get("land") and a.get("land") not in ("Norge", "Norway", "NO"):
        line += f", {a['land']}"
    return line


def _status_text(v: dict) -> str:
    flags = [w for k, w in (("bankrupt", "bankrupt (konkurs)"), ("under_liquidation", "under liquidation (under avvikling)"),
                            ("forced_dissolution", "under forced dissolution (tvangsavvikling)")) if v.get(k)]
    parts = ["flags: " + ", ".join(flags)] if flags else ["no bankruptcy, liquidation or forced-dissolution flags"]
    parts.append("in Foretaksregisteret" if v.get("in_business_register") else "not in Foretaksregisteret")
    parts.append("VAT registered" if v.get("vat_registered") else "not VAT registered")
    if v.get("in_group"):
        parts.append("part of a group (konsern)")
    return "; ".join(parts)


def _accounts_text(v: dict, full: bool = True) -> str:
    fig, cur = v.get("figures") or {}, v.get("currency") or "NOK"
    shown = [f"{FIGURE_LABEL[k].lower()} {fmt_amount(fig[k], cur, full)}" for k in
             ("revenue", "operating_result", "annual_result", "equity", "total_assets") if k in fig]
    tags = []
    if v.get("audit_opt_out"):
        tags.append("audit opt-out")
    elif v.get("audited") is False:
        tags.append("unaudited")
    if v.get("liquidation_accounts"):
        tags.append("liquidation accounts")
    head = f"{v.get('account_type') or 'Accounts'} {fiscal_label(v)}"
    return head + ": " + ("; ".join(shown) or "no key figures reported") + (f" ({', '.join(tags)})" if tags else "")


def _generic(v: Any, depth: int = 0) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return fmt_number(v)
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return ", ".join(filter(None, (_generic(x, depth + 1) for x in v[:8]))) + (" …" if len(v) > 8 else "")
    if isinstance(v, dict):
        if depth > 1:
            return ""
        for k in ("title", "name", "label", "value", "url"):
            if isinstance(v.get(k), str) and len(v) <= 2:
                return v[k]
        parts = []
        for k, x in v.items():
            t = _generic(x, depth + 1)
            if t:
                parts.append(f"{k.replace('_', ' ')}: {t}")
        return "; ".join(parts)
    return str(v)


def _proofs_text(proofs: Any) -> str:
    if not proofs:
        return ""
    items = proofs if isinstance(proofs, list) else [proofs]
    kinds = []
    for p in items:
        if isinstance(p, dict):
            kinds.append(str(p.get("type") or p.get("kind") or p.get("method") or p.get("proof") or "proof"))
        else:
            kinds.append(str(p))
    return join_words(list(dict.fromkeys(k.replace("_", " ") for k in kinds)), 4)


def describe_claim(c: dict, full: bool = True) -> str:
    """One readable line for any claim value; falls back to a generic key: value rendering."""
    f, v = c.get("field"), c.get("value")
    d = v if isinstance(v, dict) else {}
    if f == "legal_form":
        return f"{d.get('description')} ({d.get('code')})" if d else _generic(v)
    if f == "registration_status" and d:
        return _status_text(d)
    if f in ("registered_address", "postal_address"):
        return _addr_text(v)
    if f == "industry" and d:
        rank = d.get("rank")
        return f"{d.get('code')} {d.get('description') or ''}".strip() + (" (primary)" if rank == 1 else f" (secondary {rank})" if rank else "")
    if f == "share_capital" and d:
        extra = ", ".join(filter(None, [d.get("type"), plural(int(d["shares"]), "share") if isinstance(d.get("shares"), (int, float)) else None]))
        return fmt_amount(d.get("amount"), d.get("currency"), True) + (f" ({extra})" if extra else "")
    if f == "registry_contact" and d:
        return "; ".join(f"{k}: {x}" for k, x in d.items())
    if f == "registered_employees":
        return fmt_number(v) + (f" (registered {c['effective_at']})" if c.get("effective_at") else "")
    if f == "annual_accounts" and d:
        return _accounts_text(d, full)
    if f == "annual_account_filing" and d:
        return f"latest submitted year {d.get('latest_submitted_year')}"
    if f == "workforce_fte" and d:
        s = f"{fmt_number(d.get('value'))} {d.get('measure') or 'FTE'}"
        tail = ", ".join(filter(None, [str(d["year"]) if d.get("year") else None, d.get("scope")]))
        return s + (f" ({tail})" if tail else "")
    if f in ("role", "auditor") and d:
        s = f"{d.get('role') or d.get('role_code')}: {d.get('name')}"
        if d.get("holder_organisation_number"):
            s += f" (org {d['holder_organisation_number']})"
        if d.get("register_entry_changed"):
            s += f", registry entry dated {d['register_entry_changed']}"
        return s
    if f == "workplace" and d:
        s = f"{d.get('name')} ({d.get('organisation_number')})"
        if d.get("address"):
            s += ", " + _addr_text(d["address"])
        if d.get("registered_employees") is not None:
            s += f"; {plural(int(d['registered_employees']), 'employee')}"
        return s
    if f == "group_relation" and d:
        rel = {"parent": "Parent", "ultimate_parent": "Ultimate parent", "subsidiary": "Subsidiary"}.get(d.get("relation"), d.get("relation"))
        s = f"{rel}: {d.get('name') or '?'} ({d.get('organisation_number')})"
        if d.get("ownership_basis"):
            s += f", {d['ownership_basis']} ownership"
        if d.get("since"):
            s += f", since {d['since']}"
        return s
    if f in ("official_website", "shared_website") and d:
        s = d.get("url") or d.get("domain") or ""
        pr = _proofs_text(d.get("proofs"))
        if pr:
            s += f" (identity proofs: {pr})"
        return s
    if f == "social_profile" and d:
        name = SOCIAL_NAMES.get(str(d.get("platform") or "").lower(), str(d.get("platform") or "profile"))
        return f"{name}: {d.get('handle') or d.get('url')}"
    if f == "job_posting" and d:
        dates = ", ".join(filter(None, [f"published {d['published'][:10]}" if d.get("published") else None,
                                        f"expires {d['expires'][:10]}" if d.get("expires") else None]))
        return f"{d.get('title') or 'Untitled ad'}" + (f" ({dates})" if dates else "")
    if f in ("news_item", "public_mention") and d:
        when = str(d.get("published") or d.get("date") or "")[:10]
        return (f"{when}: " if when else "") + str(d.get("title") or d.get("url") or _generic(v))
    if f == "wikidata_item" and d:
        s = f"{d.get('item')}"
        if d.get("label"):
            s += f" {d['label']}"
        if d.get("description"):
            s += f": {d['description']}"
        return s
    if f == "lei_record" and d:
        s = f"LEI {d.get('lei')}"
        if d.get("registration_status"):
            s += f" ({d['registration_status'].lower()}"
            s += f", renewal due {d['next_renewal']})" if d.get("next_renewal") else ")"
        return s
    if f == "public_support" and d:
        what = d.get("instrument_en") or (d.get("instrument") or "support")
        amt = fmt_amount(d["amount"], d.get("currency") or "NOK") if isinstance(d.get("amount"), (int, float)) else None
        s = f"{d.get('granted_date') or 'undated'}: {what}"
        if amt:
            s += f" of {amt}"
        if d.get("granting_body"):
            s += f" from {d['granting_body']}"
        return s
    if f == "food_inspection" and d:
        s = f"{d.get('date') or 'undated'}: {d.get('premises') or 'premises'}"
        if d.get("grade_meaning"):
            s += f", {d['grade_meaning']}"
        return s
    if f == "procurement_award" and d:
        s = f"{d.get('published') or 'undated'}: {d.get('title') or d.get('publication_number')}"
        if d.get("buyer"):
            s += f" (buyer: {d['buyer']})"
        return s
    if f == "central_approval" and d:
        s = "valid central approval" if d.get("approved") else "central approval"
        if d.get("valid_from") and d.get("valid_to"):
            s += f" {d['valid_from']} to {d['valid_to']}"
        if d.get("areas"):
            s += f" ({plural(len(d['areas']), 'approved area')})"
        return s
    if f == "site_person" and d:
        return ", ".join(filter(None, [d.get("name"), d.get("title") or d.get("role")])) or _generic(v)
    return _generic(v)


def value_url(c: dict) -> str | None:
    """A link the value itself carries (website, profile, job ad, news article), if any."""
    v = c.get("value")
    if not isinstance(v, dict):
        return None
    for k in ("url", "public_url", "link"):
        u = v.get(k)
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            return u
    if c.get("field") == "wikidata_item" and isinstance(v.get("item"), str) and v["item"].startswith("Q"):
        return f"https://www.wikidata.org/wiki/{v['item']}"
    return None


def field_rank(field: str) -> int:
    order = list(FIELDS)
    return order.index(field) if field in order else len(order)


def facts_by_section(env: dict) -> dict[str, list[dict]]:
    """Every claim (available or not) grouped by section, in model field order."""
    out: dict[str, list[dict]] = {s: [] for s in SECTIONS}
    for c in env.get("claims") or []:
        sec = c.get("section") or FIELDS.get(c.get("field"), ("claim_evidence",))[0]
        out.setdefault(sec, []).append(c)
    for s in out:
        out[s].sort(key=lambda c: (field_rank(c.get("field", "")), c.get("key", "")))
    return out


# ---------------------------------------------------------------------------------------------- summary builder
class _Statements:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, topic: str, text: str, claims: Iterable[dict]) -> None:
        claims = [c for c in claims if c]
        if not claims:
            return  # a statement without a backing claim is never emitted
        text = sentence(text)
        keys = list(dict.fromkeys(c["key"] for c in claims))
        evs = list(dict.fromkeys(e for c in claims for e in c.get("evidence_ids") or []))
        self.items.append({"topic": topic, "text": text, "claim_keys": keys, "evidence_ids": evs})


# ---------------------------------------------------------------------------------------------- assessment
def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _full_year(value: dict) -> bool:
    d = _period_days(value)
    return d is not None and 360 <= d <= 370


def _size_word(fte: float | None, revenue: float | None) -> str | None:
    if fte is not None:
        if fte >= 250:
            return "large"
        if fte >= 50:
            return "medium-sized"
        if fte >= 10:
            return "small"
        if fte > 0:
            return "micro-sized"
    if revenue is not None and revenue > 0:
        if revenue >= 500_000_000:
            return "large"
        if revenue >= 80_000_000:
            return "medium-sized"
        if revenue >= 10_000_000:
            return "small"
        return "micro-sized"
    return None


def _age_text(founded: Any, ref: dt.date) -> str | None:
    d = _date(founded)
    if not d:
        return None
    years = (ref - d).days / 365.25
    if years < 1:
        return f"founded {d.isoformat()}, less than a year before this profile"
    n = int(years)
    return f"founded in {d.year}, about {n} {'year' if n == 1 else 'years'} ago"


def _latest_fte(cl: list[dict]) -> tuple[dict | None, float | None, str | None]:
    best = None
    for c in by_field(cl, "workforce_fte"):
        v = c.get("value") if isinstance(c.get("value"), dict) else {}
        if _num(v.get("value")) is None:
            continue
        if best is None or str(v.get("year") or "") > str(best["value"].get("year") or ""):
            best = c
    if best is None:
        return None, None, None
    return best, _num(best["value"]["value"]), (str(best["value"].get("year") or "") or None)


def _assessment(name: str, cl: list[dict], st: _Statements, ref: dt.date) -> None:
    """Decision-useful reading of the facts: size, business profile, profitability, trend, balance sheet and the
    points worth checking. Every sentence is computed from cited claims; nothing is estimated or invented."""
    sel = [c for c in accounts(cl, "SELSKAP") if isinstance(c["value"].get("figures"), dict)]
    latest = sel[-1] if sel else None
    fig = latest["value"]["figures"] if latest else {}
    cur = (latest["value"].get("currency") or "NOK") if latest else "NOK"
    fy = fiscal_label(latest["value"]) if latest else ""
    fte_c, fte, fte_year = _latest_fte(cl)
    emp_c = first(cl, "registered_employees")
    emp = _num((emp_c or {}).get("value"))
    rev = _num(fig.get("revenue"))
    head_c = fte_c if fte is not None else emp_c
    headcount = fte if fte is not None else emp

    # 1. who, how big, where, how old
    ind = sorted(by_field(cl, "industry"),
                 key=lambda c: (c.get("value") or {}).get("rank", 9) if isinstance(c.get("value"), dict) else 9)
    addr = first(cl, "registered_address") or first(cl, "postal_address")
    founded = first(cl, "founded_date") or first(cl, "registered_date")
    size = _size_word(headcount, rev)
    used: list[dict] = []
    text = f"{name} is " + (f"a {size} " if size else "a ")
    if (ind and isinstance(ind[0].get("value"), dict) and ind[0]["value"].get("description")
            and not str(ind[0]["value"].get("code") or "").startswith("00")):
        text += f"business in “{ind[0]['value']['description']}” (NACE {ind[0]['value'].get('code')})"
        used.append(ind[0])
    else:
        text += "business"
    if addr and isinstance(addr.get("value"), dict) and municipality(addr["value"]):
        text += f" based in {addr['value'].get('poststed') or municipality(addr['value'])}"
        used.append(addr)
    age = _age_text((founded or {}).get("value"), ref)
    if age:
        text += f", {age}"
        used.append(founded)
    scale = []
    if fte is not None:
        scale.append(f"{fmt_number(fte)} full-time equivalents" + (f" in {fte_year}" if fte_year else ""))
    elif emp is not None:
        scale.append(f"{fmt_number(emp)} registered {'employee' if int(emp) == 1 else 'employees'}")
    if rev is not None:
        scale.append(f"revenue {fmt_amount(rev, cur)} for {fy}")
    if scale:
        text += " (" + "; ".join(scale) + ")"
    used += [c for c in (head_c, latest if rev is not None else None) if c]
    if used:
        st.add("assessment", text + ".", used)

    if not latest:
        return
    opr, net, ar = _num(fig.get("operating_result")), _num(fig.get("net_financial")), _num(fig.get("annual_result"))
    costs, ta, eq = _num(fig.get("operating_costs")), _num(fig.get("total_assets")), _num(fig.get("equity"))
    cash, debt, pay = _num(fig.get("cash")), _num(fig.get("total_debt")), _num(fig.get("payroll_cost"))

    # 2. business profile
    if (rev is None or rev == 0) and (fte or 0) == 0:
        if net is not None and abs(net) >= 50_000 and (opr is None or abs(net) > abs(opr)):
            st.add("assessment", f"Its {fy} accounts show no operating revenue, and net financial items "
                                 f"({fmt_amount(net, cur)}) outweigh the operating result"
                                 + (f" ({fmt_amount(opr, cur)})" if opr is not None else "")
                                 + ": the profile of a holding or investment company.", [latest])
        elif (costs is None or abs(costs) < 50_000) and (net is None or abs(net) < 50_000):
            st.add("assessment", f"Its {fy} accounts show no revenue, almost no costs and no material financial "
                                 "items: the company appears dormant, not yet trading or only holding assets.", [latest])

    # 3. profitability
    if rev and rev > 0 and opr is not None:
        margin = opr / rev * 100
        word = ("profitable at operating level" if opr > 0 else
                "breaking even at operating level" if opr == 0 else "loss-making at operating level")
        if abs(margin) < 100:
            detail = f"operating margin {margin:.1f}% (operating result {fmt_amount(opr, cur)} on revenue {fmt_amount(rev, cur)})"
        else:
            detail = f"operating result {fmt_amount(opr, cur)} on revenue of only {fmt_amount(rev, cur)}"
        st.add("assessment", f"In {fy} it was {word}: {detail}.", [latest])

    # 4. multi-year trend (full-year company accounts only)
    full = [c for c in sel if _full_year(c["value"]) and _num(c["value"]["figures"].get("revenue")) is not None]
    if len(full) >= 2:
        a, b = full[0], full[-1]
        ra, rb = _num(a["value"]["figures"]["revenue"]), _num(b["value"]["figures"]["revenue"])
        ya, yb = int(str(a["value"]["period_end"])[:4]), int(str(b["value"]["period_end"])[:4])
        if ra and ra > 0 and rb is not None and yb > ya:
            direction = "grew" if rb > ra else ("fell" if rb < ra else "was flat")
            text = (f"Revenue {direction} from {fmt_amount(ra, cur)} in {fiscal_label(a['value'])} to "
                    f"{fmt_amount(rb, cur)} in {fiscal_label(b['value'])}")
            if direction != "was flat" and rb > 0 and yb - ya >= 2:
                cagr = ((rb / ra) ** (1 / (yb - ya)) - 1) * 100
                text += f", an average {'growth' if cagr > 0 else 'decline'} of about {abs(cagr):.1f}% a year"
            elif direction != "was flat" and rb > 0:
                text += f" ({(rb - ra) / ra * 100:+.1f}%)"
            st.add("assessment", text + ".", full)
    losses = 0
    for c in reversed(sel):
        r = _num(c["value"]["figures"].get("annual_result"))
        if r is not None and r < 0:
            losses += 1
        else:
            break
    if losses >= 2:
        st.add("assessment", f"It reported a net loss in each of its last {losses} filed years.", sel[-losses:])
    elif losses == 0 and len(sel) >= 2 and ar is not None and ar > 0:
        prev_r = _num(sel[-2]["value"]["figures"].get("annual_result"))
        if prev_r is not None and prev_r < 0:
            st.add("assessment", f"It returned to a profit ({fmt_amount(ar, cur)}) in {fy} after a loss of "
                                 f"{fmt_amount(abs(prev_r), cur)} in the preceding accounts.", sel[-2:])

    # 5. balance sheet
    if ta and ta > 0 and eq is not None:
        ratio = eq / ta * 100
        if eq >= 0:
            text = (f"At {latest['value'].get('period_end')} equity was {ratio:.0f}% of total assets "
                    f"({fmt_amount(eq, cur)} of {fmt_amount(ta, cur)})")
        else:
            text = (f"At {latest['value'].get('period_end')} equity was negative ({fmt_amount(eq, cur)}) against "
                    f"total assets of {fmt_amount(ta, cur)}")
        extra = []
        if cash is not None:
            extra.append(f"cash and bank {fmt_amount(cash, cur)}")
        if debt is not None:
            extra.append(f"total debt {fmt_amount(debt, cur)}")
        if extra:
            text += "; " + ", ".join(extra)
        if 0 <= ratio < 10:
            text += ". That is a thin equity buffer"
        st.add("assessment", text + ".", [latest])

    # 6. payroll per FTE (same full year only)
    if (pay and fte and fte >= 1 and fte_year and _full_year(latest["value"])
            and str(latest["value"].get("period_end", ""))[:4] == fte_year):
        st.add("assessment", f"Payroll costs were about {fmt_amount(pay / fte, cur)} per full-time equivalent in {fy} "
                             f"({fmt_amount(pay, cur)} for {fmt_number(fte)} FTE).", [latest, fte_c])

    # 7. points to check
    flags, fused = [], []
    rs = first(cl, "registration_status")
    if rs and isinstance(rs.get("value"), dict):
        for k, word in (("bankrupt", "it is registered as bankrupt"), ("under_liquidation", "it is under liquidation"),
                        ("forced_dissolution", "it is under forced dissolution")):
            if rs["value"].get(k):
                flags.append(word)
                fused.append(rs)
    if eq is not None and eq < 0:
        flags.append("negative equity")
        fused.append(latest)
    end = _date(latest["value"].get("period_end"))
    if end and (ref - end).days > 2 * 365 + 200:
        flags.append(f"the latest filed company accounts are for {fy}")
        fused.append(latest)
    if latest["value"].get("audit_opt_out") and rev and rev >= 5_000_000:
        flags.append(f"the accounts are unaudited (audit opt-out) with revenue of {fmt_amount(rev, cur)}")
        fused.append(latest)
    if flags:
        st.add("assessment", "Points to check: " + "; ".join(flags) + ".", fused)


def _what_it_does(name: str, cl: list[dict], st: _Statements) -> None:
    form, addr = first(cl, "legal_form"), first(cl, "registered_address") or first(cl, "postal_address")
    founded, regd, name_c = first(cl, "founded_date"), first(cl, "registered_date"), first(cl, "legal_name")
    if name_c:
        parts, used = [name], [name_c]
        if form and isinstance(form.get("value"), dict):
            parts.append(f"is registered as {form['value'].get('description')} ({form['value'].get('code')})")
            used.append(form)
        else:
            parts.append("is registered in Enhetsregisteret")
        if addr and municipality(addr.get("value")):
            a = addr["value"]
            place = a.get("poststed") or a.get("kommune")
            kom = a.get("kommune")
            parts.append(f"with its business address in {place}" + (f" ({kom} municipality)" if kom and kom != place else ""))
            used.append(addr)
        text = " ".join(parts)
        if founded:
            text += f", founded {founded['value']}"
            used.append(founded)
        elif regd:
            text += f", registered on {regd['value']}"
            used.append(regd)
        st.add("what_it_does", text + ".", used)
    brand = first(cl, "public_brand")
    if brand and isinstance(brand.get("value"), str) and brand["value"].strip().lower() != (name or "").strip().lower():
        st.add("what_it_does", f"It presents itself publicly as “{brand['value']}”.", [brand])
    inds = sorted(by_field(cl, "industry"), key=lambda c: (c["value"] or {}).get("rank", 9) if isinstance(c.get("value"), dict) else 9)
    if inds:
        def nace(c: dict) -> str:
            v = c["value"] if isinstance(c["value"], dict) else {}
            return f"{v.get('code')} {v.get('description') or ''}".strip()
        text = f"Its registered primary industry (NACE) is {nace(inds[0])}"
        if len(inds) > 1:
            text += "; it is also registered under " + join_words([nace(c) for c in inds[1:]], 3)
        st.add("what_it_does", text + ".", inds)
    act, purpose = first(cl, "activity_description"), first(cl, "statutory_purpose")
    if act and act.get("value"):
        st.add("what_it_does", f"Registered business activity: {quote(act['value'])}", [act])
    if purpose and purpose.get("value"):
        pv, av = clip(purpose["value"], 80).lower(), clip((act or {}).get("value"), 80).lower()
        if pv != av:
            st.add("what_it_does", f"Statutory purpose (vedtektsfestet formål): {quote(purpose['value'])}", [purpose])
    desc = first(cl, "company_description")
    if desc and desc.get("value"):
        dv = desc["value"] if isinstance(desc["value"], str) else _generic(desc["value"])
        st.add("what_it_does", f"Its own website describes it as: {quote(dv, 260)} (company-owned copy, not independently verified).", [desc])
    for lei in by_field(cl, "lei_record")[:1]:
        v = lei["value"] if isinstance(lei.get("value"), dict) else {}
        text = f"It holds the Legal Entity Identifier {v.get('lei')} (GLEIF)"
        if v.get("registration_status") and v["registration_status"] != "ISSUED":
            text += f"; its registration status is {v['registration_status'].lower()}, i.e. not renewed"
        elif v.get("next_renewal"):
            text += f", registered since {v.get('initial_registration')} and due for renewal {v['next_renewal']}"
        st.add("what_it_does", text + ".", [lei])
    ca = first(cl, "central_approval")
    if ca and isinstance(ca.get("value"), dict) and ca["value"].get("approved"):
        v = ca["value"]
        areas = [f"{a.get('function', '').lower()} in {a.get('subject_area', '').lower()} (class {a.get('class')})"
                 for a in v.get("areas") or []]
        text = "It holds central approval for building work from DiBK"
        if v.get("valid_to"):
            text += f", valid {v.get('valid_from')} to {v['valid_to']}"
        if areas:
            text += f", covering {join_words(areas, 3)}"
        st.add("what_it_does", text + ".", [ca])
    wd = first(cl, "wikidata_item")
    if wd and isinstance(wd.get("value"), dict) and wd["value"].get("description"):
        st.add("what_it_does", f"Wikidata ({wd['value'].get('item')}) describes it as {quote(wd['value']['description'], 160)}", [wd])


def _finances(cl: list[dict], st: _Statements, unknowns: list[dict]) -> None:
    sel, kon = accounts(cl, "SELSKAP"), accounts(cl, "KONSERN")
    if sel:
        latest = sel[-1]
        v, fig, cur = latest["value"], latest["value"].get("figures") or {}, latest["value"].get("currency") or "NOK"
        fy = fiscal_label(v)
        parts = [f"{label} {fmt_amount(fig[k], cur)}" for k, label in
                 (("revenue", "revenue"), ("operating_result", "operating result"), ("annual_result", "annual result"),
                  ("equity", "equity")) if k in fig]
        text = f"Company accounts (SELSKAP) for {fy}: " + (", ".join(parts) if parts else "no key figures reported") + "."
        if "revenue" not in fig:
            text += " Revenue is not included in the filed figures."
        tags = []
        if v.get("audit_opt_out"):
            tags.append("the company has opted out of audit")
        if v.get("liquidation_accounts"):
            tags.append("these are liquidation accounts")
        if tags:
            text += " Note: " + "; ".join(tags) + "."
        st.add("size_and_finances", text, [latest])
        prev = preceding(sel, latest)
        if prev:
            pfig, pfy = prev["value"].get("figures") or {}, fiscal_label(prev["value"])
            bits = []
            r, pr = fig.get("revenue"), pfig.get("revenue")
            if isinstance(r, (int, float)) and isinstance(pr, (int, float)):
                if pr > 0:
                    pct = (r - pr) / pr * 100
                    word = "unchanged" if abs(pct) < 0.05 else ("up" if pct > 0 else "down") + f" {abs(pct):.1f}%"
                    bits.append(f"revenue {word} from {fmt_amount(pr, cur)} in {pfy}")
                else:
                    bits.append(f"revenue was {fmt_amount(pr, cur)} in {pfy}")
            ar, par = fig.get("annual_result"), pfig.get("annual_result")
            if isinstance(ar, (int, float)) and isinstance(par, (int, float)):
                bits.append(f"annual result {fmt_amount(par, cur)} in {pfy} versus {fmt_amount(ar, cur)} in {fy}")
            if bits:
                st.add("size_and_finances", "Compared with the preceding year: " + "; ".join(bits) + ".", [latest, prev])
        eq = fig.get("equity")
        if isinstance(eq, (int, float)) and eq < 0:
            st.add("size_and_finances", f"Equity was negative ({fmt_amount(eq, cur)}) at {v.get('period_end')}: "
                                        "booked liabilities exceeded booked assets.", [latest])
    if kon:
        latest = kon[-1]
        fig, cur = latest["value"].get("figures") or {}, latest["value"].get("currency") or "NOK"
        parts = [f"{label} {fmt_amount(fig[k], cur)}" for k, label in
                 (("revenue", "revenue"), ("operating_result", "operating result"), ("annual_result", "annual result")) if k in fig]
        st.add("size_and_finances", f"Group accounts (KONSERN) for {fiscal_label(latest['value'])} are filed separately: "
                                    + (", ".join(parts) or "no key figures reported")
                                    + ". These cover the whole group and are not comparable with company-only figures.", [latest])
    aid = [c for c in by_field(cl, "public_support") if isinstance(c.get("value"), dict)]
    if aid:
        tot: dict[str, float] = {}
        for c in aid:
            v = c["value"]
            if isinstance(v.get("amount"), (int, float)):
                tot[v.get("currency") or "NOK"] = tot.get(v.get("currency") or "NOK", 0.0) + v["amount"]
        givers = list(dict.fromkeys(c["value"].get("granting_body") for c in aid if c["value"].get("granting_body")))
        dates = sorted(c["value"].get("granted_date") for c in aid if c["value"].get("granted_date"))
        text = f"Støtteregisteret lists {plural(len(aid), 'public support award')}"
        if tot:
            text += " totalling " + ", ".join(fmt_amount(a, cur) for cur, a in tot.items())
        if dates:
            text += f" ({dates[0]} to {dates[-1]})" if dates[0] != dates[-1] else f" ({dates[0]})"
        if givers:
            text += f", granted by {join_words(givers, 3)}"
        latest = sorted(aid, key=lambda c: c["value"].get("granted_date") or "")[-1]
        text += f". Most recent: {describe_claim(latest)}."
        st.add("size_and_finances", text, aid)
    filing = first(cl, "annual_account_filing")
    years = [str((c["value"].get("period_end") or "")[:4]) for c in sel + kon]
    if filing and isinstance(filing.get("value"), dict):
        y = str(filing["value"].get("latest_submitted_year") or "")
        if y and years and y > max(years):
            unknowns.append({"family": "annual_accounts", "state": "not_available",
                             "text": f"Accounts for {y} are listed as submitted, but no figures for {y} were returned by Regnskapsregisteret yet."})
        elif y and not years:
            st.add("size_and_finances", f"Brønnøysund lists {y} as the latest submitted annual accounts.", [filing])


def _people(cl: list[dict], st: _Statements) -> None:
    roles = by_field(cl, "role")
    val = lambda c: c["value"] if isinstance(c.get("value"), dict) else {}
    ceo = [c for c in roles if val(c).get("role_code") == "DAGL"]
    chair = [c for c in roles if val(c).get("role_code") == "LEDE"]
    board = [c for c in roles if val(c).get("role_code") in BOARD_CODES]
    deputies = [c for c in roles if val(c).get("role_code") == "VARA"]
    if ceo:
        st.add("people", f"Daglig leder (CEO): {join_words([val(c).get('name') for c in ceo])}.", ceo)
    elif roles:
        st.add("people", "No daglig leder (CEO) is registered in the roles register.", roles)
    if chair:
        st.add("people", f"Chair of the board (styrets leder): {join_words([val(c).get('name') for c in chair])}.", chair)
    if board:
        text = f"The registered board has {plural(len(board), 'member')}"
        if deputies:
            text += f" and {plural(len(deputies), 'deputy', 'deputies')}"
        st.add("people", text + ".", board + deputies)
    aud = [c for c in by_field(cl, "auditor") if val(c).get("role_code") == "REVI"]
    acc = [c for c in by_field(cl, "auditor") if val(c).get("role_code") == "REGN"]
    if aud:
        st.add("people", f"Auditor (revisor): {join_words([val(c).get('name') for c in aud])}.", aud)
    if acc:
        st.add("people", f"External accountant (regnskapsfører): {join_words([val(c).get('name') for c in acc])}.", acc)
    sp = by_field(cl, "site_person")
    if sp:
        names = [describe_claim(c) for c in sp]
        st.add("people", f"Its website names {plural(len(sp), 'person', 'people')}: {join_words(names, 3)} (company-owned copy).", sp)


def _footprint(cl: list[dict], st: _Statements) -> None:
    wps = by_field(cl, "workplace")
    if wps:
        muni = list(dict.fromkeys(municipality((c.get("value") or {}).get("address")) for c in wps if isinstance(c.get("value"), dict)))
        muni = [m for m in muni if m]
        text = f"It has {plural(len(wps), 'registered workplace')} ({'underenhet' if len(wps) == 1 else 'underenheter'})"
        if len(muni) == 1:
            text += f" in {muni[0]}"
        elif muni:
            text += f" in {plural(len(muni), 'municipality', 'municipalities')}: {join_words(muni, 5)}"
        st.add("footprint", text + ".", wps)
    web = first(cl, "official_website")
    if web and isinstance(web.get("value"), dict):
        v = web["value"]
        text = f"Its official website is {v.get('url') or v.get('domain')}"
        pr = _proofs_text(v.get("proofs"))
        text += f", tied to the organisation number by {pr}." if pr else "."
        st.add("footprint", text, [web])
    else:
        rc = first(cl, "registry_contact")
        if rc and isinstance(rc.get("value"), dict) and rc["value"].get("website"):
            st.add("footprint", f"The registry lists the website {rc['value']['website']} (as registered by the company; not independently verified).", [rc])
    shared = first(cl, "shared_website")
    if shared and isinstance(shared.get("value"), dict):
        st.add("footprint", f"It is presented on a website shared with other entities: {shared['value'].get('url') or shared['value'].get('domain')}.", [shared])
    socials = by_field(cl, "social_profile")
    if socials:
        st.add("footprint", f"Social media profiles: {join_words([describe_claim(c) for c in socials], 5)}.", socials)
    locs = by_field(cl, "site_location")
    if locs:
        st.add("footprint", f"Its website lists {plural(len(locs), 'location')}: {join_words([clip(describe_claim(c), 80) for c in locs], 3)}.", locs)
    contacts = by_field(cl, "contact_detail")
    if contacts:
        st.add("footprint", f"Its website publishes {plural(len(contacts), 'contact detail')}.", contacts)


def _hiring(cl: list[dict], st: _Statements, ref: dt.date) -> None:
    emp = first(cl, "registered_employees")
    if emp is not None and isinstance(emp.get("value"), (int, float)):
        text = f"Enhetsregisteret reports {plural(int(emp['value']), 'employee')}"
        if emp.get("effective_at"):
            text += f" (registered {emp['effective_at']})"
        st.add("hiring_and_workforce", text + ".", [emp])
    for c in sorted(by_field(cl, "workforce_fte"), key=lambda c: str((c.get("value") or {}).get("year") or ""), reverse=True)[:1]:
        v = c["value"] if isinstance(c.get("value"), dict) else {}
        text = f"Reported workforce: {fmt_number(v.get('value'))} {v.get('measure') or 'full-time equivalents'}"
        if v.get("year"):
            text += f" in {v['year']}"
        if v.get("scope"):
            text += f" ({v['scope']})"
        st.add("hiring_and_workforce", text + ".", [c])
    act, expired = active_jobs(cl, ref)
    if act:
        hosts = list(dict.fromkeys(host((c.get("value") or {}).get("public_url")) for c in act))
        titles = [f"“{(c.get('value') or {}).get('title')}”" for c in act if (c.get("value") or {}).get("title")]
        text = f"It has {plural(len(act), 'active job ad')}"
        if [h for h in hosts if h]:
            text += f" on {join_words([h for h in hosts if h], 2)}"
        if titles:
            text += f", for example {join_words(titles, 3)}"
        st.add("hiring_and_workforce", text + ".", act)
    if expired:
        st.add("hiring_and_workforce", f"{plural(len(expired), 'further job ad')} {'was' if len(expired) == 1 else 'were'} found but "
                                       f"{'has' if len(expired) == 1 else 'have'} already expired.", expired)


def _group(cl: list[dict], st: _Statements) -> None:
    rels = [c for c in by_field(cl, "group_relation") if isinstance(c.get("value"), dict)]
    rv = lambda c: c["value"]
    parent = [c for c in rels if rv(c).get("relation") == "parent"]
    ultimate = [c for c in rels if rv(c).get("relation") == "ultimate_parent"]
    subs = [c for c in rels if rv(c).get("relation") == "subsidiary"]
    for p in parent[:1]:
        v = rv(p)
        text = f"It is a subsidiary of {v.get('name')} ({v.get('organisation_number')})"
        if v.get("ownership_basis"):
            text += f", ownership {v['ownership_basis']}"
        used = [p]
        if ultimate:
            u = rv(ultimate[0])
            text += f"; the ultimate parent is {u.get('name')} ({u.get('organisation_number')})"
            used.append(ultimate[0])
        if v.get("group_size"):
            text += f". The group structure lists {plural(int(v['group_size']), 'entity', 'entities')}"
        st.add("group", text + ".", used)
    if subs:
        names = [f"{rv(c).get('name')} ({rv(c).get('organisation_number')})" for c in subs]
        st.add("group", f"It has {plural(len(subs), 'registered subsidiary', 'registered subsidiaries')}: {join_words(names, 4)}.", subs)


def _status(cl: list[dict], st: _Statements) -> None:
    rs = first(cl, "registration_status")
    if not rs or not isinstance(rs.get("value"), dict):
        return
    v = rs["value"]
    flags = [w for k, w in (("bankrupt", "bankrupt (konkurs)"), ("under_liquidation", "under liquidation (under avvikling)"),
                            ("forced_dissolution", "under forced dissolution or forced liquidation")) if v.get(k)]
    if flags:
        text = "Warning: the registry marks the company as " + join_words(flags) + "."
    else:
        text = "The registry shows it as active, with no bankruptcy, liquidation or forced-dissolution flags."
    regs = [w for k, w in (("in_business_register", "Foretaksregisteret"), ("vat_registered", "the VAT register (MVA-registeret)")) if v.get(k)]
    if regs:
        text += f" It is registered in {join_words(regs)}."
    st.add("status", text, [rs])


def _procurement(cl: list[dict], st: _Statements) -> None:
    awards = sorted(by_field(cl, "procurement_award"), key=lambda c: str((c.get("value") or {}).get("published") or ""))
    if not awards:
        return
    buyers = list(dict.fromkeys((c.get("value") or {}).get("buyer") for c in awards if (c.get("value") or {}).get("buyer")))
    text = f"TED (the EU/EEA procurement journal) lists {plural(len(awards), 'contract award notice')} naming it as a winner"
    if buyers:
        text += f", for buyers such as {join_words(buyers, 3)}"
    text += f". Most recent: {describe_claim(awards[-1])}."
    st.add("recent_activity", text, awards)


def _inspections(cl: list[dict], st: _Statements) -> None:
    ins = sorted(by_field(cl, "food_inspection"), key=lambda c: str((c.get("value") or {}).get("date") or ""))
    if not ins:
        return
    latest = ins[-1]
    worst = max(((c.get("value") or {}).get("grade") for c in ins if isinstance((c.get("value") or {}).get("grade"), int)),
                default=None)
    premises = list(dict.fromkeys((c.get("value") or {}).get("premises") for c in ins if (c.get("value") or {}).get("premises")))
    text = (f"Mattilsynet has published {plural(len(ins), 'food-safety inspection')} of its premises"
            + (f" ({join_words(premises, 3)})" if premises else "") + f". Most recent: {describe_claim(latest)}")
    if worst is not None and worst >= 2:
        text += f". At least one inspection resulted in grade {worst} ({GRADE_WORDS.get(worst, '')})"
    st.add("recent_activity", text + ".", ins)


GRADE_WORDS = {2: "orders issued", 3: "serious breaches"}


def _recent(cl: list[dict], st: _Statements) -> None:
    news = sorted(by_field(cl, "news_item"), key=lambda c: str((c.get("value") or {}).get("published") or ""), reverse=True)
    if news:
        items = []
        for c in news[:3]:
            v = c["value"] if isinstance(c.get("value"), dict) else {}
            src = host(v.get("url"))
            meta = ", ".join(filter(None, [str(v.get("published") or "")[:10], src]))
            items.append(f"“{clip(v.get('title') or v.get('url'), 110)}”" + (f" ({meta})" if meta else ""))
        text = f"{plural(len(news), 'news item')} found; latest: " + "; ".join(items) + "."
        st.add("recent_activity", text, news[:3] if len(news) > 3 else news)
    ment = by_field(cl, "public_mention")
    if ment:
        st.add("recent_activity", f"{plural(len(ment), 'other public mention')} found.", ment)


# ---------------------------------------------------------------------------------------------- changes / unknowns
CHANGE_PREFIX = {
    "new_role": "New role registered", "role_ended": "Role no longer registered", "new_auditor": "New auditor/accountant",
    "auditor_ended": "Auditor/accountant no longer registered", "new_location": "New registered workplace",
    "closed_location": "Workplace no longer registered", "new_filing": "New annual accounts filed",
    "new_job": "New job ad", "closed_job": "Job ad no longer listed", "new_activity": "New news item",
    "new_profile": "New social profile", "profile_removed": "Social profile no longer found",
    "new_website": "Official website now verified", "website_removed": "Official website no longer verified",
    "new_workforce_figure": "New workforce figure",
}


def _dict_diff(a: dict, b: dict, field: str) -> str:
    if field == "annual_accounts":
        fa, fb, cur = a.get("figures") or {}, b.get("figures") or {}, b.get("currency") or "NOK"
        diffs = [f"{FIGURE_LABEL.get(k, k).lower()} {fmt_amount(fa.get(k), cur)} → {fmt_amount(fb.get(k), cur)}"
                 for k in FIGURE_ORDER if fa.get(k) != fb.get(k)]
        return f"{b.get('account_type')} {fiscal_label(b)} revised: " + (", ".join(diffs[:4]) or "details changed")
    diffs = []
    for k in list(dict.fromkeys([*a, *b])):
        if a.get(k) != b.get(k):
            diffs.append(f"{k.replace('_', ' ')} {_generic(a.get(k)) or '(none)'} → {_generic(b.get(k)) or '(none)'}")
    return "; ".join(diffs[:5])


def change_text(ch: dict) -> str:
    field, kind = ch.get("field", ""), ch.get("change_type", "")
    label = FIELD_LABEL.get(field, field.replace("_", " ").capitalize())
    cur = {"field": field, "value": ch.get("current_value")}
    prev = {"field": field, "value": ch.get("previous_value")}
    if kind == "changed":
        a, b = ch.get("previous_value"), ch.get("current_value")
        if isinstance(a, dict) and isinstance(b, dict) and field in ("annual_accounts", "registration_status", "registry_contact"):
            return f"{label} changed: {_dict_diff(a, b, field)}."
        return f"{label} changed from “{clip(describe_claim(prev), 120)}” to “{clip(describe_claim(cur), 120)}”."
    if kind in CHANGE_PREFIX:
        ref = cur if ch.get("current_value") is not None else prev
        return f"{CHANGE_PREFIX[kind]}: {clip(describe_claim(ref), 160)}."
    if kind == "added":
        return f"New {label.lower()}: {clip(describe_claim(cur), 160)}."
    if kind == "removed":
        return f"{label} no longer found: {clip(describe_claim(prev), 160)}."
    return f"{label}: {kind.replace('_', ' ')}."


def _changes(env: dict) -> list[dict]:
    refresh = (env.get("sections") or {}).get("refresh") or {}
    out = []
    for ch in sorted(env.get("changes") or [], key=lambda c: (not c.get("material"), c.get("field", ""), c.get("key", ""))):
        out.append({"text": change_text(ch), "change_keys": [ch.get("key")], "change_type": ch.get("change_type"),
                    "material": bool(ch.get("material")),
                    "evidence_ids": list(dict.fromkeys((ch.get("current_evidence_ids") or []) + (ch.get("previous_evidence_ids") or [])))})
    if out:
        return out
    if refresh.get("baseline") or (not refresh.get("previous_run_id") and (env.get("run") or {}).get("terminal_status") == "completed"):
        return [{"text": "Baseline run: this is the first snapshot of this company, so there is no earlier version to compare with.",
                 "change_keys": [], "change_type": "baseline", "material": False, "evidence_ids": []}]
    if refresh.get("previous_run_id"):
        return [{"text": f"No changes since the previous run ({refresh['previous_run_id']}).", "change_keys": [],
                 "change_type": "none", "material": False, "evidence_ids": []}]
    return []


def unknown_text(family: str, state: str, reason: str | None) -> str:
    label = FAMILY_LABEL.get(family, family.replace("_", " "))
    if family == "official_website" and state == "not_available":
        return f"No official website could be verified" + (f": {reason}." if reason else ".")
    if state == "not_available":
        return f"No {label} found" + (f": {reason}." if reason else ".")
    why = f" ({reason})" if reason else ""
    if state == "blocked":
        return f"{cap(label)} could not be checked{why}."
    if state == "failed":
        return f"{cap(label)}: the source failed{why}."
    if state == "ambiguous":
        return f"{cap(label)}: candidates found, but none could be tied to this company with confidence{why}."
    if state == "not_applicable":
        return f"{cap(label)}: not applicable{why}."
    if state == "not_checked":
        return f"{cap(label)}: not checked in this run."
    return f"{cap(label)}: {STATE_WORDS.get(state, state)}{why}."


def _unknowns(env: dict) -> list[dict]:
    av = env.get("availability") or {}
    out = []
    order = list(EXPECTED_FAMILIES) + [f for f in av if f not in EXPECTED_FAMILIES]
    for fam in order:
        info = av.get(fam)
        if info is None:
            if env.get("state") == "available":
                out.append({"family": fam, "state": "not_checked", "text": unknown_text(fam, "not_checked", None)})
            continue
        if info.get("state") != "available":
            out.append({"family": fam, "state": info.get("state"), "text": unknown_text(fam, info.get("state"), info.get("reason"))})
    return out


def _headline(env: dict, cl: list[dict]) -> str:
    org = env.get("organisation_number")
    name = env.get("legal_name") or ((first(cl, "legal_name") or {}).get("value"))
    state = env.get("state")
    if state != "available" or not name:
        reason = next((v.get("reason") for v in (env.get("availability") or {}).values() if v.get("state") != "available" and v.get("reason")), None)
        lead = {"not_available": "No registry profile", "ambiguous": "Identity ambiguous", "blocked": "Profile blocked",
                "failed": "Profile could not be built", "not_applicable": "Not applicable"}.get(state, "No profile")
        return f"{lead} for {org}" + (f": {reason}" if reason else "") + "."
    bits = [f"{name} ({org})"]
    ind = sorted(by_field(cl, "industry"), key=lambda c: (c.get("value") or {}).get("rank", 9))
    if ind:
        bits.append((ind[0]["value"] or {}).get("description") or "")
    addr = first(cl, "registered_address")
    if addr and municipality(addr.get("value")):
        bits.append(municipality(addr["value"]))
    sel = accounts(cl, "SELSKAP")
    if sel and "revenue" in (sel[-1]["value"].get("figures") or {}):
        v = sel[-1]["value"]
        bits.append(f"revenue {fmt_amount(v['figures']['revenue'], v.get('currency'))} ({fiscal_label(v)})")
    rs = first(cl, "registration_status")
    if rs and isinstance(rs.get("value"), dict):
        if rs["value"].get("bankrupt"):
            bits.append("BANKRUPT")
        elif rs["value"].get("under_liquidation") or rs["value"].get("forced_dissolution"):
            bits.append("BEING WOUND UP")
    return " · ".join(b for b in bits if b)


def _confidence_note(env: dict, cl: list[dict], n_statements: int) -> str:
    if env.get("state") != "available":
        return ("No facts are published for this input because the company could not be matched or retrieved; "
                "see the unknowns for the reason. Nothing here comes from a language model.")
    ev = env.get("evidence") or []
    official = sum(1 for e in ev if str(e.get("source_class", "")).startswith("official"))
    text = (f"{plural(n_statements, 'statement')} built by fixed templates from {plural(len(cl), 'sourced claim')} and "
            f"{plural(len(ev), 'evidence record')} ({official} from official registers, {len(ev) - official} from websites "
            "and other public sources). Each statement cites the claims it uses; nothing here comes from a language model.")
    carried = [c for c in cl if c.get("carried_forward")]
    if carried:
        fields = join_words(list(dict.fromkeys(FIELD_LABEL.get(c["field"], c["field"]).lower() for c in carried)), 4)
        text += f" {plural(len(carried), 'value')} ({fields}) could not be re-checked this run and {'is' if len(carried) == 1 else 'are'} carried forward from the last successful check."
    if any(c.get("external") for c in cl):
        text += " Facts from company-owned websites are the company's own statements."
    if (env.get("run") or {}).get("terminal_status") == "provisional":
        text += " This is a provisional profile; external sources were still being checked."
    return text


def summarise(env: dict) -> dict:
    """Deterministic summary for one envelope. Safe on failed / not_available envelopes."""
    cl = available(env)
    st, unknowns = _Statements(), []
    name = env.get("legal_name") or (first(cl, "legal_name") or {}).get("value") or env.get("organisation_number")
    if env.get("state") == "available" and cl:
        _assessment(name, cl, st, ref_date(env))
        _what_it_does(name, cl, st)
        _finances(cl, st, unknowns)
        _people(cl, st)
        _footprint(cl, st)
        _hiring(cl, st, ref_date(env))
        _group(cl, st)
        _status(cl, st)
        _recent(cl, st)
        _procurement(cl, st)
        _inspections(cl, st)
    unknowns = _unknowns(env) + unknowns
    return {
        "generated_by": GENERATED_BY,
        "headline": _headline(env, cl),
        "statements": st.items,
        "changes": _changes(env),
        "unknowns": unknowns,
        "confidence_note": _confidence_note(env, cl, len(st.items)),
    }


# ---------------------------------------------------------------------------------------------- comparison rows
KEY_FIGURES = (
    ("legal_form", "Legal form"), ("status", "Status"), ("founded", "Founded"), ("municipality", "Municipality"),
    ("industry", "Primary industry"), ("fiscal_year", "Latest accounts"), ("revenue", "Revenue"),
    ("revenue_change", "Revenue change"), ("operating_result", "Operating result"), ("annual_result", "Annual result"),
    ("equity", "Equity"), ("employees", "Registered employees"), ("fte", "Workforce (FTE)"), ("ceo", "Daglig leder (CEO)"),
    ("chair", "Chair"), ("board", "Board members"), ("auditor", "Auditor"), ("group", "Group"),
    ("workplaces", "Registered workplaces"), ("website", "Official website"), ("socials", "Social profiles"),
    ("jobs", "Active job ads"), ("news", "News items"),
)
FIGURE_FAMILY = {"legal_form": "legal_identity", "status": "legal_identity", "founded": "legal_identity",
                 "municipality": "legal_identity", "industry": "legal_identity", "fiscal_year": "annual_accounts",
                 "revenue": "annual_accounts", "revenue_change": "annual_accounts", "operating_result": "annual_accounts",
                 "annual_result": "annual_accounts", "equity": "annual_accounts", "employees": "legal_identity",
                 "fte": "workforce", "ceo": "roles", "chair": "roles", "board": "roles", "auditor": "roles",
                 "group": "group_relation", "workplaces": "workplaces", "website": "official_website",
                 "socials": "social_profiles", "jobs": "jobs", "news": "news_activity"}


def key_figures(env: dict) -> list[dict]:
    """Rows for side-by-side comparison: {id, label, text|None, claim_keys, state}. Missing rows carry the family state."""
    cl = available(env)
    av = env.get("availability") or {}
    rows: dict[str, tuple[str, list[dict]]] = {}

    def put(fid: str, text: str, claims: list[dict]) -> None:
        if claims:
            rows[fid] = (text, claims)

    lf = first(cl, "legal_form")
    if lf and isinstance(lf.get("value"), dict):
        put("legal_form", describe_claim(lf), [lf])
    rs = first(cl, "registration_status")
    if rs and isinstance(rs.get("value"), dict):
        v = rs["value"]
        flags = [w for k, w in (("bankrupt", "bankrupt"), ("under_liquidation", "in liquidation"), ("forced_dissolution", "forced dissolution")) if v.get(k)]
        put("status", ", ".join(flags) if flags else "active, no insolvency flags", [rs])
    fd = first(cl, "founded_date") or first(cl, "registered_date")
    if fd:
        put("founded", str(fd["value"]), [fd])
    ad = first(cl, "registered_address")
    if ad and municipality(ad.get("value")):
        put("municipality", municipality(ad["value"]), [ad])
    ind = sorted(by_field(cl, "industry"), key=lambda c: (c.get("value") or {}).get("rank", 9))
    if ind:
        put("industry", describe_claim(ind[0]).replace(" (primary)", ""), [ind[0]])
    sel = accounts(cl, "SELSKAP")
    if sel:
        latest = sel[-1]
        v, fig, cur = latest["value"], latest["value"].get("figures") or {}, latest["value"].get("currency") or "NOK"
        put("fiscal_year", f"{fiscal_label(v)} (company)", [latest])
        for k in ("revenue", "operating_result", "annual_result", "equity"):
            if k in fig:
                put(k, fmt_amount(fig[k], cur) + (" (negative)" if k == "equity" and fig[k] < 0 else ""), [latest])
        prev = preceding(sel, latest)
        pr = ((prev or {}).get("value") or {}).get("figures", {}).get("revenue") if prev else None
        if prev and isinstance(fig.get("revenue"), (int, float)) and isinstance(pr, (int, float)) and pr > 0:
            pct = (fig["revenue"] - pr) / pr * 100
            put("revenue_change", f"{pct:+.1f}% vs {fiscal_label(prev['value'])}", [latest, prev])
    emp = first(cl, "registered_employees")
    if emp:
        put("employees", describe_claim(emp), [emp])
    fte = sorted(by_field(cl, "workforce_fte"), key=lambda c: str((c.get("value") or {}).get("year") or ""), reverse=True)
    if fte:
        put("fte", describe_claim(fte[0]), [fte[0]])
    roles = by_field(cl, "role")
    code = lambda c: (c.get("value") or {}).get("role_code") if isinstance(c.get("value"), dict) else None
    ceo = [c for c in roles if code(c) == "DAGL"]
    put("ceo", ", ".join((c["value"].get("name") or "") for c in ceo), ceo)
    chair = [c for c in roles if code(c) == "LEDE"]
    put("chair", ", ".join((c["value"].get("name") or "") for c in chair), chair)
    board = [c for c in roles if code(c) in BOARD_CODES]
    put("board", str(len(board)), board)
    aud = [c for c in by_field(cl, "auditor") if code(c) == "REVI"] or [c for c in by_field(cl, "auditor") if code(c) == "REGN"]
    put("auditor", ", ".join(f"{c['value'].get('name')}" + (" (accountant)" if code(c) == "REGN" else "") for c in aud), aud)
    rels = [c for c in by_field(cl, "group_relation") if isinstance(c.get("value"), dict)]
    if rels:
        par = [c for c in rels if c["value"].get("relation") == "parent"]
        subs = [c for c in rels if c["value"].get("relation") == "subsidiary"]
        bits = ([f"parent {par[0]['value'].get('name')}"] if par else []) + ([plural(len(subs), 'subsidiary', 'subsidiaries')] if subs else [])
        put("group", "; ".join(bits) or "group member", par[:1] + subs)
    wps = by_field(cl, "workplace")
    if wps:
        muni = [m for m in dict.fromkeys(municipality((c.get("value") or {}).get("address")) for c in wps) if m]
        put("workplaces", str(len(wps)) + (f" ({join_words(muni, 3)})" if muni else ""), wps)
    web = first(cl, "official_website")
    if web and isinstance(web.get("value"), dict):
        put("website", str(web["value"].get("domain") or host(web["value"].get("url")) or web["value"].get("url")), [web])
    so = by_field(cl, "social_profile")
    if so:
        put("socials", ", ".join(dict.fromkeys(SOCIAL_NAMES.get(str((c.get("value") or {}).get("platform") or "").lower(),
                                                                  str((c.get("value") or {}).get("platform") or "profile")) for c in so)), so)
    act, _ = active_jobs(cl, ref_date(env))
    put("jobs", str(len(act)), act)
    news = by_field(cl, "news_item")
    put("news", str(len(news)), news)

    out = []
    for fid, label in KEY_FIGURES:
        if fid in rows:
            text, claims = rows[fid]
            out.append({"id": fid, "label": label, "text": text, "state": "available",
                        "claim_keys": list(dict.fromkeys(c["key"] for c in claims)),
                        "evidence_ids": list(dict.fromkeys(e for c in claims for e in c.get("evidence_ids") or []))})
        else:
            fam = FIGURE_FAMILY.get(fid)
            st = (av.get(fam) or {}).get("state") or ("not_checked" if env.get("state") == "available" else env.get("state"))
            if st == "available":
                st = "not_available"  # family has data, but not this particular figure
            out.append({"id": fid, "label": label, "text": None, "state": st, "claim_keys": [], "evidence_ids": [],
                        "reason": (av.get(fam) or {}).get("reason")})
    return out
