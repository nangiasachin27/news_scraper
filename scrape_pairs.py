"""Scrape + score only the (date, symbol) pairs a trading system actually needs.

  python scrape_pairs.py pairs.csv --skip-existing
  python scrape_pairs.py pairs.csv --skip-existing --dry-run

pairs.csv needs `date` and `symbol` columns (wrsi_trader_new's scripts/sentiment_check.py
pairs writes one). Pairs are grouped by date and each date runs run_pipeline.py with just
that day's tickers; every option after the file name is passed through to run_pipeline
(e.g. --skip-existing, --google-interval 2, --log-file logs\\pairs.log).

A backfill of a whole Nifty 500 universe is hundreds of thousands of Google queries; a
backtest only ever asks about the candidates its other filters let through, which is a
small fraction of that.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import run_pipeline


def load_pairs(path: Path) -> dict[date, list[str]]:
    by_date: dict[date, set[str]] = defaultdict(set)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in reader.fieldnames or []}
        if "date" not in cols or "symbol" not in cols:
            raise ValueError(f"{path}: needs 'date' and 'symbol' columns, got {reader.fieldnames}")
        for row in reader:
            d, s = row[cols["date"]].strip(), row[cols["symbol"]].strip().upper()
            if d and s:
                by_date[date.fromisoformat(d[:10])].add(s)
    return {d: sorted(s) for d, s in sorted(by_date.items())}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scrape_pairs", description=__doc__.split("\n\n")[0])
    p.add_argument("pairs", type=Path, help="CSV with date,symbol columns")
    p.add_argument("--dry-run", action="store_true", help="print the per-date plan and exit")
    args, passthrough = p.parse_known_args(argv)
    try:
        plan = load_pairs(args.pairs)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    n = sum(len(v) for v in plan.values())
    print(f"{n} ticker-days over {len(plan)} date(s), {len({s for v in plan.values() for s in v})} symbols")
    if args.dry_run:
        for d, syms in plan.items():
            print(f"  {d}  {len(syms):3d}  {' '.join(syms)}")
        return 0
    worst = 0
    for i, (d, syms) in enumerate(plan.items(), 1):
        print(f"\n=== [{i}/{len(plan)}] {d}: {len(syms)} ticker(s)", flush=True)
        rc = run_pipeline.main(["--start", d.isoformat(), "--tickers", *syms, *passthrough])
        worst = max(worst, rc)
    if worst:
        print("\nsome dates finished incomplete; re-run the same command (with --skip-existing) to retry them")
    return worst


if __name__ == "__main__":
    sys.exit(main())
