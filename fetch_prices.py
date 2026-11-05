# fetch_prices.py  ->  python fetch_prices.py data\sentiment\summary_2026-01-01_2026-09-22.csv
import sys
import pandas as pd
import yfinance as yf

summary = pd.read_csv(sys.argv[1])
dates = pd.to_datetime(summary["date"])
symbols = sorted(summary["symbol"].dropna().unique())
tickers = [f"{s}.NS" for s in symbols] + ["^NSEI"]          # NIFTY 50 for market-adjusting

start = (dates.min() - pd.Timedelta(days=10)).date()        # buffer for prior-day returns
end = (dates.max() + pd.Timedelta(days=20)).date()          # buffer for forward returns
data = yf.download(tickers, start=start, end=end, auto_adjust=False,
                   group_by="ticker", progress=False, threads=True)

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

out = pd.concat(frames, ignore_index=True)
out.columns = [c.lower().replace(" ", "_") for c in out.columns]
out.to_csv("prices.csv", index=False)
print(f"prices.csv: {out['symbol'].nunique()} symbols, {len(out)} rows, {out['date'].min()} .. {out['date'].max()}")