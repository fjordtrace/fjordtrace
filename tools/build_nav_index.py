"""Build the declared NAV job-feed cache shipped as ``src/signalpost/data/nav_index.json.gz``.

    python -m uv run python tools/build_nav_index.py              # full build (last ~6 months of the feed)
    python -m uv run python tools/build_nav_index.py --update     # continue from the shipped index's cursor

Steps (all resumable; caches live in ``--work-dir``):
1. Crawl the feed from ``now - --days`` (an ad is never active for more than 6 months, per NAV's docs) to the end,
   in parallel daily segments found with ``If-Modified-Since``. Full pages are cached on disk.
2. For every ad whose latest feed item is ACTIVE, fetch the entry (``/api/v1/feedentry/{uuid}``) to get the employer
   orgnr. Entries are appended to ``entries.jsonl`` so a restart skips what is done.
3. Map ad orgnrs that are Brønnøysund subunits to their parent (``overordnetEnhet``) from the subunit bulk file.
4. Write the index: ``by_org`` (orgnr -> active ads, no contact persons), ``parent_of``, ``feed_cursor``, stats.

Token: ``SIGNALPOST_NAV_TOKEN`` or NAV's public token. Terms: https://arbeidsplassen.nav.no/vilkar-api.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from signalpost.connectors.jobs_nav import (  # noqa: E402
    ENTRY_URL, FEED_URL, INDEX_PATH, TERMS_URL, TOKEN_URL, compact_ad, crawl_feed, is_expired, parse_time, parse_token,
)
from signalpost.http import USER_AGENT  # noqa: E402

SUBUNITS_BULK = "https://data.brreg.no/enhetsregisteret/api/underenheter/lastned/csv"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Client:
    def __init__(self, work: Path):
        self.work = work
        self.pages_dir = work / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        self.s.mount("https://", adapter)
        self.s.headers["User-Agent"] = USER_AGENT + " nav-index-builder"
        self.token = os.environ.get("SIGNALPOST_NAV_TOKEN") or self.public_token()
        self.lock = threading.Lock()
        self.requests = 0

    def public_token(self) -> str:
        r = self.s.get(TOKEN_URL, timeout=30)
        r.raise_for_status()
        tok = parse_token(r.text)
        if not tok:
            raise SystemExit("could not parse NAV public token")
        return tok

    def get_json(self, url: str, headers: dict[str, str]):
        page_id = url.rsplit("/", 1)[-1] if "/api/v1/feed/" in url else None
        cache = self.pages_dir / f"{page_id}.json" if page_id else None
        if cache and cache.exists():
            return 200, json.loads(cache.read_text(encoding="utf-8")), None
        headers = {**headers, "Authorization": f"Bearer {self.token}"}
        err = None
        for attempt in range(5):
            try:
                with self.lock:
                    self.requests += 1
                r = self.s.get(url, headers=headers, timeout=60)
            except requests.RequestException as exc:
                err = f"{type(exc).__name__}: {exc}"
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 401 and not os.environ.get("SIGNALPOST_NAV_TOKEN"):
                self.token = self.public_token()  # rotated
                headers["Authorization"] = f"Bearer {self.token}"
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                err = f"http_{r.status_code}"
                time.sleep(2 ** attempt + 1)
                continue
            if r.status_code != 200:
                return r.status_code, None, f"http_{r.status_code}"
            body = r.json()
            # cache only full, chained pages (the last page keeps growing)
            if cache and isinstance(body, dict) and body.get("next_id"):
                cache.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            return 200, body, None
        return None, None, err


def load_entries(path: Path) -> dict[str, dict | None]:
    done: dict[str, dict | None] = {}
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                done[row["uuid"]] = row.get("ad")
    return done


def fetch_entries(client: Client, uuids: list[str], path: Path, workers: int) -> dict[str, dict | None]:
    done = load_entries(path)
    todo = [u for u in uuids if u not in done]
    log(f"entries: {len(done)} cached, {len(todo)} to fetch")
    lock = threading.Lock()
    t0 = time.monotonic()
    n = [0]
    auth = {"Accept": "application/json"}

    def one(uuid: str) -> None:
        status, body, err = client.get_json(ENTRY_URL.format(uuid=uuid), auth)
        if status == 200 and isinstance(body, dict):
            ad = compact_ad(body)
            row = {"uuid": uuid, "status": body.get("status"), "ad": ad}
        elif status == 404:
            row = {"uuid": uuid, "status": "MISSING", "ad": None}
        else:
            return  # transient: retried on the next run
        with lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            done[uuid] = ad if status == 200 else None
            n[0] += 1
            if n[0] % 1000 == 0:
                rate = n[0] / max(1e-6, time.monotonic() - t0)
                log(f"entries: {n[0]}/{len(todo)} ({rate:.1f}/s, eta {(len(todo) - n[0]) / max(rate, 1e-6) / 60:.0f} min)")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))
    return done


def parent_map(orgnrs: set[str], work: Path, csv_path: str | None) -> dict[str, str]:
    path = Path(csv_path) if csv_path else work / "underenheter.csv.gz"
    if not path.exists():
        log(f"downloading Bronnoysund subunit bulk file -> {path}")
        with requests.get(SUBUNITS_BULK, stream=True, timeout=120, headers={"User-Agent": USER_AGENT}) as r:
            r.raise_for_status()
            tmp = path.with_suffix(".part")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
            os.replace(tmp, path)
    out: dict[str, str] = {}
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
        for row in reader:
            org = row.get("organisasjonsnummer")
            if org in orgnrs and row.get("overordnetEnhet") and not row.get("nedleggelsesdato"):
                out[org] = row["overordnetEnhet"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(INDEX_PATH))
    ap.add_argument("--work-dir", default="out/nav-build")
    ap.add_argument("--days", type=int, default=190, help="feed window for a full build (ads live <= 6 months)")
    ap.add_argument("--update", action="store_true", help="continue from the cursor of the existing --out index")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-pages", type=int, default=5000)
    ap.add_argument("--subunits-csv", default=None, help="Brønnøysund underenheter CSV(.gz); downloaded if absent")
    args = ap.parse_args()

    t_start = time.monotonic()
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    client = Client(work)
    now = datetime.now(timezone.utc)
    old: dict = {}
    if args.update:
        with gzip.open(args.out, "rt", encoding="utf-8") as fh:
            old = json.load(fh)
        cur = old.get("feed_cursor") or {}
        start_time = parse_time(cur.get("item_time")) - timedelta(minutes=1)
        start_page = cur.get("page_id")
        log(f"update from cursor {start_page} @ {cur.get('item_time')}")
    else:
        start_time, start_page = now - timedelta(days=args.days), None
        log(f"full build from {start_time.isoformat()}")

    crawl = crawl_feed(client.get_json, client.token, start_time=start_time, now=now,
                       max_pages=args.max_pages, workers=args.workers, log=log)
    t_crawl = time.monotonic() - t_start
    log(f"crawl: {crawl.pages} pages, {len(crawl.latest)} distinct ads, complete={crawl.complete}, "
        f"{t_crawl:.0f}s, errors={crawl.errors[:3]}")
    if not crawl.pages:
        raise SystemExit("feed crawl returned nothing")

    # current active set
    ads_by_uuid: dict[str, dict] = {}
    if args.update:
        for org, ads in (old.get("by_org") or {}).items():
            for ad in ads:
                ads_by_uuid[ad["uuid"]] = {**ad, "orgnr": org}
    for uuid, head in crawl.latest.items():
        if head.get("status") != "ACTIVE":
            ads_by_uuid.pop(uuid, None)
    active = [u for u, h in crawl.latest.items() if h.get("status") == "ACTIVE"]
    entries_path = work / (f"entries-update-{start_page}.jsonl" if args.update else "entries.jsonl")
    entries = fetch_entries(client, active, entries_path, args.workers)
    no_orgnr = inactive_on_fetch = 0
    for uuid in active:
        if uuid not in entries:
            continue  # transient failure: keep the previous record if any
        ad = entries[uuid]
        if not ad:
            inactive_on_fetch += 1
            ads_by_uuid.pop(uuid, None)
        elif not ad.get("orgnr"):
            no_orgnr += 1
            ads_by_uuid.pop(uuid, None)
        else:
            ads_by_uuid[uuid] = ad
    ads_by_uuid = {u: a for u, a in ads_by_uuid.items() if not is_expired(a, now)}
    t_entries = time.monotonic() - t_start - t_crawl

    by_org: dict[str, list[dict]] = {}
    for ad in ads_by_uuid.values():
        org = ad["orgnr"]
        rec = {k: v for k, v in ad.items() if k != "orgnr"}
        by_org.setdefault(org, []).append(rec)
    for ads in by_org.values():
        ads.sort(key=lambda a: a.get("updated") or "", reverse=True)

    parents = dict(old.get("parent_of") or {})
    missing = {o for o in by_org if o not in parents}
    try:
        parents.update(parent_map(missing, work, args.subunits_csv))
    except Exception as exc:  # the runtime falls back to registry subunits
        log(f"subunit parent map failed: {exc}")
    parents = {s: p for s, p in parents.items() if s in by_org}

    index = {
        "schema": 1,
        "description": "Declared cache of NAV job feed ads that were ACTIVE when built. Every ad is live-verified "
                       "(status ACTIVE, employer orgnr match) before it is published; inactive ads are dropped.",
        "source": FEED_URL,
        "terms": TERMS_URL,
        "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_start": (old.get("window_start") if args.update else start_time.strftime("%Y-%m-%dT%H:%M:%SZ")),
        "feed_cursor": {"page_id": crawl.cursor_page_id, "item_time": crawl.cursor_time, "complete": crawl.complete},
        "stats": {
            "active_ads": sum(len(v) for v in by_org.values()),
            "employer_orgnrs": len(by_org),
            "subunit_orgnrs_mapped": len(parents),
            "active_in_feed_without_orgnr": no_orgnr,
            "inactive_on_entry_fetch": inactive_on_fetch,
            "pages_crawled": crawl.pages,
            "distinct_ads_in_window": len(crawl.latest),
            "requests": client.requests,
            "crawl_seconds": round(t_crawl),
            "entry_seconds": round(t_entries),
            "total_seconds": round(time.monotonic() - t_start),
            "mode": "update" if args.update else "full",
        },
        "parent_of": dict(sorted(parents.items())),
        "by_org": dict(sorted(by_org.items())),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out)
    log(f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB): {json.dumps(index['stats'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
