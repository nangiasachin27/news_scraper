"""Pure functions: article -> text units, article -> weight, scored articles -> aggregate.

Kept free of model code so the weighting is unit-testable and can later be fitted
(e.g. regress next-day returns on these factors) without touching inference.

Per-unit sentiment s = p_positive - p_negative, in [-1, 1].
Article score  = unit-weighted mean of s over present units (body = mean over sentences).
Ticker score   = article-weight-weighted mean of article scores. Neutral articles pull
                 the aggregate toward 0 on purpose: a day of mostly neutral flow is not
                 a strongly positive day because one item was positive.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from .config import SentimentSettings

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def body_sentences(body: str, cfg: SentimentSettings) -> list[str]:
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(body or "")]
    return [s for s in sentences if len(s) >= cfg.min_sentence_chars][: cfg.max_body_sentences]


def text_units(article: dict[str, Any], cfg: SentimentSettings) -> list[tuple[str, str]]:
    """[(unit_name, text)] - unit_name in title|summary|body. Summary is skipped when
    it merely repeats the title (common for exchange filings)."""
    units: list[tuple[str, str]] = []
    title = (article.get("title") or "").strip()
    summary = (article.get("summary") or "").strip()
    if title:
        units.append(("title", title))
    if summary and summary.lower() != title.lower():
        units.append(("summary", summary))
    for sentence in body_sentences(article.get("body") or "", cfg):
        units.append(("body", sentence))
    return units


def article_weight(article: dict[str, Any], window_end: datetime, cfg: SentimentSettings) -> tuple[float, dict[str, float]]:
    factors: dict[str, float] = {
        "source_type": cfg.source_type_weights.get(article.get("source_type"), 0.5),
        "impact_tier": cfg.tier_weights.get(article.get("impact_tier") or "unclassified", 0.5),
        "relevance": float(article.get("relevance_score") or 1.0),
    }
    tag_factor = 1.0
    for tag in article.get("tags") or []:
        tag_factor *= cfg.tag_multipliers.get(tag, 1.0)
    factors["tags"] = tag_factor
    outlets = min(len(article.get("also_reported_by") or []), cfg.syndication_cap)
    factors["syndication"] = 1.0 + cfg.syndication_step * outlets
    if cfg.half_life_hours > 0:
        published = datetime.fromisoformat(article["published_at"])
        age_h = max(0.0, (window_end - published).total_seconds() / 3600.0)
        factors["recency"] = 0.5 ** (age_h / cfg.half_life_hours)
    else:
        factors["recency"] = 1.0
    weight = math.prod(factors.values())
    return weight, {k: round(v, 4) for k, v in factors.items()}


def combine_units(units: list[tuple[str, dict[str, float]]], cfg: SentimentSettings) -> dict[str, Any]:
    """units: [(unit_name, probs)] -> article-level probs and score."""
    by_unit: dict[str, list[dict[str, float]]] = {}
    for name, probs in units:
        by_unit.setdefault(name, []).append(probs)
    total_w = sum(cfg.unit_weights.get(n, 0.0) for n in by_unit)
    if total_w <= 0:
        return {"probs": None, "score": None, "units_used": []}
    agg = {"positive": 0.0, "negative": 0.0, "neutral": 0.0}
    for name, rows in by_unit.items():
        w = cfg.unit_weights.get(name, 0.0) / total_w
        for label in agg:
            agg[label] += w * sum(r[label] for r in rows) / len(rows)
    return {
        "probs": {k: round(v, 4) for k, v in agg.items()},
        "score": round(agg["positive"] - agg["negative"], 4),
        "units_used": sorted(by_unit, key=["title", "summary", "body"].index),
    }


def label_for(score: float | None, cfg: SentimentSettings) -> str:
    if score is None:
        return "none"
    if score > cfg.label_threshold:
        return "positive"
    if score < -cfg.label_threshold:
        return "negative"
    return "neutral"


def aggregate(scored: list[dict[str, Any]], cfg: SentimentSettings) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """scored: article records with 'score' and 'weight'. Returns (aggregate, drivers)."""
    usable = [a for a in scored if a.get("score") is not None and a.get("weight", 0) > 0]
    weight_sum = sum(a["weight"] for a in usable)
    if weight_sum > 0:
        mean = sum(a["weight"] * a["score"] for a in usable) / weight_sum
        var = sum(a["weight"] * (a["score"] - mean) ** 2 for a in usable) / weight_sum
    else:
        mean, var = None, None
    labels = [label_for(a["score"], cfg) for a in usable]
    agg = {
        "score": round(mean, 4) if mean is not None else None,
        "label": label_for(mean, cfg),
        "weight_sum": round(weight_sum, 4),
        "dispersion": round(math.sqrt(var), 4) if var is not None else None,
        "n_scored": len(usable),
        "n_positive": labels.count("positive"),
        "n_negative": labels.count("negative"),
        "n_neutral": labels.count("neutral"),
        "insufficient": weight_sum < cfg.min_weight_sum,
    }
    drivers = sorted(usable, key=lambda a: abs(a["weight"] * a["score"]), reverse=True)[: cfg.top_drivers]
    driver_rows = [
        {
            "id": a["id"],
            "title": a["title"],
            "score": a["score"],
            "weight": round(a["weight"], 4),
            "contribution": round(a["weight"] * a["score"] / weight_sum, 4),
        }
        for a in drivers
    ]
    return agg, driver_rows
