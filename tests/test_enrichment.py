import json
from dataclasses import replace
from datetime import date, datetime

from news_scraper.enrich import Enricher, meta_description
from news_scraper.models import Article, TickerInfo
from news_scraper.pipeline import NewsPipeline
from news_scraper.relevance import RelevanceMatcher
from news_scraper.sources.news import GoogleNews
from news_scraper.timeutils import IST

from .conftest import DEFAULT_ROUTES, FakeHttp

PAGE = """<html><head>
<meta property="og:title" content="x">
<meta content="S&amp;P Global affirmed RIL&#39;s A- rating, citing resilient earnings and Jio listing." property="og:description">
<meta name="description" content="fallback description">
</head><body><article><p>%s</p></article></body></html>""" % (
    "S&P Global Ratings affirmed its long-term issuer credit rating on Reliance Industries. " * 12
)


def test_meta_description_prefers_og_and_unescapes():
    assert meta_description(PAGE) == "S&P Global affirmed RIL's A- rating, citing resilient earnings and Jio listing."
    assert meta_description('<meta name="description" content="only plain">') == "only plain"
    assert meta_description("<html></html>") is None


def _news(url):
    return Article(source="google_news", source_type="news", title="t", url=url,
                   published_at=datetime(2026, 11, 4, 12, tzinfo=IST))


def test_google_url_decoded_then_page_extracted(settings):
    http = FakeHttp({"business-standard.com": PAGE})
    s = replace(settings, fetch_article_body=True)
    a = _news("https://news.google.com/rss/articles/AAA?oc=5")
    original_id = a.id
    Enricher(http, s, google_decoder=lambda u: "https://www.business-standard.com/story.html").enrich([a])
    assert a.url == "https://www.business-standard.com/story.html"
    assert a.meta["google_news_url"].startswith("https://news.google.com/")
    assert a.summary.startswith("S&P Global affirmed")
    assert a.body and "issuer credit rating" in a.body
    assert a.id == original_id  # id pinned to the original Google URL


def test_decode_failure_recorded_not_raised(settings):
    a = _news("https://news.google.com/rss/articles/AAA")
    Enricher(FakeHttp({}), replace(settings, fetch_article_body=True), google_decoder=lambda u: None).enrich([a])
    assert a.summary is None and "no publisher URL" in a.meta["body_error"]


def test_fetch_failure_recorded_not_raised(settings):
    a = _news("https://www.livemint.com/x")
    Enricher(FakeHttp({"livemint": ConnectionError("paywall")}), replace(settings, fetch_article_body=True)).enrich([a])
    assert "fetch failed" in a.meta["body_error"]


def test_pipeline_text_source_and_reuse_on_rerun(settings):
    routes = dict(DEFAULT_ROUTES)
    routes["testwire.example/markets"] = PAGE
    calls = {"n": 0}

    def decoder(url):
        calls["n"] += 1
        return "https://www.testwire.example/markets/decoded-" + url[-8:-5]

    s = replace(settings, fetch_article_body=True)
    p = NewsPipeline(s, http=FakeHttp(routes))
    p.enricher.google_decoder = decoder
    summary = p.run(date(2026, 11, 5), ["RELIANCE"])[0]
    out = json.loads(summary.path.read_text(encoding="utf-8"))
    news = [a for a in out["articles"] if a["source_type"] == "news"]
    assert all(a["meta"]["text_source"] == "body" for a in news)
    assert out["stats"]["text_sources"]["body"] == len(news)
    first_calls = calls["n"]
    assert first_calls >= 1

    http2 = FakeHttp(routes)
    p2 = NewsPipeline(s, http=http2)
    p2.enricher.google_decoder = decoder
    out2 = json.loads(p2.run(date(2026, 11, 5), ["RELIANCE"])[0].path.read_text(encoding="utf-8"))
    assert calls["n"] == first_calls  # nothing re-decoded
    assert not any("decoded-" in u for u, _ in http2.calls)  # nothing re-downloaded
    assert out2["article_count"] == out["article_count"]  # no duplicates from URL swap


def test_counterparty_mention_downweighted():
    info = TickerInfo(symbol="RELIANCE", company_name="Reliance Industries Limited",
                      aliases=["Reliance Industries"], strict_aliases=["RIL"])
    m = RelevanceMatcher(info)
    score, _, tags = m.score("Artson Receives Work Order From Reliance Industries For 15.4 Million Rupees")
    assert (score, tags) == (0.5, ["counterparty"])
    assert m.score("Reliance Industries bags order from Saudi Aramco")[2] == []  # RIL is the subject
    assert m.score("S&P affirms RIL's credit rating")[2] == ["acronym_only"]


def test_google_query_drops_symbol_subsumed_by_alias(settings):
    info = TickerInfo(symbol="RELIANCE", company_name="Reliance Industries Limited",
                      aliases=["Reliance Industries"], strict_aliases=["RIL", "RELIANCE"],
                      exclude_terms=["Reliance Power"])
    start = datetime(2026, 11, 3, 15, 30, tzinfo=IST)
    end = datetime(2026, 11, 4, 15, 30, tzinfo=IST)
    q = GoogleNews(None, settings).build_query(info, start, end)
    assert q == '("Reliance Industries" OR "RIL") -"Reliance Power" after:2026-11-02 before:2026-11-05'
    tcs = TickerInfo(symbol="TCS", company_name="x", aliases=["Tata Consultancy Services"], strict_aliases=["TCS"])
    assert '"TCS"' in GoogleNews(None, settings).build_query(tcs, start, end)


def _ril():
    return TickerInfo(symbol="RELIANCE", company_name="Reliance Industries Limited",
                      aliases=["Reliance Industries"], strict_aliases=["RIL", "RELIANCE"])


def test_counterparty_with_period_in_amount():
    score, _, tags = RelevanceMatcher(_ril()).score("Artson Ltd receives additional order of Rs. 1.54 crore from RIL")
    assert "counterparty" in tags and score == 0.5


def test_acronym_only_downweighted_and_confirmed_by_text():
    m = RelevanceMatcher(_ril())
    score, _, tags = m.score("Tata Steel's Merger Scheme With RIL Takes Effect")
    assert (score, tags) == (0.6, ["acronym_only"])
    # full name in summary -> not ambiguous
    assert m.score("S&P affirms RIL's rating", "S&P affirmed Reliance Industries Ltd's rating")[2] == []
    # unrelated summary keeps it flagged; confirming body restores 1.0
    assert "acronym_only" in m.confirm("Tata Steel's Merger Scheme With RIL Takes Effect",
                                       "Tata Steel completed the merger of Rohit Industries.", None)[2]
    assert m.confirm("RIL shares jump", None, "Reliance Industries Ltd rose 3%...") [0] == 1.0


def test_company_name_acronym_not_flagged():
    itc = TickerInfo(symbol="ITC", company_name="ITC Limited", aliases=["ITC Ltd"], strict_aliases=["ITC"])
    assert RelevanceMatcher(itc).score("ITC shares rise after Q2")[2] == []


def test_pipeline_confirms_acronym_after_enrichment(settings):
    from news_scraper.models import Article as A
    p = NewsPipeline(settings, http=FakeHttp({}))
    a = A(source="google_news", source_type="news", title="RIL shares jump 3%",
          published_at=datetime(2026, 11, 4, 10, tzinfo=IST), tags=["acronym_only"], relevance_score=0.6,
          summary="Shares of Reliance Industries rose after the Jio IPO price band.")
    p._confirm_acronyms(p.resolver.resolve("RELIANCE"), [a])
    assert a.relevance_score == 1.0 and a.tags == [] and a.meta["acronym_confirmed"]
