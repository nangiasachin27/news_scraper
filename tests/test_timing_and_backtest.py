import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from news_scraper.models import Article
from news_scraper.pipeline import NewsPipeline
from news_scraper.sources.news import GoogleNews
from news_scraper.timeutils import IST, end_of_local_day, is_date_only, known_at
from sentiment_backtest.core import BacktestSettings, build_signals, load_items, parity, run_backtest
from sentiment_engine.config import SentimentSettings, select_units

from .conftest import DEFAULT_ROUTES, FakeHttp

DATE_ONLY_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Google News</title>
<item><title>Reliance Industries posts record quarterly profit - Mint</title>
<link>https://news.google.com/rss/articles/DO1?oc=5</link>
<pubDate>Thu, 05 Nov 2026 07:00:00 GMT</pubDate><source url="https://www.livemint.com">Mint</source></item>
<item><title>Reliance Industries shares rise in early trade - ET</title>
<link>https://news.google.com/rss/articles/EX1?oc=5</link>
<pubDate>Thu, 05 Nov 2026 04:12:33 GMT</pubDate><source url="https://economictimes.indiatimes.com">ET</source></item>
</channel></rss>"""


# ---- fix 1: timestamp precision ---------------------------------------------------
def test_date_only_detection_and_known_at():
    placeholder = datetime(2026, 11, 5, 12, 30, tzinfo=IST)  # 07:00:00 UTC
    exact = datetime(2026, 11, 5, 9, 42, 33, tzinfo=IST)
    assert is_date_only(placeholder) and not is_date_only(exact)
    assert end_of_local_day(placeholder) == datetime(2026, 11, 5, 23, 59, 59, tzinfo=IST)
    assert known_at(placeholder, "date") == datetime(2026, 11, 5, 23, 59, 59, tzinfo=IST)
    assert known_at(exact, "exact") == exact


def test_google_date_only_item_moves_to_next_session(settings):
    routes = {k: v for k, v in DEFAULT_ROUTES.items()}
    routes["news.google.com"] = DATE_ONLY_RSS
    s = replace(settings, enabled_sources=["google_news"])
    p = NewsPipeline(s, http=FakeHttp(routes))
    thu = json.loads(p.run(date(2026, 11, 5), ["RELIANCE"])[0].path.read_text("utf-8"))
    titles = [a["title"] for a in thu["articles"]]
    assert titles == ["Reliance Industries shares rise in early trade"]  # exact 09:42 item stays

    fri = json.loads(NewsPipeline(s, http=FakeHttp(routes)).run(date(2026, 11, 6), ["RELIANCE"])[0].path.read_text("utf-8"))
    moved = [a for a in fri["articles"] if "record quarterly profit" in a["title"]]
    assert len(moved) == 1
    assert moved[0]["published_at"] == "2026-11-05T23:59:59+05:30"
    assert moved[0]["meta"]["time_precision"] == "date"
    assert moved[0]["meta"]["reported_published_at"].startswith("2026-11-05T12:30:00")


def test_date_only_policy_keep(settings):
    g = GoogleNews(FakeHttp({"news.google.com": DATE_ONLY_RSS}), replace(settings, date_only_policy="keep"))
    items = g._search("q")
    assert all("time_precision" not in a.meta for a in items)


# ---- fixes 2 and 3 --------------------------------------------------------------------
def test_reliance_and_mahindra_bare_names(settings):
    p = NewsPipeline(settings, http=FakeHttp({}))
    from news_scraper.relevance import RelevanceMatcher

    rel = RelevanceMatcher(p.resolver.resolve("RELIANCE"))
    assert rel.score("Reliance Q2 profit rises 9%")[0] == 1.0
    assert rel.score("Reliance Power shares hit upper circuit")[0] == 0.0
    assert rel.score("Jio Financial Q2 profit up")[0] == 0.0
    assert rel.score("reliance on imports rises")[0] == 0.0
    mm = RelevanceMatcher(p.resolver.resolve("M&M"))
    assert mm.score("Mahindra sales jump 18% in October")[0] == 1.0
    assert mm.score("Tech Mahindra Q2 results beat")[0] == 0.0
    assert mm.score("Kotak Mahindra Bank shares fall")[0] == 0.0


def test_ex_dividend_headline_tagged_mechanical(settings):
    p = NewsPipeline(settings, http=FakeHttp({}))
    info = p.resolver.resolve("TCS")
    a = Article(source="google_news", source_type="news", title="TCS shares fall 1% as stock turns ex-dividend",
                published_at=datetime(2026, 11, 5, 10, 3, 7, tzinfo=IST))
    kept, stats = p._postprocess(info, [a])
    assert kept[0].tags[:1] == ["mechanical_price_adjustment"] and "price_recap" in kept[0].tags
    assert (kept[0].category, kept[0].impact_tier) == ("corporate_action_news", "low")
    assert stats["mechanical_tagged"] == 1


def test_select_units():
    w = SentimentSettings().unit_weights
    assert select_units("title", w) == {"title": 0.5}
    assert select_units("all", w) == w
    with pytest.raises(ValueError):
        select_units("headline", w)


# ---- backtester -------------------------------------------------------------------------
CAL = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5), date(2026, 3, 6),
       date(2026, 3, 9), date(2026, 3, 10), date(2026, 3, 11), date(2026, 3, 12), date(2026, 3, 13)]


def _art(i, title, pub, score, source_type="news", precision=None):
    rec = {"id": f"a{i}", "title": title, "source": "google_news", "source_type": source_type,
           "published_at": pub.isoformat(), "score": score, "weight": 1.0, "weight_factors": {"recency": 1.0}}
    if precision:
        rec["time_precision"] = precision
    return rec


def _write_sent(root, d, sym, arts):
    f = root / d.isoformat()
    f.mkdir(parents=True, exist_ok=True)
    (f / f"{sym}.json").write_text(json.dumps({"ticker": sym, "articles": arts}), encoding="utf-8")


@pytest.fixture
def bt(tmp_path):
    root = tmp_path / "sent"
    tue = date(2026, 3, 3)
    _write_sent(root, tue, "AAA", [
        # placeholder time (12:30 IST = 07:00 UTC), no precision field: an old file
        _art(1, "AAA wins big order", datetime(2026, 3, 3, 12, 30, tzinfo=IST), -0.8),
        # exact filing during the session
        _art(2, "Outcome of board meeting", datetime(2026, 3, 3, 10, 0, 7, tzinfo=IST), -0.6, "exchange_filing"),
        # exact news during the session
        _art(3, "AAA plant fire", datetime(2026, 3, 3, 11, 0, 9, tzinfo=IST), -0.9),
        _art(4, "AAA shares fall as stock turns ex-dividend", datetime(2026, 3, 3, 9, 30, 5, tzinfo=IST), -0.9),
    ])
    return BacktestSettings(sentiment_dir=root, output_dir=tmp_path / "out", half_life_hours=0, n_boot=200)


def _sig(cfg):
    sig = build_signals(load_items(cfg, CAL[0], CAL[-1]), CAL, cfg)
    return {(r.decision, r.date): r for r in sig.itertuples()}


def test_no_lookahead_on_date_only_items(bt):
    s = _sig(bt)
    tue, wed = date(2026, 3, 3), date(2026, 3, 4)
    # Tuesday open (09:15): nothing from Tuesday is known yet
    assert ("open", tue) not in s
    # Tuesday close (15:15): the exact 10:00 filing and 11:00 news, NOT the date-only item
    assert s[("close", tue)].n == 2
    # Wednesday open: the date-only item becomes known (23:59 Tue) alongside the rest
    assert s[("open", wed)].n == 3
    # Wednesday close: date-only item known since Tue 23:59 is inside [Tue 15:15, Wed 15:15)
    assert s[("close", wed)].n == 1
    assert s[("close", wed)].date_only_share == 1.0


def test_ex_dividend_items_zeroed_in_old_files(bt):
    items = load_items(bt, CAL[0], CAL[-1])
    assert "a4" not in set(items.id)
    assert "a4" in set(load_items(replace(bt, zero_mechanical=False), CAL[0], CAL[-1]).id)


def test_degrade_moves_exact_media_but_not_filings(bt):
    s = _sig(replace(bt, degrade_media=True))
    tue = date(2026, 3, 3)
    assert s[("close", tue)].n == 1  # only the exact filing survives at Tuesday close


def test_parity_detects_difference(bt):
    a = build_signals(load_items(bt, CAL[0], CAL[-1]), CAL, bt)
    b = build_signals(load_items(replace(bt, degrade_media=True), CAL[0], CAL[-1]), CAL, bt)
    par = parity(a, b, bt).set_index("decision")
    assert par.loc["close", "rows"] >= 2


def test_run_backtest_end_to_end(tmp_path):
    """Synthetic market where negative news days are followed by underperformance."""
    rng = np.random.default_rng(0)
    root = tmp_path / "sent"
    cal = [d.date() for d in pd.bdate_range("2026-01-05", periods=80)]
    syms = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    price = {s: 100.0 for s in syms}
    rows = []
    bad = {}
    for i, d in enumerate(cal):
        for s in syms:
            neg = rng.random() < 0.15
            if neg:
                bad[(s, i)] = True
                _write_sent(root, d, s, [_art(i * 10 + syms.index(s), f"{s} probe", datetime.combine(d, time(11, 0, 7), IST), -0.9),
                                         _art(i * 10 + syms.index(s) + 5, f"{s} fine", datetime.combine(d, time(11, 30, 7), IST), -0.5)])
            else:
                _write_sent(root, d, s, [_art(i * 10 + syms.index(s), f"{s} update", datetime.combine(d, time(11, 0, 7), IST), 0.3)])
    for i, d in enumerate(cal):
        for s in syms:
            drift = -0.004 if any(bad.get((s, j)) for j in range(max(0, i - 5), i)) else 0.0
            o = price[s]
            c = o * (1 + drift + rng.normal(0, 0.002))
            rows.append(dict(date=d.isoformat(), open=o, high=max(o, c), low=min(o, c), close=c, adj_close=c, volume=1000, symbol=s))
            price[s] = c
        rows.append(dict(date=d.isoformat(), open=1, high=1, low=1, close=1, adj_close=1, volume=0, symbol="NIFTY50"))
    prices = tmp_path / "prices.csv"
    pd.DataFrame(rows).to_csv(prices, index=False)
    cfg = BacktestSettings(sentiment_dir=root, output_dir=tmp_path / "out", n_boot=300, periods=2)
    rep = run_backtest(cal[0], cal[-1], prices, cfg, run_parity=True)
    m = pd.DataFrame(rep["metrics"])
    row = m[(m["mode"] == "as recorded") & (m.decision == "close") & (m.target == "close_5d") & (m.period == "all")].iloc[0]
    assert row.neg_rows > 20 and row.neg_gap_pts < -0.5 and row.neg_p < 0.05
    assert (tmp_path / "out" / f"{cal[0]}_{cal[-1]}" / "parity.csv").exists()
    assert rep["data"]["media_date_only_share"] == 0.0


def test_mislabelled_exact_placeholder_still_treated_as_date_only(bt):
    """Files scored from pre-fix news were labelled 'exact' by default; the placeholder
    time must still be recognised, or the date-only protection silently switches off."""
    f = next(bt.sentiment_dir.glob("*/AAA.json"))
    d = json.loads(f.read_text("utf-8"))
    for a in d["articles"]:
        a["time_precision"] = "exact"
    f.write_text(json.dumps(d), encoding="utf-8")
    items = load_items(bt, CAL[0], CAL[-1]).set_index("id")
    assert items.loc["a1", "precision"] == "date"   # 12:30 IST placeholder
    assert items.loc["a3", "precision"] == "exact"  # real 11:00:09 time


def test_engine_detects_placeholder_when_news_has_no_flag():
    from sentiment_engine.engine import time_precision

    assert time_precision({"source_type": "news", "published_at": "2026-03-03T12:30:00+05:30"}) == "date"
    assert time_precision({"source_type": "news", "published_at": "2026-03-03T11:00:09+05:30"}) == "exact"
    assert time_precision({"source_type": "news", "published_at": "2026-03-03T23:59:59+05:30",
                           "meta": {"time_precision": "date"}}) == "date"
    assert time_precision({"source_type": "exchange_filing", "published_at": "2026-03-03T16:00:00+05:30"}) == "exact"


# ---- body off by default, minor fixes --------------------------------------------------
def test_article_pages_off_by_default_and_cli_switch():
    from news_scraper.config import Settings
    import run_pipeline as rp

    assert Settings().fetch_article_body is False
    a = rp.build_parser().parse_args(["--start", "2026-11-02", "--with-body"])
    assert a.with_body and not a.no_body
    with pytest.raises(SystemExit):  # the two switches can't be combined
        rp.build_parser().parse_args(["--start", "2026-11-02", "--with-body", "--no-body"])


def test_title_units_send_only_headlines_to_model(tmp_path):
    from sentiment_engine.engine import SentimentEngine

    from .test_sentiment import KeywordScorer, _art, _write_news

    cfg = SentimentSettings(news_dir=tmp_path / "news", output_dir=tmp_path / "sent",
                            unit_weights=select_units("title", SentimentSettings().unit_weights))
    body = "Profit rose sharply on strong demand across all segments this quarter. " * 5
    _write_news(cfg, "INFY", [_art(1, "Infosys shares jump", summary="A long enough summary sentence.", body=body)])

    class Spy(KeywordScorer):
        def score(self, texts):
            self.texts = list(texts)
            return super().score(texts)

    spy = Spy()
    out = SentimentEngine(spy, cfg).run(date(2026, 11, 4))
    assert spy.texts == ["Infosys shares jump"]
    assert out[0]["articles"][0]["units_used"] == ["title"]


def test_first_day_looks_back_to_previous_trading_day(tmp_path):
    root = tmp_path / "sent"
    fri, mon = date(2026, 3, 6), date(2026, 3, 9)
    _write_sent(root, fri, "AAA", [_art(1, "AAA results beat", datetime(2026, 3, 6, 17, 0, 7, tzinfo=IST), 0.7,
                                        "exchange_filing")])
    cfg = BacktestSettings(sentiment_dir=root, output_dir=tmp_path / "out", half_life_hours=0)
    items = load_items(cfg, mon, mon)
    sig = build_signals(items, [mon], cfg, full_calendar=[fri, mon])
    assert set(sig.decision) == {"open", "close"}  # Friday 17:00 filing reaches Monday's decisions
    assert build_signals(items, [mon], cfg).empty  # without the full calendar it falls back to 24 hours
