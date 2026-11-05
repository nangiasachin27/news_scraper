"""Duplicate collapsing.

Two distinct problems, two passes:

* Cross-exchange: dual-listed companies file the same disclosure on NSE and BSE
  within minutes. Kept once (NSE preferred), with ``also_reported_by`` and both
  exchange ids. Compared on title+summary because both exchanges use generic
  subjects ("Updates", "Outcome of Board Meeting") that would collide on title
  alone; a time window bounds false merges.
* Media syndication: one wire story republished by many outlets. Kept once at the
  earliest timestamp; ``also_reported_by`` lists the other publishers. The count is
  a usable coverage-intensity feature, so it is preserved rather than discarded.

O(n^2) per ticker-day; n is in the tens to low hundreds.
"""
from __future__ import annotations

from .config import Settings
from .models import Article
from .textutils import similarity

FILING_PRIORITY = {"nse_announcements": 0, "bse_announcements": 1}


def _union(dst: list, src: list) -> None:
    for item in src:
        if item not in dst:
            dst.append(item)


def _filing_text(a: Article) -> str:
    return f"{a.title} {(a.summary or '')[:300]}"


def _merge_filing(keep: Article, dup: Article) -> None:
    _union(keep.also_reported_by, [dup.source])
    keep.meta.setdefault("cross_listing", {})[dup.source] = dup.meta.get("exchange_id")
    if dup.summary and dup.summary != keep.summary:
        keep.meta.setdefault("alt_summaries", {})[dup.source] = dup.summary
    seen = {att.url for att in keep.attachments}
    keep.attachments.extend(att for att in dup.attachments if att.url not in seen)
    _union(keep.tags, dup.tags)


def _dedupe_filings(items: list[Article], s: Settings) -> list[Article]:
    items = sorted(items, key=lambda a: (FILING_PRIORITY.get(a.source, 9), a.published_at))
    kept: list[Article] = []
    window = s.cross_exchange_window_minutes * 60
    for a in items:
        match = None
        if a.source_type == "exchange_filing":
            for k in kept:
                if (
                    k.source_type == "exchange_filing"
                    and k.source != a.source
                    and a.source not in k.also_reported_by
                    and abs((k.published_at - a.published_at).total_seconds()) <= window
                    and similarity(_filing_text(k), _filing_text(a)) >= s.cross_exchange_threshold
                ):
                    match = k
                    break
        if match:
            _merge_filing(match, a)
        else:
            kept.append(a)
    return kept


def _dedupe_news(items: list[Article], s: Settings) -> list[Article]:
    items = sorted(items, key=lambda a: a.published_at)
    kept: list[Article] = []
    for a in items:
        match = next((k for k in kept if similarity(k.title, a.title) >= s.dedup_title_threshold), None)
        if match is None:
            kept.append(a)
            continue
        label = a.publisher or a.source
        if label != match.publisher:
            _union(match.also_reported_by, [label])
        match.relevance_score = max(match.relevance_score, a.relevance_score)
        _union(match.matched_terms, a.matched_terms)
        _union(match.tags, a.tags)
        if not match.summary and a.summary:
            match.summary = a.summary
        # Prefer a direct publisher URL over a Google News redirect for later body extraction.
        if match.url and "news.google.com" in match.url and a.url and "news.google.com" not in a.url:
            match.meta["google_news_url"] = match.url
            match.url = a.url
    return kept


def dedupe(articles: list[Article], settings: Settings) -> list[Article]:
    by_id: dict[str, Article] = {}
    for a in articles:
        by_id.setdefault(a.id, a)
    unique = list(by_id.values())
    filings = [a for a in unique if a.source_type != "news"]
    news = [a for a in unique if a.source_type == "news"]
    return _dedupe_filings(filings, settings) + _dedupe_news(news, settings)
