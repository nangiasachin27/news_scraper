"""Read data/news/<date>/*.json -> score -> write data/sentiment/<date>/<SYMBOL>.json
plus data/sentiment/<date>/_summary.csv (one row per ticker, for the trading systems).

All text units across all tickers of a date are scored in one batched pass, which is
what keeps GPU utilisation high; assembly per ticker happens afterwards.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from news_scraper.timeutils import is_date_only, now_ist
from news_scraper.writer import write_json

from .aggregate import aggregate, article_weight, combine_units, label_for, text_units
from .config import SCHEMA_VERSION, SentimentSettings
from .scorers import Scorer

log = logging.getLogger(__name__)

SUMMARY_FIELDS = [
    "symbol", "score", "label", "weight_sum", "n_scored", "n_positive", "n_negative",
    "n_neutral", "dispersion", "insufficient", "degraded", "failed_sources", "top_driver",
]


def time_precision(art: dict[str, Any]) -> str:
    """'date' or 'exact'. News scraped before the date-only fix carries no flag, so
    placeholder times (a whole hour in UTC) are detected here rather than assumed exact."""
    flag = (art.get("meta") or {}).get("time_precision")
    if flag:
        return flag
    if art.get("source_type") == "news" and art.get("published_at"):
        return "date" if is_date_only(datetime.fromisoformat(art["published_at"])) else "exact"
    return "exact"


def summary_row(payload: dict[str, Any]) -> dict[str, Any]:
    """One CSV row (SUMMARY_FIELDS) from a per-ticker sentiment payload."""
    a = payload["aggregate"]
    return {
        "symbol": payload["ticker"],
        **{k: a[k] for k in ("score", "label", "weight_sum", "n_scored", "n_positive",
                             "n_negative", "n_neutral", "dispersion", "insufficient")},
        "degraded": payload["data_quality"]["degraded"],
        "failed_sources": ";".join(payload["data_quality"]["failed_sources"]),
        "top_driver": payload["drivers"][0]["title"] if payload["drivers"] else "",
    }


@dataclass
class _Pending:
    path: Path
    news: dict[str, Any]
    records: list[dict[str, Any]]
    units: list[tuple[int, str, str]]  # (record index, unit name, text)


class SentimentEngine:
    def __init__(self, scorer: Scorer, settings: SentimentSettings | None = None, materiality=None) -> None:
        self.scorer = scorer
        self.settings = settings or SentimentSettings()
        self.materiality = materiality
        if self.materiality is None and self.settings.materiality_model:
            from .scorers import ZeroShotMateriality

            self.materiality = ZeroShotMateriality(self.settings.materiality_model, self.settings.device,
                                                   self.settings.materiality_batch_size, self.settings.fp16)

    def _apply_materiality(self, pending: list["_Pending"]) -> None:
        cfg = self.settings
        targets = [(rec, rec["title"]) for p in pending for rec in p.records
                   if rec["weight"] > 0 and rec["source_type"] in cfg.materiality_source_types and rec.get("title")]
        if not targets:
            return
        scores = self.materiality.score([t for _, t in targets])
        if len(scores) != len(targets):
            raise RuntimeError(f"materiality scorer returned {len(scores)} results for {len(targets)} inputs")
        for (rec, _), m in zip(targets, scores):
            factor = cfg.materiality_floor + (1 - cfg.materiality_floor) * m
            rec["materiality"] = round(m, 4)
            rec["weight_factors"]["materiality"] = round(factor, 4)
            rec["weight"] = round(rec["weight"] * factor, 4)

    # ---- discovery -------------------------------------------------------------
    def news_files(self, d: date, tickers: list[str] | None = None) -> list[Path]:
        folder = Path(self.settings.news_dir) / d.isoformat()
        if not folder.is_dir():
            raise FileNotFoundError(f"no news folder for {d}: {folder} (run news_scraper first)")
        files = sorted(p for p in folder.glob("*.json") if not p.name.startswith("_"))
        if tickers:
            wanted = {t.upper().removesuffix(".NS") for t in tickers}
            files = [p for p in files if p.stem.upper() in wanted]
            missing = wanted - {p.stem.upper() for p in files}
            if missing:
                log.warning("no news file for: %s", ", ".join(sorted(missing)))
        return files

    # ---- run -------------------------------------------------------------------
    def run(self, d: date, tickers: list[str] | None = None) -> list[dict[str, Any]]:
        pending = [self._prepare(p) for p in self.news_files(d, tickers)]
        if self.materiality is not None:
            self._apply_materiality(pending)
        flat = [(i, u) for i, p in enumerate(pending) for u in p.units]
        log.info("scoring %d text units across %d tickers with %s", len(flat), len(pending), self.scorer.name)
        probs = self.scorer.score([u[2] for _, u in flat]) if flat else []
        if len(probs) != len(flat):
            raise RuntimeError(f"scorer returned {len(probs)} results for {len(flat)} inputs")

        per_record: dict[tuple[int, int], list[tuple[str, dict[str, float]]]] = {}
        for (pi, (ri, unit_name, _)), p in zip(flat, probs):
            per_record.setdefault((pi, ri), []).append((unit_name, p))

        out_dir = Path(self.settings.output_dir) / d.isoformat()
        summaries = []
        for pi, pend in enumerate(pending):
            for ri, rec in enumerate(pend.records):
                if rec["excluded_reason"]:
                    continue
                combined = combine_units(per_record.get((pi, ri), []), self.settings)
                rec.update(combined)
                rec["label"] = label_for(combined["score"], self.settings)
            payload = self._assemble(pend, d)
            write_json(out_dir / pend.path.name, payload)
            summaries.append(payload)
        self._write_summary(out_dir / "_summary.csv", summaries)
        return summaries

    # ---- internals ---------------------------------------------------------------
    def _prepare(self, path: Path) -> _Pending:
        news = json.loads(path.read_text(encoding="utf-8"))
        window_end = datetime.fromisoformat(news["window"]["end"])
        records, units = [], []
        for art in news.get("articles", []):
            weight, factors = article_weight(art, window_end, self.settings)
            rec = {
                "id": art["id"],
                "title": art.get("title"),
                "source": art.get("source"),
                "source_type": art.get("source_type"),
                "publisher": art.get("publisher"),
                "published_at": art.get("published_at"),
                "category": art.get("category"),
                "impact_tier": art.get("impact_tier"),
                "text_source": (art.get("meta") or {}).get("text_source"),
                # "date" = the feed gave only a date; published_at is then end of that day.
                # Backtests use this to decide when the item was knowable.
                "time_precision": time_precision(art),
                "tags": art.get("tags") or [],
                "weight": round(weight, 4),
                "weight_factors": factors,
                "excluded_reason": None if weight > 0 else "zero_weight",
                "probs": None,
                "score": None,
                "label": "none",
                "units_used": [],
            }
            if weight > 0:
                art_units = text_units(art, self.settings)
                if not art_units:
                    rec["excluded_reason"] = "no_text"
                units.extend((len(records), name, text) for name, text in art_units)
            records.append(rec)
        return _Pending(path=path, news=news, records=records, units=units)

    def _assemble(self, pend: _Pending, d: date) -> dict[str, Any]:
        news = pend.news
        failed = [n for n, st in news.get("sources", {}).items() if not st.get("ok") and not st.get("skipped")]
        agg, drivers = aggregate(pend.records, self.settings)
        return {
            "schema_version": SCHEMA_VERSION,
            "ticker": news["ticker"]["symbol"],
            "date": d.isoformat(),
            "model": self.scorer.name,
            "materiality_model": self.materiality.name if self.materiality is not None else None,
            "generated_at": now_ist().isoformat(),
            "input": {
                "file": str(pend.path),
                "news_generated_at": news.get("generated_at"),
                "window": news.get("window"),
            },
            # A failed source means "unknown", not "no news": consumers should
            # discount a degraded aggregate rather than read silence as neutral.
            "data_quality": {"degraded": bool(failed), "failed_sources": failed},
            "aggregate": agg,
            "drivers": drivers,
            "articles": sorted(pend.records, key=lambda r: r["weight"], reverse=True),
        }

    @staticmethod
    def _write_summary(path: Path, payloads: list[dict[str, Any]]) -> None:
        """Merge by symbol into an existing summary, so scoring a subset of tickers
        doesn't drop rows for the others."""
        rows: dict[str, dict[str, Any]] = {}
        if path.exists():
            with open(path, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if r.get("symbol"):
                        rows[r["symbol"]] = {k: r.get(k, "") for k in SUMMARY_FIELDS}
        for p in payloads:
            rows[p["ticker"]] = summary_row(p)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=SUMMARY_FIELDS, lineterminator="\n")
        writer.writeheader()
        for symbol in sorted(rows):
            writer.writerow(rows[symbol])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(buf.getvalue(), encoding="utf-8")
        os.replace(tmp, path)
