"""CLI.

  python -m news_scraper run --tickers RELIANCE TCS.NS 500180 --date 2026-11-04
  python -m news_scraper run --universe universe.txt            # date defaults to today (IST)
  python -m news_scraper run --tickers INFY --sources nse_announcements,google_news
  python -m news_scraper refresh-reference                     # NSE/BSE lists + holidays
  python -m news_scraper diagnose                              # probe every upstream endpoint
  python -m news_scraper resolve M&M
  python -m news_scraper sources

Exit code 1 if any ticker raised or had every active source fail.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

from .config import Settings
from .http import HttpClient
from .sources import REGISTRY
from .ticker_resolver import TickerResolver
from .timeutils import now_ist


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


def _read_universe(path: Path) -> list[str]:
    tickers = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            tickers.append(line)
    return tickers


def _build_parser() -> argparse.ArgumentParser:
    # -v is accepted before OR after the subcommand. On subparsers the default is
    # SUPPRESS so an absent sub-level -v doesn't reset a top-level one to 0.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS, help="-v info, -vv debug")

    p = argparse.ArgumentParser(prog="news_scraper", description="Per-ticker financial news scraper (NSE/BSE)")
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv debug")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", parents=[common], help="scrape news for tickers on a date")
    target = r.add_mutually_exclusive_group(required=True)
    target.add_argument("--tickers", nargs="+", metavar="TICKER")
    target.add_argument("--universe", type=Path, help="file with one ticker per line ('#' comments)")
    r.add_argument("--date", type=_parse_date, help="YYYY-MM-DD (default: today IST)")
    r.add_argument("--sources", help=f"comma-separated subset of: {', '.join(sorted(REGISTRY))}")
    r.add_argument("--window", choices=["session", "calendar"])
    r.add_argument("--output-dir", type=Path)
    r.add_argument("--workers", type=int)
    r.add_argument("--keep-noise", action="store_true", help="keep noise-tier filings")
    r.add_argument("--with-attachments", action="store_true", help="extract PDF text for high-impact filings")
    r.add_argument("--no-body", action="store_true", help="skip article summary/body extraction (faster)")
    r.add_argument("--no-merge", action="store_true", help="overwrite instead of merging with an existing file")

    sub.add_parser("refresh-reference", parents=[common], help="re-download NSE/BSE lists and NSE holidays")
    sub.add_parser("diagnose", parents=[common], help="probe every upstream endpoint and report status")
    rs = sub.add_parser("resolve", parents=[common], help="show how a ticker resolves")
    rs.add_argument("ticker")
    sub.add_parser("sources", parents=[common], help="list registered sources")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    level = logging.WARNING if args.verbose == 0 else logging.INFO if args.verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    settings = Settings()

    if args.cmd == "sources":
        for name in sorted(REGISTRY):
            marker = "*" if name in settings.enabled_sources else " "
            print(f"{marker} {name}")
        print("\n* = enabled by default")
        return 0

    http = HttpClient(settings)

    if args.cmd == "diagnose":
        from .diagnose import run_diagnostics

        return run_diagnostics(settings, http)

    if args.cmd == "refresh-reference":
        resolver = TickerResolver(settings, http)
        print(f"http backend: {http.backend}")
        ok = True
        for label, available in resolver.refresh().items():
            print(f"{label:<16} {'OK' if available else 'UNAVAILABLE'}")
            ok &= available
        try:
            n = resolver.refresh_holidays()
            print(f"{'NSE holidays':<16} OK ({n} dates -> {settings.holidays_file})")
        except Exception as exc:
            ok = False
            print(f"{'NSE holidays':<16} FAILED ({exc}); existing file kept")
        print(f"\nreference dir: {settings.reference_dir}")
        if not ok:
            print("run `python -m news_scraper diagnose` for per-endpoint detail")
        return 0 if ok else 1

    if args.cmd == "resolve":
        info = TickerResolver(settings, http).resolve(args.ticker)
        print(json.dumps(info.to_dict(), indent=2, ensure_ascii=False))
        return 0

    # ---- run ----------------------------------------------------------------
    overrides = {}
    if args.window:
        overrides["window_mode"] = args.window
    if args.output_dir:
        overrides["output_dir"] = args.output_dir
    if args.workers:
        overrides["max_workers"] = args.workers
    if args.keep_noise:
        overrides["drop_noise"] = False
    if args.with_attachments:
        overrides["fetch_attachments"] = True
    if args.no_body:
        overrides["fetch_article_body"] = False
    if args.no_merge:
        overrides["merge_existing"] = False
    if args.sources:
        overrides["enabled_sources"] = [s.strip() for s in args.sources.split(",") if s.strip()]
    settings = replace(settings, **overrides)

    from .pipeline import NewsPipeline  # deferred: keeps `sources`/`resolve` fast

    tickers = args.tickers or _read_universe(args.universe)
    run_date = args.date or now_ist().date()
    pipeline = NewsPipeline(settings, http=HttpClient(settings))
    summaries = pipeline.run(run_date, tickers)

    exit_code = 0
    for s in summaries:
        failed = [n for n, st in s.sources.items() if not st["ok"]]
        if s.error:
            exit_code = 1
        status = "OK " if s.ok else "ERR"
        line = f"{status} {s.ticker:<14} {s.article_count:>4} articles ({s.new_count} this run)"
        if failed:
            line += f"  failed: {', '.join(failed)}"
        if s.error:
            line += f"  error: {s.error}"
        print(line)
    print(f"\noutput: {Path(settings.output_dir) / run_date.isoformat()}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
