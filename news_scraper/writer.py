"""Output layout: <output_dir>/<YYYY-MM-DD>/<filename_format>.json

Writes are atomic (temp file + os.replace), so a downstream reader never sees a
half-written file even if a run is killed. ``os.replace`` is atomic on NTFS too.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .models import TickerInfo
from .timeutils import normalize_article_time

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
