"""Central configuration.

Every knob lives on ``Settings``. The CLI builds one with ``dataclasses.replace``
for per-run overrides, so library users can do the same:

    from news_scraper.config import Settings
    s = Settings(window_mode="calendar", drop_noise=False)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = Path(os.environ.get("NEWS_SCRAPER_HOME", PACKAGE_DIR.parent))

# Rolling publisher feeds. These only ever expose the latest ~50-100 items, so they
# add coverage for same-day runs but contribute nothing to historical backfills.
# URLs are publisher-maintained and change without notice: a dead feed is reported
# in the per-source status block and does not fail the run.
DEFAULT_PUBLISHER_FEEDS: dict[str, str] = {
    "Economic Times - Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Economic Times - Stocks": "https://economictimes.indiatimes.com/markets/stocks/news/rssfeeds/2146842.cms",
    "Moneycontrol - Business": "https://www.moneycontrol.com/rss/business.xml",
    "Moneycontrol - Markets": "https://www.moneycontrol.com/rss/marketreports.xml",
    "Livemint - Markets": "https://www.livemint.com/rss/markets",
    "Livemint - Companies": "https://www.livemint.com/rss/companies",
    "Business Standard - Markets": "https://www.business-standard.com/rss/markets-106.rss",
    "Business Standard - Companies": "https://www.business-standard.com/rss/companies-101.rss",
    "Hindu BusinessLine - Markets": "https://www.thehindubusinessline.com/markets/feeder/default.rss",
    # Added v0.1.3 - verify with `diagnose`; drop any that FAIL.
    "Moneycontrol - Latest": "https://www.moneycontrol.com/rss/latestnews.xml",
    "Moneycontrol - Buzzing Stocks": "https://www.moneycontrol.com/rss/buzzingstocks.xml",
    "Financial Express - Market": "https://www.financialexpress.com/market/feed/",
    "CNBC-TV18 - Market": "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml",
}

# Outlets queried individually through Google News (`site:`), one request each per ticker.
DEFAULT_GOOGLE_NEWS_SITES: list[str] = [
    "moneycontrol.com",
    "economictimes.indiatimes.com",
    "business-standard.com",
    "reuters.com",
]

DEFAULT_SOURCES: list[str] = [
    "nse_announcements",
    "nse_financial_results",
    "nse_board_meetings",
    "nse_corporate_actions",
    "bse_announcements",
    "google_news",
    "google_news_sites",
    "yahoo_finance",
    "publisher_rss",
]


@dataclass
class Settings:
    # ---- paths -------------------------------------------------------------
    output_dir: Path = PROJECT_DIR / "data" / "news"
    reference_dir: Path = PROJECT_DIR / "data" / "reference"
    aliases_file: Path = PROJECT_DIR / "config" / "aliases.yaml"
    holidays_file: Path = PROJECT_DIR / "config" / "nse_holidays.txt"
    # Placeholders: {symbol} (NSE symbol), {bse_code}, {isin}
    filename_format: str = "{symbol}.json"

    # ---- time window -------------------------------------------------------
    # "session":  previous trading day's close -> this date's close. Everything that
    #             can move price between two closes lands in one file; post-close
    #             results on day D therefore land in D+1's file (they hit D+1's open).
    # "calendar": 00:00 -> 24:00 IST on the given date.
    window_mode: str = "session"
    session_close: str = "15:30"

    # ---- sources -----------------------------------------------------------
    enabled_sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    publisher_feeds: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PUBLISHER_FEEDS))
    google_news_max_terms: int = 3
    google_news_sites: list[str] = field(default_factory=lambda: list(DEFAULT_GOOGLE_NEWS_SITES))
    yahoo_news_count: int = 25
    bse_max_pages: int = 10
    nse_result_periods: tuple[str, ...] = ("Quarterly", "Annual")
    publisher_feed_ttl_seconds: int = 600
    # NSE results / board-meeting endpoints return full history regardless of date:
    # cached per ticker for this long so multi-date runs fetch them once.
    nse_static_cache_ttl_seconds: int = 3600

    # ---- filtering / dedup -------------------------------------------------
    min_relevance: float = 0.5
    drop_noise: bool = True
    dedup_title_threshold: float = 0.88
    cross_exchange_threshold: float = 0.80
    cross_exchange_window_minutes: int = 180

    # ---- enrichment ----------------------------------------------------------
    # Article text: on by default. Runs only on items that survived relevance/dedup
    # (single digits to low tens per ticker-day), and results are reused on re-runs.
    fetch_article_body: bool = True
    # PDF text from filings: off by default (large downloads).
    fetch_attachments: bool = False
    attachment_tiers: tuple[str, ...] = ("high",)
    max_attachment_bytes: int = 10_000_000
    max_pdf_pages: int = 40
    max_text_chars: int = 20_000

    # ---- http --------------------------------------------------------------
    # "auto": curl_cffi if installed, else requests. "curl_cffi" fails fast if missing.
    http_backend: str = "auto"
    impersonate: str = "chrome"
    request_timeout: float = 20.0
    max_retries: int = 3
    host_min_interval: dict[str, float] = field(
        default_factory=lambda: {
            "www.nseindia.com": 1.0,
            "nsearchives.nseindia.com": 0.5,
            "api.bseindia.com": 1.0,
            "www.bseindia.com": 0.5,
            "news.google.com": 1.0,
            "query2.finance.yahoo.com": 0.5,
        }
    )
    default_min_interval: float = 0.3
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )

    # ---- run behaviour -----------------------------------------------------
    reference_max_age_days: int = 7
    max_workers: int = 4
    # Re-running a date merges into the existing file (keyed by article id) instead
    # of overwriting. Needed because rolling RSS feeds drop items within hours.
    merge_existing: bool = True
