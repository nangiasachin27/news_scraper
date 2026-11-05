import json
from dataclasses import replace
from datetime import date

import pytest

from sentiment_backtest.core import BacktestSettings, load_items
from sentiment_engine.config import SentimentSettings
from sentiment_engine.engine import SentimentEngine
from sentiment_engine.scorers import MATERIAL_LABEL, ROUTINE_LABEL, ZeroShotMateriality

from .test_sentiment import KeywordScorer, _art, _write_news


class FakeMateriality:
    name = "test/materiality"

    def __init__(self):
        self.calls = 0
        self.seen = []

    def score(self, texts):
        self.calls += 1
        self.seen.extend(texts)
        return [0.05 if "catering" in t.lower() else 0.9 for t in texts]


@pytest.fixture
def cfg(tmp_path):
    return SentimentSettings(news_dir=tmp_path / "news", output_dir=tmp_path / "sentiment", materiality_floor=0.1)


def _run(cfg, mat):
    _write_news(cfg, "INFY", [
        _art(1, "LPG shortage forces Infosys to limit catering services on campuses"),
        _art(2, "Infosys shares jump after deal win"),
        _art(3, "Outcome of board meeting", source_type="exchange_filing"),
    ])
    SentimentEngine(KeywordScorer(), cfg, materiality=mat).run(date(2026, 11, 4))
    return json.loads((cfg.output_dir / "2026-11-04" / "INFY.json").read_text("utf-8"))


def test_materiality_scales_news_weights_only(cfg):
    mat = FakeMateriality()
    out = _run(cfg, mat)
    arts = {a["id"]: a for a in out["articles"]}
    assert mat.calls == 1 and len(mat.seen) == 2  # one batch; the filing is not scored
    lpg, deal, filing = arts["id1"], arts["id2"], arts["id3"]
    assert lpg["materiality"] == 0.05
    assert lpg["weight_factors"]["materiality"] == pytest.approx(0.1 + 0.9 * 0.05)
    assert deal["weight_factors"]["materiality"] == pytest.approx(0.1 + 0.9 * 0.9)
    assert "materiality" not in filing["weight_factors"] and "materiality" not in filing
    assert out["materiality_model"] == "test/materiality"
    assert out["drivers"][0]["id"] == "id2"  # the material story now leads


def test_without_materiality_the_trivial_story_dominates(cfg):
    out = _run(cfg, None)
    assert out["materiality_model"] is None
    lpg = next(a for a in out["articles"] if a["id"] == "id1")
    assert "materiality" not in lpg["weight_factors"]


def test_backtester_can_divide_materiality_back_out(cfg, tmp_path):
    _run(cfg, FakeMateriality())
    bt = BacktestSettings(sentiment_dir=cfg.output_dir, output_dir=tmp_path / "bt")
    on = load_items(bt, date(2026, 11, 4), date(2026, 11, 4)).set_index("id")
    off = load_items(replace(bt, use_materiality=False), date(2026, 11, 4), date(2026, 11, 4)).set_index("id")
    assert on.loc["id1", "base_weight"] < off.loc["id1", "base_weight"]
    assert on.loc["id1", "base_weight"] == pytest.approx(off.loc["id1", "base_weight"] * (0.1 + 0.9 * 0.05), rel=1e-3)
    assert on.loc["id3", "base_weight"] == off.loc["id3", "base_weight"]  # filing untouched
    assert on.loc["id1", "materiality"] == 0.05


def test_zero_shot_label_mapping():
    z = ZeroShotMateriality.__new__(ZeroShotMateriality)
    z.batch_size = 2

    def pipe(texts, candidate_labels, hypothesis_template, multi_label, batch_size):
        assert candidate_labels == [MATERIAL_LABEL, ROUTINE_LABEL] and not multi_label
        # the pipeline returns labels sorted by score, so the order varies per row
        return [{"labels": [ROUTINE_LABEL, MATERIAL_LABEL], "scores": [0.8, 0.2]},
                {"labels": [MATERIAL_LABEL, ROUTINE_LABEL], "scores": [0.7, 0.3]}]

    z._pipe = pipe
    assert z.score(["a", "b"]) == [0.2, 0.7]
    assert z.score([]) == []
