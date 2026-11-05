"""Yahoo Finance per-ticker news via the search endpoint behind finance.yahoo.com
(the same one yfinance's ``Ticker.news`` used).

Unofficial and unversioned, like the exchange APIs. Returns only the latest
``yahoo_news_count`` stories (no date range), so it adds precision for same-day
runs and little for backfills.

Yahoo tags each story with ``relatedTickers``. A story tagged with our symbol is
kept even if the headline never names the company (``source_tagged``), because
Yahoo's tagging is curated rather than keyword-based.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..models import Article
from ..textutils import strip_html
from ..timeutils import IST, now_ist
from .base import Source, register

YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"


def yahoo_symbol(ticker) -> str | None:
    if ticker.nse_listed:
        return f"{ticker.symbol}.NS"
    if ticker.bse_code:
        return f"{ticker.bse_code}.BO"
    return None


@register
class YahooFinanceNews(Source):
    name = "yahoo_finance"

    def fetch(self, ticker, start, end, notes):
        sym = yahoo_symbol(ticker)
        if not sym:
            notes.append("no Yahoo symbol derivable")
            return []
        data = self.http.get_json(
            YAHOO_SEARCH_URL,
            params={
                "q": sym,
                "quotesCount": 0,
                "newsCount": self.settings.yahoo_news_count,
                "enableFuzzyQuery": "false",
                "lang": "en-US",
                "region": "IN",
            },
            headers={"Accept": "application/json", "Referer": "https://finance.yahoo.com/"},
        )
        items = (data or {}).get("news") or [] if isinstance(data, dict) else []
        out: list[Article] = []
        for n in items:
            title = strip_html(n.get("title"))
            ts = n.get("providerPublishTime")
            if not title or not isinstance(ts, (int, float)):
                continue
            related = [str(r).upper() for r in (n.get("relatedTickers") or [])]
            out.append(
                Article(
                    source=self.name,
                    source_type="news",
                    title=title,
                    url=n.get("link"),
                    published_at=datetime.fromtimestamp(ts, tz=IST),
                    publisher=n.get("publisher"),
                    meta={
                        "yahoo_uuid": n.get("uuid"),
                        "related_tickers": related,
                        "source_ticker_tagged": sym.upper() in related,
                    },
                )
            )
        if out and min(a.published_at for a in out) > start and start < now_ist() - timedelta(days=2):
            notes.append(f"Yahoo returns only the latest {self.settings.yahoo_news_count} stories; window start not reached")
        return out
