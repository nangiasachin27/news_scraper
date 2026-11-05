"""Daily prices for the backtester.

  python fetch_prices.py                                          # newest data/sentiment/summary_*.csv
  python fetch_prices.py data/sentiment/summary_2026-01-01_2026-09-22.csv
  python fetch_prices.py --tickers RELIANCE TCS --start 2026-01-01 --end 2026-09-22

Writes prices.csv: date, open, high, low, close, adj_close, volume, symbol, with NIFTY 50
as symbol NIFTY50 (needed for market-relative returns). Pads 10 days before and 20 after
the range so prior-day and 5-day forward returns exist at the edges.
"""
import argparse
import sys
from pathlib import Path

import pandas as pd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("summary", nargs="?", type=Path, help="combined summary CSV from run_pipeline.py")
    ap.add_argument("--tickers", nargs="+")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--out", type=Path, default=Path("prices.csv"))
    a = ap.parse_args()

    if a.tickers:
        if not (a.start and a.end):
            sys.exit("--tickers needs --start and --end")
        symbols, lo, hi = a.tickers, pd.Timestamp(a.start), pd.Timestamp(a.end)
    else:
        path = a.summary
        if path is None:
            found = sorted(Path("data/sentiment").glob("summary_*.csv"), key=lambda p: p.stat().st_mtime)
            if not found:
                sys.exit("no data/sentiment/summary_*.csv found; pass a summary CSV or --tickers/--start/--end")
            path = found[-1]
            print(f"using {path}")
        summary = pd.read_csv(path)
        symbols = sorted(summary["symbol"].dropna().unique())
        dates = pd.to_datetime(summary["date"])
        lo, hi = dates.min(), dates.max()

    import yfinance as yf

    tickers = [f"{s}.NS" for s in symbols] + ["^NSEI"]
    data = yf.download(tickers, start=(lo - pd.Timedelta(days=10)).date(), end=(hi + pd.Timedelta(days=20)).date(),
                       auto_adjust=False, group_by="ticker", progress=False, threads=True)
    frames = []
    for t in tickers:
        if t not in data.columns.get_level_values(0):
            print("no data:", t)
            continue
        df = data[t].dropna(how="all").reset_index()
        if df.empty:
            print("no data:", t)
            continue
        df["symbol"] = "NIFTY50" if t == "^NSEI" else t.removesuffix(".NS")
        frames.append(df)
    if not frames:
        sys.exit("no price data downloaded")
    out = pd.concat(frames, ignore_index=True)
    out.columns = [c.lower().replace(" ", "_") for c in out.columns]
    out.to_csv(a.out, index=False)
    print(f"{a.out}: {out['symbol'].nunique()} symbols, {len(out)} rows, {out['date'].min()} .. {out['date'].max()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
