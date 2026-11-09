"""News relevance quality: quote pages, story grouping, sponsorship exclusions.

Cases are taken from a real TCS file (window 6-9 Nov 2026).
"""
from datetime import datetime, timedelta

import pytest
import yaml

from news_scraper.config import PROJECT_DIR, Settings
from news_scraper.dedup import dedupe
from news_scraper.models import Article, TickerInfo
from news_scraper.pipeline import NewsPipeline
from news_scraper.relevance import RelevanceMatcher, is_quote_page
from news_scraper.timeutils import IST


@pytest.fixture
def tcs():
    cfg = yaml.safe_load((PROJECT_DIR / "config" / "aliases.yaml").read_text(encoding="utf-8"))["TCS"]
    return TickerInfo(symbol="TCS", company_name="Tata Consultancy Services Limited", isin="INE467B01029",
                      bse_code="532540", aliases=cfg["aliases"], strict_aliases=cfg["strict_aliases"],
                      exclude_terms=cfg.get("exclude_terms", []))


def _news(title, minutes, publisher="P", url=None, tier="unclassified"):
    return Article(source="google_news", source_type="news", title=title, url=url or f"https://x/{minutes}",
                   published_at=datetime(2026, 11, 6, 18, 0, tzinfo=IST) + timedelta(minutes=minutes),
                   publisher=publisher, relevance_score=1.0, category="general_news", impact_tier=tier)


# ---- quote / landing pages -----------------------------------------------------------
@pytest.mark.parametrize("title,url", [
    ("TCS Share Price Live Updates: TCS Shares Report a Decrease",
     "https://economictimes.indiatimes.com/markets/stocks/stock-liveblog/tcs-share-price-live-09-nov-2026/liveblog/1.cms"),
    ("Tata Consultancy Services (TCS) Share Price Today (NSE), Market News", "https://techgraph.co/stock-market/quote/tcs-ns/"),
    ("Tata Consultancy Services Ltd (TCS.NS) | Stock Price & Latest News", "https://www.reuters.com/markets/companies/TCS.NS/"),
    ("Reliance Industries Limited (RELIANCE.NS) stock price, news, quote and history", None),
    ("Some headline", "https://finance.yahoo.com/quote/TCS.NS/"),
    ("Some headline", "https://www.moneycontrol.com/india/stockpricequote/computers-software/tcs/TCS"),
])
def test_quote_pages_detected(title, url):
    assert is_quote_page(title, url)


@pytest.mark.parametrize("title", [
    "TCS share price falls 3% after Q2 results miss estimates",
    "IT Stocks Crash! TCS, Infosys, HCL Tech lead Rs 50,000 crore mcap erosion as Nifty IT falls 2%",
    "Tata Consultancy Services Limited: 29 of 48 analysts buy",
    "TCS stock price target raised by Jefferies to Rs 2,900",
])
def test_articles_are_not_quote_pages(title):
    assert not is_quote_page(title, "https://www.livemint.com/market/stock-market-news/tcs-q2-11762.html")


# ---- sponsorship / CSR exclusions ----------------------------------------------------
def test_sponsorship_and_csr_do_not_match(tcs):
    m = RelevanceMatcher(tcs)
    assert m.score("Media Advisory: Jaguar TCS Racing confirms GEN4 race car name: Jaguar I-Type 08")[0] == 0.0
    assert m.score("NIT-T launches AI and ML centre with TCS Foundation backing")[0] == 0.0
    assert m.score("TCS World 10K Bengaluru draws record entries")[0] == 0.0
    # a TCS business story that also mentions the sponsorship still counts
    assert m.score("TCS wins $1 billion deal; Jaguar TCS Racing unveils car")[0] == 1.0


# ---- story grouping ------------------------------------------------------------------
NASHIK = [
    ("Former MP named in Nashik TCS-BPO harassment case, allegedly provided shelter to accused Nida Khan", "Hindustan Times"),
    ("TCS Nashik case: AIMIM leader Imtiyaz Jaleel named as accused in supplementary chargesheet", "Deccan Herald"),
    ("AIMIM Ex-MP Imtiaz Jaleel Named Prime Accused In TCS Nashik Case: Minister", "NDTV"),
    ("AIMIM’s Imtiaz Jaleel named accused for allegedly sheltering accused in TCS case", "The Indian Express"),
    ("TCS Nashik case: AIMIM leader Imtiyaz Jaleel named as accused", "OpIndia"),
    ("TCS Saga: Case Registered Against AIMIM’s Imtiaz Jaleel for Giving Shelter to Nida Khan", "Clarion India"),
]


def test_same_event_from_six_outlets_becomes_one_item(tcs):
    items = [_news(t, 30 * i, p) for i, (t, p) in enumerate(NASHIK)]
    out = dedupe(items, Settings(), NewsPipeline._alias_stems(tcs))
    assert len(out) == 1
    [a] = out
    assert a.publisher == "Hindustan Times"                      # earliest kept
    assert a.published_at == datetime(2026, 11, 6, 18, 0, tzinfo=IST)
    assert sorted(a.also_reported_by) == sorted(p for _, p in NASHIK[1:])
    assert len(a.meta["story_titles"]) == 5


def test_two_versions_of_a_tie_up_merge(tcs):
    items = [_news("TCS and SASTRA University tie up to equip students with Japanese language and business skills", 0, "BusinessLine"),
             _news("TCS, Sastra launch centre to train students for Japan’s IT market", 110, "The Times of India")]
    out = dedupe(items, Settings(), NewsPipeline._alias_stems(tcs))
    assert len(out) == 1 and out[0].also_reported_by == ["The Times of India"]


def test_different_tcs_stories_stay_separate(tcs):
    titles = [
        "TCS Q2 results: net profit rises 5% to Rs 12,075 crore, beats estimates",
        "TCS announces interim dividend of Rs 11 per share, record date fixed",
        "TCS bags $500 million deal from UK insurer Aviva",
        "TCS CEO Krithivasan says AI deal pipeline is strongest in years",
        "TCS to cut 2% of global workforce in restructuring",
        "IT Stocks Crash! TCS, Infosys, HCL Tech lead Rs 50,000 crore mcap erosion as Nifty IT falls 2%",
        "Tata Consultancy Services Limited: 29 of 48 analysts buy",
        "TCS, Sastra launch centre to train students for Japan’s IT market",
        "AIMIM Ex-MP Imtiaz Jaleel Named Prime Accused In TCS Nashik Case: Minister",
    ]
    items = [_news(t, i) for i, t in enumerate(titles)]
    assert len(dedupe(items, Settings(), NewsPipeline._alias_stems(tcs))) == len(titles)


def test_grouping_takes_the_highest_impact_tier(tcs):
    items = [_news("TCS Nashik case: AIMIM leader Imtiyaz Jaleel named as accused", 0, "A"),
             _news("AIMIM Ex-MP Imtiaz Jaleel Named Prime Accused In TCS Nashik Case", 5, "B", tier="medium")]
    [a] = dedupe(items, Settings(), NewsPipeline._alias_stems(tcs))
    assert a.impact_tier == "medium"


def test_grouping_can_be_switched_off(tcs):
    items = [_news(t, 30 * i, p) for i, (t, p) in enumerate(NASHIK)]
    s = Settings(story_grouping=False)
    assert len(dedupe(items, s, NewsPipeline._alias_stems(tcs))) == len(NASHIK)


def test_pipeline_drops_quote_pages_and_groups(settings):
    import json
    from dataclasses import replace
    from datetime import date

    from .conftest import DEFAULT_ROUTES, FakeHttp

    def item(title, hhmm):
        return (f"<item><title>{title} - Pub</title><link>https://news.google.com/rss/articles/{abs(hash(title))}</link>"
                f"<pubDate>Thu, 05 Nov 2026 {hhmm}:17 +0530</pubDate><source url='https://p'>Pub</source></item>")
    rss = ("<rss><channel>"
           + item("Reliance Industries Limited (RELIANCE.NS) Share Price Today, Market News", "10:01")
           + item("Reliance Industries hit with Rs 500 crore tax demand over Jamnagar refinery levy", "10:02")
           + item("Reliance Industries gets Rs 500-crore tax demand notice for Jamnagar refinery", "11:03")
           + "</channel></rss>").encode()
    routes = dict(DEFAULT_ROUTES)
    routes["news.google.com"] = rss
    s = replace(settings, enabled_sources=["google_news"])
    summary = NewsPipeline(s, http=FakeHttp(routes)).run(date(2026, 11, 5), ["RELIANCE"])[0]
    data = json.loads(summary.path.read_text("utf-8"))
    assert data["stats"]["dropped_quote_page"] == 1
    [a] = data["articles"]
    assert a["published_at"] == "2026-11-05T10:02:17+05:30" and a["also_reported_by"] == []  # same publisher
    assert len(a["meta"]["story_titles"]) == 1
