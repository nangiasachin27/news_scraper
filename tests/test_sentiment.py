import csv
import json
from dataclasses import replace
from datetime import date, datetime

import pytest

from news_scraper.timeutils import IST
from sentiment_engine.aggregate import aggregate, article_weight, body_sentences, combine_units, text_units
from sentiment_engine.config import SentimentSettings
from sentiment_engine.engine import SentimentEngine

WINDOW_END = datetime(2026, 11, 4, 15, 30, tzinfo=IST)


class KeywordScorer:
    """Deterministic stand-in for FinBERT."""

    name = "test/keyword"

    def __init__(self):
        self.calls = 0

    def score(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            t = t.lower()
            if any(w in t for w in ("rises", "jump", "affirms", "upgrade")):
                out.append({"positive": 0.9, "negative": 0.05, "neutral": 0.05})
            elif any(w in t for w in ("falls", "downgrade", "penalty", "slump")):
                out.append({"positive": 0.05, "negative": 0.9, "neutral": 0.05})
            else:
                out.append({"positive": 0.1, "negative": 0.1, "neutral": 0.8})
        return out


def _art(i, title, **kw):
    base = {
        "id": f"id{i}", "title": title, "source": "google_news", "source_type": "news",
        "published_at": "2026-11-04T15:30:00+05:30", "impact_tier": "high", "relevance_score": 1.0,
        "tags": [], "also_reported_by": [], "summary": None, "body": None, "meta": {},
    }
    base.update(kw)
    return base


@pytest.fixture
def cfg(tmp_path):
    return SentimentSettings(news_dir=tmp_path / "news", output_dir=tmp_path / "sentiment")


# ---- pure functions -------------------------------------------------------------
def test_weight_factors_multiply(cfg):
    a = _art(1, "x", impact_tier="medium", relevance_score=0.5, tags=["roundup"],
             also_reported_by=["Mint", "ET"], published_at="2026-11-04T03:30:00+05:30")
    w, f = article_weight(a, WINDOW_END, cfg)
    assert f == {"source_type": 1.0, "impact_tier": 0.6, "relevance": 0.5, "tags": 0.5,
                 "syndication": 1.5, "recency": 0.5}  # 12h old at 12h half-life
    assert w == pytest.approx(0.6 * 0.5 * 0.5 * 1.5 * 0.5)


@pytest.mark.parametrize("override, expected", [
    ({"source_type": "corporate_action", "tags": ["mechanical_price_adjustment"]}, 0.0),
    ({"impact_tier": "noise"}, 0.0),
])
def test_zero_weight_cases(cfg, override, expected):
    assert article_weight(_art(1, "x", **override), WINDOW_END, cfg)[0] == expected


def test_text_units_skip_duplicate_summary_and_cap_body(cfg):
    body = " ".join(f"Sentence number {i} has enough characters to count." for i in range(20))
    units = text_units(_art(1, "Same text", summary="same TEXT", body=body), cfg)
    names = [n for n, _ in units]
    assert names.count("title") == 1 and "summary" not in names
    assert names.count("body") == cfg.max_body_sentences


def test_body_sentences_drop_fragments(cfg):
    assert body_sentences("Short. This one is a properly long sentence about earnings growth.", cfg) == [
        "This one is a properly long sentence about earnings growth."
    ]


def test_combine_units_renormalises_over_present_units(cfg):
    pos = {"positive": 1.0, "negative": 0.0, "neutral": 0.0}
    neg = {"positive": 0.0, "negative": 1.0, "neutral": 0.0}
    # title 0.5 vs summary 0.3 -> renormalised 0.625 / 0.375
    r = combine_units([("title", pos), ("summary", neg)], cfg)
    assert r["score"] == pytest.approx(0.625 - 0.375)
    assert r["units_used"] == ["title", "summary"]
    # body sentences averaged before weighting
    r2 = combine_units([("title", pos), ("body", pos), ("body", neg)], cfg)
    assert r2["score"] == pytest.approx((0.5 * 1 + 0.2 * 0) / 0.7, abs=1e-4)  # output rounded to 4dp


def test_aggregate_neutral_dilutes_and_insufficient_flag(cfg):
    recs = [
        {"id": "a", "title": "a", "score": 0.8, "weight": 1.0},
        {"id": "b", "title": "b", "score": 0.0, "weight": 1.0},
        {"id": "c", "title": "c", "score": None, "weight": 1.0},
        {"id": "d", "title": "d", "score": -0.9, "weight": 0.0},
    ]
    agg, drivers = aggregate(recs, cfg)
    assert agg["score"] == pytest.approx(0.4) and agg["label"] == "positive"
    assert agg["n_scored"] == 2 and agg["insufficient"] is False
    assert drivers[0]["id"] == "a" and drivers[0]["contribution"] == pytest.approx(0.4)
    empty, _ = aggregate([], cfg)
    assert empty["score"] is None and empty["insufficient"] is True and empty["label"] == "none"


# ---- engine end to end ------------------------------------------------------------
def _write_news(cfg, symbol, articles, failed=()):
    folder = cfg.news_dir / "2026-11-04"
    folder.mkdir(parents=True, exist_ok=True)
    sources = {"google_news": {"ok": True, "skipped": False}}
    for f in failed:
        sources[f] = {"ok": False, "skipped": False, "error": "down"}
    payload = {
        "ticker": {"symbol": symbol},
        "window": {"start": "2026-11-03T15:30:00+05:30", "end": "2026-11-04T15:30:00+05:30"},
        "generated_at": "2026-11-04T16:00:00+05:30",
        "sources": sources,
        "articles": articles,
    }
    (folder / f"{symbol}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_engine_end_to_end(cfg):
    _write_news(cfg, "RELIANCE", [
        _art(1, "S&P affirms RIL rating", summary="Outlook stable after Jio listing."),
        _art(2, "Ex-date: Dividend Rs 10", source_type="corporate_action", tags=["mechanical_price_adjustment"]),
        _art(3, "Stocks to watch today", tags=["roundup"], impact_tier="unclassified"),
    ])
    _write_news(cfg, "TCS", [_art(1, "TCS shares slump after weak guidance")], failed=["bse_announcements"])
    scorer = KeywordScorer()
    results = SentimentEngine(scorer, cfg).run(date(2026, 11, 4))
    assert scorer.calls == 1  # one batched pass across all tickers

    rel = json.loads((cfg.output_dir / "2026-11-04" / "RELIANCE.json").read_text("utf-8"))
    by_id = {a["id"]: a for a in rel["articles"]}
    assert by_id["id2"]["excluded_reason"] == "zero_weight" and by_id["id2"]["score"] is None
    assert by_id["id1"]["units_used"] == ["title", "summary"] and by_id["id1"]["label"] == "positive"
    assert rel["aggregate"]["label"] == "positive" and rel["drivers"][0]["id"] == "id1"
    assert rel["data_quality"] == {"degraded": False, "failed_sources": []}

    tcs = next(r for r in results if r["ticker"] == "TCS")
    assert tcs["aggregate"]["label"] == "negative"
    assert tcs["data_quality"] == {"degraded": True, "failed_sources": ["bse_announcements"]}

    rows = list(csv.DictReader(open(cfg.output_dir / "2026-11-04" / "_summary.csv", encoding="utf-8")))
    assert [r["symbol"] for r in rows] == ["RELIANCE", "TCS"]
    assert rows[1]["degraded"] == "True" and rows[1]["failed_sources"] == "bse_announcements"


def test_engine_ticker_filter_and_missing_folder(cfg):
    _write_news(cfg, "RELIANCE", [_art(1, "RIL shares jump")])
    _write_news(cfg, "TCS", [_art(1, "TCS flat")])
    out = SentimentEngine(KeywordScorer(), cfg).run(date(2026, 11, 4), ["reliance.ns"])
    assert [r["ticker"] for r in out] == ["RELIANCE"]
    with pytest.raises(FileNotFoundError):
        SentimentEngine(KeywordScorer(), cfg).run(date(2026, 11, 5))


def test_scorer_length_mismatch_is_an_error(cfg):
    class Broken(KeywordScorer):
        def score(self, texts):
            return super().score(texts)[:-1]

    _write_news(cfg, "RELIANCE", [_art(1, "RIL shares jump"), _art(2, "x")])
    with pytest.raises(RuntimeError, match="scorer returned"):
        SentimentEngine(Broken(), cfg).run(date(2026, 11, 4))


def test_no_articles_gives_insufficient_not_crash(cfg):
    _write_news(cfg, "INFY", [])
    out = SentimentEngine(KeywordScorer(), replace(cfg), ).run(date(2026, 11, 4))
    assert out[0]["aggregate"]["insufficient"] is True and out[0]["aggregate"]["score"] is None
