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


# ---- story grouping ------------------------------------------------------------
# Words that say nothing about WHICH story a headline is: function words, and market /
# newsroom vocabulary that appears in most headlines about any listed company.
_STORY_STOPWORDS = frozenset("""
a an the and or but nor for so yet of in on at to from by with without into onto over under
about after before during since until via as than then also just only very more most less
is are was were be been being has have had do does did will would can could may might must
shall should this that these those its it his her their our your they them he she we you
who whom whose which what when where why how not no all any each every some such other
here there out up down off new latest today now says said say amid ahead per
share shares stock stocks price prices market markets news update updates report reports
company firm limited ltd india indian crore lakh rupees percent cent year years day days
week weeks month months quarter quarters fy fy27 fy26 q1 q2 q3 q4 live check know what's
""".split())


def story_tokens(title: str | None, ignore: frozenset[str] = frozenset()) -> frozenset[str]:
    """Distinctive words of a headline, crudely stemmed to their first 4 letters.

    The prefix makes spelling variants and inflections meet ("Imtiaz"/"Imtiyaz",
    "shelter"/"sheltering", "named"/"name"). ``ignore`` holds the ticker's own alias
    words, which every headline in the file shares and so can't tell stories apart."""
    words = normalize_title(title).split()
    return frozenset(
        w[:4] for w in words
        if len(w) >= 3 and w not in _STORY_STOPWORDS and w[:4] not in ignore and not w.isdigit()
    )


def same_story(a: frozenset[str], b: frozenset[str], min_shared: int, min_overlap: float) -> bool:
    """Two headlines report the same event: they share at least ``min_shared`` distinctive
    words, and those make up at least ``min_overlap`` of the shorter headline's."""
    if not a or not b:
        return False
    shared = len(a & b)
    return shared >= min_shared and shared / min(len(a), len(b)) >= min_overlap
