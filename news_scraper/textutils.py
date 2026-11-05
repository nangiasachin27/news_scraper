"""Text and URL normalisation shared by dedup, ids and parsers."""
from __future__ import annotations

import html
import re
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_TRACKING_KEYS = {"fbclid", "gclid", "cmpid", "mc_cid", "mc_eid", "ocid", "ref", "utm"}
_HOST_PREFIXES = ("www.", "m.", "amp.")


def strip_html(value: object) -> str:
    if value is None:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", str(value)))
    return _WS_RE.sub(" ", text).strip()


def canonical_url(url: str | None) -> str | None:
    """Scheme/host-normalised URL with tracking params removed, for identity checks."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    for prefix in _HOST_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix):]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_KEYS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    t = title.lower().replace("&", " and ")
    return _WS_RE.sub(" ", _NON_ALNUM_RE.sub(" ", t)).strip()


def similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    matcher = SequenceMatcher(None, na, nb, autojunk=False)
    if matcher.real_quick_ratio() < 0.5:
        return 0.0
    return matcher.ratio()
