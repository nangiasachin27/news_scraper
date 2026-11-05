import json
from datetime import date

from news_scraper.pipeline import NewsPipeline

from .conftest import DEFAULT_ROUTES, FakeHttp

RUN_DATE = date(2026, 11, 5)  # Thursday -> window 2026-11-04 15:30 .. 2026-11-05 15:30 IST


def _run(settings, http, ticker="RELIANCE.NS"):
    summaries = NewsPipeline(settings, http=http).run(RUN_DATE, [ticker])
    assert len(summaries) == 1
    s = summaries[0]
    return s, json.loads(s.path.read_text(encoding="utf-8"))


def test_end_to_end_output(settings, fake_http):
    summary, out = _run(settings, fake_http)
    assert summary.ok
    assert summary.path == settings.output_dir / "2026-11-05" / "RELIANCE.json"

    assert out["schema_version"] == "1.0"
    assert out["ticker"]["bse_code"] == "500325"
    assert out["window"]["start"] == "2026-11-04T15:30:00+05:30"
    assert all(st["ok"] for st in out["sources"].values())
    assert out["sources"]["nse_announcements"]["raw_count"] == 3
    assert out["sources"]["nse_announcements"]["in_window"] == 2  # 11-03 item trimmed
    assert out["sources"]["nse_financial_results"]["in_window"] == 1  # April result trimmed

    by_type = {}
    for a in out["articles"]:
        by_type.setdefault(a["source_type"], []).append(a)

    # NSE + BSE copies of the same disclosure collapsed; newspaper publication dropped
    filings = by_type["exchange_filing"]
    assert len(filings) == 1
    assert filings[0]["source"] == "nse_announcements"
    assert filings[0]["also_reported_by"] == ["bse_announcements"]
    assert (filings[0]["category"], filings[0]["impact_tier"]) == ("results", "high")
    assert len(filings[0]["attachments"]) == 2

    result = by_type["financial_result"][0]
    assert result["meta"]["period"] == "Second Quarter" and result["impact_tier"] == "high"

    # Syndicated Q2 story merged; Reliance Power and unrelated Infosys story dropped
    news = by_type["news"]
    titles = [a["title"] for a in news]
    assert not any("Reliance Power" in t for t in titles)
    assert not any("Infosys wins" in t for t in titles)
    q2 = next(a for a in news if "Q2 profit" in a["title"])
    assert q2["title"] == "Reliance Industries Q2 profit rises 9% on retail, Jio"
    assert q2["publisher"] == "Economic Times" and q2["also_reported_by"] == ["Mint"]
    assert q2["category"] == "results"
    roundup = next(a for a in news if "roundup" in a["tags"])
    assert roundup["relevance_score"] == 0.5

    assert out["stats"]["dropped_noise"] == 1
    assert out["stats"]["dropped_irrelevant"] == 2
    assert out["stats"]["merged_duplicates"] == 2
    assert out["article_count"] == len(out["articles"]) == 4

    times = [a["published_at"] for a in out["articles"]]
    assert times == sorted(times, reverse=True)


def test_rerun_merges_and_preserves_first_seen(settings, fake_http):
    _, first = _run(settings, fake_http)
    first_seen = {a["id"]: a["first_seen_at"] for a in first["articles"]}
    _, second = _run(settings, FakeHttp(DEFAULT_ROUTES))
    assert second["article_count"] == first["article_count"]
    assert {a["id"]: a["first_seen_at"] for a in second["articles"]} == first_seen


def test_rerun_keeps_items_that_rolled_off_feeds(settings, fake_http):
    _run(settings, fake_http)
    routes = dict(DEFAULT_ROUTES)
    routes["feeds.testwire.example"] = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
    _, second = _run(settings, FakeHttp(routes))
    assert any("roundup" in a["tags"] for a in second["articles"])


def test_source_failure_is_isolated(settings):
    routes = dict(DEFAULT_ROUTES)
    routes["AnnSubCategoryGetData"] = ConnectionError("BSE down")
    summary, out = _run(settings, FakeHttp(routes))
    assert summary.ok
    bse = out["sources"]["bse_announcements"]
    assert bse["ok"] is False and "BSE down" in bse["error"]
    filing = next(a for a in out["articles"] if a["source_type"] == "exchange_filing")
    assert filing["also_reported_by"] == []


def test_all_sources_failing_flags_ticker(settings):
    routes = {k: RuntimeError("down") for k in DEFAULT_ROUTES}
    summary, out = _run(settings, FakeHttp(routes))
    assert summary.ok is False and summary.error == "every active source failed"
    assert out["article_count"] == 0  # file still written: the status block is the signal


def test_bse_only_ticker_skips_nse_sources(settings):
    routes = dict(DEFAULT_ROUTES)
    routes["AnnSubCategoryGetData"] = {"Table": []}
    summary, out = _run(settings, FakeHttp(routes), ticker="590099")
    assert out["sources"]["nse_announcements"]["skipped"] is True
    assert out["sources"]["bse_announcements"]["skipped"] is False
    assert summary.path.name == "BSEONLY.json"


def test_keep_noise(settings, fake_http):
    from dataclasses import replace

    _, out = _run(replace(settings, drop_noise=False), fake_http)
    assert any(a["impact_tier"] == "noise" for a in out["articles"])


def test_unknown_ticker_reported_not_raised(settings, fake_http):
    summaries = NewsPipeline(settings, http=fake_http).run(RUN_DATE, ["RELIANCE", "NOPE"])
    bad = next(s for s in summaries if s.ticker == "NOPE")
    assert bad.ok is False and "UnknownTickerError" in bad.error
    assert next(s for s in summaries if s.ticker == "RELIANCE").ok
