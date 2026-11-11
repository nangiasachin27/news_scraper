"""Output layout: <output_dir>/<YYYY-MM-DD>/<filename_format>.json (+ optional <name>.csv)

Writes are atomic (temp file + os.replace), so a downstream reader never sees a
half-written file even if a run is killed. ``os.replace`` is atomic on NTFS too.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .models import TickerInfo
from .timeutils import is_date_only, normalize_article_time

log = logging.getLogger(__name__)

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def output_path(settings: Settings, d: date, ticker: TickerInfo) -> Path:
    name = settings.filename_format.format(
        symbol=ticker.symbol, bse_code=ticker.bse_code or "", isin=ticker.isin or ""
    )
    return Path(settings.output_dir) / d.isoformat() / _ILLEGAL_FILENAME_CHARS.sub("_", name)


def merge_with_existing(
    path: Path, new_articles: list[dict[str, Any]], date_only_policy: str = "end_of_day"
) -> list[dict[str, Any]]:
    """Union by id. New version of an item wins, but keeps the original first_seen_at.

    Items read back from the file are first brought up to the current timestamp rules
    (``normalize_article_time``): files written by older code may hold date-only items
    at a placeholder time inside this window. Call ``trim_to_window`` on the result so
    items that now belong to another date are not kept here."""
    if not path.exists():
        return new_articles
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        backup = path.with_name(path.name + ".corrupt")
        log.warning("existing %s unreadable (%s); moved to %s", path, exc, backup.name)
        os.replace(path, backup)
        return new_articles
    merged: dict[str, dict[str, Any]] = {}
    for a in existing.get("articles", []):
        if "id" not in a:
            continue
        normalize_article_time(a, date_only_policy)
        merged[a["id"]] = a
    for art in new_articles:
        old = merged.get(art["id"])
        if old:
            if old.get("first_seen_at"):
                art["first_seen_at"] = old["first_seen_at"]
            # A failed fetch this run must not erase text a previous run obtained.
            for key in ("summary", "body"):
                if not art.get(key) and old.get(key):
                    art[key] = old[key]
        merged[art["id"]] = art
    return list(merged.values())


def trim_to_window(
    articles: list[dict[str, Any]], start: datetime, end: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into (inside [start, end), outside). A merged file must only hold items its
    window owns: sources trim new items, but items carried over from an earlier run may
    have moved (timestamp rules fixed, holiday calendar added, window mode changed)."""
    kept, dropped = [], []
    for a in articles:
        try:
            inside = start <= datetime.fromisoformat(a["published_at"]) < end
        except (KeyError, TypeError, ValueError):
            inside = False
        (kept if inside else dropped).append(a)
    return kept, dropped


def _load_existing(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}  # merge_with_existing handles and backs up the corrupt file
    return {a["id"]: a for a in data.get("articles", []) if "id" in a}


def reuse_enrichment(path: Path, articles: list) -> int:
    """Copy summary/body/decoded URL from an existing file onto matching new Article
    objects, so re-runs skip re-downloading. Returns how many were reused."""
    existing = _load_existing(path)
    reused = 0
    for a in articles:
        old = existing.get(a.id)
        if not old or a.body:
            continue
        if old.get("body") or old.get("summary"):
            a.summary = a.summary or old.get("summary")
            a.body = old.get("body")
            old_meta = old.get("meta") or {}
            if old_meta.get("body_error"):
                a.meta["body_error"] = old_meta["body_error"]  # tried before; enricher skips
            gurl = old_meta.get("google_news_url")
            if gurl and old.get("url"):
                a.meta["google_news_url"] = gurl
                a.url = old["url"]
            reused += 1
    return reused


def sort_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(articles, key=lambda a: datetime.fromisoformat(a["published_at"]), reverse=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ---- CSV ---------------------------------------------------------------------------------
# JSON stays the stored format (re-runs merge into it, the sentiment engine reads it); CSV is
# a flat view of the same articles for spreadsheets and other tools. One row per article.
# Lists are joined with "; ". Nested data (attachments, full meta) stays JSON-only.
ARTICLE_CSV_FIELDS = [
    "date", "symbol", "id", "published_at", "time_precision", "source", "source_type", "publisher",
    "category", "impact_tier", "relevance_score", "tags", "title", "summary", "body", "url",
    "also_reported_by", "matched_terms", "text_source", "attachments", "first_seen_at",
]


def _time_precision(a: dict[str, Any]) -> str:
    flag = (a.get("meta") or {}).get("time_precision")
    if flag:
        return flag
    try:
        if a.get("source_type") == "news" and is_date_only(datetime.fromisoformat(a["published_at"])):
            return "date"
    except (KeyError, TypeError, ValueError):
        pass
    return "exact"


def article_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat CSV rows (ARTICLE_CSV_FIELDS) for one news JSON payload."""
    sym = (payload.get("ticker") or {}).get("symbol", "")
    join = lambda v: "; ".join(str(x) for x in v or [])  # noqa: E731
    rows = []
    for a in payload.get("articles", []):
        meta = a.get("meta") or {}
        rows.append({
            "date": payload.get("date", ""), "symbol": sym, "id": a.get("id", ""),
            "published_at": a.get("published_at", ""), "time_precision": _time_precision(a),
            "source": a.get("source", ""), "source_type": a.get("source_type", ""),
            "publisher": a.get("publisher") or "", "category": a.get("category") or "",
            "impact_tier": a.get("impact_tier") or "", "relevance_score": a.get("relevance_score", ""),
            "tags": join(a.get("tags")), "title": a.get("title") or "", "summary": a.get("summary") or "",
            "body": a.get("body") or "", "url": a.get("url") or "",
            "also_reported_by": join(a.get("also_reported_by")), "matched_terms": join(a.get("matched_terms")),
            "text_source": meta.get("text_source") or "",
            "attachments": join(x.get("url") for x in a.get("attachments") or [] if isinstance(x, dict)),
            "first_seen_at": a.get("first_seen_at") or "",
        })
    return rows


def write_csv_rows(path: Path, rows: list[dict[str, Any]], fields: list[str] = ARTICLE_CSV_FIELDS) -> None:
    """Atomic write, UTF-8 with BOM so Excel shows Hindi/rupee characters correctly."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(buf.getvalue())
    os.replace(tmp, path)


def csv_path_for(json_path: Path) -> Path:
    return json_path.with_suffix(".csv")
