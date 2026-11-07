"""Point-in-time daily sentiment CSV for trading systems.

  python -m sentiment_backtest.export --start 2026-01-01 --end 2026-10-31 --out sentiment_2026-01-01_2026-10-31.csv
  python -m sentiment_backtest.export --start 2026-11-09 --out ..\\wrsi_trader_new\\sentiment_gate\\sentiment_2026-11-09.csv

One row per (date, symbol) that has a sentiment file for that date, i.e. every ticker-day
that was actually scraped, including days with no news (n = 0). A ticker-day without a
row was never scraped: consumers should treat it as "unknown", not as "no news".

Each row is the signal the backtester validated, computed exactly as it computes it
(sentiment_backtest.core.build_signals): items known in [decision time on the previous
trading day, decision time on T), weighted by article weight with a fresh recency decay.
The default decision time is 15:15 IST, so a row for T only uses news knowable before an
end-of-day scan on T. Date-only timestamps count as known at the end of their day.

`negative` is the frozen rule: score <= -0.25 and weight >= 1.0 and n_neg > n_pos.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pandas as pd

from .core import BacktestSettings, build_signals, load_items

log = logging.getLogger(__name__)

FIELDS = ["date", "symbol", "decision_time", "score", "weight", "n", "n_neg", "n_pos",
          "date_only_share", "negative"]


def _folders(root: Path) -> dict[date, Path]:
    out = {}
    for p in root.iterdir() if root.is_dir() else []:
        if p.is_dir():
            try:
                out[date.fromisoformat(p.name)] = p
            except ValueError:
                continue
    return out


def _session_start(folder: Path) -> date | None:
    """Previous session's date, from the scraper window recorded in any sentiment file of
    this folder (window.start = previous trading day's close, holiday-aware)."""
    for f in sorted(folder.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            start = (json.loads(f.read_text(encoding="utf-8")).get("input") or {}).get("window", {}).get("start")
            if start:
                return datetime.fromisoformat(start).date()
        except (OSError, ValueError, AttributeError):
            continue
    return None


def export_rows(start: date, end: date, cfg: BacktestSettings | None = None,
                decision_time: str = "15:15") -> list[dict[str, Any]]:
    cfg = replace(cfg or BacktestSettings(), decisions={"export": decision_time})
    time.fromisoformat(decision_time)  # validate early
    folders = _folders(Path(cfg.sentiment_dir))
    # Sentiment folders exist only for trading days (holidays roll into the next session),
    # so they are the calendar; earlier folders give the previous session for the first day.
    full_calendar = sorted(folders)
    calendar = [d for d in full_calendar if start <= d <= end]
    if not calendar:
        raise ValueError(f"no sentiment folders under {cfg.sentiment_dir} between {start} and {end}")
    if calendar[0] == full_calendar[0]:
        # No earlier folder (e.g. a fresh daily run): take the previous session from the
        # scraper window, so Monday still looks back to Friday's decision time, not 24 hours.
        prev = _session_start(folders[calendar[0]])
        if prev and prev < calendar[0]:
            full_calendar = sorted({prev, *full_calendar})

    covered: list[tuple[date, str]] = []
    for d in calendar:
        for f in sorted(folders[d].glob("*.json")):
            if f.name.startswith("_"):
                continue
            sym = f.stem
            if cfg.tickers and sym not in cfg.tickers or sym in cfg.exclude:
                continue
            covered.append((d, sym))

    try:
        items = load_items(cfg, start, end)
    except ValueError:  # no scored articles at all: every covered row is n = 0
        items = pd.DataFrame()
    sig = build_signals(items, calendar, cfg, full_calendar) if not items.empty else pd.DataFrame()
    by_key = {(r.date, r.symbol): r for r in sig.itertuples()} if not sig.empty else {}

    rows = []
    for d, sym in covered:
        r = by_key.get((d, sym))
        if r is None or not r.weight:
            rows.append(dict(date=d.isoformat(), symbol=sym, decision_time=decision_time, score="", weight=0.0,
                             n=0 if r is None else r.n, n_neg=0 if r is None else r.n_neg,
                             n_pos=0 if r is None else r.n_pos, date_only_share="", negative=0))
            continue
        negative = (r.score <= cfg.veto_score) and (r.weight >= cfg.veto_weight) and (r.n_neg > r.n_pos)
        rows.append(dict(date=d.isoformat(), symbol=sym, decision_time=decision_time,
                         score=round(r.score, 4), weight=round(r.weight, 4), n=r.n, n_neg=r.n_neg,
                         n_pos=r.n_pos, date_only_share=round(r.date_only_share, 3), negative=int(negative)))
    return rows


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _d(v: str) -> date:
    try:
        return date.fromisoformat(v)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {v!r}") from exc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sentiment_backtest.export",
                                description="Point-in-time daily sentiment CSV (one row per scraped ticker-day)")
    p.add_argument("--start", type=_d, required=True)
    p.add_argument("--end", type=_d, help="inclusive (default: --start)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--decision-time", default="15:15", help="HH:MM IST cut-off for day T (default 15:15)")
    p.add_argument("--tickers", help="comma-separated subset")
    p.add_argument("--exclude", help="comma-separated tickers to leave out")
    p.add_argument("--sentiment-dir", type=Path)
    p.add_argument("-v", "--verbose", action="count", default=0)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if not args.verbose else logging.INFO)
    end = args.end or args.start
    if args.start > end:
        print("error: --start is after --end", file=sys.stderr)
        return 2
    over: dict[str, Any] = {}
    if args.tickers:
        over["tickers"] = tuple(t.strip().upper() for t in args.tickers.split(",") if t.strip())
    if args.exclude:
        over["exclude"] = tuple(t.strip().upper() for t in args.exclude.split(",") if t.strip())
    if args.sentiment_dir:
        over["sentiment_dir"] = args.sentiment_dir
    try:
        rows = export_rows(args.start, end, replace(BacktestSettings(), **over), args.decision_time)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    write_rows(args.out, rows)
    days = len({r["date"] for r in rows})
    neg = sum(r["negative"] for r in rows)
    with_news = sum(1 for r in rows if r["n"])
    print(f"{len(rows)} ticker-days over {days} date(s): {with_news} with news, {neg} flagged negative -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
