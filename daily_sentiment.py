"""Daily job: scrape + score today's news for a whole universe and write the point-in-time
sentiment CSV that wrsi_trader_new's `sentiment` filter reads.

  python daily_sentiment.py --wrsi-universe ..\\wrsi_trader_new\\config\\universe.json --out-dir ..\\wrsi_trader_new\\sentiment_gate
  python daily_sentiment.py --date 2026-11-06 --tickers TCS INFY "M&M" --out-dir out
  python daily_sentiment.py --wrsi-universe ... --out-dir ... -- --workers 4 --cooldown 5   # after --: run_pipeline options

Steps: skip if the date isn't an NSE trading day -> run_pipeline for that date (headlines
only, the backtested setting) -> sentiment_backtest.export for that date ->
<out-dir>/sentiment_<date>.csv. Run it after 15:30 IST: the CSV row for day T uses news
known before 15:15 IST on T, and the scraper window closes at 15:30.

Exit codes: 0 = CSV written (some tickers may still be incomplete - they are listed, and
the filter treats tickers without a row as unknown); 2 = nothing written; 0 with a notice
on a non-trading day. On GitHub Actions a summary is added to the job page.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path

import run_pipeline
from news_scraper.config import Settings
from news_scraper.timeutils import is_trading_day, load_holidays, now_ist
from sentiment_backtest.core import BacktestSettings
from sentiment_backtest.export import export_rows, write_rows

DEFAULT_PIPELINE_ARGS = ["--units", "title", "--skip-existing", "--passes", "2", "--pass-cooldown", "5",
                         "--cooldown", "5", "--max-cooldowns", "2", "--allow-sleep"]


def wrsi_tickers(path: Path, list_name: str) -> list[str]:
    u = json.loads(Path(path).read_text(encoding="utf-8"))
    lists = u.get("lists") or {}
    if list_name not in lists:
        raise ValueError(f"{path}: no list {list_name!r} (has: {', '.join(lists) or 'none'})")
    return [str(s).strip().upper() for s in lists[list_name] if str(s).strip()]


def source_failures(news_dir: Path, d: date) -> Counter:
    """How many tickers each source failed for on date d (from the news JSONs)."""
    bad: Counter = Counter()
    for f in (Path(news_dir) / d.isoformat()).glob("*.json"):
        if f.name.startswith("_"):
            continue
        try:
            srcs = json.loads(f.read_text(encoding="utf-8")).get("sources", {})
        except (OSError, ValueError):
            continue
        for name, st in srcs.items():
            if not st.get("ok") and not st.get("skipped"):
                bad[name] += 1
    return bad


def _summary(lines: list[str]) -> None:
    print("\n".join(lines))
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(prog="daily_sentiment", description=__doc__.split("\n\n")[0])
    p.add_argument("--date", type=run_pipeline._parse_date, help="YYYY-MM-DD (default: today, IST)")
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--wrsi-universe", type=Path, help="wrsi_trader_new config/universe.json")
    who.add_argument("--universe", type=Path, help="news_scraper universe file (JSON list or text)")
    who.add_argument("--tickers", nargs="+")
    p.add_argument("--list", default="nifty500", help="list name inside --wrsi-universe (default nifty500)")
    p.add_argument("--out-dir", type=Path, required=True, help="where sentiment_<date>.csv goes")
    p.add_argument("--decision-time", default="15:15")
    a = p.parse_args(argv)

    settings = Settings()
    d = a.date or now_ist().date()
    if not is_trading_day(d, load_holidays(settings.holidays_file)):
        _summary([f"### Sentiment {d}", f"{d} is not an NSE trading day - nothing to do."])
        return 0

    try:
        if a.wrsi_universe:
            tickers = wrsi_tickers(a.wrsi_universe, a.list)
        elif a.universe:
            tickers = [t.upper() for t in run_pipeline.load_universe(a.universe)]
        else:
            tickers = [t.strip().upper() for t in a.tickers if t.strip()]
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{d}: {len(tickers)} tickers", flush=True)

    rc = run_pipeline.main(["--start", d.isoformat(), "--tickers", *tickers, *DEFAULT_PIPELINE_ARGS, *passthrough])

    try:
        cfg = BacktestSettings()
        if "--sentiment-out" in passthrough:  # keep the export pointed at where run_pipeline wrote
            cfg = replace(cfg, sentiment_dir=Path(passthrough[passthrough.index("--sentiment-out") + 1]))
        rows = export_rows(d, d, cfg, a.decision_time)
    except ValueError as exc:
        print(f"error: export failed: {exc}", file=sys.stderr)
        return 2
    out = a.out_dir / f"sentiment_{d.isoformat()}.csv"
    write_rows(out, rows)

    have = {r["symbol"] for r in rows}
    missing = [t for t in tickers if t not in have]
    flagged = sorted(r["symbol"] for r in rows if r["negative"])
    with_news = sum(1 for r in rows if r["n"])
    fails = source_failures(settings.output_dir, d)
    lines = [f"### Sentiment {d}",
             f"- CSV: `{out}`",
             f"- covered {len(rows)}/{len(tickers)} tickers, {with_news} with news, **{len(flagged)} flagged negative**"
             + (f": {', '.join(flagged)}" if flagged else ""),
             f"- run_pipeline exit code {rc}" + (" (some ticker-dates incomplete)" if rc else "")]
    if fails:
        lines.append("- source failures (tickers): " + ", ".join(f"{k} {v}" for k, v in fails.most_common()))
    if missing:
        lines.append(f"- no row (treated as unknown by the filter): {len(missing)}"
                     + (f" - {', '.join(missing[:40])}{' ...' if len(missing) > 40 else ''}"))
    _summary(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
