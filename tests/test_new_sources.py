import json
from dataclasses import replace
from datetime import date, datetime

from news_scraper.config import DEFAULT_SOURCES
from news_scraper.models import TickerInfo
from news_scraper.pipeline import NewsPipeline
from news_scraper.sources.news import GoogleNewsSites
from news_scraper.sources.yahoo import YahooFinanceNews, yahoo_symbol
from news_scraper.timeutils import IST

from .conftest import DEFAULT_ROUTES, GOOGLE_RSS, FakeHttp

START = datetime(2026, 11, 4, 15, 30, tzinfo=IST)
END = datetime(2026, 11, 5, 15, 30, tzinfo=IST)


def _ts(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=IST).timestamp())


YAHOO = {
    "news": [
        {"uuid": "y1", "title": "Telecom tariff hike lifts sector outlook", "publisher": "Reuters",
         "link": "https://finance.yahoo.com/news/jio-ipo-subscribed.html",
         "providerPublishTime": _ts(2026, 11, 5, 10, 0), "relatedTickers": ["RELIANCE.NS", "JIOFIN.NS"]},
        {"uuid": "y2", "title": "Asian markets mixed ahead of Fed", "publisher": "Bloomberg",
         "link": "https://finance.yahoo.com/news/asia-markets.html",
         "providerPublishTime": _ts(2026, 11, 5, 9, 0), "relatedTickers": ["^N225"]},
        {"uuid": "y3", "title": "missing timestamp", "link": "x"},
    ]
}


def _ril():
    return TickerInfo(symbol="RELIANCE", company_name="Reliance Industries Limited", bse_code="500325",
                      aliases=["Reliance Industries"], strict_aliases=["RIL"], exclude_terms=["Reliance Power"])


def test_yahoo_symbol():
    assert yahoo_symbol(_ril()) == "RELIANCE.NS"
    bse_only = TickerInfo(symbol="BSEONLY", company_name="x", bse_code="590099", nse_listed=False)
    assert yahoo_symbol(bse_only) == "590099.BO"


def test_yahoo_parse(settings):
    http = FakeHttp({"finance.yahoo.com": YAHOO})
    res = YahooFinanceNews(http, settings).run(_ril(), START, END)
    assert res.ok and res.raw_count == 2
    tagged = {a.meta["yahoo_uuid"]: a.meta["source_ticker_tagged"] for a in res.articles}
    assert tagged == {"y1": True, "y2": False}
    assert http.calls[0][1]["q"] == "RELIANCE.NS"


def test_google_sites_one_query_per_site_and_partial_failure(settings):
    seen = []

    def route(url, params):
        seen.append(params["q"])
        if "reuters.com" in params["q"]:
            raise ConnectionError("blocked")
        return GOOGLE_RSS

    s = replace(settings, google_news_sites=["moneycontrol.com", "reuters.com"])
    res = GoogleNewsSites(FakeHttp({"news.google.com": route}), s).run(_ril(), START, END)
    assert res.ok is False and res.error.startswith("partial:")  # so retries pick it up
    assert len(res.articles) == 3  # ...but what did arrive is kept
    assert [q.rsplit(" ", 1)[-1] for q in seen] == ["site:moneycontrol.com", "site:reuters.com"]
    assert any("moneycontrol.com=3" in n for n in res.notes)
    assert any("reuters.com (ConnectionError)" in n for n in res.notes)


def test_google_sites_all_fail_is_source_failure(settings):
    s = replace(settings, google_news_sites=["a.com", "b.com"])
    res = GoogleNewsSites(FakeHttp({"news.google.com": OSError("x")}), s).run(_ril(), START, END)
    assert res.ok is False


def test_full_default_sources_pipeline(settings):
    """All default sources together: Yahoo-tagged story kept even though its headline
    never names the company; untagged Yahoo noise dropped; duplicate Google hits
    from the open + site: queries collapse by id."""
    routes = dict(DEFAULT_ROUTES)
    routes["finance.yahoo.com"] = YAHOO
    s = replace(settings, enabled_sources=list(DEFAULT_SOURCES), google_news_sites=["moneycontrol.com"])
    out = json.loads(NewsPipeline(s, http=FakeHttp(routes)).run(date(2026, 11, 5), ["RELIANCE"])[0].path.read_text("utf-8"))
    assert all(st["ok"] for st in out["sources"].values())
    titles = [a["title"] for a in out["articles"]]
    jio = next(a for a in out["articles"] if a["title"].startswith("Telecom tariff hike"))
    assert jio["tags"] == ["source_tagged"] and jio["relevance_score"] == 0.5
    assert not any("Asian markets" in t for t in titles)
    assert sum("Q2 profit rises 9%" in t for t in titles) == 1


def _rss(n, day="05"):
    items = "".join(
        f"<item><title>Reliance Industries story {day}-{i}</title>"
        f"<link>https://news.google.com/rss/articles/{day}x{i}</link>"
        f"<pubDate>Thu, {day} Nov 2026 0{i % 9}:{10 + i % 40}:07 GMT</pubDate>"
        f"<source url='https://x.example'>X</source></item>"
        for i in range(n)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>g</title>{items}</channel></rss>'.encode()


def test_google_cap_splits_into_per_day_queries(settings):
    from news_scraper.sources.news import GoogleNews

    queries = []

    def route(url, params):
        q = params["q"]
        queries.append(q)
        if "after:2026-11-03 before:2026-11-06" in q:  # padded window query -> capped
            return _rss(100, "05")
        if "after:2026-11-04 before:2026-11-06" in q:  # per-day: 11-05
            return _rss(30, "05") + b""
        return _rss(0)

    # window 11-04 15:30 .. 11-05 15:30 -> calendar days 11-04, 11-05
    res = GoogleNews(FakeHttp({"news.google.com": route}), settings).run(_ril(), START, END)
    assert res.ok
    assert len(queries) == 3  # 1 window + 2 per-day
    assert any("split into per-day queries" in n for n in res.notes)
