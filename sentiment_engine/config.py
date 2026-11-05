"""Sentiment engine settings. Every weight is a named, overridable default so the
weighting can be tuned (and later fitted against forward returns) without code edits."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from news_scraper.config import PROJECT_DIR

SCHEMA_VERSION = "1.0"


UNIT_NAMES = ("title", "summary", "body")


def select_units(spec: str, unit_weights: dict[str, float]) -> dict[str, float]:
    """'all', 'title' or 'title,summary' -> unit weights restricted to those units.
    Headline-only scoring is the backtest robustness check: bodies of old articles are
    downloaded long after publication and may have been edited since."""
    if spec.strip().lower() == "all":
        return dict(unit_weights)
    chosen = [u.strip().lower() for u in spec.split(",") if u.strip()]
    bad = [u for u in chosen if u not in UNIT_NAMES]
    if bad or not chosen:
        raise ValueError(f"--units: expected 'all' or a list of {', '.join(UNIT_NAMES)}; got {spec!r}")
    return {u: w for u, w in unit_weights.items() if u in chosen}


@dataclass
class SentimentSettings:
    # ---- io ----------------------------------------------------------------
    news_dir: Path = PROJECT_DIR / "data" / "news"
    output_dir: Path = PROJECT_DIR / "data" / "sentiment"

    # ---- model -------------------------------------------------------------
    model_name: str = "ProsusAI/finbert"
    device: str = "auto"  # auto | cuda | cpu
    batch_size: int = 32
    max_length: int = 512
    fp16: bool = True  # only applied on CUDA

    # ---- text units per article ------------------------------------------
    # FinBERT is sentence-level: score title / summary / first body sentences
    # separately, then combine with these weights (renormalised over present units).
    unit_weights: dict[str, float] = field(
        default_factory=lambda: {"title": 0.5, "summary": 0.3, "body": 0.2}
    )
    max_body_sentences: int = 8
    min_sentence_chars: int = 30

    # ---- article weight factors (multiplied together) ----------------------
    tier_weights: dict[str, float] = field(
        default_factory=lambda: {
            "high": 1.0, "medium": 0.6, "low": 0.3, "unclassified": 0.5, "noise": 0.0,
        }
    )
    source_type_weights: dict[str, float] = field(
        default_factory=lambda: {
            "news": 1.0,
            # Filing text is procedural ("Outcome of Board Meeting"); FinBERT reads it as
            # neutral regardless of what the numbers say. Results need XBRL deltas, not tone.
            "exchange_filing": 0.4,
            "financial_result": 0.4,
            "board_meeting": 0.2,
            "corporate_action": 0.0,  # ex-dates: mechanical price adjustment, not sentiment
        }
    )
    tag_multipliers: dict[str, float] = field(
        default_factory=lambda: {
            "roundup": 0.5,
            "counterparty": 0.3,
            "acronym_only": 0.6,
            "source_tagged": 0.7,
            "mechanical_price_adjustment": 0.0,
            # Headlines reporting a move already in the price. Left at 1.0: the
            # Jan-Sep 2026 out-of-sample test found no gain from down-weighting them.
            "price_recap": 1.0,
        }
    )
    syndication_step: float = 0.25  # +25% weight per extra outlet...
    syndication_cap: int = 4  # ...up to 4 outlets (x2.0 max)
    half_life_hours: float = 12.0  # recency decay towards window end; 0 disables

    # ---- aggregate ------------------------------------------------------------
    label_threshold: float = 0.15  # |score| below this -> neutral
    min_weight_sum: float = 0.5  # below this the aggregate is flagged insufficient
    top_drivers: int = 3
