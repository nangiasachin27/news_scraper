"""Shared RSS/Atom parsing."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import feedparser

from ..textutils import strip_html
from ..timeutils import from_struct_time


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str | None
    summary: str | None
    published_at: datetime
    publisher: str | None


def parse_feed(content: bytes, default_publisher: str | None = None) -> list[FeedItem]:
    parsed = feedparser.parse(content)
    if parsed.get("bozo") and not parsed.entries:
        raise ValueError(f"unparseable feed: {parsed.get('bozo_exception')}")
    items: list[FeedItem] = []
    for entry in parsed.entries:
        st = entry.get("published_parsed") or entry.get("updated_parsed")
        title = strip_html(entry.get("title"))
        if not st or not title:
            continue  # undated items can't be placed in a window
        src = entry.get("source")
        publisher = (src.get("title") if hasattr(src, "get") else None) or default_publisher
        items.append(
            FeedItem(
                title=title,
                link=entry.get("link"),
                summary=strip_html(entry.get("summary")) or None,
                published_at=from_struct_time(st),
                publisher=publisher,
            )
        )
    return items
