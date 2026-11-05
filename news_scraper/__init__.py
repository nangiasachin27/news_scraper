"""Per-ticker financial news scraper for Indian equities (NSE/BSE).

Output feeds the downstream sentiment engine: one JSON file per ticker per date.
"""
from .config import Settings
from .models import SCHEMA_VERSION, Article, TickerInfo

__all__ = ["Settings", "SCHEMA_VERSION", "Article", "TickerInfo", "NewsPipeline"]
__version__ = "0.1.0"


def __getattr__(name):  # lazy: avoid importing requests/feedparser for light uses
    if name == "NewsPipeline":
        from .pipeline import NewsPipeline

        return NewsPipeline
    raise AttributeError(name)
