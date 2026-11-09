"""Point-in-time backtest of sentiment against prices.

Every item gets a ``known_at`` time: its timestamp when exact, the latest moment it could
have appeared when the source gave a date only (``news_scraper.timeutils.known_at``). A signal for a trading decision uses only items known before
that decision, so the backtest cannot see the future even on date-only backfills. It is
a conservative lower bound on production, where the same news arrives with real times.
"""
from .core import BacktestSettings, run_backtest

__all__ = ["BacktestSettings", "run_backtest"]
