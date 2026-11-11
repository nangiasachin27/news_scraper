"""Measure GDELT's news coverage of Indian large/mid/small caps across 2024-2026.

  python gdelt_probe.py                         # built-in sample of 12 companies, ~15 min
  python gdelt_probe.py --names names.csv       # your own: CSV with symbol,name,bucket
  python gdelt_probe.py --weeks 2               # weeks sampled per quarter (default 1)

For each company it searches GDELT's DOC 2.0 API (free, no key) for the company name over
sampled weeks - the first full week of each quarter from 2024 to now - and reports per year:
articles found, share from Indian outlets, the top domains, and how often a week hit
GDELT's 250-article cap. If data/news/<date>/<SYMBOL>.json exists for those days, it also
counts the media articles the scraper already has, for comparison.

What the numbers answer:
  * Does GDELT return anything for 2024-2025 at all? (its client library warns that only
    the last 3 months are officially supported)
  * Which Indian outlets does it see (moneycontrol, economictimes, livemint, ...)?
  * Does coverage hold up for mid and small caps, or only large caps?

GDELT publishes "one request every 5 seconds" but enforces more than that (HTTP 429). This
waits 10 s between requests; on a 429 it backs off 30 s, 60 s, 120 s ... (or GDELT's own
Retry-After) and slows the pace for the rest of the run. Every result is appended to
gdelt_probe.csv / gdelt_probe_articles.csv as it arrives; re-run with --resume to skip
company-weeks already fetched (rows that ended in an error are tried again).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

API = "https://api.gdeltproject.org/api/v2/doc/doc"
CAP = 250

# Distinctive names (a generic name like "Campus" would match unrelated stories).
SAMPLE = [
    ("RELIANCE", "Reliance Industries", "large"), ("TCS", "Tata Consultancy Services", "large"),
    ("HDFCBANK", "HDFC Bank", "large"), ("M&M", "Mahindra & Mahindra", "large"),
    ("PERSISTENT", "Persistent Systems", "mid"), ("VOLTAS", "Voltas", "mid"),
    ("DIXON", "Dixon Technologies", "mid"), ("POLYCAB", "Polycab", "mid"),
    ("CAMPUS", "Campus Activewear", "small"), ("RATEGAIN", "RateGain", "small"),
    ("EASEMYTRIP", "EaseMyTrip", "small"), ("SULA", "Sula Vineyards", "small"),
]


def sample_weeks(start_year: int, today: date, per_quarter: int) -> list[tuple[date, date]]:
    """Monday-Sunday weeks starting at the first Monday of each quarter (+ following weeks)."""
    weeks = []
    for y in range(start_year, today.year + 1):
        for m in (1, 4, 7, 10):
            d = date(y, m, 1)
            d += timedelta(days=(7 - d.weekday()) % 7)  # first Monday
            for k in range(per_quarter):
                s = d + timedelta(weeks=k)
                e = s + timedelta(days=6)
                if e < today:
                    weeks.append((s, e))
    return weeks


def query(name: str, start: date, end: date, timeout: int = 60) -> tuple[list[dict], str | None, float | None]:
    """(articles, error, retry_after_seconds)."""
    params = {
        "query": f'"{name}"', "mode": "artlist", "format": "json", "maxrecords": str(CAP), "sort": "datedesc",
        "startdatetime": start.strftime("%Y%m%d000000"), "enddatetime": end.strftime("%Y%m%d235959"),
    }
    req = urllib.request.Request(f"{API}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": "news_scraper-gdelt-probe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        ra = None
        try:
            ra = float(exc.headers.get("Retry-After")) if exc.headers.get("Retry-After") else None
        except (TypeError, ValueError):
            ra = None
        return [], f"HTTP {exc.code}", ra
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return [], f"network: {exc}", None
    try:
        return (json.loads(raw).get("articles") or []), None, None
    except ValueError:
        # GDELT answers some errors (rate limit, bad query) as plain text with HTTP 200
        return [], raw.strip()[:160] or "empty response", None


def retriable(err: str) -> bool:
    e = err.lower()
    return e.startswith(("http 429", "http 5", "network")) or "limit" in e or "empty response" in e


ROW_FIELDS = ["symbol", "name", "bucket", "week_start", "week_end", "articles", "india", "capped",
              "local_media", "error"]
ART_FIELDS = ["symbol", "week", "seendate", "domain", "sourcecountry", "language", "title", "url"]


def _append(path: Path, fields: list[str], rows: list[dict]) -> None:
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)
        fh.flush()


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def local_media_count(news_dir: Path, symbol: str, start: date, end: date) -> int | None:
    n, seen = 0, False
    d = start
    while d <= end:
        f = news_dir / d.isoformat() / f"{symbol}.json"
        if f.exists():
            seen = True
            try:
                n += sum(1 for a in json.loads(f.read_text(encoding="utf-8")).get("articles", [])
                         if a.get("source_type") == "news")
            except (OSError, ValueError):
                pass
        d += timedelta(days=1)
    return n if seen else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--names", type=Path, help="CSV with symbol,name,bucket columns (default: built-in 12)")
    p.add_argument("--start-year", type=int, default=2024)
    p.add_argument("--weeks", type=int, default=1, help="weeks sampled per quarter (default 1)")
    p.add_argument("--pause", type=float, default=10.0, help="seconds between requests (default 10)")
    p.add_argument("--max-pause", type=float, default=60.0, help="ceiling for the pace after 429s (default 60)")
    p.add_argument("--retries", type=int, default=6, help="attempts per request on 429/5xx (default 6)")
    p.add_argument("--resume", action="store_true", help="skip company-weeks already in gdelt_probe.csv")
    p.add_argument("--news-dir", type=Path, default=Path("data/news"))
    a = p.parse_args(argv)

    companies = SAMPLE
    if a.names:
        with open(a.names, newline="", encoding="utf-8-sig") as fh:
            companies = [(r["symbol"].strip(), r["name"].strip(), (r.get("bucket") or "?").strip())
                         for r in csv.DictReader(fh)]
    today = date.today()
    weeks = sample_weeks(a.start_year, today, a.weeks)
    out, arts_out = Path("gdelt_probe.csv"), Path("gdelt_probe_articles.csv")
    if not a.resume:
        for f in (out, arts_out):
            if f.exists():
                f.replace(f.with_name(f"{f.stem}_prev{f.suffix}"))  # keep the last run, start fresh
    done = {(r["symbol"], r["week_start"]) for r in _read(out) if not r.get("error")}
    todo = [(c, w) for c in companies for w in weeks if (c[0], w[0].isoformat()) not in done]
    print(f"{len(companies)} companies x {len(weeks)} weeks: {len(todo)} request(s) to make"
          + (f" ({len(done)} already done)" if done else "") + f", ~{len(todo) * a.pause / 60:.0f} min at {a.pause:.0f}s each\n",
          flush=True)

    pause = a.pause
    for n, ((sym, name, bucket), (s, e)) in enumerate(todo, 1):
        for attempt in range(1, a.retries + 1):
            arts, err, retry_after = query(name, s, e)
            if not err or not retriable(err) or attempt == a.retries:
                break
            wait = retry_after or min(300.0, 30.0 * 2 ** (attempt - 1))
            pause = min(a.max_pause, pause * 1.5)
            print(f"  {sym:12s} {s}  {err[:40]} - waiting {wait:.0f}s (attempt {attempt}/{a.retries}); "
                  f"pace now {pause:.0f}s", flush=True)
            time.sleep(wait)
        india = sum(1 for x in arts if (x.get("sourcecountry") or "").lower() == "india")
        local = local_media_count(a.news_dir, sym, s, e)
        _append(out, ROW_FIELDS, [{"symbol": sym, "name": name, "bucket": bucket, "week_start": s.isoformat(),
                                   "week_end": e.isoformat(), "articles": len(arts), "india": india,
                                   "capped": int(len(arts) >= CAP), "local_media": "" if local is None else local,
                                   "error": err or ""}])
        if arts:
            _append(arts_out, ART_FIELDS, [{"symbol": sym, "week": s.isoformat(), "seendate": x.get("seendate"),
                                            "domain": x.get("domain"), "sourcecountry": x.get("sourcecountry"),
                                            "language": x.get("language"), "title": x.get("title"),
                                            "url": x.get("url")} for x in arts])
        if err:
            print(f"  [{n}/{len(todo)}] {sym:12s} {s}  FAILED: {err[:80]} (re-run with --resume to retry)", flush=True)
        else:
            print(f"  [{n}/{len(todo)}] {sym:12s} {s}  {len(arts):4d} articles ({india} Indian outlets)"
                  + (f"  | scraper has {local}" if local is not None else ""), flush=True)
        if n < len(todo):
            time.sleep(pause)

    # Summary from the files, so a resumed run reports everything fetched so far.
    rows = _read(out)
    latest: dict[tuple, dict] = {}
    for r in rows:  # a later successful retry replaces an earlier failed row
        k = (r["symbol"], r["week_start"])
        if k not in latest or not r.get("error"):
            latest[k] = r
    by_key: dict[tuple, Counter] = defaultdict(Counter)
    errors = Counter()
    for r in latest.values():
        k = (r["bucket"], r["symbol"], int(r["week_start"][:4]))
        n_art, err = int(r["articles"] or 0), r.get("error") or ""
        by_key[k].update(weeks=1, articles=n_art, india=int(r["india"] or 0), capped=int(r["capped"] or 0),
                         zero=not n_art and not err, errors=bool(err))
        if r.get("local_media", "") != "":
            by_key[k].update(local_weeks=1, local=int(r["local_media"]))
        if err:
            errors[err[:60]] += 1
    bucket_of = {r["symbol"]: r["bucket"] for r in latest.values()}
    domains: dict[tuple, Counter] = defaultdict(Counter)
    for x in _read(arts_out):
        domains[(bucket_of.get(x["symbol"], "?"), int(x["week"][:4]))][x.get("domain") or "?"] += 1

    print("\nPER COMPANY AND YEAR (articles per sampled week)")
    print(f"  {'bucket':6s} {'symbol':12s} {'year':4s} {'weeks':>5s} {'avg/wk':>7s} {'India%':>7s} {'0-wks':>5s} {'capped':>6s} {'scraper/wk':>10s}")
    for (bucket, sym, yr), c in sorted(by_key.items(), key=lambda kv: (["large", "mid", "small"].index(kv[0][0])
                                                                       if kv[0][0] in ("large", "mid", "small") else 9, kv[0][1], kv[0][2])):
        ok = c["weeks"] - c["errors"]
        avg = f"{c['articles'] / ok:.1f}" if ok else "-"
        ind = 100 * c["india"] / c["articles"] if c["articles"] else 0
        loc = f"{c['local'] / c['local_weeks']:.1f}" if c["local_weeks"] else "-"
        print(f"  {bucket:6s} {sym:12s} {yr:4d} {c['weeks']:5d} {avg:>7s} {ind:6.0f}% {c['zero']:5d} {c['capped']:6d} {loc:>10s}")
    print("\nTOP DOMAINS BY BUCKET AND YEAR")
    for (bucket, yr), c in sorted(domains.items()):
        print(f"  {bucket:6s} {yr}: " + ", ".join(f"{d} {n}" for d, n in c.most_common(8)))
    if errors:
        print("\nERRORS: " + "; ".join(f"{k} x{v}" for k, v in errors.most_common(5)))
    print(f"\nper company-week: {out}\nevery article:    {arts_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())