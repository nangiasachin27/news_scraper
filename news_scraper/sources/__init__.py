"""Importing this package registers every built-in source."""
from . import bse, news, nse, yahoo  # noqa: F401  (registration side effects)
from .base import REGISTRY, Source, build_sources, register

__all__ = ["REGISTRY", "Source", "build_sources", "register"]
