"""CLI.

  python -m sentiment_engine run --date 2026-11-04
  python -m sentiment_engine run --date 2026-11-04 --tickers RELIANCE TCS
  python -m sentiment_engine run --date 2026-11-04 --device cpu --model yiyanghkust/finbert-tone
  python -m sentiment_engine check          # load model, score 3 test sentences
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

from news_scraper.timeutils import now_ist

from .config import SentimentSettings, select_units


def _parse_date(v: str) -> date:
    try:
        return date.fromisoformat(v)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {v!r}") from exc


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS)
    common.add_argument("--model", help="Hugging Face model id (default ProsusAI/finbert)")
    common.add_argument("--device", choices=["auto", "cuda", "cpu"])
    common.add_argument("--batch-size", type=int)
    common.add_argument("--units", help="text units to score: all (default), title, or title,summary")
    common.add_argument("--materiality", action="store_true",
                        help="weight news by a zero-shot materiality score of the headline (experimental)")
    common.add_argument("--materiality-model", help="NLI model for --materiality (default facebook/bart-large-mnli)")

    p = argparse.ArgumentParser(prog="sentiment_engine", description="FinBERT sentiment over news_scraper output")
    p.add_argument("-v", "--verbose", action="count", default=0)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", parents=[common], help="score a date folder")
    r.add_argument("--date", type=_parse_date, help="YYYY-MM-DD (default: today IST)")
    r.add_argument("--tickers", nargs="+")
    r.add_argument("--news-dir", type=Path)
    r.add_argument("--output-dir", type=Path)
    sub.add_parser("check", parents=[common], help="load the model and score sample sentences")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    level = logging.WARNING if args.verbose == 0 else logging.INFO if args.verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    overrides = {k: v for k, v in {
        "model_name": args.model, "device": args.device, "batch_size": args.batch_size,
        "news_dir": getattr(args, "news_dir", None), "output_dir": getattr(args, "output_dir", None),
    }.items() if v}
    if args.materiality or args.materiality_model:
        overrides["materiality_model"] = args.materiality_model or "facebook/bart-large-mnli"
    if args.units:
        try:
            overrides["unit_weights"] = select_units(args.units, SentimentSettings().unit_weights)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    cfg = replace(SentimentSettings(), **overrides)

    from .scorers import FinBERTScorer  # heavy import deferred until needed

    scorer = FinBERTScorer(cfg.model_name, cfg.device, cfg.batch_size, cfg.max_length, cfg.fp16)
    print(f"model: {scorer.name}  device: {scorer.device}")

    if args.cmd == "check":
        samples = [
            "Reliance Industries Q2 net profit rises 9% on strong retail and Jio growth",
            "S&P downgrades company outlook to negative on rising debt",
            "Board meeting scheduled on November 10 to consider financial results",
        ]
        for text, probs in zip(samples, scorer.score(samples)):
            s = probs["positive"] - probs["negative"]
            print(f"{s:+.3f}  pos={probs['positive']:.2f} neg={probs['negative']:.2f} neu={probs['neutral']:.2f}  {text}")
        if cfg.materiality_model:
            from .scorers import ZeroShotMateriality

            mat = ZeroShotMateriality(cfg.materiality_model, cfg.device, cfg.materiality_batch_size, cfg.fp16)
            heads = [
                "LPG shortage forces Infosys to limit catering services on campuses",
                "Infosys cuts FY27 revenue growth guidance to 1-3%",
                "SEBI issues show-cause notice to HDFC Bank over disclosure lapses",
                "TCS wins $1 billion deal from European bank",
                "Bharti Airtel launches new prepaid plan with OTT bundle",
            ]
            print(f"\nmateriality model: {mat.name}  device: {mat.device}")
            for text, m in zip(heads, mat.score(heads)):
                print(f"{m:.2f}  {text}")
        return 0

    from .engine import SentimentEngine

    run_date = args.date or now_ist().date()
    results = SentimentEngine(scorer, cfg).run(run_date, args.tickers)
    for r in results:
        a = r["aggregate"]
        flags = [f for f, on in (("INSUFFICIENT", a["insufficient"]), ("DEGRADED", r["data_quality"]["degraded"])) if on]
        score = f"{a['score']:+.3f}" if a["score"] is not None else "  n/a "
        print(f"{r['ticker']:<14} {score}  {a['label']:<8} n={a['n_scored']:<3} w={a['weight_sum']:.2f}  {' '.join(flags)}")
    print(f"\noutput: {Path(cfg.output_dir) / run_date.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
