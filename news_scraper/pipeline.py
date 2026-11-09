"""Orchestration: resolve -> window -> fetch (all sources) -> relevance -> classify
-> noise filter -> dedup -> enrich -> merge with existing -> atomic write.

Tickers run in parallel (``max_workers``); sources within a ticker run sequentially,
which keeps per-host request rates predictable under the shared rate limiter.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .classifier import MECHANICAL_TAG, classify, is_mechanical, is_price_recap
from .config import Settings
from .dedup import dedupe
from .enrich import Enricher, text_source
from .http import HttpClient
from .models import SCHEMA_VERSION, Article, SourceResult, TickerInfo
from .relevance import RelevanceMatcher, is_digest, is_quote_page
from .textutils import story_tokens
from .sources import build_sources
from .ticker_resolver import TickerResolver
from .timeutils import IST, compute_window, load_holidays, now_ist
from .writer import merge_with_existing, output_path, reuse_enrichment, sort_articles, trim_to_window, write_json

log = logging.getLogger(__name__)


@dataclass
class TickerRunSummary:
    ticker: str
    ok: bool
    path: Path | None = None
    article_count: int = 0
    new_count: int = 0
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None

    @property
    def all_sources_failed(self) -> bool:
        active = [s for s in self.sources.values() if not s["skipped"]]
        return bool(active) and all(not s["ok"] for s in active)


class NewsPipeline:
    def __init__(self, settings: Settings | None = None, http=None, source_names: list[str] | None = None):
        self.settings = settings or Settings()
        self.http = http or HttpClient(self.settings)
        self.resolver = TickerResolver(self.settings, self.http)
        self.sources = build_sources(source_names or self.settings.enabled_sources, self.http, self.settings)
        self.holidays = load_holidays(self.settings.holidays_file)
        self.enricher = Enricher(self.http, self.settings)

    # ---- public --------------------------------------------------------------
    def run(self, d: date, tickers: list[str]) -> list[TickerRunSummary]:
        summaries: list[TickerRunSummary] = []
        workers = max(1, min(self.settings.max_workers, len(tickers)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="news") as pool:
            futures = {pool.submit(self.run_ticker, d, t): t for t in tickers}
            for fut in as_completed(futures):
                raw = futures[fut]
                try:
                    summaries.append(fut.result())
                except Exception as exc:
                    log.error("%s failed: %s", raw, exc)
                    summaries.append(TickerRunSummary(ticker=raw, ok=False, error=f"{type(exc).__name__}: {exc}"))
        order = {t: i for i, t in enumerate(tickers)}
        return sorted(summaries, key=lambda s: order.get(s.ticker, len(order)))

    def run_ticker(self, d: date, raw_ticker: str) -> TickerRunSummary:
        info = self.resolver.resolve(raw_ticker)
        start, end = compute_window(d, self.settings.window_mode, self.settings.session_close, self.holidays, IST)
        log.info("%s: window %s -> %s", info.symbol, start.isoformat(), end.isoformat())

        results: list[SourceResult] = [src.run(info, start, end) for src in self.sources]
        articles = [a for r in results for a in r.articles]
        articles, stats = self._postprocess(info, articles)
        path = output_path(self.settings, d, info)
        if self.settings.merge_existing:
            reuse_enrichment(path, articles)  # don't re-download what a previous run already got
        if self.enricher.active:
            self.enricher.enrich(articles)
        self._confirm_acronyms(info, articles)
        self._flag_digests(articles)
        # decoded publisher URLs can reveal quote pages the headline didn't
        quote_pages = [a for a in articles if a.source_type == "news" and is_quote_page(a.title, a.url)]
        if quote_pages:
            articles = [a for a in articles if a not in quote_pages]
            stats["dropped_quote_page"] = stats.get("dropped_quote_page", 0) + len(quote_pages)
            stats["kept"] = len(articles)
        for a in articles:
            if a.source_type == "news":
                a.meta["text_source"] = text_source(a)
        stats["text_sources"] = {
            k: sum(1 for a in articles if a.meta.get("text_source") == k)
            for k in ("body", "summary", "title_only")
        }

        generated = now_ist()
        new_dicts = []
        for a in articles:
            a.first_seen_at = a.first_seen_at or generated
            new_dicts.append(a.to_dict())

        if self.settings.merge_existing:
            final = merge_with_existing(path, new_dicts, self.settings.date_only_policy)
            final, stale = trim_to_window(final, start, end)
            if stale:
                stats["dropped_out_of_window"] = len(stale)
                log.info("%s: dropped %d carried-over item(s) now outside the window", info.symbol, len(stale))
        else:
            final = new_dicts
        final = sort_articles(final)
        source_status = {r.source: r.status() for r in results}
        payload = {
            "schema_version": SCHEMA_VERSION,
            "ticker": info.to_dict(),
            "date": d.isoformat(),
            "window": {
                "mode": self.settings.window_mode,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "timezone": "Asia/Kolkata",
            },
            "generated_at": generated.isoformat(),
            "sources": source_status,
            "stats": stats,
            "article_count": len(final),
            "articles": final,
        }
        write_json(path, payload)
        summary = TickerRunSummary(
            ticker=info.symbol,
            ok=True,
            path=path,
            article_count=len(final),
            new_count=len(new_dicts),
            sources=source_status,
        )
        if summary.all_sources_failed:
            summary.ok = False
            summary.error = "every active source failed"
        return summary

    # ---- internals -----------------------------------------------------------
    _RELEVANCE_TAGS = ("acronym_only", "counterparty", "roundup")

    def _confirm_acronyms(self, info: TickerInfo, articles: list[Article]) -> None:
        """An acronym-only headline ("...merger scheme with RIL") is re-scored once
        enrichment supplies summary/body: a full company name there confirms it."""
        matcher = RelevanceMatcher(info)
        for a in articles:
            if "acronym_only" not in a.tags or not (a.summary or a.body):
                continue
            score, matched, tags = matcher.confirm(a.title, a.summary, a.body)
            if "acronym_only" in tags:
                continue
            a.relevance_score = score
            a.matched_terms = sorted(set(a.matched_terms) | set(matched))
            a.tags = [t for t in a.tags if t not in self._RELEVANCE_TAGS] + tags
            a.meta["acronym_confirmed"] = True

    @staticmethod
    def _alias_stems(info: TickerInfo) -> frozenset[str]:
        """The ticker's own name words, ignored when grouping stories: every headline
        in its file contains them."""
        words = [info.symbol, info.company_name or "", *info.aliases, *info.strict_aliases]
        return frozenset().union(*(story_tokens(w) for w in words))

    @staticmethod
    def _flag_digests(articles: list[Article]) -> None:
        """Enrichment can reveal a digest the title alone didn't ("Get ready for the
        new working week with a summary of all the stories..."). Tag it ``roundup`` and
        cap relevance at 0.5, as the title-time check would have."""
        for a in articles:
            if a.source_type != "news" or "roundup" in a.tags or not a.summary:
                continue
            if is_digest(a.title, a.summary):
                a.tags.append("roundup")
                a.relevance_score = min(a.relevance_score, 0.5)
                a.meta["digest_from"] = "summary"

    def _postprocess(self, info: TickerInfo, articles: list[Article]) -> tuple[list[Article], dict[str, int]]:
        s = self.settings
        matcher = RelevanceMatcher(info)
        stats = {"in_window": len(articles), "dropped_irrelevant": 0, "dropped_noise": 0}
        kept: list[Article] = []
        for a in articles:
            if a.source_type == "news" and is_quote_page(a.title, a.url):
                stats["dropped_quote_page"] = stats.get("dropped_quote_page", 0) + 1
                continue
            if a.source_type == "news":
                score, matched, tags = matcher.score(a.title, a.summary)
                if a.meta.get("source_ticker_tagged") and score < s.min_relevance:
                    # The source itself tagged this story with our ticker (Yahoo
                    # relatedTickers): keep it at threshold even without a name match.
                    score = s.min_relevance
                    tags = [*tags, "source_tagged"]
                a.relevance_score, a.matched_terms = score, matched
                a.tags.extend(t for t in tags if t not in a.tags)
                if score < s.min_relevance:
                    stats["dropped_irrelevant"] += 1
                    continue
                # Classify media on the headline only: RSS summaries often carry
                # boilerplate ("...results, dividend and more") that skews rules.
                text = a.title
                if is_mechanical(a.title):
                    # "shares fall as stock turns ex-dividend": an exchange price
                    # adjustment, not news about the company. Kept for the record,
                    # weighted zero by the sentiment engine via the tag.
                    if MECHANICAL_TAG not in a.tags:
                        a.tags.append(MECHANICAL_TAG)
                    a.category, a.impact_tier = "corporate_action_news", "low"
                    stats["mechanical_tagged"] = stats.get("mechanical_tagged", 0) + 1
                if is_price_recap(a.title) and "price_recap" not in a.tags:
                    a.tags.append("price_recap")  # describes a move already in the price
            else:
                text = f"{a.title} {a.summary or ''}"
            if a.category is None or a.impact_tier is None:
                category, tier = classify(text, a.source_type)
                a.category = a.category or category
                a.impact_tier = a.impact_tier or tier
            if s.drop_noise and a.impact_tier == "noise":
                stats["dropped_noise"] += 1
                continue
            kept.append(a)
        deduped = dedupe(kept, s, self._alias_stems(info))
        stats["merged_duplicates"] = len(kept) - len(deduped)
        stats["kept"] = len(deduped)
        return deduped, stats
