"""Media sources.

google_news   Ticker-specific search; the only media source that supports a date
              range, so the only one useful for backfills. Google caps a query at ~100
              items: when a window hits the cap it is re-queried one calendar day at a
              time and the results unioned. Links are news.google.com redirects,
              decoded to publisher URLs during enrichment.

publisher_rss Rolling feeds (ET, Moneycontrol, Mint, BS, BusinessLine). Fetched once
              per run and shared across tickers via a TTL cache; relevance is applied
              centrally in the pipeline. Direct publisher URLs.
"""
from __future__ import annotations

import threading
import time
from datetime import date, timedelta

from ..models import Article
from ..timeutils import now_ist
from ._rss import FeedItem, parse_feed
from .base import PartialResult, Source, register

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
GOOGLE_CAP = 100


def _strip_publisher_suffix(title: str, publisher: str | None) -> str:
    if publisher and title.endswith(f" - {publisher}"):
        return title[: -len(publisher) - 3].rstrip()
    return title


@register
class GoogleNews(Source):
    name = "google_news"

    def build_query(self, ticker, start, end, *, after: date | None = None, before: date | None = None) -> str:
        terms: list[str] = list(ticker.aliases)
        loose = [a.lower() for a in ticker.aliases]
        for t in ticker.strict_aliases:
            # Google matching is case-insensitive, so a strict alias that is a word of a
            # longer alias ("RELIANCE" in "Reliance Industries") just widens the query to
            # every group company and burns the 100-item cap. Local matching still uses it.
            if any(t.lower() in a.split() or t.lower() == a for a in loose):
                continue
            if t not in terms:
                terms.append(t)
        terms = terms[: self.settings.google_news_max_terms] or [ticker.company_name]
        include = " OR ".join(f'"{t}"' for t in terms)
        exclude = " ".join(f'-"{x}"' for x in ticker.exclude_terms[:6])
        # after:/before: are day-granular and Google doesn't document whether they are
        # inclusive, so pad a day on each side; Source.run trims to the exact window.
        after = after or (start.date() - timedelta(days=1))
        before = before or (end.date() + timedelta(days=1))
        return " ".join(
            p for p in (f"({include})", exclude, f"after:{after.isoformat()}", f"before:{before.isoformat()}") if p
        )

    def _search(self, query: str) -> list[Article]:
        resp = self.http.get(GOOGLE_NEWS_RSS, params={"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
        return [
            Article(
                source=self.name,
                source_type="news",
                title=_strip_publisher_suffix(it.title, it.publisher),
                url=it.link,
                published_at=it.published_at,
                publisher=it.publisher,
            )
            for it in parse_feed(resp.content)
        ]

    def fetch(self, ticker, start, end, notes):
        query = self.build_query(ticker, start, end)
        notes.append(f"query: {query}")
        items = self._search(query)
        if len(items) >= GOOGLE_CAP:
            items = self._split_by_day(ticker, start, end, items, notes)
        return items

    def _split_by_day(self, ticker, start, end, first: list[Article], notes: list[str]) -> list[Article]:
        """Re-query each calendar day the window touches and union by URL."""
        seen = {a.url for a in first}
        out = list(first)
        still_capped = []
        day = start.date()
        while day <= end.date():
            part = self._search(
                self.build_query(ticker, start, end, after=day - timedelta(days=1), before=day + timedelta(days=1))
            )
            if len(part) >= GOOGLE_CAP:
                still_capped.append(day.isoformat())
            for a in part:
                if a.url not in seen:
                    seen.add(a.url)
                    out.append(a)
            day += timedelta(days=1)
        notes.append(f"hit Google's ~{GOOGLE_CAP} cap; split into per-day queries (+{len(out) - len(first)} items)")
        if still_capped:
            notes.append("per-day query still capped for " + ", ".join(still_capped) + "; those days may be incomplete")
        return out


@register
class GoogleNewsSites(GoogleNews):
    """Same ticker query, once per outlet with ``site:``. Gives date-ranged, per-ticker
    coverage of specific publishers (Moneycontrol, ET, ...) that the open query may
    rank out of its 100 items. Cost: one Google request per site per ticker, and all
    Google requests share one per-host rate limit, so this dominates run time on
    large universes (4 sites x 200 tickers at 1 req/s ~ 13 min)."""

    name = "google_news_sites"

    def fetch(self, ticker, start, end, notes):
        sites = self.settings.google_news_sites
        if not sites:
            notes.append("no sites configured")
            return []
        base = self.build_query(ticker, start, end)
        out: list[Article] = []
        counts: list[str] = []
        failed: list[str] = []
        for site in sites:
            try:
                items = self._search(f"{base} site:{site}")
            except Exception as exc:
                failed.append(f"{site} ({type(exc).__name__})")
                continue
            counts.append(f"{site}={len(items)}" + (" (capped)" if len(items) >= GOOGLE_CAP else ""))
            out.extend(items)
        notes.append("per-site items: " + ", ".join(counts))
        if failed:
            notes.append("failed sites: " + "; ".join(failed))
            if len(failed) == len(sites):
                raise RuntimeError("all site queries failed")
            # Site queries all go to Google, so a failure is transient (throttling), not a
            # dead site: keep what we got, but report the source as not ok so it's retried.
            raise PartialResult(f"{len(failed)}/{len(sites)} site queries failed", out)
        return out


@register
class PublisherRSS(Source):
    name = "publisher_rss"

    # url -> (fetched_at_monotonic, items | None, error | None). Failures are cached too,
    # so 200 tickers don't each hammer a dead feed.
    _cache: dict[str, tuple[float, list[FeedItem] | None, str | None]] = {}
    _locks: dict[str, threading.Lock] = {}
    _guard = threading.Lock()

    @classmethod
    def clear_cache(cls) -> None:
        with cls._guard:
            cls._cache.clear()

    def _feed(self, name: str, url: str) -> list[FeedItem]:
        with self._guard:
            lock = self._locks.setdefault(url, threading.Lock())
        with lock:
            hit = self._cache.get(url)
            if hit and time.monotonic() - hit[0] < self.settings.publisher_feed_ttl_seconds:
                if hit[2]:
                    raise RuntimeError(hit[2])
                return hit[1] or []
            try:
                items = parse_feed(self.http.get(url).content, default_publisher=name)
            except Exception as exc:
                self._cache[url] = (time.monotonic(), None, f"{type(exc).__name__}: {exc}"[:200])
                raise
            self._cache[url] = (time.monotonic(), items, None)
            return items

    def fetch(self, ticker, start, end, notes):
        feeds = self.settings.publisher_feeds
        if not feeds:
            notes.append("no publisher feeds configured")
            return []
        out: list[Article] = []
        failed: list[str] = []
        for name, url in feeds.items():
            try:
                items = self._feed(name, url)
            except Exception as exc:
                failed.append(f"{name} ({exc})")
                continue
            out.extend(
                Article(
                    source=self.name,
                    source_type="news",
                    title=it.title,
                    url=it.link,
                    summary=it.summary,
                    published_at=it.published_at,
                    publisher=it.publisher or name,
                )
                for it in items
            )
        if failed:
            notes.append("failed feeds: " + "; ".join(failed))
        if len(failed) == len(feeds):
            raise RuntimeError("all publisher feeds failed")
        if start < now_ist() - timedelta(hours=24):
            notes.append("rolling feeds hold only recent items; historical windows will be sparse")
        return out
