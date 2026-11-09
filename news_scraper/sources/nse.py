"""NSE sources: corporate announcements, financial results, board meetings, ex-dates.

Endpoints are the JSON APIs behind nseindia.com's own pages. They are unofficial and
unversioned: field names have changed before, so every lookup goes through
``_first(row, *candidates)`` and a missing timestamp drops the row rather than
raising. If a source starts returning raw_count=0 for liquid names, re-inspect the
endpoint in the browser's network tab first.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from ..models import Article, Attachment, TickerInfo
from ..textutils import strip_html
from ..timeutils import exchange_time, parse_exchange_dt
from .base import Source, register

NSE_API = "https://www.nseindia.com/api"
FILINGS_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
RESULTS_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
BOARD_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings"
ACTIONS_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-actions"


class _TTLCache:
    """Process-wide cache for NSE endpoints that ignore the date (full-history
    responses). A multi-date backfill then fetches them once per ticker instead of
    once per ticker-date. Only successful responses are cached."""

    def __init__(self) -> None:
        self._data: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._key_locks: dict[tuple, threading.Lock] = {}

    def get_or_fetch(self, key: tuple, ttl: float, fetch):
        with self._lock:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            hit = self._data.get(key)
            if hit and ttl > 0 and time.monotonic() - hit[0] < ttl:
                return hit[1]
            value = fetch()
            self._data[key] = (time.monotonic(), value)
            return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_STATIC_CACHE = _TTLCache()


def clear_cache() -> None:
    _STATIC_CACHE.clear()


def _rows(data) -> list[dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("data", "rows", "Table"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
    return []


def _first(row: dict, *keys: str):
    for key in keys:
        val = row.get(key)
        if val not in (None, "", "-", "null"):
            return val
    return None


def _attachment(url) -> Attachment | None:
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return None
    name = url.rsplit("/", 1)[-1]
    kind = name.rsplit(".", 1)[-1].lower() if "." in name else None
    return Attachment(url=url, name=name, kind=kind)


def _ddmmyyyy(dt: datetime) -> str:
    return dt.strftime("%d-%m-%Y")


def _symbol_mismatch(row: dict, ticker: TickerInfo, *keys: str) -> bool:
    sym = _first(row, *keys)
    return bool(sym) and str(sym).upper() != ticker.symbol


class _NSESource(Source):
    requires = ("nse_listed",)


@register
class NSEAnnouncements(_NSESource):
    name = "nse_announcements"

    def fetch(self, ticker, start, end, notes):
        data = self.http.nse_get_json(
            f"{NSE_API}/corporate-announcements",
            params={
                "index": "equities",
                "symbol": ticker.symbol,
                "from_date": _ddmmyyyy(start),
                "to_date": _ddmmyyyy(end),
            },
            referer=FILINGS_PAGE,
        )
        out: list[Article] = []
        for r in _rows(data):
            if _symbol_mismatch(r, ticker, "symbol"):
                continue
            ts, tmeta = exchange_time(r, "sort_date", "an_dt", "exchdisstime")
            title = strip_html(_first(r, "desc"))
            if not ts or not title:
                continue
            att = _attachment(_first(r, "attchmntFile"))
            out.append(
                Article(
                    source=self.name,
                    source_type="exchange_filing",
                    title=title,
                    summary=strip_html(_first(r, "attchmntText")) or None,
                    url=att.url if att else None,
                    published_at=ts,
                    publisher="NSE",
                    attachments=[att] if att else [],
                    meta={
                        "exchange": "NSE",
                        "exchange_id": _first(r, "seq_id"),
                        "industry": _first(r, "smIndustry"),
                        **tmeta,
                    },
                )
            )
        return out


@register
class NSEFinancialResults(_NSESource):
    """Structured results filings. The API returns the full history (no date filter),
    so the window trim in ``Source.run`` does the filtering."""

    name = "nse_financial_results"

    def fetch(self, ticker, start, end, notes):
        out: list[Article] = []
        seen: set[tuple] = set()
        for period in self.settings.nse_result_periods:
            data = _STATIC_CACHE.get_or_fetch(
                ("results", ticker.symbol, period),
                self.settings.nse_static_cache_ttl_seconds,
                lambda: self.http.nse_get_json(
                    f"{NSE_API}/corporates-financial-results",
                    params={"index": "equities", "symbol": ticker.symbol, "period": period},
                    referer=RESULTS_PAGE,
                ),
            )
            for r in _rows(data):
                if _symbol_mismatch(r, ticker, "symbol"):
                    continue
                ts, tmeta = exchange_time(r, "broadCastDate", "exchdisstime", "filingDate")
                if not ts:
                    continue
                consolidated = str(_first(r, "consolidated") or "").strip()
                audited = str(_first(r, "audited") or "").strip()
                seq = _first(r, "seqNumber", "seq_id")
                key = (seq, consolidated, ts)
                if key in seen:
                    continue
                seen.add(key)
                relating = _first(r, "relatingTo")
                to_date = _first(r, "toDate")
                title = " ".join(
                    p
                    for p in (
                        ticker.company_name,
                        consolidated,
                        audited,
                        "financial results",
                        f"({relating})" if relating else "",
                        f"for period ended {to_date}" if to_date else "",
                    )
                    if p
                )
                atts = [a for a in (_attachment(_first(r, "xbrl")), _attachment(_first(r, "attachment"))) if a]
                out.append(
                    Article(
                        source=self.name,
                        source_type="financial_result",
                        title=title,
                        url=atts[0].url if atts else None,
                        published_at=ts,
                        publisher="NSE",
                        category="results",
                        impact_tier="high",
                        attachments=atts,
                        meta={
                            "exchange": "NSE",
                            "exchange_id": f"{seq}|{consolidated}" if seq else None,
                            "period": relating,
                            "period_type": period,
                            "from_date": _first(r, "fromDate"),
                            "to_date": to_date,
                            "financial_year": _first(r, "financialYear"),
                            "consolidated": consolidated or None,
                            "audited": audited or None,
                            **tmeta,
                        },
                    )
                )
        return out


@register
class NSEBoardMeetings(_NSESource):
    """Placed on the window by *announcement* time; the meeting date is in meta.
    The intimation itself is information (it fixes the results date)."""

    name = "nse_board_meetings"

    def fetch(self, ticker, start, end, notes):
        data = _STATIC_CACHE.get_or_fetch(
            ("board", ticker.symbol),
            self.settings.nse_static_cache_ttl_seconds,
            lambda: self.http.nse_get_json(
                f"{NSE_API}/corporate-board-meetings",
                params={"index": "equities", "symbol": ticker.symbol},
                referer=BOARD_PAGE,
            ),
        )
        out: list[Article] = []
        for r in _rows(data):
            if _symbol_mismatch(r, ticker, "bm_symbol", "symbol"):
                continue
            ts, tmeta = exchange_time(r, "bm_timestamp", "attchmntTime")
            if not ts:
                continue
            meeting_date = _first(r, "bm_date")
            purpose = strip_html(_first(r, "bm_purpose"))
            title = f"Board meeting on {meeting_date}" + (f": {purpose}" if purpose else "")
            att = _attachment(_first(r, "attachment"))
            out.append(
                Article(
                    source=self.name,
                    source_type="board_meeting",
                    title=title,
                    summary=strip_html(_first(r, "bm_desc")) or None,
                    url=att.url if att else None,
                    published_at=ts,
                    publisher="NSE",
                    category="board_meeting",
                    impact_tier="medium",
                    attachments=[att] if att else [],
                    meta={"exchange": "NSE", "meeting_date": meeting_date, "purpose": purpose or None, **tmeta},
                )
            )
        return out


@register
class NSECorporateActions(_NSESource):
    """Emits ex-date events only, timestamped at market open (09:15 IST) on the
    ex-date. These are mechanical price adjustments: the sentiment engine should
    NOT read an ex-dividend gap-down as negative news. Tagged accordingly."""

    name = "nse_corporate_actions"

    def fetch(self, ticker, start, end, notes):
        data = self.http.nse_get_json(
            f"{NSE_API}/corporates-corporateActions",
            params={
                "index": "equities",
                "symbol": ticker.symbol,
                "from_date": _ddmmyyyy(start),
                "to_date": _ddmmyyyy(end),
            },
            referer=ACTIONS_PAGE,
        )
        out: list[Article] = []
        for r in _rows(data):
            if _symbol_mismatch(r, ticker, "symbol"):
                continue
            ex = parse_exchange_dt(_first(r, "exDate"))
            subject = strip_html(_first(r, "subject"))
            if not ex or not subject:
                continue
            ex = ex.replace(hour=9, minute=15, second=0, microsecond=0)
            out.append(
                Article(
                    source=self.name,
                    source_type="corporate_action",
                    title=f"Ex-date: {subject}",
                    published_at=ex,
                    publisher="NSE",
                    impact_tier="medium",
                    tags=["mechanical_price_adjustment"],
                    meta={
                        "exchange": "NSE",
                        "date_basis": "ex_date_market_open",
                        "ex_date": _first(r, "exDate"),
                        "record_date": _first(r, "recDate"),
                        "face_value": _first(r, "faceVal"),
                    },
                )
            )
        return out
