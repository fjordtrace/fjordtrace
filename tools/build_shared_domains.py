"""Build src/signalpost/data/shared_domains.json.gz from a Brønnøysund bulk entity CSV.

Fallback for runs without a bulk snapshot: domains that more than one organisation lists as its
website or e-mail domain (managers, accountants, groups). Usage:
    python tools/build_shared_domains.py <brreg-enheter.csv[.gz]>
"""
from __future__ import annotations

import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from signalpost.registry import BulkSnapshot  # noqa: E402

snap = BulkSnapshot(sys.argv[1], [])
out = {
    "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "source_sha256": snap.file_sha256,
    "rows_scanned": snap.rows_scanned,
    "email": {d: n for d, n in snap.email_domain_count.items() if n >= 2},
    "site": {d: n for d, n in snap.site_domain_count.items() if n >= 2},
}
dest = Path(__file__).resolve().parents[1] / "src" / "signalpost" / "data" / "shared_domains.json.gz"
with gzip.open(dest, "wt", encoding="utf-8") as fh:
    json.dump(out, fh, separators=(",", ":"), sort_keys=True)
print(dest, len(out["email"]), len(out["site"]), dest.stat().st_size)
