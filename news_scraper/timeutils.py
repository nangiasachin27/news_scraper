"""IST time handling: trading-day logic, scrape windows and exchange timestamp parsing.

All timestamps leaving this module are timezone-aware IST. Exchange APIs return
naive local strings; they are IST by definition.

Windows note: ``zoneinfo`` needs the ``tzdata`` pip package on Windows.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_EXCHANGE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d %b %Y %H:%M:%S",
    "%d-%b-%Y",
    "%d-%m-%Y",
    "%Y-%m-%d",
)


def load_holidays(path: Path | None) -> frozenset[date]:
    """One ISO date per line; '#' starts a comment. Missing file -> weekends only."""
    if not path or not Path(path).exists():
        return frozenset()
    days: set[date] = set()
    for line_no, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            days.add(date.fromisoformat(line))
        except ValueError:
            log.warning("holidays file %s line %d: cannot parse %r", path, line_no, line)
    return frozenset(days)


def is_trading_day(d: date, holidays: frozenset[date] = frozenset()) -> bool:
    return d.weekday() < 5 and d not in holidays


def previous_trading_day(d: date, holidays: frozenset[date] = frozenset()) -> date:
    cur = d - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cur, holidays):
            return cur
        cur -= timedelta(days=1)
    raise ValueError(f"no trading day found in the 30 days before {d}")


def compute_window(
    d: date,
    mode: str = "session",
    session_close: str = "15:30",
    holidays: frozenset[date] = frozenset(),
    tz: ZoneInfo = IST,
) -> tuple[datetime, datetime]:
    """Half-open [start, end) window for date ``d``."""
    if mode == "calendar":
        start = datetime.combine(d, time(0, 0), tz)
        return start, start + timedelta(days=1)
    if mode == "session":
        hh, mm = (int(x) for x in session_close.split(":"))
        close = time(hh, mm)
        start = datetime.combine(previous_trading_day(d, holidays), close, tz)
        end = datetime.combine(d, close, tz)
        return start, end
    raise ValueError(f"unknown window mode {mode!r} (expected 'session' or 'calendar')")


def parse_exchange_dt(value: object, tz: ZoneInfo = IST) -> datetime | None:
    if value in (None, "", "-"):
        return None
    s = str(value).strip()
    for fmt in _EXCHANGE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        log.debug("unparseable exchange timestamp %r", s)
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def from_struct_time(st) -> datetime:
    """feedparser's *_parsed fields are UTC struct_time."""
    return datetime(*st[:6], tzinfo=timezone.utc).astimezone(IST)


def now_ist() -> datetime:
    return datetime.now(IST)


# ---- timestamp precision --------------------------------------------------------
# Google News gives older items a date only, rendered as a fixed hour in UTC
# (07:00:00 or 08:00:00 GMT, i.e. 12:30 / 13:30 IST). Treating that as a real time
# puts news published after the close into the session it came after: lookahead.

def is_date_only(dt: datetime) -> bool:
    """True when a timestamp is a whole hour in UTC: how date-only feed items look.
    A genuine item published exactly on the hour is misread as date-only, which only
    delays it to the end of its day (conservative, never lookahead)."""
    u = dt.astimezone(timezone.utc)
    return u.minute == 0 and u.second == 0 and u.microsecond == 0


def end_of_local_day(dt: datetime, tz: ZoneInfo = IST) -> datetime:
    """Latest moment a date-only item could have been published (23:59:59 local).
    Even if the feed's date is in UTC, the true time is at most 05:30 IST the next
    morning, still before any trading decision that day."""
    return datetime.combine(dt.astimezone(tz).date(), time(23, 59, 59), tz)


def known_at(published_at: datetime, precision: str | None) -> datetime:
    """When an item can be assumed public: its time if exact, else end of its day."""
    return end_of_local_day(published_at) if precision == "date" else published_at
