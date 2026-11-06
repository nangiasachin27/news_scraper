"""Point-in-time sentiment backtest.

Decisions
  open   at 09:15 on trading day T  -> open T to close T, open T to close T+4
  close  at 15:15 on trading day T  -> close T to close T+1, close T to close T+5
Signal for a decision at time t on day T: items with  t_prev <= known_at < t, where
t_prev is the same decision time on the previous trading day, each weighted by the
sentiment engine's article weight (recency removed) times a fresh recency decay from t.

Returns are abnormal: stock minus NIFTY 50 over the same span. Statistics resample
blocks of consecutive trading days (stocks on one day move together, and 5-day
returns on neighbouring days overlap).

Parity mode recomputes every signal with all media timestamps cut back to dates, as a
backfill would see them. Run it on live-collected days to measure how far the
backtest's view of the signal is from production's.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from news_scraper.classifier import is_mechanical
from news_scraper.config import PROJECT_DIR
from news_scraper.timeutils import IST, is_date_only, known_at

log = logging.getLogger(__name__)

TARGETS = {
    "open": ("open_close", "open_5d"),
    "close": ("close_1d", "close_5d"),
}
TARGET_LABELS = {
    "open_close": "open T → close T",
    "open_5d": "open T → close T+4",
    "close_1d": "close T → close T+1",
    "close_5d": "close T → close T+5",
}


@dataclass
class BacktestSettings:
    sentiment_dir: Path = PROJECT_DIR / "data" / "sentiment"
    output_dir: Path = PROJECT_DIR / "data" / "backtest"
    decisions: dict[str, str] = field(default_factory=lambda: {"open": "09:15", "close": "15:15"})
    half_life_hours: float = 12.0
    min_weight: float = 0.5          # signal rows below this total weight are treated as no signal
    veto_score: float = -0.25        # "negative" rule, as in the trading gate
    veto_weight: float = 1.0
    positive_score: float = 0.5      # "strongly positive" rule
    article_neg: float = -0.15       # per-article label thresholds (as the sentiment engine)
    article_pos: float = 0.15
    periods: int = 3                 # consecutive sub-periods for the stability check
    exclude: tuple[str, ...] = ()
    tickers: tuple[str, ...] = ()
    degrade_media: bool = False      # treat every media timestamp as date-only
    zero_mechanical: bool = True     # apply the ex-dividend rule to files scored before it existed
    # Files scored with --materiality carry a materiality factor in each article's weight.
    # False divides it back out: same files, weights as without materiality (the A/B baseline).
    use_materiality: bool = True
    n_boot: int = 2000
    # Resample blocks of this many consecutive trading days. Consecutive 5-day returns
    # overlap, so resampling single dates would overstate significance.
    block_days: int = 5
    seed: int = 7


# ---------------------------------------------------------------- loading
def load_items(cfg: BacktestSettings, start: date, end: date) -> pd.DataFrame:
    """One row per (symbol, article), from the sentiment engine's per-ticker files."""
    root = Path(cfg.sentiment_dir)
    lo, hi = start - timedelta(days=10), end
    rows: list[dict[str, Any]] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            d = date.fromisoformat(folder.name)
        except ValueError:
            continue
        if not (lo <= d <= hi):
            continue
        for f in folder.glob("*.json"):
            if f.name.startswith("_"):
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
            sym = data.get("ticker")
            if cfg.tickers and sym not in cfg.tickers:
                continue
            if sym in cfg.exclude:
                continue
            for a in data.get("articles", []):
                if a.get("score") is None or not a.get("weight"):
                    continue
                factors = a.get("weight_factors") or {}
                rec = factors.get("recency") or 1.0
                base = a["weight"] / rec if rec else 0.0
                if not cfg.use_materiality and factors.get("materiality"):
                    base /= factors["materiality"]
                pub = datetime.fromisoformat(a["published_at"])
                is_news = a.get("source_type") == "news"
                # A media item whose time is a placeholder (whole hour UTC) is date-only, whatever
                # its label says: files scored from pre-fix news were labelled "exact" by default.
                # Erring this way only delays an item, never lets it leak forward.
                precision = "date" if a.get("time_precision") == "date" or (is_news and is_date_only(pub)) else "exact"
                if cfg.degrade_media and is_news:
                    precision = "date"
                if cfg.zero_mechanical and is_news and is_mechanical(a.get("title")):
                    base = 0.0
                if base <= 0:
                    continue
                rows.append(dict(
                    symbol=sym, id=a["id"], source_type=a.get("source_type"), title=a.get("title"),
                    score=float(a["score"]), base_weight=float(base), precision=precision,
                    known_at=known_at(pub, precision), materiality=a.get("materiality"),
                ))
    items = pd.DataFrame(rows)
    if items.empty:
        raise ValueError(f"no scored articles found under {root} for {start}..{end}")
    return items.drop_duplicates(["symbol", "id"]).reset_index(drop=True)


def load_prices(path: Path) -> pd.DataFrame:
    """fetch_prices.py output: date, open, high, low, close, adj_close, volume, symbol;
    NIFTY 50 as symbol NIFTY50. Returns abnormal returns indexed by (date, symbol)."""
    p = pd.read_csv(path)
    need = {"date", "open", "close", "adj_close", "volume", "symbol"}
    if not need <= set(p.columns):
        raise ValueError(f"{path}: needs columns {sorted(need)}")
    if "NIFTY50" not in set(p.symbol):
        raise ValueError(f"{path}: no NIFTY50 rows (needed for market-relative returns)")
    p = p[(p.volume > 0) | (p.symbol == "NIFTY50")].copy()  # drops holiday placeholder rows
    p["date"] = pd.to_datetime(p.date).dt.date
    p = p.sort_values(["symbol", "date"])
    p["adj_open"] = p.open * p.adj_close / p.close
    out = []
    for _, g in p.groupby("symbol"):
        g = g.copy()
        c, o = g.adj_close, g.adj_open
        g["open_close"] = g.close / g.open - 1
        g["open_5d"] = c.shift(-4) / o - 1
        g["close_1d"] = c.shift(-1) / c - 1
        g["close_5d"] = c.shift(-5) / c - 1
        out.append(g)
    p = pd.concat(out)
    cols = ["open_close", "open_5d", "close_1d", "close_5d"]
    mkt = p[p.symbol == "NIFTY50"].set_index("date")[cols]
    stk = p[p.symbol != "NIFTY50"].set_index(["date", "symbol"])[cols]
    return stk.sub(mkt, level="date")


# ---------------------------------------------------------------- signals
def build_signals(items: pd.DataFrame, calendar: list[date], cfg: BacktestSettings,
                  full_calendar: list[date] | None = None) -> pd.DataFrame:
    """``calendar``: the days to evaluate. ``full_calendar``: every trading day in the
    price file, used to find the session before the first evaluated day (so a range that
    starts on a Monday still sees Friday's news)."""
    hl = cfg.half_life_hours * 3600.0
    full = sorted(set(full_calendar or calendar) | set(calendar))
    pos = {d: i for i, d in enumerate(full)}
    prev_day = {T: (full[pos[T] - 1] if pos[T] > 0 else None) for T in calendar}
    rows = []
    for sym, g in items.groupby("symbol"):
        g = g.sort_values("known_at")
        ka = np.array([t.timestamp() for t in g.known_at])
        sc, bw = g.score.to_numpy(), g.base_weight.to_numpy()
        is_date = (g.precision == "date").to_numpy()
        for kind, hhmm in cfg.decisions.items():
            dec = time.fromisoformat(hhmm)
            for T in calendar:
                t = datetime.combine(T, dec, IST).timestamp()
                p = prev_day[T]
                t_prev = (datetime.combine(p, dec, IST) if p else
                          datetime.combine(T, dec, IST) - timedelta(days=1)).timestamp()
                lo, hi = np.searchsorted(ka, t_prev, "left"), np.searchsorted(ka, t, "left")
                if hi <= lo:
                    continue
                w = bw[lo:hi] * 0.5 ** ((t - ka[lo:hi]) / hl) if hl > 0 else bw[lo:hi]
                ws = w.sum()
                s = sc[lo:hi]
                rows.append(dict(symbol=sym, date=T, decision=kind, score=float((w * s).sum() / ws) if ws else np.nan,
                                 weight=float(ws), n=int(hi - lo), n_neg=int((s < cfg.article_neg).sum()),
                                 n_pos=int((s > cfg.article_pos).sum()),
                                 date_only_share=float(w[is_date[lo:hi]].sum() / ws) if ws else np.nan))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- statistics
def _spearman(a: pd.Series, b: pd.Series) -> float:
    m = a.notna() & b.notna()
    if m.sum() < 10:
        return float("nan")
    with np.errstate(invalid="ignore", divide="ignore"):  # constant series -> NaN, treated as no value
        return float(a[m].rank().corr(b[m].rank()))


def _boot_diff(df: pd.DataFrame, flag: str, y: str, n_boot: int, rng, block_days: int = 1) -> dict[str, float]:
    """Mean of y for flagged rows minus the rest, with a 95% CI and two-sided p-value
    from resampling blocks of consecutive trading days."""
    d = df[[flag, y, "date"]].dropna().copy()
    if d[flag].sum() < 5 or (~d[flag]).sum() < 5:
        return dict(n=int(d[flag].sum()), mean=np.nan, rest=np.nan, diff=np.nan, lo=np.nan, hi=np.nan, p=np.nan)
    order = {x: i for i, x in enumerate(sorted(d.date.unique()))}
    d["block"] = d.date.map(order) // max(1, block_days)
    f = d[flag].astype(bool)
    d["s1"], d["c1"] = d[y].where(f, 0.0), f.astype(int)
    d["s0"], d["c0"] = d[y].where(~f, 0.0), (~f).astype(int)
    sums = d.groupby("block")[["s1", "c1", "s0", "c0"]].sum()
    s1, c1, s0, c0 = (sums[c].to_numpy(dtype=float) for c in ("s1", "c1", "s0", "c0"))
    idx = rng.integers(0, len(s1), (n_boot, len(s1)))
    with np.errstate(invalid="ignore", divide="ignore"):
        boots = s1[idx].sum(1) / c1[idx].sum(1) - s0[idx].sum(1) / c0[idx].sum(1)
    boots = boots[np.isfinite(boots)]
    mean, rest = s1.sum() / c1.sum(), s0.sum() / c0.sum()
    diff = mean - rest
    p = float(min(1.0, 2 * min((boots <= 0).mean(), (boots >= 0).mean()))) if len(boots) else np.nan
    return dict(n=int(c1.sum()), mean=float(mean), rest=float(rest), diff=float(diff),
                lo=float(np.percentile(boots, 2.5)), hi=float(np.percentile(boots, 97.5)), p=p)


def evaluate(sig: pd.DataFrame, rets: pd.DataFrame, cfg: BacktestSettings, label: str) -> pd.DataFrame:
    rng = np.random.default_rng(cfg.seed)
    df = sig[sig.weight >= cfg.min_weight].merge(rets.reset_index(), on=["date", "symbol"], how="inner")
    if df.empty:
        return pd.DataFrame()
    df["negative"] = (df.score <= cfg.veto_score) & (df.weight >= cfg.veto_weight) & (df.n_neg > df.n_pos)
    df["positive"] = (df.score >= cfg.positive_score) & (df.weight >= cfg.veto_weight)
    dates = sorted(df.date.unique())
    bounds = np.array_split(np.array(dates, dtype=object), max(1, cfg.periods))
    periods = [("all", dates[0], dates[-1])] + [(f"P{i + 1}", b[0], b[-1]) for i, b in enumerate(bounds) if len(b)]
    out = []
    for kind, targets in TARGETS.items():
        dk = df[df.decision == kind]
        for y in targets:
            for pname, a, b in periods:
                d = dk[(dk.date >= a) & (dk.date <= b)].dropna(subset=[y])
                if len(d) < 20:
                    continue
                with np.errstate(invalid="ignore", divide="ignore"):
                    daily = [x.score.rank().corr(x[y].rank()) for _, x in d.groupby("date") if len(x) >= 4]
                daily = np.array([v for v in daily if np.isfinite(v)])
                neg = _boot_diff(d, "negative", y, cfg.n_boot, rng, cfg.block_days)
                pos = _boot_diff(d, "positive", y, cfg.n_boot, rng, cfg.block_days)
                out.append(dict(
                    mode=label, decision=kind, target=y, target_span=TARGET_LABELS[y], period=pname,
                    start=str(a), end=str(b), rows=len(d),
                    date_only_weight_share=round(float(d.date_only_share.mean()), 3),
                    ic=round(_spearman(d.score, d[y]), 4),
                    ic_daily=round(float(daily.mean()), 4) if len(daily) else np.nan,
                    ic_daily_t=round(float(daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily)))), 2) if len(daily) > 2 else np.nan,
                    neg_rows=neg["n"], neg_pct=_pct(neg["mean"]), rest_pct=_pct(neg["rest"]), neg_gap_pts=_pct(neg["diff"]),
                    neg_gap_lo=_pct(neg["lo"]), neg_gap_hi=_pct(neg["hi"]), neg_p=_r(neg["p"], 3),
                    pos_rows=pos["n"], pos_pct=_pct(pos["mean"]), pos_gap_pts=_pct(pos["diff"]), pos_p=_r(pos["p"], 3),
                ))
    return pd.DataFrame(out)


def _pct(x):
    return round(x * 100, 3) if x is not None and np.isfinite(x) else np.nan


def _r(x, n):
    return round(x, n) if x is not None and np.isfinite(x) else np.nan


def stability(metrics: pd.DataFrame) -> pd.DataFrame:
    """Per decision/target: does the negative gap keep its sign in every sub-period?"""
    rows = []
    for (mode, kind, y), g in metrics[metrics.period != "all"].groupby(["mode", "decision", "target"]):
        gaps = g.neg_gap_pts.dropna()
        ics = g.ic.dropna()
        allp = metrics[(metrics["mode"] == mode) & (metrics.decision == kind) & (metrics.target == y) & (metrics.period == "all")]
        rows.append(dict(
            mode=mode, decision=kind, target=y, periods=len(g),
            neg_gap_all=float(allp.neg_gap_pts.iloc[0]) if len(allp) else np.nan,
            neg_p_all=float(allp.neg_p.iloc[0]) if len(allp) else np.nan,
            neg_gap_same_sign=bool(len(gaps) == len(g) and (np.sign(gaps) == np.sign(gaps.iloc[0])).all()),
            neg_gap_negative_every_period=bool(len(gaps) == len(g) and (gaps < 0).all()),
            ic_same_sign=bool(len(ics) == len(g) and (np.sign(ics) == np.sign(ics.iloc[0])).all()),
            verdict=_verdict(gaps, allp),
        ))
    return pd.DataFrame(rows)


def _verdict(gaps: pd.Series, allp: pd.DataFrame) -> str:
    if len(allp) == 0 or gaps.empty:
        return "insufficient data"
    p = float(allp.neg_p.iloc[0])
    consistent = (gaps < 0).all()
    if consistent and p < 0.05:
        return "negative news lags: consistent and significant"
    if consistent:
        return "negative news lags in every period, not significant"
    return "no consistent effect"


def parity(precise: pd.DataFrame, degraded: pd.DataFrame, cfg: BacktestSettings) -> pd.DataFrame:
    """How much the signal changes when media timestamps are cut back to dates."""
    m = precise.merge(degraded, on=["symbol", "date", "decision"], how="outer", suffixes=("_p", "_d"))
    rows = []
    for kind, g in m.groupby("decision"):
        both = g.dropna(subset=["score_p", "score_d"])
        on_p = g.weight_p.fillna(0) >= cfg.min_weight
        on_d = g.weight_d.fillna(0) >= cfg.min_weight
        neg_p = on_p & (g.score_p <= cfg.veto_score) & (g.weight_p >= cfg.veto_weight)
        neg_d = on_d & (g.score_d <= cfg.veto_score) & (g.weight_d >= cfg.veto_weight)
        rows.append(dict(
            decision=kind, rows=len(g), rows_with_both=len(both),
            score_rank_corr=round(_spearman(both.score_p, both.score_d), 3),
            mean_abs_score_diff=round(float((both.score_p - both.score_d).abs().mean()), 3) if len(both) else np.nan,
            signal_rows_precise=int(on_p.sum()), signal_rows_degraded=int(on_d.sum()),
            negative_flags_precise=int(neg_p.sum()), negative_flags_degraded=int(neg_d.sum()),
            negative_flag_agreement=round(float((neg_p == neg_d).mean()), 3),
        ))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- orchestration
def run_backtest(start: date, end: date, prices: Path, cfg: BacktestSettings | None = None,
                 run_parity: bool = False) -> dict[str, Any]:
    cfg = cfg or BacktestSettings()
    rets = load_prices(prices)
    full_calendar = sorted({d for d, _ in rets.index})
    calendar = [d for d in full_calendar if start <= d <= end]
    if not calendar:
        raise ValueError(f"no trading days in {prices} between {start} and {end}")
    items = load_items(cfg, start, end)
    label = "date-only media" if cfg.degrade_media else "as recorded"
    sig = build_signals(items, calendar, cfg, full_calendar)
    metrics = evaluate(sig, rets, cfg, label)
    frames = [metrics]
    par = pd.DataFrame()
    if run_parity:
        cfg_d = replace(cfg, degrade_media=True)
        sig_d = build_signals(load_items(cfg_d, start, end), calendar, cfg_d, full_calendar)
        frames.append(evaluate(sig_d, rets, cfg_d, "date-only media"))
        par = parity(sig, sig_d, cfg)
    metrics = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    stab = stability(metrics) if not metrics.empty else pd.DataFrame()

    out_dir = Path(cfg.output_dir) / f"{start.isoformat()}_{end.isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    sig.to_csv(out_dir / "signals.csv", index=False)
    metrics.to_csv(out_dir / "metrics.csv", index=False)
    stab.to_csv(out_dir / "stability.csv", index=False)
    if not par.empty:
        par.to_csv(out_dir / "parity.csv", index=False)
    news = items[items.source_type == "news"]
    data_notes = {
        "items": int(len(items)), "tickers": sorted(items.symbol.unique()),
        "media_items": int(len(news)),
        "media_date_only_share": round(float((news.precision == "date").mean()), 3) if len(news) else None,
        "media_with_materiality": round(float(news.materiality.notna().mean()), 3) if len(news) else None,
        "materiality_applied": cfg.use_materiality,
        "trading_days": len(calendar),
    }
    report = {"start": str(start), "end": str(end), "settings": _jsonable(asdict(cfg)), "data": data_notes,
              "metrics": metrics.to_dict("records"), "stability": stab.to_dict("records"),
              "parity": par.to_dict("records"), "output_dir": str(out_dir)}
    (out_dir / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return report


def _jsonable(d):
    return {k: (str(v) if isinstance(v, Path) else list(v) if isinstance(v, tuple) else v) for k, v in d.items()}
