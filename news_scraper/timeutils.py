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
PACIFIC = ZoneInfo("America/Los_Angeles")  # Google News date-only placeholders

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
# Formats that carry no time of day. A value in one of these is a date, not midnight.
_DATE_ONLY_FORMATS = frozenset({"%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"})


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


def parse_exchange_dt_precise(value: object, tz: ZoneInfo = IST) -> tuple[datetime | None, str | None]:
    """Parse an exchange timestamp and say whether it had a time of day.

    Returns ``(dt, "exact")``, ``(dt, "date")`` for a date-only value (dt is midnight;
    callers must not treat that as a real time), or ``(None, None)``."""
    if value in (None, "", "-"):
        return None, None
    s = str(value).strip()
    for fmt in _EXCHANGE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
        return dt, ("date" if fmt in _DATE_ONLY_FORMATS else "exact")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        log.debug("unparseable exchange timestamp %r", s)
        return None, None
    precision = "exact" if ("T" in s or " " in s or ":" in s) else "date"
    return (dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)), precision


def parse_exchange_dt(value: object, tz: ZoneInfo = IST) -> datetime | None:
    """Timestamp only; a date-only value comes back as midnight. Use
    ``exchange_time`` for publication times, which must not treat that as a real time."""
    return parse_exchange_dt_precise(value, tz)[0]


def exchange_time(row: dict, *keys: str) -> tuple[datetime | None, dict]:
    """Publication time of an exchange row from candidate fields, in priority order.

    The first field with a time of day wins. If every present field is date-only, the
    item is placed at 23:59:59 IST of that date (the latest it could have been
    disseminated), so it lands in the NEXT session's window rather than before a close
    it may have followed. Returns ``(dt, meta)``; meta records the field used and, for
    date-only values, ``time_precision="date"`` and the raw value."""
    date_only: tuple[datetime, str, object] | None = None
    for key in keys:
        raw = row.get(key)
        if raw in (None, "", "-", "null"):
            continue
        dt, precision = parse_exchange_dt_precise(raw)
        if dt is None:
            continue
        if precision == "exact":
            return dt, {"time_field": key}
        if date_only is None:
            date_only = (dt, key, raw)
    if date_only is None:
        return None, {}
    dt, key, raw = date_only
    return end_of_local_day(dt), {"time_field": key, "time_precision": "date", "reported_published_at": str(raw)}


def from_struct_time(st) -> datetime:
    """feedparser's *_parsed fields are UTC struct_time."""
    return datetime(*st[:6], tzinfo=timezone.utc).astimezone(IST)


def now_ist() -> datetime:
    return datetime.now(IST)


# ---- timestamp precision --------------------------------------------------------
# Google News gives older items a date only, rendered as midnight US Pacific time:
# 07:00:00 GMT while US daylight saving is on (PDT), 08:00:00 GMT otherwise (PST),
# i.e. 12:30 / 13:30 IST. The date is the Pacific date, so the article may have come
# out at any moment in the following 24 hours - up to 12:29 / 13:29 IST the NEXT day.
# Treating the placeholder as a real time, or as "by 23:59 IST that day", lets an item
# be counted before it existed: lookahead.

DATE_SPAN = timedelta(days=1) - timedelta(seconds=1)
# Old scrapes moved placeholders to 23:59:59 IST of the reported date and kept no
# record of whether it was PDT or PST; the PST bound (13:29:59 IST next day) is the later.
_LEGACY_END_OF_DAY = time(23, 59, 59)
_LEGACY_LATEST = time(13, 29, 59)


def is_date_only(dt: datetime) -> bool:
    """True when a timestamp is a whole hour in UTC: how date-only feed items look.
    A genuine item published exactly on the hour is misread as date-only, which only
    delays it (conservative, never lookahead)."""
    u = dt.astimezone(timezone.utc)
    return u.minute == 0 and u.second == 0 and u.microsecond == 0


def end_of_local_day(dt: datetime, tz: ZoneInfo = IST) -> datetime:
    """23:59:59 local on dt's date: the latest time for a date given in IST
    (exchange date-only fields). NOT a valid bound for Google News dates, which are
    US Pacific dates; use ``latest_bound`` for those."""
    return datetime.combine(dt.astimezone(tz).date(), time(23, 59, 59), tz)


def latest_bound(placeholder: datetime) -> datetime:
    """Latest moment a Google News date-only item could have been published, from its
    placeholder timestamp (midnight US Pacific). Correct under PDT and PST alike, and
    still conservative if the date were an IST or UTC date."""
    return placeholder + DATE_SPAN


def date_only_placeholder(dt: datetime) -> datetime:
    """The placeholder Google News shows for an item at ``dt`` once only its date is
    kept: midnight US Pacific of that Pacific date. Used to simulate how a backfill
    sees live media timestamps (backtest ``degrade_media`` / parity)."""
    return datetime.combine(dt.astimezone(PACIFIC).date(), time(0), PACIFIC).astimezone(IST)


def known_at(published_at: datetime, precision: str | None, source_type: str | None = "news") -> datetime:
    """When an item can be assumed public.

    Exact items: their timestamp. Date-only items: the latest moment they could have
    appeared. Current scrapes already store that bound in ``published_at``; older files
    hold either the raw placeholder or the 23:59:59 IST of the previous rule, and are
    converted here so backtests over old files stay free of lookahead."""
    if precision != "date":
        return published_at
    if source_type not in (None, "news"):
        # exchange date-only fields are IST dates; idempotent on 23:59:59
        return end_of_local_day(published_at)
    if is_date_only(published_at):  # raw placeholder (old "keep" policy / unflagged files)
        return latest_bound(published_at)
    local = published_at.astimezone(IST)
    if local.time() == _LEGACY_END_OF_DAY:  # previous end_of_day rule
        return datetime.combine(local.date() + timedelta(days=1), _LEGACY_LATEST, IST)
    return published_at  # already the latest bound


def is_legacy_exchange_date(dt: datetime, source_type: str | None) -> bool:
    """Exchange items scraped before date-only parsing was fixed carry midnight IST and
    no flag. No filing is disseminated at exactly 00:00:00, so read that as a date.
    Corporate actions are excluded: they are pinned to 09:15 on purpose."""
    if source_type in (None, "news", "corporate_action"):
        return False
    local = dt.astimezone(IST)
    return local.hour == 0 and local.minute == 0 and local.second == 0 and local.microsecond == 0


def normalize_article_time(art: dict, date_only_policy: str = "end_of_day") -> bool:
    """Bring a stored article dict's ``published_at`` in line with the current rules
    (used on items read back from an existing file before merging). Returns True if it
    changed. Idempotent for items written by the current code."""
    pub_raw = art.get("published_at")
    if not pub_raw:
        return False
    try:
        pub = datetime.fromisoformat(pub_raw)
    except ValueError:
        return False
    meta = dict(art["meta"]) if isinstance(art.get("meta"), dict) else {}
    source_type = art.get("source_type")
    new = None
    if source_type == "news":
        if date_only_policy != "end_of_day":
            return False
        if meta.get("time_precision") == "date":
            reported = meta.get("reported_published_at")
            try:
                new = latest_bound(datetime.fromisoformat(reported)) if reported else known_at(pub, "date")
            except ValueError:
                new = known_at(pub, "date")
        elif art.get("source") in ("google_news", "google_news_sites") and is_date_only(pub):
            meta["time_precision"] = "date"
            meta["reported_published_at"] = pub.isoformat()
            new = latest_bound(pub)
    elif meta.get("time_precision") != "date" and is_legacy_exchange_date(pub, source_type):
        meta["time_precision"] = "date"
        meta["reported_published_at"] = pub.date().isoformat()
        new = end_of_local_day(pub)
    if new is None or new == pub:
        return False
    art["meta"] = meta
    art["published_at"] = new.isoformat()
    return True
