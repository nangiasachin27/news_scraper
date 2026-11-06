"""End-to-end pipeline over a date range: scrape -> sentiment -> combined summary.

  python run_pipeline.py --start 2026-10-01 --end 2026-11-04            # universe.json
  python run_pipeline.py --start 2026-11-02 --tickers RELIANCE TCS INFY  # single date
  python run_pipeline.py --start 2026-01-01 --end 2026-09-22 --skip-existing --log-file backfill.log
  python run_pipeline.py --start 2026-10-01 --end 2026-10-31 --skip-scrape   # re-score only

Outputs
  data/news/<date>/<SYMBOL>.json                 per ticker-date news   (news_scraper)
  data/sentiment/<date>/<SYMBOL>.json            per ticker-date scores (sentiment_engine)
  data/sentiment/<date>/_summary.csv             per-date summary
  data/sentiment/summary_<start>_<end>.csv       one row per (date, ticker) for the range

Completeness (built for unattended runs)
* A ticker-date is COMPLETE when its news file exists and every source expected for
  that date reported ok. --skip-existing skips only complete ticker-dates, so degraded
  files, and files scraped with fewer sources, are redone (merging, never losing items).
* Outage cooldown: if a source that has worked earlier in the run fails for EVERY
  ticker on a date, that's a block (Google 429/captcha, NSE throttling), not missing
  news. The run sleeps (--cooldown, doubling, --max-cooldowns times) and retries that
  date instead of writing degraded files for the rest of the night.
* Passes: after the full range, incomplete ticker-dates are retried (--passes). Sources
  that never succeeded in the run are reported as broken rather than retried forever.
* Only trading days are processed. Nothing is lost: each session window starts at the
  previous trading day's close, so weekend/holiday news lands in the next session's file.
* Dates older than --rolling-days skip publisher_rss / yahoo_finance, which only serve
  the latest items and return nothing for past windows.

Exit code: 0 complete, 1 if anything is incomplete or failed, 2 on bad input.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from news_scraper.config import PROJECT_DIR, Settings
from news_scraper.models import TickerInfo
from news_scraper.timeutils import is_trading_day, load_holidays, now_ist
from news_scraper.writer import output_path
from sentiment_engine.config import SentimentSettings, select_units
from sentiment_engine.engine import SUMMARY_FIELDS, summary_row

log = logging.getLogger("run_pipeline")

ROLLING_SOURCES = ("publisher_rss", "yahoo_finance")
DEFAULT_UNIVERSE = PROJECT_DIR / "universe.json"
COMBINED_FIELDS = ["date", "symbol", "status", "scrape_error", *SUMMARY_FIELDS[1:]]


# ---- inputs ----------------------------------------------------------------------
def load_universe(path: Path) -> list[str]:
    """JSON list, JSON {"tickers": [...]}, or text with one ticker per line ('#' comments)."""
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("tickers")
        if not isinstance(data, list) or not all(isinstance(t, str) for t in data):
            raise ValueError(f'{path}: expected a JSON list of tickers or {{"tickers": [...]}}')
        items = data
    else:
        items = [line.split("#", 1)[0] for line in text.splitlines()]
    out, seen = [], set()
    for t in items:
        t = t.strip()
        if t and t.upper() not in seen:
            seen.add(t.upper())
            out.append(t)
    return out


def dates_in_range(start: date, end: date, holidays: frozenset[date], include_non_trading: bool) -> list[date]:
    out, d = [], start
    while d <= end:
        if include_non_trading or is_trading_day(d, holidays):
            out.append(d)
        d += timedelta(days=1)
    return out


def resolve_tickers(raw: list[str], settings: Settings, http) -> tuple[list[TickerInfo], dict[str, str]]:
    from news_scraper.ticker_resolver import TickerResolver

    resolver = TickerResolver(settings, http)
    infos, errors, seen = [], {}, set()
    for t in raw:
        try:
            info = resolver.resolve(t)
        except Exception as exc:
            errors[t] = f"{type(exc).__name__}: {exc}"
            continue
        if info.symbol not in seen:  # RELIANCE and 500325 resolve to the same symbol
            seen.add(info.symbol)
            infos.append(info)
    return infos, errors


# ---- completeness ------------------------------------------------------------------
def expected_sources(d: date, settings: Settings, today: date, rolling_days: int) -> list[str]:
    if d < today - timedelta(days=rolling_days):
        return [s for s in settings.enabled_sources if s not in ROLLING_SOURCES]
    return list(settings.enabled_sources)


def incompleteness(path: Path, expected: list[str], ignore: set[str] = frozenset()) -> str | None:
    """None if complete, else the reason. ``ignore``: sources known broken this run."""
    if not path.exists():
        return "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unreadable"
    sources = data.get("sources") or {}
    absent = [s for s in expected if s not in sources and s not in ignore]
    if absent:
        return "not scraped with: " + ",".join(absent)
    failed = [n for n in expected if n in sources and not sources[n].get("ok")
              and not sources[n].get("skipped") and n not in ignore]
    if failed:
        return "failed: " + ",".join(failed)
    return None


def _failed_sources(summary) -> set[str]:
    return {n for n, st in (summary.sources or {}).items() if not st["ok"] and not st["skipped"]}


# ---- run ---------------------------------------------------------------------------
@dataclass
class DateResult:
    date: date
    scraped: set[str] = field(default_factory=set)
    skipped_complete: int = 0
    scrape_errors: dict[str, str] = field(default_factory=dict)
    incomplete: dict[str, str] = field(default_factory=dict)  # symbol -> reason, after all passes
    sentiment: dict[str, dict[str, Any]] = field(default_factory=dict)
    sentiment_error: str | None = None


@dataclass
class RunState:
    """Shared across passes: which sources have worked at least once this run."""
    ever_ok: set[str] = field(default_factory=set)
    warned_broken: set[str] = field(default_factory=set)


def _fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def scrape_pass(
    results: list[DateResult],
    infos: list[TickerInfo],
    *,
    settings: Settings,
    http,
    state: RunState,
    skip_complete: bool,
    ignore: set[str],
    rolling_days: int,
    today: date,
    cooldown_minutes: float,
    max_cooldowns: int,
    sleep: Callable[[float], None],
    label: str,
) -> None:
    from news_scraper.pipeline import NewsPipeline

    pipes: dict[tuple[str, ...], Any] = {}
    t0 = time.monotonic()
    worked = 0
    for n, res in enumerate(results, 1):
        expected = expected_sources(res.date, settings, today, rolling_days)
        todo = []
        for info in infos:
            if skip_complete and incompleteness(output_path(settings, res.date, info), expected, ignore) is None:
                res.skipped_complete += 1
            else:
                todo.append(info.symbol)
        if todo and not expected:
            for sym in todo:
                res.scrape_errors[sym] = "only rolling sources enabled; nothing to fetch for a past date"
            todo = []
        if todo:
            worked += 1
            key = tuple(expected)
            if key not in pipes:
                pipes[key] = NewsPipeline(settings, http=http, source_names=expected)
            attempt = 0
            while todo:
                summaries = pipes[key].run(res.date, todo)
                for s in summaries:
                    if s.path is not None:
                        res.scraped.add(s.ticker)
                    if s.error:
                        res.scrape_errors[s.ticker] = s.error
                    else:
                        res.scrape_errors.pop(s.ticker, None)
                    state.ever_ok |= {name for name, st in (s.sources or {}).items() if st["ok"] and not st["skipped"]}
                with_sources = [s for s in summaries if s.sources]
                common = set.intersection(*(_failed_sources(s) for s in with_sources)) if with_sources else set()
                for broken in sorted(common - state.ever_ok - state.warned_broken):
                    state.warned_broken.add(broken)
                    print(f"  WARNING: {broken} has not succeeded once this run; it may be broken or blocked. "
                          "Check `python -m news_scraper diagnose`.", flush=True)
                outage = common & state.ever_ok
                if not outage or attempt >= max_cooldowns:
                    if outage:
                        print(f"  {', '.join(sorted(outage))} still failing after {max_cooldowns} cooldowns; "
                              f"moving on (will retry in the next pass)", flush=True)
                    break
                wait = cooldown_minutes * 60 * (2 ** attempt)
                attempt += 1
                print(f"  {', '.join(sorted(outage))} failing for every ticker on {res.date} after working "
                      f"earlier: likely rate-limited. Cooling down {_fmt_secs(wait)} "
                      f"({attempt}/{max_cooldowns}) then retrying this date.", flush=True)
                sleep(wait)
                todo = [s.ticker for s in with_sources if _failed_sources(s) & outage]
        elapsed = time.monotonic() - t0
        remaining = len(results) - n
        eta = f"  eta {_fmt_secs(elapsed / worked * remaining)}" if worked and remaining else ""
        print(
            f"[{label} {n}/{len(results)}] {res.date}  scraped={len(res.scraped)} "
            f"complete-skipped={res.skipped_complete} errors={len(res.scrape_errors)}  "
            f"elapsed {_fmt_secs(elapsed)}{eta}",
            flush=True,
        )


def collect_incomplete(results, infos, settings, today, rolling_days, ignore) -> int:
    total = 0
    for res in results:
        expected = expected_sources(res.date, settings, today, rolling_days)
        res.incomplete = {}
        for info in infos:
            reason = incompleteness(output_path(settings, res.date, info), expected, ignore)
            if reason:
                res.incomplete[info.symbol] = reason
        total += len(res.incomplete)
    return total


def score_range(results: list[DateResult], symbols: list[str], *, engine) -> None:
    for n, res in enumerate(results, 1):
        try:
            payloads = engine.run(res.date, symbols)
        except FileNotFoundError as exc:
            res.sentiment_error = f"no news folder: {exc}"
        except Exception as exc:
            res.sentiment_error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("sentiment failed for %s", res.date)
        else:
            res.sentiment = {p["ticker"]: p for p in payloads}
        status = res.sentiment_error or f"{len(res.sentiment)} tickers scored"
        print(f"[sentiment {n}/{len(results)}] {res.date}  {status}", flush=True)


def combined_rows(results: list[DateResult], symbols: list[str], scored: bool) -> list[dict[str, Any]]:
    rows = []
    for res in results:
        for sym in symbols:
            payload = res.sentiment.get(sym)
            row: dict[str, Any] = {k: "" for k in COMBINED_FIELDS}
            err = res.scrape_errors.get(sym) or res.incomplete.get(sym, "")
            row.update(date=res.date.isoformat(), symbol=sym, scrape_error=err)
            if payload:
                row.update(summary_row(payload))
                row["status"] = "ok"
            elif not scored:
                row["status"] = "not_scored"
            elif res.sentiment_error:
                row["status"] = "sentiment_failed"
                row["scrape_error"] = row["scrape_error"] or res.sentiment_error
            else:
                row["status"] = "no_news_file"
            rows.append(row)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COMBINED_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    os.replace(tmp, path)


def run(
    start: date,
    end: date,
    raw_tickers: list[str],
    *,
    settings: Settings,
    sent_settings: SentimentSettings,
    http,
    scorer_factory: Callable[[], Any] | None,
    skip_scrape: bool = False,
    skip_existing: bool = False,
    include_non_trading: bool = False,
    rolling_days: int = 2,
    passes: int = 1,
    pass_cooldown_minutes: float = 0,
    cooldown_minutes: float = 10,
    max_cooldowns: int = 0,
    today: date | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[DateResult], list[dict[str, Any]], Path | None, dict[str, str]]:
    """Library entry point. ``scorer_factory=None`` skips sentiment.
    Returns (per-date results, combined rows, combined csv path or None, unresolved tickers)."""
    today = today or now_ist().date()
    sent_settings = replace(sent_settings, news_dir=settings.output_dir)
    holidays = load_holidays(settings.holidays_file)
    dates = dates_in_range(start, end, holidays, include_non_trading)
    if not dates:
        return [], [], None, {}
    infos, unresolved = resolve_tickers(raw_tickers, settings, http)
    for t, err in unresolved.items():
        print(f"WARNING: unresolved ticker {t} will be skipped: {err}", flush=True)
    print(f"resolved {len(infos)}/{len(raw_tickers)} tickers", flush=True)
    symbols = [i.symbol for i in infos]
    results = [DateResult(d) for d in dates]
    if not infos:
        return results, [], None, unresolved

    state = RunState()
    if not skip_scrape:
        common = dict(settings=settings, http=http, state=state, rolling_days=rolling_days, today=today,
                      cooldown_minutes=cooldown_minutes, max_cooldowns=max_cooldowns, sleep=sleep)
        scrape_pass(results, infos, skip_complete=skip_existing, ignore=set(),
                    label=f"scrape pass 1/{passes}", **common)
        for p in range(2, passes + 1):
            # Sources that never worked are broken (config/IP), not transient: don't
            # re-scrape the whole range for them.
            ignore = set(settings.enabled_sources) - state.ever_ok
            pending = collect_incomplete(results, infos, settings, today, rolling_days, ignore)
            if not pending:
                break
            print(f"\npass {p}/{passes}: {pending} incomplete ticker-date(s); "
                  f"cooling down {_fmt_secs(pass_cooldown_minutes * 60)} first", flush=True)
            sleep(pass_cooldown_minutes * 60)
            scrape_pass(results, infos, skip_complete=True, ignore=ignore,
                        label=f"scrape pass {p}/{passes}", **common)
    remaining = collect_incomplete(results, infos, settings, today, rolling_days, set())
    if remaining and not skip_scrape:
        broken = sorted(set(settings.enabled_sources) - state.ever_ok)
        print(f"\n{remaining} ticker-date(s) still incomplete" +
              (f"; never-working sources: {', '.join(broken)}" if broken else ""), flush=True)

    csv_path = None
    if scorer_factory is not None:
        from sentiment_engine.engine import SentimentEngine

        engine = SentimentEngine(scorer_factory(), sent_settings)
        score_range(results, symbols, engine=engine)
    rows = combined_rows(results, symbols, scored=scorer_factory is not None)
    if scorer_factory is not None:
        csv_path = Path(sent_settings.output_dir) / f"summary_{start.isoformat()}_{end.isoformat()}.csv"
        write_csv(csv_path, rows)
    return results, rows, csv_path, unresolved


# ---- unattended-run helpers ---------------------------------------------------------
class _Tee(io.TextIOBase):
    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            st.write(s)
        return len(s)

    def flush(self):
        for st in self._streams:
            st.flush()


def keep_awake(enable: bool) -> None:
    """Windows: stop the machine sleeping while the run is alive (released on exit).
    Doesn't override closing a laptop lid if the lid action is 'sleep'."""
    if sys.platform != "win32":
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    flags = es_continuous | (es_system_required if enable else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


# ---- CLI ---------------------------------------------------------------------------
def _parse_date(v: str) -> date:
    try:
        return date.fromisoformat(v)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {v!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_pipeline", description=__doc__.split("\n\n")[0])
    p.add_argument("--start", type=_parse_date, required=True, help="YYYY-MM-DD")
    p.add_argument("--end", type=_parse_date, help="YYYY-MM-DD, inclusive (default: --start)")
    who = p.add_mutually_exclusive_group()
    who.add_argument("--tickers", nargs="+", metavar="TICKER")
    who.add_argument("--universe", type=Path, help=f"JSON or text ticker list (default: {DEFAULT_UNIVERSE.name})")
    p.add_argument("--skip-existing", action="store_true", help="skip ticker-dates that are already COMPLETE")
    p.add_argument("--skip-scrape", action="store_true", help="only run sentiment on existing news files")
    p.add_argument("--skip-sentiment", action="store_true", help="only scrape")
    p.add_argument("--include-non-trading", action="store_true", help="also process weekends/holidays")
    p.add_argument("--rolling-days", type=int, default=2, help="dates older than this skip rolling feeds (default 2)")
    p.add_argument("--passes", type=int, default=3, help="retry passes over incomplete ticker-dates (default 3)")
    p.add_argument("--pass-cooldown", type=float, default=15, help="minutes to wait before each retry pass (default 15)")
    p.add_argument("--cooldown", type=float, default=10,
                   help="minutes to wait when a source is blocked for every ticker; doubles each time (default 10)")
    p.add_argument("--max-cooldowns", type=int, default=3, help="cooldowns per date before moving on (default 3)")
    p.add_argument("--google-interval", type=float, help="seconds between Google requests (default 1.0)")
    p.add_argument("--sources", help="comma-separated scraper sources (default: all enabled)")
    p.add_argument("--workers", type=int, help="parallel tickers per date")
    body = p.add_mutually_exclusive_group()
    body.add_argument("--with-body", action="store_true",
                      help="fetch article pages for summary/body (off by default; slower, hits paywalls)")
    body.add_argument("--no-body", action="store_true", help="(default) skip article pages; kept for older scripts")
    p.add_argument("--model", help="sentiment model id (default ProsusAI/finbert)")
    p.add_argument("--device", choices=["auto", "cuda", "cpu"])
    p.add_argument("--batch-size", type=int)
    p.add_argument("--units", help="sentiment text units: all (default), title, or title,summary")
    p.add_argument("--materiality", action="store_true",
                   help="weight news by a zero-shot materiality score of the headline (experimental)")
    p.add_argument("--materiality-model", help="NLI model for --materiality (default facebook/bart-large-mnli)")
    p.add_argument("--sentiment-out", type=Path, help="write sentiment to this folder instead of data/sentiment")
    p.add_argument("--log-file", type=Path, help="also write all output and logs to this file")
    p.add_argument("--allow-sleep", action="store_true", help="don't keep Windows awake during the run")
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING if args.verbose == 0 else logging.INFO if args.verbose == 1 else logging.DEBUG
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_fh = None
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(args.log_file, "a", encoding="utf-8")
        sys.stdout = _Tee(sys.__stdout__, log_fh)
        fh = logging.StreamHandler(log_fh)
        fh.setLevel(logging.INFO)  # the file always gets INFO, whatever -v says
        handlers.append(fh)
        level = min(level, logging.INFO)
        handlers[0].setLevel(logging.WARNING if args.verbose == 0 else logging.DEBUG)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        handlers=handlers, force=True)
    try:
        return _main(args)
    finally:
        keep_awake(False)
        if log_fh:
            sys.stdout = sys.__stdout__
            log_fh.close()


def _main(args) -> int:
    print(f"--- run_pipeline started {now_ist().isoformat(timespec='seconds')}", flush=True)
    end = args.end or args.start
    today = now_ist().date()
    if args.start > end:
        print(f"error: --start {args.start} is after --end {end}", file=sys.stderr)
        return 2
    if args.start > today:
        print(f"error: --start {args.start} is in the future (today is {today})", file=sys.stderr)
        return 2
    if end > today:
        print(f"note: --end {end} is in the future; capped to today {today}")
        end = today
    if args.skip_scrape and args.skip_sentiment:
        print("error: --skip-scrape and --skip-sentiment together leave nothing to do", file=sys.stderr)
        return 2
    if args.passes < 1:
        print("error: --passes must be >= 1", file=sys.stderr)
        return 2

    if args.tickers:
        raw = args.tickers
    else:
        upath = args.universe or DEFAULT_UNIVERSE
        if not upath.exists():
            print(f"error: universe file {upath} not found (pass --tickers or --universe)", file=sys.stderr)
            return 2
        try:
            raw = load_universe(upath)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # Fail before a long scrape, not after it.
    if not args.skip_sentiment:
        missing = [m for m in ("torch", "transformers") if importlib.util.find_spec(m) is None]
        if missing:
            print(f"error: sentiment needs {', '.join(missing)} (see requirements-sentiment.txt), "
                  "or pass --skip-sentiment", file=sys.stderr)
            return 2

    base = Settings()
    overrides: dict[str, Any] = {}
    if args.sources:
        overrides["enabled_sources"] = [s.strip() for s in args.sources.split(",") if s.strip()]
    if args.workers:
        overrides["max_workers"] = args.workers
    if args.with_body:
        overrides["fetch_article_body"] = True
    elif args.no_body:
        overrides["fetch_article_body"] = False
    if args.google_interval:
        overrides["host_min_interval"] = {**base.host_min_interval, "news.google.com": args.google_interval}
    settings = replace(base, **overrides)
    sent_overrides = {k: v for k, v in {"model_name": args.model, "device": args.device,
                                        "batch_size": args.batch_size,
                                        "output_dir": args.sentiment_out}.items() if v}
    if args.materiality or args.materiality_model:
        sent_overrides["materiality_model"] = args.materiality_model or "facebook/bart-large-mnli"
    if args.units:
        try:
            sent_overrides["unit_weights"] = select_units(args.units, SentimentSettings().unit_weights)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    sent_settings = replace(SentimentSettings(), **sent_overrides)

    from news_scraper.http import HttpClient
    from news_scraper.sources import REGISTRY

    bad = [s for s in settings.enabled_sources if s not in REGISTRY]
    if bad:
        print(f"error: unknown source(s) {bad}; available: {sorted(REGISTRY)}", file=sys.stderr)
        return 2

    scorer_factory = None
    if not args.skip_sentiment:
        def scorer_factory():
            from sentiment_engine.scorers import FinBERTScorer

            s = FinBERTScorer(sent_settings.model_name, sent_settings.device, sent_settings.batch_size,
                              sent_settings.max_length, sent_settings.fp16)
            print(f"model: {s.name}  device: {s.device}", flush=True)
            return s

    holidays = load_holidays(settings.holidays_file)
    n_dates = len(dates_in_range(args.start, end, holidays, args.include_non_trading))
    print(f"range {args.start} .. {end}: {n_dates} date(s) x {len(raw)} ticker(s)"
          f"{'' if args.include_non_trading else ' (trading days only)'}; "
          f"sources: {', '.join(settings.enabled_sources)}", flush=True)
    if not holidays:
        print("note: no NSE holidays loaded (run `python -m news_scraper refresh-reference`); "
              "holidays will get their own file instead of rolling into the next session")
    if end == today and now_ist().time() < datetime.strptime(settings.session_close, "%H:%M").time():
        print(f"note: today's session hasn't closed; {today}'s window is partial. Re-run after "
              f"{settings.session_close} to complete it (re-runs merge).")

    keep_awake(not args.allow_sleep)
    results, rows, csv_path, unresolved = run(
        args.start, end, raw,
        settings=settings, sent_settings=sent_settings, http=HttpClient(settings),
        scorer_factory=scorer_factory, skip_scrape=args.skip_scrape, skip_existing=args.skip_existing,
        include_non_trading=args.include_non_trading, rolling_days=args.rolling_days, today=today,
        passes=args.passes, pass_cooldown_minutes=args.pass_cooldown,
        cooldown_minutes=args.cooldown, max_cooldowns=args.max_cooldowns,
    )

    for t, err in unresolved.items():
        print(f"unresolved ticker {t}: {err}", file=sys.stderr)
    if not results:
        print("no trading days in range; nothing to do")
        return 0
    if not rows:
        print("no tickers resolved; nothing to do", file=sys.stderr)
        return 2

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    incomplete = sum(len(r.incomplete) for r in results)
    print(f"\n--- finished {now_ist().isoformat(timespec='seconds')}")
    print("status: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
          + f"; incomplete ticker-dates={incomplete}")
    print(f"news:      {settings.output_dir}")
    if csv_path:
        print(f"combined:  {csv_path}")
    if incomplete:
        print("re-run the same command with --skip-existing to retry only the incomplete ticker-dates")
    failed = bool(unresolved) or incomplete > 0 or any(r["status"] not in ("ok", "not_scored") for r in rows)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
