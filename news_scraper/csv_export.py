"""CSV views of the stored news JSON.

  python -m news_scraper export-csv --start 2026-11-02 --end 2026-11-06            # per ticker + _articles.csv per date
  python -m news_scraper export-csv --start 2026-01-01 --end 2026-09-22 --out all.csv --tickers TCS INFY

Per date folder it writes <SYMBOL>.csv next to each <SYMBOL>.json (only when missing or
older than the JSON, unless --force) and _articles.csv with every ticker's rows. With
--out it writes one CSV for the whole range instead and leaves the folders alone.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from .writer import article_rows, csv_path_for, write_csv_rows

log = logging.getLogger(__name__)

COMBINED_NAME = "_articles.csv"


def _json_files(folder: Path, symbols: set[str] | None) -> list[Path]:
    files = sorted(p for p in folder.glob("*.json") if not p.name.startswith("_"))
    return [p for p in files if symbols is None or p.stem.upper() in symbols]


def _dates(news_dir: Path, start: date, end: date) -> Iterable[tuple[date, Path]]:
    d = start
    while d <= end:
        folder = Path(news_dir) / d.isoformat()
        if folder.is_dir():
            yield d, folder
        d += timedelta(days=1)


def _rows(path: Path) -> list[dict]:
    try:
        return article_rows(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        log.warning("skipping %s: %s", path, exc)
        return []


def export(news_dir: Path, start: date, end: date, symbols: Iterable[str] | None = None, *,
           per_ticker: bool = True, combined: bool = True, force: bool = False, out: Path | None = None) -> dict:
    """Returns counts: dates, files, rows, written."""
    wanted = {s.upper().removesuffix(".NS") for s in symbols} if symbols else None
    stats = {"dates": 0, "files": 0, "rows": 0, "written": 0}
    everything: list[dict] = []
    for _, folder in _dates(news_dir, start, end):
        files = _json_files(folder, wanted)
        if not files:
            continue
        stats["dates"] += 1
        day_rows: list[dict] = []
        for f in files:
            rows = _rows(f)
            stats["files"] += 1
            stats["rows"] += len(rows)
            day_rows.extend(rows)
            if out is None and per_ticker:
                target = csv_path_for(f)
                if force or not target.exists() or target.stat().st_mtime < f.stat().st_mtime:
                    write_csv_rows(target, rows)
                    stats["written"] += 1
        if out is not None:
            everything.extend(day_rows)
        elif combined:
            # The combined file always covers every ticker in the folder, filter or not.
            all_rows = day_rows if wanted is None else [r for f in _json_files(folder, None) for r in _rows(f)]
            write_csv_rows(folder / COMBINED_NAME, all_rows)
            stats["written"] += 1
    if out is not None:
        write_csv_rows(Path(out), everything)
        stats["written"] += 1
    return stats
