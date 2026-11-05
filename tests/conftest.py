"""Offline fixtures. FakeHttp routes by URL substring; nothing touches the network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from news_scraper.config import PROJECT_DIR, Settings
from news_scraper.sources.news import PublisherRSS

NSE_CSV = """SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10
RPOWER,Reliance Power Limited,EQ,11-FEB-2008,10,1,INE614G01033,10
TCS,Tata Consultancy Services Limited,EQ,25-AUG-2004,1,1,INE467B01029,1
ITC,ITC Limited,EQ,23-AUG-1995,1,1,INE154A01025,1
M&M,Mahindra & Mahindra Limited,EQ,17-NOV-1995,5,1,INE101A01026,5
"""

BSE_LIST = [
    {"SCRIP_CD": "500325", "Scrip_Name": "RELIANCE INDUSTRIES LTD.", "scrip_id": "RELIANCE", "ISIN_NUMBER": "INE002A01018", "Issuer_Name": "Reliance Industries Ltd"},
    {"SCRIP_CD": "532540", "Scrip_Name": "TATA CONSULTANCY SERVICES LTD.", "scrip_id": "TCS", "ISIN_NUMBER": "INE467B01029", "Issuer_Name": "Tata Consultancy Services Ltd"},
    {"SCRIP_CD": "500875", "Scrip_Name": "ITC LTD.", "scrip_id": "ITC", "ISIN_NUMBER": "INE154A01025", "Issuer_Name": "ITC Ltd"},
    {"SCRIP_CD": "590099", "Scrip_Name": "BSE ONLY CO LTD.", "scrip_id": "BSEONLY", "ISIN_NUMBER": "INE999Z01011", "Issuer_Name": "BSE Only Company Ltd"},
]

RESULTS_TEXT = (
    "Reliance Industries Limited has informed the Exchange regarding Outcome of Board Meeting "
    "held on November 05, 2026 approving unaudited financial results for the quarter ended September 30, 2026"
)

NSE_ANNOUNCEMENTS = [
    {"symbol": "RELIANCE", "desc": "Outcome of Board Meeting", "attchmntText": RESULTS_TEXT,
     "an_dt": "05-Nov-2026 09:12:45", "sort_date": "2026-11-05 09:12:45", "seq_id": "111",
     "attchmntFile": "https://nsearchives.nseindia.com/corporate/RELIANCE_05112026_outcome.pdf", "smIndustry": "Refineries"},
    {"symbol": "RELIANCE", "desc": "Copy of Newspaper Publication",
     "attchmntText": "Newspaper publication of financial results for the quarter ended September 30, 2026",
     "sort_date": "2026-11-05 10:00:00", "seq_id": "112", "attchmntFile": "-"},
    {"symbol": "RELIANCE", "desc": "Updates", "attchmntText": "out of window",
     "sort_date": "2026-11-03 10:00:00", "seq_id": "100"},
]

NSE_RESULTS = [
    {"symbol": "RELIANCE", "consolidated": "Consolidated", "audited": "Un-Audited", "relatingTo": "Second Quarter",
     "fromDate": "01-Jul-2026", "toDate": "30-Sep-2026", "seqNumber": "9001", "broadCastDate": "05-Nov-2026 09:15:00",
     "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/RES_9001.xml"},
    {"symbol": "RELIANCE", "consolidated": "Consolidated", "audited": "Audited", "relatingTo": "Fourth Quarter",
     "toDate": "31-Mar-2026", "seqNumber": "8001", "broadCastDate": "25-Apr-2026 19:00:00"},
]

BSE_ANNOUNCEMENTS = {
    "Table": [
        {"NEWSID": "bse-1", "SCRIP_CD": 500325,
         "NEWSSUB": "Reliance Industries Ltd - 500325 - Outcome of Board Meeting",
         "HEADLINE": RESULTS_TEXT, "DissemDT": "2026-11-05T09:13:10", "NEWS_DT": "2026-11-05T09:13:10.17",
         "CATEGORYNAME": "Board Meeting", "SUBCATNAME": "Outcome of Board Meeting",
         "ATTACHMENTNAME": "abc123.pdf", "PDFFLAG": 0, "CRITICALNEWS": 0, "TotalPageCnt": 1},
    ],
    "Table1": [{"ROWCNT": 1}],
}

GOOGLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Google News</title>
<item><title>Reliance Industries Q2 profit rises 9% on retail, Jio - Economic Times</title>
<link>https://news.google.com/rss/articles/AAA?oc=5</link>
<pubDate>Thu, 05 Nov 2026 06:04:11 GMT</pubDate>
<source url="https://economictimes.indiatimes.com">Economic Times</source></item>
<item><title>Reliance Industries Q2 profit rises 9% on retail, Jio - Mint</title>
<link>https://news.google.com/rss/articles/BBB?oc=5</link>
<pubDate>Thu, 05 Nov 2026 06:31:07 GMT</pubDate>
<source url="https://www.livemint.com">Mint</source></item>
<item><title>Reliance Power shares hit upper circuit - Business Today</title>
<link>https://news.google.com/rss/articles/CCC?oc=5</link>
<pubDate>Thu, 05 Nov 2026 07:02:45 GMT</pubDate>
<source url="https://www.businesstoday.in">Business Today</source></item>
</channel></rss>"""

PUBLISHER_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>TestWire Markets</title>
<item><title>Stocks to watch: Reliance Industries, TCS, Infosys, Wipro</title>
<link>https://www.testwire.example/markets/stocks-to-watch-5-nov?utm_source=rss</link>
<description>A roundup of stocks in focus today.</description>
<pubDate>Thu, 05 Nov 2026 02:03:19 GMT</pubDate></item>
<item><title>Infosys wins $1 billion deal from European bank</title>
<link>https://www.testwire.example/companies/infosys-deal</link>
<description>Infosys has bagged a large deal.</description>
<pubDate>Thu, 05 Nov 2026 03:01:52 GMT</pubDate></item>
</channel></rss>"""

DEFAULT_ROUTES = {
    "corporate-announcements": NSE_ANNOUNCEMENTS,
    "corporates-financial-results": NSE_RESULTS,
    "corporate-board-meetings": [],
    "corporates-corporateActions": [],
    "AnnSubCategoryGetData": BSE_ANNOUNCEMENTS,
    "news.google.com": GOOGLE_RSS,
    "feeds.testwire.example": PUBLISHER_RSS,
}


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.status_code = 200

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self):
        return json.loads(self.content)

    def iter_content(self, chunk_size=65536):
        yield self.content


class FakeHttp:
    def __init__(self, routes: dict):
        self.routes = dict(routes)
        self.calls: list[tuple[str, dict | None]] = []

    def _match(self, url, params):
        self.calls.append((url, params))
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload(url, params) if callable(payload) else payload
        raise AssertionError(f"unrouted URL in test: {url}")

    def get(self, url, *, params=None, headers=None, stream=False):
        payload = self._match(url, params)
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        elif isinstance(payload, str):
            payload = payload.encode()
        return FakeResponse(payload)

    def get_json(self, url, *, params=None, headers=None):
        payload = self._match(url, params)
        return json.loads(payload) if isinstance(payload, (bytes, str)) else payload

    def nse_get_json(self, url, params=None, referer=None):
        return self.get_json(url, params=params)

    def bse_get_json(self, url, params=None):
        return self.get_json(url, params=params)


@pytest.fixture(autouse=True)
def _clear_feed_cache():
    from news_scraper.sources import nse

    PublisherRSS.clear_cache()
    nse.clear_cache()
    yield
    PublisherRSS.clear_cache()
    nse.clear_cache()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "nse_equity_list.csv").write_text(NSE_CSV, encoding="utf-8")
    (ref / "bse_scrip_list.json").write_text(json.dumps(BSE_LIST), encoding="utf-8")
    return Settings(
        output_dir=tmp_path / "news",
        reference_dir=ref,
        aliases_file=PROJECT_DIR / "config" / "aliases.yaml",
        holidays_file=tmp_path / "no_holidays.txt",
        publisher_feeds={"TestWire": "https://feeds.testwire.example/markets.xml"},
        host_min_interval={},
        default_min_interval=0.0,
        max_workers=2,
        fetch_article_body=False,  # enrichment tests enable it explicitly
        # Pinned so existing pipeline assertions stay exact; new sources tested separately.
        enabled_sources=[
            "nse_announcements", "nse_financial_results", "nse_board_meetings", "nse_corporate_actions",
            "bse_announcements", "google_news", "publisher_rss",
        ],
    )


@pytest.fixture
def fake_http() -> FakeHttp:
    return FakeHttp(DEFAULT_ROUTES)
