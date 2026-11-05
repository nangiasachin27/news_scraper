"""Data model. ``Article.to_dict`` defines the per-item JSON contract the sentiment
engine consumes; bump ``SCHEMA_VERSION`` on any breaking change."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .textutils import canonical_url, normalize_title

SCHEMA_VERSION = "1.0"

# exchange_filing   NSE/BSE corporate announcements
# financial_result  structured results filings (NSE results API)
# board_meeting     board meeting intimations (announcement time, not meeting date)
# corporate_action  ex-date events -> mechanical price adjustment, not sentiment
# news              media coverage
SOURCE_TYPES = ("exchange_filing", "financial_result", "board_meeting", "corporate_action", "news")
IMPACT_TIERS = ("high", "medium", "low", "noise", "unclassified")


@dataclass
class TickerInfo:
    symbol: str
    company_name: str
    isin: str | None = None
    bse_code: str | None = None
    bse_symbol: str | None = None
    nse_listed: bool = True
    aliases: list[str] = field(default_factory=list)
    strict_aliases: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    # strict aliases that other companies also use (RIL); matches on these alone are
    # down-weighted until a full-name mention confirms them
    ambiguous_aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Attachment:
    url: str
    name: str | None = None
    kind: str | None = None  # pdf | xml | zip | ...
    text: str | None = None
    error: str | None = None


@dataclass
class Article:
    source: str
    source_type: str
    title: str
    published_at: datetime
    url: str | None = None
    summary: str | None = None
    body: str | None = None
    publisher: str | None = None
    category: str | None = None
    impact_tier: str | None = None
    relevance_score: float = 1.0
    matched_terms: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    also_reported_by: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    first_seen_at: datetime | None = None

    @property
    def id(self) -> str:
        """Stable across re-runs, so merging a date folder is idempotent."""
        exchange_id = self.meta.get("exchange_id")
        # Enrichment/dedup may swap a Google redirect for the publisher URL; the id
        # stays pinned to the URL as first fetched so re-runs merge, not duplicate.
        ident_url = self.meta.get("google_news_url") or self.url
        if exchange_id:
            key = f"{self.source}|{exchange_id}"
        elif self.source_type == "news" and ident_url:
            key = f"news|{canonical_url(ident_url)}"
        else:
            key = f"{self.source}|{normalize_title(self.title)}|{self.published_at.isoformat()}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["id"] = self.id
        d["published_at"] = self.published_at.isoformat()
        d["first_seen_at"] = self.first_seen_at.isoformat() if self.first_seen_at else None
        return d


@dataclass
class SourceResult:
    source: str
    ok: bool
    articles: list[Article] = field(default_factory=list)
    raw_count: int = 0
    error: str | None = None
    elapsed_ms: int = 0
    skipped: bool = False
    notes: list[str] = field(default_factory=list)

    def status(self) -> dict[str, Any]:
        # Lets the sentiment engine tell "no news" apart from "source was down".
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "raw_count": self.raw_count,
            "in_window": len(self.articles),
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "notes": self.notes,
        }
