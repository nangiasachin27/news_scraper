"""CLI.

  python -m sentiment_backtest --start 2026-01-01 --end 2026-09-22 --prices prices.csv
  python -m sentiment_backtest --start 2026-01-01 --end 2026-09-22 --prices prices.csv --exclude RELIANCE,M&M
  python -m sentiment_backtest --start 2026-11-10 --end 2027-02-10 --prices prices.csv --parity   # on live data

Reads data/sentiment/<date>/*.json (the sentiment engine's output) and a prices file from
fetch_prices.py. Writes data/backtest/<start>_<end>/: signals.csv, metrics.csv,
stability.csv, parity.csv (with --parity) and report.json.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

from .core import BacktestSettings, run_backtest


def _d(v: str) -> date:
    try:
        return date.fromisoformat(v)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {v!r}") from exc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sentiment_backtest", description="Point-in-time sentiment backtest")
    p.add_argument("--start", type=_d, required=True)
    p.add_argument("--end", type=_d, required=True)
    p.add_argument("--prices", type=Path, required=True, help="CSV from fetch_prices.py (includes NIFTY50)")
    p.add_argument("--tickers", help="comma-separated subset")
    p.add_argument("--exclude", help="comma-separated tickers to leave out, e.g. ones with poor news coverage")
    p.add_argument("--periods", type=int, default=3, help="sub-periods for the stability check (default 3)")
    p.add_argument("--parity", action="store_true", help="also run with media timestamps cut back to dates")
    p.add_argument("--degrade", action="store_true", help="run only with media timestamps cut back to dates")
    p.add_argument("--sentiment-dir", type=Path)
    p.add_argument("--out-dir", type=Path)
    p.add_argument("-v", "--verbose", action="count", default=0)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if not args.verbose else logging.INFO)

    if args.start > args.end:
        print("error: --start is after --end", file=sys.stderr)
        return 2
    if not args.prices.exists():
        print(f"error: {args.prices} not found (create it with fetch_prices.py)", file=sys.stderr)
        return 2
    cfg = BacktestSettings()
    over = {"periods": args.periods, "degrade_media": args.degrade}
    if args.tickers:
        over["tickers"] = tuple(t.strip().upper() for t in args.tickers.split(",") if t.strip())
    if args.exclude:
        over["exclude"] = tuple(t.strip().upper() for t in args.exclude.split(",") if t.strip())
    if args.sentiment_dir:
        over["sentiment_dir"] = args.sentiment_dir
    if args.out_dir:
        over["output_dir"] = args.out_dir
    cfg = replace(cfg, **over)

    try:
        rep = run_backtest(args.start, args.end, args.prices, cfg, run_parity=args.parity)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    d = rep["data"]
    print(f"{d['trading_days']} trading days, {len(d['tickers'])} tickers, {d['items']} scored items; "
          f"{d['media_date_only_share']:.0%} of media items have date-only timestamps")
    m = pd.DataFrame(rep["metrics"])
    pd.set_option("display.width", 220)
    if not m.empty:
        cols = ["mode", "decision", "target_span", "period", "rows", "ic", "neg_rows", "neg_gap_pts",
                "neg_gap_lo", "neg_gap_hi", "neg_p", "pos_rows", "pos_gap_pts", "pos_p"]
        print("\nMETRICS (gaps in percentage points vs NIFTY; negative gap = flagged names lagged)")
        print(m[cols].to_string(index=False))
    s = pd.DataFrame(rep["stability"])
    if not s.empty:
        print("\nSTABILITY ACROSS SUB-PERIODS")
        print(s[["mode", "decision", "target", "neg_gap_all", "neg_p_all", "neg_gap_negative_every_period", "verdict"]].to_string(index=False))
    par = pd.DataFrame(rep["parity"])
    if not par.empty:
        print("\nPARITY: as recorded vs media timestamps cut back to dates")
        print(par.to_string(index=False))
    print(f"\noutput: {rep['output_dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
