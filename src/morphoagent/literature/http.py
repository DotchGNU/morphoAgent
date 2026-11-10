"""HTTPS GET with per-service rate limits, retries, timeouts and an on-disk response cache.

Only the standard library is used. Each request is identified by its service,
endpoint and query parameters *without* credentials (``tool``, ``email``,
``api_key``), so cache files never contain an e-mail address or a key and the
same cache answers requests made with or without them.

Cache entry (one JSON file per request)::

    {"schema": "morphoagent-http-cache/1", "service": "ncbi", "endpoint": "esummary.fcgi",
     "params": {...}, "status": 200, "content_type": "...", "fetched_utc": "...", "body": "..."}

Responses with status 200 and 404 are cached; other statuses are not.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__

LOG = logging.getLogger("morphoagent")

CACHE_SCHEMA = "morphoagent-http-cache/1"
#: Query parameters that identify the caller, not the request; excluded from cache keys and files.
CREDENTIAL_PARAMS = frozenset({"tool", "email", "api_key"})
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
CACHED_STATUS = frozenset({200, 404})
MAX_RETRY_AFTER_S = 30.0


class LiteratureError(RuntimeError):
    """A literature request failed (network, service error, unparsable response, offline cache miss)."""


class NotFoundError(LiteratureError):
    """The requested record does not exist or has no such content."""


@dataclass
class Response:
    """A response body with its status; ``from_cache`` tells whether the network was used."""

    status: int
    body: str
    content_type: str = ""
    from_cache: bool = False


#: ``opener(url, headers, timeout_s) -> (status, body_bytes, content_type, headers)``; raises
#: :class:`urllib.error.URLError` / :class:`TimeoutError` on transport failures.
Opener = Callable[[str, Mapping[str, str], float], tuple[int, bytes, str, Mapping[str, str]]]


def default_cache_dir() -> Path:
    """User cache folder for literature responses (outside any repository).

    ``%LOCALAPPDATA%\\morphoagent\\cache\\literature`` on Windows,
    ``~/Library/Caches/morphoagent/literature`` on macOS, otherwise
    ``$XDG_CACHE_HOME/morphoagent/literature`` (default ``~/.cache``).
    """
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "morphoagent" / "cache" / "literature"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "morphoagent" / "literature"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "morphoagent" / "literature"


def urllib_opener(url: str, headers: Mapping[str, str], timeout_s: float) -> tuple[int, bytes, str, Mapping[str, str]]:
    """Default opener: :func:`urllib.request.urlopen`; HTTP error statuses are returned, not raised."""
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", ""), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return exc.code, body, exc.headers.get("Content-Type", "") if exc.headers else "", dict(exc.headers or {})


class RateLimiter:
    """Minimum interval between the starts of consecutive requests (thread-safe).

    Parameters
    ----------
    requests_per_second : float
        Upper bound of the request rate (requests / s).
    clock, sleep : callable
        Injected for tests (``time.monotonic`` and ``time.sleep`` by default).
    """

    def __init__(self, requests_per_second: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be > 0")
        self.interval_s = 1.0 / requests_per_second
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until the next request may start."""
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self.interval_s


class CachedHttp:
    """GET requests to named services with rate limits, retries and a response cache.

    Parameters
    ----------
    services : mapping
        ``name -> (base_url, requests_per_second)``; base URLs must use https.
    credentials : mapping
        ``name -> {param: value}`` added to every request of that service (never cached).
    cache_dir : Path or None
        Response cache folder; ``None`` disables the cache.
    cache_ttl_days : float
        Maximum age of a reused cache entry (days); ignored when ``offline``.
    offline : bool
        Answer from the cache only; a miss raises :class:`LiteratureError`.
    timeout_s : float
        Per-request timeout (s).
    max_retries : int
        Retries after HTTP 429 / 5xx, timeouts and connection errors.
    backoff_s : float
        Wait ``backoff_s * 2**attempt`` s before retry ``attempt + 1`` (or the
        ``Retry-After`` header, at most 30 s).
    opener, sleep, clock : callable
        Injected for tests.
    """

    def __init__(
        self,
        services: Mapping[str, tuple[str, float]],
        credentials: Mapping[str, Mapping[str, str]] | None = None,
        cache_dir: Path | None = None,
        cache_ttl_days: float = 30.0,
        offline: bool = False,
        timeout_s: float = 20.0,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        opener: Opener | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for name, (base, _rps) in services.items():
            if not base.startswith("https://"):
                raise ValueError(f"service {name!r}: base URL must use https, got {base!r}")
        self.services = {k: v[0].rstrip("/") for k, v in services.items()}
        self.limiters = {k: RateLimiter(v[1], clock=clock, sleep=sleep) for k, v in services.items()}
        self.credentials = {k: dict(v) for k, v in (credentials or {}).items()}
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.cache_ttl_s = cache_ttl_days * 86400.0
        self.offline = offline
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.opener: Opener = opener or urllib_opener
        self._sleep = sleep
        self.n_network = 0
        self.n_cache_hits = 0

    # ------------------------------------------------------------------ cache
    @staticmethod
    def cache_key(service: str, endpoint: str, params: Mapping[str, Any]) -> str:
        """SHA-256 of the request without credential parameters."""
        clean = {k: str(v) for k, v in sorted(params.items()) if k not in CREDENTIAL_PARAMS}
        text = json.dumps([service, endpoint, clean], sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def cache_path(self, service: str, endpoint: str, params: Mapping[str, Any]) -> Path | None:
        """Cache file of a request (``<service>_<endpoint>_<16 hex>.json``), or ``None`` without a cache."""
        if self.cache_dir is None:
            return None
        stem = endpoint.replace(".fcgi", "").replace("/", "-").strip("-") or "root"
        return self.cache_dir / f"{service}_{stem}_{self.cache_key(service, endpoint, params)[:16]}.json"

    def _read_cache(self, path: Path | None) -> Response | None:
        if path is None or not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            LOG.warning("ignoring unreadable cache entry %s", path.name)
            return None
        if entry.get("schema") != CACHE_SCHEMA:
            return None
        if not self.offline:
            try:
                fetched = _dt.datetime.fromisoformat(entry["fetched_utc"])
            except (KeyError, ValueError):
                return None
            age = (_dt.datetime.now(_dt.timezone.utc) - fetched).total_seconds()
            if age > self.cache_ttl_s:
                return None
        return Response(int(entry["status"]), entry["body"], entry.get("content_type", ""), from_cache=True)

    def _write_cache(self, path: Path | None, service: str, endpoint: str, params: Mapping[str, Any],
                     resp: Response) -> None:
        if path is None or resp.status not in CACHED_STATUS:
            return
        entry = {
            "schema": CACHE_SCHEMA,
            "service": service,
            "endpoint": endpoint,
            "params": {k: str(v) for k, v in sorted(params.items()) if k not in CREDENTIAL_PARAMS},
            "status": resp.status,
            "content_type": resp.content_type,
            "fetched_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "body": resp.body,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp{os.getpid()}_{threading.get_ident()}")
            tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            LOG.warning("could not write cache entry %s: %s", path, exc)

    # ---------------------------------------------------------------- request
    def get(self, service: str, endpoint: str, params: Mapping[str, Any] | None = None,
            retry_on: Callable[[Response], bool] | None = None) -> Response:
        """GET ``<base>/<endpoint>?<params>`` of ``service``.

        Parameters
        ----------
        service : str
            Name given in ``services``.
        endpoint : str
            Path below the base URL.
        params : mapping, optional
            Query parameters (credentials are added automatically).
        retry_on : callable, optional
            Extra retry condition on a 200 response (e.g. a rate-limit message in the body).

        Returns
        -------
        Response
            The final response; statuses other than 200 are returned, not raised.

        Raises
        ------
        LiteratureError
            Transport failure after all retries, or a cache miss in offline mode.
        """
        if service not in self.services:
            raise ValueError(f"unknown service {service!r}")
        params = dict(params or {})
        path = self.cache_path(service, endpoint, params)
        cached = self._read_cache(path)
        if cached is not None:
            self.n_cache_hits += 1
            return cached
        if self.offline:
            raise LiteratureError(f"offline mode: no cached response for {service} {endpoint} {params}")
        query = {**params, **self.credentials.get(service, {})}
        url = f"{self.services[service]}/{endpoint.lstrip('/')}?{urllib.parse.urlencode(query)}"
        headers = {"User-Agent": f"morphoagent/{__version__}", "Accept": "*/*"}
        last_error = ""
        for attempt in range(self.max_retries + 1):
            self.limiters[service].wait()
            retry_after: float | None = None
            try:
                status, raw, ctype, rheaders = self.opener(url, headers, self.timeout_s)
                self.n_network += 1
                resp = Response(status, raw.decode("utf-8", errors="replace"), ctype)
                if status == 200 and not (retry_on and retry_on(resp)):
                    self._write_cache(path, service, endpoint, params, resp)
                    return resp
                if status not in RETRY_STATUS and status != 200:
                    self._write_cache(path, service, endpoint, params, resp)
                    return resp
                last_error = f"HTTP {status}"
                retry_after = _retry_after(rheaders)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"
                resp = None
            if attempt < self.max_retries:
                wait = retry_after if retry_after is not None else self.backoff_s * (2**attempt)
                LOG.info("%s %s: %s; retry %d/%d in %.1f s", service, endpoint, last_error, attempt + 1,
                         self.max_retries, wait)
                self._sleep(wait)
            elif resp is not None:
                return resp
        raise LiteratureError(f"{service} {endpoint} failed after {self.max_retries + 1} attempt(s): {last_error}")


def _retry_after(headers: Mapping[str, str]) -> float | None:
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                return min(max(float(v), 0.0), MAX_RETRY_AFTER_S)
            except ValueError:
                return None
    return None
