"""Official Norwegian registers: Brønnøysund Enhetsregisteret and Regnskapsregisteret (NLOD 2.0).

The organisation number is the identity anchor. The evaluator-supplied bulk snapshot seeds identity;
live endpoints add roles, registered workplaces, group links and filed annual accounts. Endpoint
normalisation follows Builderr's MIT starter kit (see NOTICE).
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .http import Fetcher, FetchResult, utcnow
from .model import Contribution, make_claim, make_evidence

BULK_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"
ENTITY_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
ROLES_URL = ENTITY_URL + "/roller"
SUBUNITS_URL = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=200"
GROUP_URL = "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}"
ACCOUNTS_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
BRREG_PUBLIC_PAGE = "https://virksomhet.brreg.no/nb/oppslag/enheter/{org}"

ORG_RE = re.compile(r"^\d{9}$")
_W = (3, 2, 7, 6, 5, 4, 3, 2)

FREE_MAIL = {
    "gmail.com", "gmail.no", "hotmail.com", "hotmail.no", "online.no", "outlook.com", "outlook.no", "live.no",
    "live.com", "yahoo.com", "yahoo.no", "icloud.com", "me.com", "mac.com", "msn.com", "frisurf.no", "broadpark.no",
    "getmail.no", "start.no", "c2i.net", "lyse.net", "altibox.no", "mail.com", "tele2.no", "haugnett.no",
    "enivest.net", "ebnett.no", "loqal.no", "sf-nett.no", "gmx.com", "gmx.net", "protonmail.com", "proton.me",
    "pvv.ntnu.no", "chello.no", "netcom.no", "telia.no", "bluezone.no", "combitel.no", "tdcnet.no", "eunet.no",
    "aol.com", "googlemail.com", "kpnmail.nl", "web.de",
}


def valid_org(org: str) -> bool:
    if not ORG_RE.match(org):
        return False
    s = sum(int(d) * w for d, w in zip(org[:8], _W))
    check = 11 - s % 11
    check = 0 if check == 11 else check
    return check != 10 and check == int(org[8])


# ---------------------------------------------------------------- batch input
def read_batch(path: str) -> list[str]:
    """Accept JSONL ({"organisation_number": ...}), JSON list/dict, CSV with organisation_number, or text."""
    raw = Path(path).read_bytes()
    if path.endswith(".gz"):
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8-sig").strip()
    out: list[str] = []

    def pick(obj: Any) -> str:
        if isinstance(obj, dict):
            for k in ("organisation_number", "organisationNumber", "orgnr", "organisasjonsnummer", "org"):
                if k in obj:
                    return str(obj[k]).strip()
            return ""
        return str(obj).strip()

    if text.startswith("["):
        return [pick(x) for x in json.loads(text)]
    if text.startswith("{") and "\n" not in text.strip():
        obj = json.loads(text)
        for k in ("organisation_numbers", "organisations", "companies", "items"):
            if isinstance(obj.get(k), list):
                return [pick(x) for x in obj[k]]
        return [pick(obj)]
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and lines[0].lstrip().startswith("{"):
        try:
            return [pick(json.loads(ln)) for ln in lines]
        except json.JSONDecodeError:
            obj = json.loads(text)
            for k in ("organisation_numbers", "organisations", "companies", "items"):
                if isinstance(obj.get(k), list):
                    return [pick(x) for x in obj[k]]
    if lines and "organisation_number" in lines[0] or (lines and "organisasjonsnummer" in lines[0]):
        rdr = csv.DictReader(io.StringIO(text))
        return [pick(r) for r in rdr]
    for ln in lines:
        out.append(re.sub(r"\s+", "", ln.split(",")[0].strip().strip('"')))
    return out


# ---------------------------------------------------------------- bulk snapshot
def _open_text(path: str):
    with open(path, "rb") as fh:
        magic = fh.read(2)
    if magic == b"\x1f\x8b":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", newline="")
    return open(path, encoding="utf-8", newline="")


def email_domain(email: str) -> str:
    email = (email or "").strip().lower()
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def site_domain(url: str) -> str:
    url = (url or "").strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = url.split("/")[0].split("?")[0]
    return url[4:] if url.startswith("www.") else url


class BulkSnapshot:
    """Rows for the batch plus domain-sharing counts over the whole register."""

    def __init__(self, path: str | None, wanted: Iterable[str]):
        self.path = path
        self.rows: dict[str, dict[str, str]] = {}
        self.email_domain_count: Counter = Counter()
        self.site_domain_count: Counter = Counter()
        self.rows_scanned = 0
        self.file_sha256 = None
        if not path:
            return
        wanted = set(wanted)
        sha = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                sha.update(chunk)
        self.file_sha256 = sha.hexdigest()
        csv.field_size_limit(10**8)
        with _open_text(path) as fh:
            first = fh.read(1)
            fh.seek(0) if hasattr(fh, "seek") else None
            if first in ("{", "["):
                self._load_json(fh, wanted)
                return
            for row in csv.DictReader(fh):
                self.rows_scanned += 1
                d = email_domain(row.get("epostadresse", ""))
                if d:
                    self.email_domain_count[d] += 1
                s = site_domain(row.get("hjemmeside", ""))
                if s:
                    self.site_domain_count[s] += 1
                org = row.get("organisasjonsnummer", "")
                if org in wanted:
                    self.rows[org] = row

    def _load_json(self, fh, wanted: set[str]) -> None:
        text = fh.read()
        items = json.loads(text) if text.lstrip().startswith("[") else [json.loads(x) for x in text.splitlines() if x.strip()]
        for item in items:
            row = _flatten(item)
            self.rows_scanned += 1
            d = email_domain(row.get("epostadresse", ""))
            if d:
                self.email_domain_count[d] += 1
            s = site_domain(row.get("hjemmeside", ""))
            if s:
                self.site_domain_count[s] += 1
            if row.get("organisasjonsnummer") in wanted:
                self.rows[row["organisasjonsnummer"]] = row


def _flatten(obj: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, list):
            out[key] = ", ".join(str(x) for x in v)
        elif v is None:
            out[key] = ""
        else:
            out[key] = str(v).lower() if isinstance(v, bool) else str(v)
    return out


def entity_to_row(body: dict) -> dict[str, str]:
    """Flatten a live entity API response to bulk-CSV column names."""
    row = _flatten({k: v for k, v in body.items() if k != "_links"})
    if "forretningsadresse.adresse" in row:
        pass
    return row


# ---------------------------------------------------------------- claim builders
def _addr(row: dict[str, str], prefix: str) -> dict[str, str] | None:
    parts = {k: row.get(f"{prefix}.{k}", "") for k in ("adresse", "postnummer", "poststed", "kommune", "kommunenummer", "land")}
    if not any(parts.values()):
        return None
    return {k: v for k, v in parts.items() if v}


def _num(v: str) -> float | int | None:
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def registry_claims(org: str, row: dict[str, str], source_url: str, source_class: str, retrieved_at: str,
                    extraction: str, out: Contribution) -> None:
    row_json = json.dumps(row, sort_keys=True, ensure_ascii=False)
    row_hash = hashlib.sha256(row_json.encode("utf-8")).hexdigest()

    def ev(span_fields: list[str]) -> dict:
        span = "; ".join(f"{f}={row.get(f, '')}" for f in span_fields if row.get(f))
        return make_evidence(source_url, source_class, retrieved_at, row_hash, f"organisasjonsnummer={org}; {span}",
                             extraction, locator="row:" + ",".join(span_fields), access_policy="NLOD-2.0")

    def put(field: str, value: Any, cols: list[str], disc: Any = None, **kw: Any) -> None:
        if value in (None, "", {}, []):
            return
        e = ev(cols)
        out.add(make_claim(field, value, [e], discriminator=disc, **kw), e)

    put("legal_name", row.get("navn"), ["navn"])
    put("legal_form", {"code": row.get("organisasjonsform.kode"), "description": row.get("organisasjonsform.beskrivelse")},
        ["organisasjonsform.kode", "organisasjonsform.beskrivelse"])
    status = {
        "bankrupt": row.get("konkurs") == "true",
        "under_liquidation": row.get("underAvvikling") == "true",
        "forced_dissolution": row.get("underTvangsavviklingEllerTvangsopplosning") == "true",
        "in_business_register": row.get("registrertIForetaksregisteret") == "true",
        "vat_registered": row.get("registrertIMvaRegisteret") == "true",
        "in_group": row.get("erIKonsern") == "true",
    }
    put("registration_status", status, ["konkurs", "underAvvikling", "underTvangsavviklingEllerTvangsopplosning",
                                        "registrertIForetaksregisteret", "registrertIMvaRegisteret", "erIKonsern"])
    a = _addr(row, "forretningsadresse")
    put("registered_address", a, [f"forretningsadresse.{k}" for k in ("adresse", "postnummer", "poststed", "kommune")])
    p = _addr(row, "postadresse")
    put("postal_address", p, [f"postadresse.{k}" for k in ("adresse", "postnummer", "poststed")])
    for i in (1, 2, 3):
        code = row.get(f"naeringskode{i}.kode")
        if code:
            put("industry", {"code": code, "description": row.get(f"naeringskode{i}.beskrivelse"), "rank": i},
                [f"naeringskode{i}.kode", f"naeringskode{i}.beskrivelse"], disc=code)
    put("founded_date", row.get("stiftelsesdato"), ["stiftelsesdato"])
    put("registered_date", row.get("registreringsdatoenhetsregisteret") or row.get("registreringsdatoEnhetsregisteret"),
        ["registreringsdatoenhetsregisteret", "registreringsdatoEnhetsregisteret"])
    put("statutory_purpose", row.get("vedtektsfestetFormaal"), ["vedtektsfestetFormaal"])
    put("activity_description", row.get("aktivitet"), ["aktivitet"])
    if row.get("kapital.belop"):
        put("share_capital", {"amount": _num(row.get("kapital.belop")), "currency": row.get("kapital.valuta") or "NOK",
                              "shares": _num(row.get("kapital.antallAksjer", "")), "type": row.get("kapital.type")},
            ["kapital.belop", "kapital.valuta", "kapital.antallAksjer"])
    if row.get("harRegistrertAntallAnsatte") == "true" and row.get("antallAnsatte"):
        put("registered_employees", _num(row["antallAnsatte"]),
            ["antallAnsatte", "registreringsdatoantallansatteNAVAaregisteret"],
            effective_at=row.get("registreringsdatoantallansatteNAVAaregisteret") or row.get("registreringsdatoAntallAnsatteNAVAaregisteret") or None)
    contact = {k: v for k, v in {"website": row.get("hjemmeside"), "email": row.get("epostadresse"),
                                  "phone": row.get("telefon"), "mobile": row.get("mobil")}.items() if v}
    if contact:
        put("registry_contact", contact, ["hjemmeside", "epostadresse", "telefon", "mobil"])
    put("annual_account_filing", {"latest_submitted_year": row.get("sisteInnsendteAarsregnskap")},
        ["sisteInnsendteAarsregnskap"]) if row.get("sisteInnsendteAarsregnskap") else None


def _g(v: Any, *path: str) -> Any:
    for k in path:
        if not isinstance(v, dict):
            return None
        v = v.get(k)
    return v


def accounts_claims(org: str, res: FetchResult, out: Contribution) -> list[dict]:
    """One claim per filed period and account type. Company and group accounts are kept apart."""
    if res.status in (404, 410) or (res.ok and res.content.strip() in (b"[]", b"")):
        out.state("annual_accounts", "not_available", "Regnskapsregisteret returned no normalised accounts")
        return []
    if not res.ok:
        out.state("annual_accounts", res.state_hint or "failed", res.error or "accounts fetch failed")
        out.error("registry.accounts", res.error or "fetch failed", res.url)
        return []
    try:
        records = res.json()
    except ValueError:
        out.state("annual_accounts", "failed", "accounts response not JSON")
        return []
    norm = []
    for item in records if isinstance(records, list) else []:
        period = item.get("regnskapsperiode") or {}
        fields = {
            "revenue": _g(item, "resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
            "operating_result": _g(item, "resultatregnskapResultat", "driftsresultat", "driftsresultat"),
            "payroll_cost": _g(item, "resultatregnskapResultat", "driftsresultat", "driftskostnad", "loennskostnad"),
            "operating_costs": _g(item, "resultatregnskapResultat", "driftsresultat", "driftskostnad", "sumDriftskostnad"),
            "net_financial": _g(item, "resultatregnskapResultat", "finansresultat", "nettoFinans"),
            "profit_before_tax": _g(item, "resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"),
            "annual_result": _g(item, "resultatregnskapResultat", "aarsresultat"),
            "total_assets": _g(item, "eiendeler", "sumEiendeler"),
            "cash": _g(item, "eiendeler", "sumBankinnskuddOgKontanter"),
            "equity": _g(item, "egenkapitalGjeld", "egenkapital", "sumEgenkapital"),
            "total_debt": _g(item, "egenkapitalGjeld", "gjeldOversikt", "sumGjeld"),
        }
        value = {
            "account_type": item.get("regnskapstype"),
            "currency": item.get("valuta"),
            "period_start": period.get("fraDato"),
            "period_end": period.get("tilDato"),
            "audited": not _g(item, "revisjon", "ikkeRevidertAarsregnskap"),
            "audit_opt_out": bool(_g(item, "revisjon", "fravalgRevisjon")),
            "liquidation_accounts": bool(item.get("avviklingsregnskap")),
            "small_company_rules": bool(_g(item, "regnkapsprinsipper", "smaaForetak")),
            # missing values stay missing (None is dropped), never zero-filled
            "figures": {k: v for k, v in fields.items() if v is not None},
        }
        span = (f"regnskapstype={value['account_type']}; periode={value['period_start']}..{value['period_end']}; "
                + "; ".join(f"{k}={v}" for k, v in value["figures"].items()))
        e = make_evidence(res.url, "official_annual_accounts", res.retrieved_at, res.content_sha256, span,
                          "brreg_regnskap_api_json", locator=f"id={item.get('id')};journalnr={item.get('journalnr')}",
                          http_status=res.status, access_policy="NLOD-2.0", effective_at=period.get("tilDato"))
        disc = f"{value['account_type']}:{(value['period_end'] or '')[:10]}"
        out.add(make_claim("annual_accounts", value, [e], discriminator=disc,
                           reporting_period={"start": value["period_start"], "end": value["period_end"]},
                           effective_at=value["period_end"]), e)
        norm.append(value)
    if not norm:
        out.state("annual_accounts", "not_available", "no account records")
    return sorted(norm, key=lambda v: (v["account_type"] or "", v["period_end"] or ""))


ROLE_PRIORITY = {"DAGL": 0, "LEDE": 1, "NEST": 2, "MEDL": 3, "VARA": 4, "OBS": 5, "REVI": 6, "REGN": 7}


def roles_claims(org: str, res: FetchResult, out: Contribution) -> list[dict]:
    if res.status in (404, 410):
        out.state("roles", "not_available", "no roles registered")
        return []
    if not res.ok:
        out.state("roles", res.state_hint or "failed", res.error or "roles fetch failed")
        out.error("registry.roles", res.error or "fetch failed", res.url)
        return []
    body = res.json()
    roles = []
    for group in body.get("rollegrupper", []):
        changed = group.get("sistEndret")
        for item in group.get("roller", []):
            if item.get("avregistrert") or item.get("fratraadt"):
                continue
            person = item.get("person") or {}
            if person.get("erDoed"):
                continue
            name = person.get("navn") or {}
            ent = item.get("enhet") or {}
            display = " ".join(filter(None, [name.get("fornavn"), name.get("mellomnavn"), name.get("etternavn")]))
            holder_org = ent.get("organisasjonsnummer")
            if not display:
                display = " ".join(ent.get("navn") or []) if isinstance(ent.get("navn"), list) else ent.get("navn")
            code = _g(item, "type", "kode")
            # birth dates are deliberately discarded (personal data minimisation)
            role = {"name": display, "role_code": code, "role": _g(item, "type", "beskrivelse"),
                    "group": _g(group, "type", "beskrivelse"), "holder_type": "organisation" if holder_org else "person",
                    "holder_organisation_number": holder_org, "register_entry_changed": changed}
            role = {k: v for k, v in role.items() if v}
            roles.append(role)
    roles.sort(key=lambda r: (ROLE_PRIORITY.get(r.get("role_code", ""), 9), r.get("name", "")))
    for r in roles:
        span = f"{r.get('group')}: {r.get('role')} {r.get('name')}"
        e = make_evidence(res.url, "official_roles", res.retrieved_at, res.content_sha256, span, "brreg_roles_api_json",
                          http_status=res.status, access_policy="NLOD-2.0", effective_at=r.get("register_entry_changed"),
                          locator=f"rolle={r.get('role_code')}")
        field = "auditor" if r.get("role_code") in ("REVI", "REGN") else "role"
        disc = f"{r.get('role_code')}:{r.get('holder_organisation_number') or r.get('name')}"
        out.add(make_claim(field, r, [e], discriminator=disc, effective_at=r.get("register_entry_changed")), e)
    if not roles:
        out.state("roles", "not_available", "no active roles")
    return roles


def subunit_claims(org: str, res: FetchResult, out: Contribution) -> list[dict]:
    if not res.ok:
        out.state("workplaces", res.state_hint or "failed", res.error or "subunit fetch failed")
        out.error("registry.subunits", res.error or "fetch failed", res.url)
        return []
    units = _g(res.json(), "_embedded", "underenheter") or []
    found = []
    for u in units:
        if u.get("slettedato") or u.get("nedleggelsesdato"):
            continue
        addr = u.get("beliggenhetsadresse") or u.get("postadresse") or {}
        value = {
            "organisation_number": u.get("organisasjonsnummer"),
            "name": u.get("navn"),
            "address": {k: (", ".join(v) if isinstance(v, list) else v) for k, v in addr.items()
                        if k in ("adresse", "postnummer", "poststed", "kommune", "kommunenummer", "land") and v},
            "industry": _g(u, "naeringskode1", "beskrivelse"),
            "registered_employees": u.get("antallAnsatte") if u.get("harRegistrertAntallAnsatte") else None,
            "website": u.get("hjemmeside"),
            "started": u.get("oppstartsdato"),
        }
        value = {k: v for k, v in value.items() if v not in (None, "", {})}
        span = f"underenhet {value.get('organisation_number')} {value.get('name')} {value.get('address', {}).get('poststed', '')}"
        e = make_evidence(res.url, "official_subunits", res.retrieved_at, res.content_sha256, span,
                          "brreg_subunits_api_json", http_status=res.status, access_policy="NLOD-2.0",
                          locator=f"underenhet={value.get('organisation_number')}")
        out.add(make_claim("workplace", value, [e], discriminator=value.get("organisation_number")), e)
        found.append(value)
    if not found:
        out.state("workplaces", "not_available", "no registered subunits")
    return found


def group_claims(org: str, res: FetchResult, out: Contribution) -> None:
    if res.status in (404, 410):
        out.state("group_relation", "not_available", "no group structure returned")
        return
    if not res.ok:
        out.state("group_relation", res.state_hint or "failed", res.error or "group fetch failed")
        return
    try:
        body = res.json()
    except ValueError:
        return
    # Response root is the ultimate parent; ``children`` lists every member with its direct parent.
    root = {"organisation_number": body.get("organisasjonsnummer"), "name": body.get("navn")}
    children = body.get("children") or []
    rels: list[dict] = []
    own = next((c for c in children if c.get("organisasjonsnummer") == org), None)
    if root["organisation_number"] and root["organisation_number"] != org:
        if own and own.get("parentOrganisasjonsnummer") and own["parentOrganisasjonsnummer"] != root["organisation_number"]:
            rels.append({"organisation_number": own["parentOrganisasjonsnummer"], "name": own.get("parentNavn"),
                         "relation": "parent", "ownership_basis": own.get("grunnlag"),
                         "link_type": _g(own, "knytningsform", "beskrivelse"), "since": own.get("dato")})
            rels.append({**root, "relation": "ultimate_parent"})
        else:
            rels.append({**root, "relation": "parent", "ownership_basis": (own or {}).get("grunnlag"),
                         "link_type": _g(own or {}, "knytningsform", "beskrivelse"), "since": (own or {}).get("dato")})
    for c in children:
        if c.get("parentOrganisasjonsnummer") == org:
            rels.append({"organisation_number": c.get("organisasjonsnummer"), "name": c.get("navn"), "relation": "subsidiary",
                         "ownership_basis": c.get("grunnlag"), "link_type": _g(c, "knytningsform", "beskrivelse"),
                         "since": c.get("dato")})
    group_size = len(children) + 1
    for r in rels:
        r = {k: v for k, v in r.items() if v}
        r["group_size"] = group_size
        e = make_evidence(res.url, "official_group_structure", res.retrieved_at, res.content_sha256,
                          f"konsern: {r['relation']} {r.get('organisation_number')} {r.get('name')} {r.get('ownership_basis', '')}",
                          "brreg_group_api_json", http_status=res.status, access_policy="NLOD-2.0", effective_at=r.get("since"))
        out.add(make_claim("group_relation", r, [e], discriminator=f"{r['relation']}:{r.get('organisation_number')}",
                           effective_at=r.get("since")), e)
    if not rels:
        out.state("group_relation", "not_available", "group structure returned no direct parent or subsidiary")


def collect(org: str, row: dict[str, str] | None, fetcher: Fetcher, snapshot_sha: str | None, live_entity: bool = True) -> Contribution:
    """Registry phase for one company. Returns contribution with facts['profile']."""
    out = Contribution()
    profile_row = row
    if live_entity:
        res = fetcher.get(ENTITY_URL.format(org=org), "registry", essential=True)
        if res.status == 410 or (res.status == 404 and row is None):
            out.facts["deleted"] = True
            out.state("legal_identity", "not_available",
                      "organisation number is deleted from or absent in Enhetsregisteret" if res.status == 410 else "not found in Enhetsregisteret")
            out.facts["profile"] = row or {}
            return out
        if res.ok:
            try:
                profile_row = entity_to_row(res.json())
                registry_claims(org, profile_row, res.url, "official_registry_live", res.retrieved_at,
                                "brreg_entity_api_json", out)
            except ValueError:
                res = None
        elif row is None:
            out.state("legal_identity", res.state_hint or "failed", res.error or "entity fetch failed")
            out.error("registry.entity", res.error or "fetch failed", res.url)
    if row is not None and not any(c["field"] == "legal_name" for c in out.claims):
        registry_claims(org, row, BULK_URL, "official_registry_bulk", utcnow(), "brreg_bulk_csv_row", out)
    out.facts["profile"] = profile_row or {}

    accounts = accounts_claims(org, fetcher.get(ACCOUNTS_URL.format(org=org), "registry", essential=True), out)
    roles = roles_claims(org, fetcher.get(ROLES_URL.format(org=org), "registry", essential=True), out)
    units = subunit_claims(org, fetcher.get(SUBUNITS_URL.format(org=org), "registry", essential=True), out)
    if (profile_row or {}).get("erIKonsern") == "true":
        group_claims(org, fetcher.get(GROUP_URL.format(org=org), "registry"), out)
    else:
        out.state("group_relation", "not_applicable", "registry does not mark the company as part of a group")
    out.facts.update({"accounts": accounts, "roles": roles, "workplaces": units,
                      "group": [c["value"] for c in out.claims if c["field"] == "group_relation"]})
    return out
