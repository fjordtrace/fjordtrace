"""Brønnøysund bulk files for roles and registered subunits (NLOD 2.0), used by the tight-budget plan.

One request each covers the whole batch instead of one call per company. Each company's slice is turned
into the same shape as the per-company API response, so ``registry.roles_claims`` / ``subunit_claims``
extract claims exactly as they do from the live API. The evidence points to the bulk file (its dated file
name, the company's slice hash and an ``organisasjonsnummer=`` locator).

Both parsers fail closed: if a file cannot be downloaded or read, ``None`` is returned and the registry
pass falls back to the per-company endpoints within the remaining budget.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
import zlib
from dataclasses import dataclass
from typing import Iterable, Iterator

from .http import Fetcher, FetchResult

ROLES_BULK_URL = "https://data.brreg.no/enhetsregisteret/api/roller/totalbestand"
SUBUNITS_BULK_URL = "https://data.brreg.no/enhetsregisteret/api/underenheter/lastned/csv"
MAX_BULK_BYTES = 600_000_000
MIN_SUBUNIT_ROWS = 300_000  # the full file has ~1M rows; fewer means a truncated download
_CHUNK = 1 << 22

_ROLE_ORG = re.compile(rb'\n  "organisasjonsnummer"\s*:\s*"(\d{9})"')
# As served (checked 2026-11-13): '[\n  {\n  "_links" : ...\n},\n  {\n  "_links" : ...\n} ]'. A company is
# closed by a brace at column 0, which nothing nested uses (role groups close at two spaces: "  }, {").
_ROLE_SEP = b"\n},\n  {\n"
_ROLE_NEXT = _ROLE_SEP
MAX_ROLE_BUFFER = 64 << 20  # no company boundary within 64 MB -> the layout changed; give up (per-company fallback)


@dataclass
class BulkSource:
    url: str
    final_name: str  # dated file name from Content-Disposition, when the server gives one
    retrieved_at: str
    sha256: str
    records: dict[str, bytes]  # org -> API-shaped JSON body for that company

    def result_for(self, org: str, empty_body: bytes) -> FetchResult:
        body = self.records.get(org, empty_body)
        return FetchResult(self.url, self.url, 200, self.retrieved_at, body, "application/json",
                           headers={"x-bulk-file": self.final_name, "x-bulk-sha256": self.sha256})


def _gunzip_chunks(data: bytes) -> Iterator[bytes]:
    d = zlib.decompressobj(31)
    for i in range(0, len(data), _CHUNK):
        out = d.decompress(data[i:i + _CHUNK])
        if out:
            yield out
    tail = d.flush()
    if tail:
        yield tail


def parse_roles(data: bytes, wanted: set[str]) -> dict[str, bytes]:
    """Stream the pretty-printed ``roller/totalbestand`` JSON array and keep only the wanted companies.

    Top-level objects are separated by ``\\n},\\n  {\\n`` and carry their own organisation number at
    two-space indentation; nested entities sit deeper, so they never match."""
    found: dict[str, bytes] = {}
    buf = b""
    first = True
    for piece in _gunzip_chunks(data):
        buf += piece
        cut = buf.rfind(_ROLE_NEXT)
        if cut < 0:
            if len(buf) > MAX_ROLE_BUFFER:
                raise ValueError("roles bulk file: no company boundary found; layout changed")
            continue
        _take_roles(buf[:cut], wanted, found, first)
        first = False
        buf = buf[cut + len(_ROLE_SEP):]
    _take_roles(buf, wanted, found, first, last=True)
    return found


def _take_roles(block: bytes, wanted: set[str], found: dict[str, bytes], first: bool, last: bool = False) -> None:
    for m in _ROLE_ORG.finditer(block):
        org = m.group(1).decode()
        if org not in wanted:
            continue
        start = block.rfind(_ROLE_NEXT, 0, m.start())
        start = 0 if start < 0 else start + len(_ROLE_SEP)
        end = block.find(_ROLE_NEXT, m.end())
        end = len(block) if end < 0 else end
        text = block[start:end]
        if start == 0 and first:
            text = re.sub(rb"^\s*\[\s*\{", b"", text)
        if end == len(block) and last:
            text = re.sub(rb"\s*\}\s*\]\s*$", b"", text)
        try:
            obj = json.loads(b"{" + text + b"}")
        except ValueError:
            continue
        if obj.get("organisasjonsnummer") == org:
            found[org] = json.dumps({"rollegrupper": obj.get("rollegrupper") or []}, ensure_ascii=False).encode()


def _addr(row: dict[str, str], prefix: str) -> dict:
    out = {}
    for k in ("adresse", "postnummer", "poststed", "kommune", "kommunenummer", "land"):
        v = (row.get(f"{prefix}.{k}") or "").strip()
        if v:
            # the CSV keeps address lines on separate lines; the API returns them as a list
            out[k] = [a.strip() for a in v.splitlines() if a.strip()] if k == "adresse" else v
    return out


def _subunit_obj(row: dict[str, str]) -> dict:
    emp = (row.get("antallAnsatte") or "").strip()
    obj = {
        "organisasjonsnummer": row.get("organisasjonsnummer"),
        "navn": row.get("navn"),
        "beliggenhetsadresse": _addr(row, "beliggenhetsadresse"),
        "postadresse": _addr(row, "postadresse"),
        "naeringskode1": {"beskrivelse": row.get("naeringskode1.beskrivelse")} if row.get("naeringskode1.beskrivelse") else None,
        "antallAnsatte": int(emp) if emp.isdigit() else None,
        "harRegistrertAntallAnsatte": (row.get("harRegistrertAntallAnsatte") or "").lower() == "true",
        "hjemmeside": row.get("hjemmeside") or None,
        "oppstartsdato": row.get("oppstartsdato") or None,
        "nedleggelsesdato": row.get("nedleggelsesdato") or None,
        "slettedato": row.get("slettedato") or None,
    }
    return {k: v for k, v in obj.items() if v not in (None, "", {})}


def parse_subunits(data: bytes, wanted: set[str], min_rows: int | None = None) -> dict[str, bytes] | None:
    text = io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(data)), encoding="utf-8", newline="")
    csv.field_size_limit(10**8)
    units: dict[str, list[dict]] = {}
    rows = 0
    reader = csv.DictReader(text)
    if not reader.fieldnames or "overordnetEnhet" not in reader.fieldnames:
        return None
    for row in reader:
        rows += 1
        parent = row.get("overordnetEnhet", "")
        if parent in wanted:
            units.setdefault(parent, []).append(_subunit_obj(row))
    if rows < (MIN_SUBUNIT_ROWS if min_rows is None else min_rows):
        return None
    return {org: json.dumps({"_embedded": {"underenheter": u}}, ensure_ascii=False).encode() for org, u in units.items()}


def _download(fetcher: Fetcher, url: str, phase: str, local: str | None) -> FetchResult | None:
    if local:
        from .http import utcnow
        with open(local, "rb") as fh:
            content = fh.read()
        return FetchResult(url, url, 200, utcnow(), content, "application/gzip",
                           headers={"content-disposition": f'attachment; filename="{local.replace(chr(92), "/").split("/")[-1]}"'})
    res = fetcher.get(url, phase, essential=True, max_bytes=MAX_BULK_BYTES, timeout=120.0)
    return res if res.ok and len(res.content) < MAX_BULK_BYTES else None


def _source(res: FetchResult, records: dict[str, bytes]) -> BulkSource:
    cd = res.headers.get("content-disposition", "")
    m = re.search(r'filename="?([^";]+)', cd)
    return BulkSource(res.url, m.group(1) if m else "", res.retrieved_at, res.content_sha256, records)


def load_roles(fetcher: Fetcher, wanted: Iterable[str], local: str | None = None) -> BulkSource | None:
    res = _download(fetcher, ROLES_BULK_URL, "registry.bulk", local)
    if res is None:
        return None
    try:
        records = parse_roles(res.content, set(wanted))
    except (zlib.error, ValueError, MemoryError):
        return None
    return _source(res, records) if records else None


def load_subunits(fetcher: Fetcher, wanted: Iterable[str], local: str | None = None) -> BulkSource | None:
    res = _download(fetcher, SUBUNITS_BULK_URL, "registry.bulk", local)
    if res is None:
        return None
    try:
        records = parse_subunits(res.content, set(wanted))
    except (zlib.error, OSError, EOFError, ValueError, csv.Error, UnicodeDecodeError):
        return None
    return None if records is None else _source(res, records)
