"""HTTP client: per-host rate limiting, retries, thread-local sessions, NSE cookie warmup.

Why curl_cffi
-------------
nseindia.com and api.bseindia.com sit behind Akamai bot protection, which
fingerprints the TLS handshake (JA3/JA4). Python ``requests`` has a recognisably
non-browser fingerprint and gets 403 regardless of headers. ``curl_cffi`` replays
Chrome's TLS fingerprint, so it is the default backend when installed; ``requests``
remains the fallback (fine for Google News and publisher RSS).

With impersonation the User-Agent and sec-ch-* headers come from the impersonated
browser profile. They are deliberately NOT overridden: a UA that disagrees with the
TLS fingerprint is itself a bot signal.

NSE's /api endpoints also need the cookies set by loading an HTML page first, and
answer 401/403 when those expire, so ``nse_get_json`` warms up and retries once.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from .config import Settings

log = logging.getLogger(__name__)

try:  # optional, strongly recommended
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover - depends on environment
    cffi_requests = None

NSE_HOME = "https://www.nseindia.com/"
# Tried in order if the homepage itself is blocked; any page that sets the cookies works.
NSE_WARMUP_PAGES = (
    NSE_HOME,
    "https://www.nseindia.com/market-data/live-equity-market",
    "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
)
BSE_HOME = "https://www.bseindia.com/"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
JSON_ACCEPT = "application/json, text/plain, */*"


def status_of(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) if resp is not None else None


class HostRateLimiter:
    """Minimum spacing between requests to the same host, shared across threads."""

    def __init__(self, intervals: dict[str, float], default: float) -> None:
        self._intervals = {k.lower(): v for k, v in intervals.items()}
        self._default = default
        self._last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        interval = self._intervals.get(host, self._default)
        if interval <= 0:
            return
        with self._guard:
            lock = self._locks.setdefault(host, threading.Lock())
        with lock:
            delay = self._last.get(host, 0.0) + interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last[host] = time.monotonic()


class HttpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.limiter = HostRateLimiter(settings.host_min_interval, settings.default_min_interval)
        self._local = threading.local()
        wanted = settings.http_backend
        if wanted == "curl_cffi" and cffi_requests is None:
            raise RuntimeError("http_backend='curl_cffi' but curl_cffi is not installed (pip install curl_cffi)")
        self.backend = "curl_cffi" if (wanted in ("auto", "curl_cffi") and cffi_requests is not None) else "requests"
        if self.backend == "requests":
            log.warning(
                "using plain `requests`: NSE/BSE will likely return 403 (Akamai TLS fingerprinting). "
                "Install curl_cffi to fix."
            )

    # ---- session management ------------------------------------------------
    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            if self.backend == "curl_cffi":
                session = cffi_requests.Session(impersonate=self.settings.impersonate)
                session.headers.update({"Accept-Language": "en-US,en;q=0.9,hi;q=0.8"})
            else:
                session = requests.Session()
                session.headers.update(
                    {
                        "User-Agent": self.settings.user_agent,
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept": "*/*",
                    }
                )
            self._local.session = session
            self._local.nse_ready = False
        return session

    # ---- generic -----------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ):
        """GET with retries on 429/5xx and network errors (never on other 4xx)."""
        attempts = self.settings.max_retries + 1
        for attempt in range(1, attempts + 1):
            self.limiter.wait(url)
            try:
                resp = self._session().get(
                    url, params=params, headers=headers, timeout=self.settings.request_timeout, stream=stream
                )
                resp.raise_for_status()
                return resp
            except Exception as exc:
                status = status_of(exc)
                transient = status in RETRY_STATUSES or status is None
                if not transient or attempt == attempts:
                    raise
                retry_after = None
                if status == 429:
                    try:
                        retry_after = float(exc.response.headers.get("Retry-After", ""))
                    except (TypeError, ValueError):
                        retry_after = None
                delay = retry_after if retry_after is not None else 2 ** (attempt - 1)
                log.debug("GET %s failed (%s); retry %d/%d in %.1fs", url, status or exc, attempt, attempts - 1, delay)
                time.sleep(min(delay, 30))
        raise AssertionError("unreachable")

    def get_json(self, url: str, *, params=None, headers=None) -> Any:
        resp = self.get(url, params=params, headers=headers)
        body = resp.content.strip()
        if not body:
            raise ValueError(f"empty response body from {urlsplit(url).netloc}")
        if body[:1] == b"<":
            raise ValueError(f"HTML instead of JSON from {urlsplit(url).netloc} (likely a bot-check page)")
        return resp.json()

    # ---- NSE ---------------------------------------------------------------
    def _nse_warmup(self, force: bool = False) -> None:
        session = self._session()
        if self._local.nse_ready and not force:
            return
        if force:
            session.cookies.clear()
        last_exc: Exception | None = None
        for page in NSE_WARMUP_PAGES:
            try:
                self.get(page, headers={"Accept": HTML_ACCEPT, "Referer": NSE_HOME})
                self._local.nse_ready = True
                log.debug("NSE session warmed via %s (backend=%s)", page, self.backend)
                return
            except Exception as exc:
                last_exc = exc
                log.debug("NSE warmup via %s failed: %s", page, exc)
        raise RuntimeError(f"NSE warmup blocked on all pages (backend={self.backend}): {last_exc}") from last_exc

    def nse_get_json(self, url: str, params: dict[str, Any] | None = None, referer: str | None = None) -> Any:
        self._nse_warmup()
        headers = {"Referer": referer or NSE_HOME, "Accept": JSON_ACCEPT, "X-Requested-With": "XMLHttpRequest"}
        try:
            return self.get_json(url, params=params, headers=headers)
        except Exception as exc:
            if status_of(exc) not in (401, 403):
                raise
            log.info("NSE returned %s; refreshing cookies and retrying once", status_of(exc))
            self._nse_warmup(force=True)
            return self.get_json(url, params=params, headers=headers)

    # ---- BSE ---------------------------------------------------------------
    def bse_get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        headers = {"Referer": BSE_HOME, "Origin": BSE_HOME.rstrip("/"), "Accept": JSON_ACCEPT}
        return self.get_json(url, params=params, headers=headers)
