"""Budgeted, auditable and URL-safe HTTP.

* Every request is charged to the run budget under a phase name.
* Only http/https to public addresses; private, loopback, link-local and reserved targets are refused
  on every redirect hop (outbound URL policy, documented in SECURITY.md).
* Company sites: robots.txt is honoured and each host is rate-limited.
* Every response is hashed and timestamped so it can back an evidence record.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from .budget import Budget

USER_AGENT = "SignalpostResearchAgent/0.2 (+https://builderr.ai/challenges/signalpost)"
MAX_BYTES = 4_000_000
MAX_REDIRECTS = 5


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    retrieved_at: str
    content: bytes = b""
    content_type: str = ""
    redirects: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    error: str | None = None
    state_hint: str | None = None  # "blocked" when refused by robots/budget/policy
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.error is None

    @property
    def content_sha256(self) -> str:
        return sha256(self.content)

    @property
    def text(self) -> str:
        if not self.content:
            return ""
        enc = "utf-8"
        ct = self.content_type.lower()
        if "charset=" in ct:
            enc = ct.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


class UnsafeURL(ValueError):
    pass


_dns_cache: dict[str, bool] = {}
_dns_lock = threading.Lock()


def check_url_safe(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL(f"scheme not allowed: {parts.scheme}")
    host = parts.hostname
    if not host:
        raise UnsafeURL("no host")
    if parts.port not in (None, 80, 443):
        raise UnsafeURL(f"port not allowed: {parts.port}")
    with _dns_lock:
        cached = _dns_cache.get(host)
    if cached is None:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise UnsafeURL(f"dns failure: {exc}") from exc
        cached = all(ipaddress.ip_address(info[4][0]).is_global for info in infos)
        with _dns_lock:
            _dns_cache[host] = cached
    if not cached:
        raise UnsafeURL(f"non-public address for {host}")


class Fetcher:
    def __init__(self, budget: Budget, timeout: float = 12.0, per_host_interval: float = 0.6):
        self.budget = budget
        self.timeout = timeout
        self.per_host_interval = per_host_interval
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=0)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en;q=0.6"})
        self._host_lock = threading.Lock()
        self._host_next: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        self.bytes = 0
        self.count = 0
        self._stats_lock = threading.Lock()

    # ----- politeness -------------------------------------------------
    def _wait_host(self, host: str) -> None:
        with self._host_lock:
            now = time.monotonic()
            slot = max(now, self._host_next.get(host, 0.0))
            self._host_next[host] = slot + self.per_host_interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def robots_allows(self, url: str, phase: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            if origin in self._robots:
                parser = self._robots[origin]
                return True if parser is None else parser.can_fetch(USER_AGENT, url)
        result = self.get(origin + "/robots.txt", phase=phase, check_robots=False, max_bytes=500_000)
        parser: urllib.robotparser.RobotFileParser | None = None
        if result.status == 200 and result.content:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(result.text.splitlines())
        elif result.status in (401, 403):
            parser = urllib.robotparser.RobotFileParser()
            parser.disallow_all = True
        with self._robots_lock:
            self._robots[origin] = parser
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    # ----- core -------------------------------------------------------
    def get(
        self,
        url: str,
        phase: str,
        *,
        essential: bool = False,
        check_robots: bool = False,
        polite: bool = False,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        max_bytes: int = MAX_BYTES,
        timeout: float | None = None,
        method: str = "GET",
        json_body: Any = None,
    ) -> FetchResult:
        retrieved_at = utcnow()
        try:
            check_url_safe(url)
        except UnsafeURL as exc:
            return FetchResult(url, url, None, retrieved_at, error=f"unsafe_url: {exc}", state_hint="blocked")
        if check_robots and not self.robots_allows(url, phase):
            return FetchResult(url, url, None, retrieved_at, error="robots_disallow", state_hint="blocked")
        if not self.budget.take(phase, essential=essential):
            return FetchResult(url, url, None, retrieved_at, error="budget_exhausted", state_hint="blocked")

        started = time.monotonic()
        current = url
        redirects: list[str] = []
        try:
            for _ in range(MAX_REDIRECTS + 1):
                host = urlsplit(current).hostname or ""
                if polite:
                    self._wait_host(host)
                first_hop = current == url
                resp = self.session.request(
                    method if first_hop else "GET", current, headers=headers, params=params if first_hop else None,
                    json=json_body if first_hop else None,
                    timeout=timeout or self.timeout, allow_redirects=False, stream=True,
                )
                if resp.is_redirect and resp.headers.get("location"):
                    nxt = urljoin(current, resp.headers["location"])
                    resp.close()
                    check_url_safe(nxt)
                    redirects.append(current)
                    current = nxt
                    continue
                chunks, size = [], 0
                for chunk in resp.iter_content(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        break
                content = b"".join(chunks)[:max_bytes]
                with self._stats_lock:
                    self.bytes += len(content)
                    self.count += 1
                return FetchResult(
                    url, resp.url or current, resp.status_code, retrieved_at, content,
                    resp.headers.get("content-type", ""), redirects,
                    int((time.monotonic() - started) * 1000),
                    None if resp.status_code < 400 else f"http_{resp.status_code}",
                    "blocked" if resp.status_code in (401, 403, 429, 451) else None,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                )
            return FetchResult(url, current, None, retrieved_at, redirects=redirects, error="too_many_redirects")
        except UnsafeURL as exc:
            return FetchResult(url, current, None, retrieved_at, redirects=redirects, error=f"unsafe_url: {exc}", state_hint="blocked")
        except requests.RequestException as exc:
            return FetchResult(url, current, None, retrieved_at, redirects=redirects,
                               elapsed_ms=int((time.monotonic() - started) * 1000), error=f"{type(exc).__name__}: {exc}"[:300])

