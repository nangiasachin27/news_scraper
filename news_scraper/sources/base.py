"""Source contract and registry.

A source implements ``fetch(ticker, start, end, notes) -> list[Article]``. It may
return items outside the window (many APIs only filter by date); ``run`` trims to
[start, end), times the call and converts exceptions into a failed SourceResult so
one broken source never takes the ticker down.

Sources are shared across worker threads: keep per-call state in locals or in the
``notes`` list, never on ``self``.

Adding a source:
    @register
    class MySource(Source):
        name = "my_source"
        requires = ("bse_code",)          # TickerInfo attributes that must be truthy
        def fetch(self, ticker, start, end, notes): ...
then add "my_source" to Settings.enabled_sources (or pass --sources).
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime

from ..config import Settings
from ..models import Article, SourceResult, TickerInfo

log = logging.getLogger(__name__)

REGISTRY: dict[str, type["Source"]] = {}


class PartialResult(Exception):
    """Raised by a source that got some data but not all of it. The articles are kept,
    but the source is reported ok=False so retries pick the ticker-date up again."""

    def __init__(self, message: str, articles: list[Article]) -> None:
        super().__init__(message)
        self.articles = articles


def register(cls: type["Source"]) -> type["Source"]:
    if not getattr(cls, "name", ""):
        raise TypeError(f"{cls.__name__} must define a non-empty `name`")
    if cls.name in REGISTRY:
        raise ValueError(f"duplicate source name {cls.name!r}")
    REGISTRY[cls.name] = cls
    return cls


class Source(ABC):
    name: str = ""
    requires: tuple[str, ...] = ()

    def __init__(self, http, settings: Settings) -> None:
        self.http = http
        self.settings = settings

    @abstractmethod
    def fetch(self, ticker: TickerInfo, start: datetime, end: datetime, notes: list[str]) -> list[Article]:
        ...

    def run(self, ticker: TickerInfo, start: datetime, end: datetime) -> SourceResult:
        missing = [attr for attr in self.requires if not getattr(ticker, attr, None)]
        if missing:
            return SourceResult(
                source=self.name, ok=True, skipped=True, notes=[f"skipped: ticker has no {', '.join(missing)}"]
            )
        notes: list[str] = []
        t0 = time.perf_counter()
        try:
            raw = self.fetch(ticker, start, end, notes)
        except PartialResult as exc:
            elapsed = int((time.perf_counter() - t0) * 1000)
            log.warning("%s partial for %s: %s", self.name, ticker.symbol, exc)
            return SourceResult(
                source=self.name,
                ok=False,
                articles=[a for a in exc.articles if start <= a.published_at < end],
                raw_count=len(exc.articles),
                error=f"partial: {exc}"[:500],
                elapsed_ms=elapsed,
                notes=notes,
            )
        except Exception as exc:
            elapsed = int((time.perf_counter() - t0) * 1000)
            log.warning("%s failed for %s: %s", self.name, ticker.symbol, exc)
            return SourceResult(
                source=self.name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}"[:500],
                elapsed_ms=elapsed,
                notes=notes,
            )
        elapsed = int((time.perf_counter() - t0) * 1000)
        in_window = [a for a in raw if start <= a.published_at < end]
        return SourceResult(
            source=self.name, ok=True, articles=in_window, raw_count=len(raw), elapsed_ms=elapsed, notes=notes
        )


def build_sources(names: list[str], http, settings: Settings) -> list[Source]:
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise ValueError(f"unknown source(s) {unknown}; available: {sorted(REGISTRY)}")
    return [REGISTRY[n](http, settings) for n in names]
