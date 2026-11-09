"""Decide whether a media item is actually about the ticker.

Exchange-filing sources are queried by symbol, so they are relevant by construction
and skip this step. News is matched against aliases with these safeguards:

* ``exclude_terms`` are masked out *before* matching, so "Reliance Power" can never
  satisfy an alias like "Reliance".
* ``strict_aliases`` are case-sensitive (RIL, L&T, ITC must not match "ril"/"itc").
* Ambiguous aliases (opt-in via ``ambiguous_aliases`` in aliases.yaml): RIL is also
  other companies' abbreviation, "Titan" is also a submersible. If the only match is an
  ambiguous alias and no full-name alias appears in title or summary, the item scores
  0.6 and is tagged ``acronym_only``. Unambiguous brands (IndiGo, ONGC, JLR) aren't listed. ``confirm`` re-checks after enrichment adds summary/body
  text; a full-name hit there restores the score and drops the tag.
* Counterparty mentions ("X bags order from <ticker>") score 0.5, tagged ``counterparty``.
* Market round-ups ("Stocks to watch: A, B, C...") score 0.5 (0.3 summary-only),
  tagged ``roundup``. Multi-story digests ("A; B; C; plus much more", "Get ready for the
  week with a summary of all the stories...") are round-ups too: the ticker is one item
  in a list, and FinBERT reads the list as neutral noise. ``is_digest`` checks the title
  before enrichment and title + summary after it (never the body: article bodies carry
  "newsletter"/"read more" boilerplate).

Score: 1.0 title, 0.6 summary-only or acronym-only, 0.5 counterparty/round-up.
"""
from __future__ import annotations

import re

from .models import TickerInfo

ROUNDUP_RE = re.compile(
    r"stocks? to (watch|buy|track)|stocks in (news|focus|the news)|buzzing stocks|"
    r"top (gainers|losers)|market (live|wrap|highlights|close|update)|stock market (today|live|highlights)|"
    r"closing bell|opening bell|trade setup|share price live|sensex.{0,40}nifty|nifty.{0,40}sensex|"
    r"\blive updates?\b|stocks? (that|which) (moved|hogged)",
    re.I,
)

# Weekly / daily digests that list many unrelated stories. Kept specific: a false hit
# halves a real story's weight.
DIGEST_RE = re.compile(
    r"\bplus (much|lots|plenty|a lot) more\b|\band (much|lots) more\s*$|"
    r"\bweek in (review|brief)\b|\bweekly (round-?up|wrap|digest|recap|briefing)\b|"
    r"\b(news|morning|evening) (digest|round-?up|briefing)\b|"
    r"\bsummary of (all )?the (stories|news|articles)\b|\beverything we covered\b|"
    r"\b(stories|articles) (posted|published) .{0,40}\b(past|last) (seven|7|few) days\b",
    re.I,
)


def is_digest(title: str | None, lede: str | None = None) -> bool:
    """True for multi-story digests: four or more headline clauses joined by ';' in the
    title, or digest wording in the title or its lede (summary). Two or three clauses are
    left alone: "RIL Q2: profit up 9%; Jio ARPU rises; retail slows" is one company's news."""
    title = title or ""
    if title.count(";") >= 3:
        return True
    return bool(DIGEST_RE.search(title) or (lede and DIGEST_RE.search(lede)))


# Quote pages, company landing pages and share-price live blogs. They are not news, and
# their text is rewritten all day: fetched after the window, the page carries prices
# from after the decision time. Dropped outright (before and after enrichment, since
# Google redirect links only reveal the publisher URL once decoded).
QUOTE_TITLE_RE = re.compile(
    r"\b(share|stock) price (today|live)\b|\b(share|stock) price\b.{0,40}\blive (updates?|blog)\b|"
    r"\bstock price\s*(&|and)\s*(latest )?news\b|\bstock price, news, quote\b|"
    r"\bquotes? (and|&) (history|news)\b|\([A-Z0-9&-]+\.(NS|BO)\)\s*[|:-]|"
    r"\b(share|stock) price,? (chart|history|target today)\b",
    re.I,
)
QUOTE_URL_RE = re.compile(
    r"/quotes?/|/stockpricequote/|/markets/companies/|/stock-liveblog/|/market-stats/|"
    r"/companyid-|/stock-market/quote/|/stocks?/[a-z0-9.-]+/(quote|overview)\b",
    re.I,
)


def is_quote_page(title: str | None, url: str | None = None) -> bool:
    """Quote / landing / price live-blog page rather than an article."""
    return bool((title and QUOTE_TITLE_RE.search(title)) or (url and QUOTE_URL_RE.search(url)))


# Text immediately before the alias: "...order(s)/contract/deal/mandate ... from|by <alias>".
# Periods are allowed in the gap ("order of Rs. 1.54 crore from RIL"); ':' and '|' end
# a headline clause, so they block the match.
COUNTERPARTY_BEFORE_RE = re.compile(
    r"\b(orders?|contracts?|deals?|mandates?|lo[ia]s?|letters? of (intent|award)|tenders?)\b"
    r"[^:|]{0,60}\b(from|by)\s+(the\s+)?$",
    re.I,
)


def _alias_pattern(alias: str, case_sensitive: bool) -> re.Pattern:
    body = r"\s+".join(re.escape(part) for part in alias.split())
    flags = 0 if case_sensitive else re.I
    # '&' counts as part of a token so "M&M" doesn't match inside "M&M Financial" via "M"
    return re.compile(rf"(?<![\w&]){body}(?![\w&])", flags)


class RelevanceMatcher:
    def __init__(self, ticker: TickerInfo) -> None:
        self._loose = [(a, _alias_pattern(a, case_sensitive=False)) for a in ticker.aliases]
        self._strict = [(a, _alias_pattern(a, case_sensitive=True)) for a in ticker.strict_aliases]
        self._patterns = self._loose + self._strict
        self._ambiguous = set(ticker.ambiguous_aliases) & set(ticker.strict_aliases)
        # Longest first so "Reliance Industries Infrastructure" is masked before shorter terms.
        excludes = sorted(ticker.exclude_terms, key=len, reverse=True)
        self._excludes = [_alias_pattern(x, case_sensitive=False) for x in excludes]

    def _mask(self, text: str) -> str:
        for pat in self._excludes:
            text = pat.sub(" ", text)
        return text

    def _hits(self, text: str) -> list[str]:
        return [alias for alias, pat in self._patterns if pat.search(text)]

    def _has_full_name(self, masked_text: str) -> bool:
        return any(pat.search(masked_text) for _, pat in self._loose)

    def _counterparty_only(self, masked_title: str) -> bool:
        """True when every title mention is the *counterparty* of someone else's deal:
        "Artson receives work order from Reliance Industries". That is the supplier's
        news; for this ticker it is at best weak information."""
        starts = [m.start() for _, pat in self._patterns for m in pat.finditer(masked_title)]
        return bool(starts) and all(COUNTERPARTY_BEFORE_RE.search(masked_title[:s]) for s in starts)

    def score(self, title: str | None, summary: str | None = None) -> tuple[float, list[str], list[str]]:
        title = title or ""
        masked_title = self._mask(title)
        masked_summary = self._mask(summary or "")
        title_hits = self._hits(masked_title)
        summary_hits = self._hits(masked_summary)
        matched = sorted(set(title_hits) | set(summary_hits))
        if not matched:
            return 0.0, [], []
        score = 1.0 if title_hits else 0.6
        tags: list[str] = []
        if (
            self._loose  # only meaningful when a full name exists to confirm against
            and set(matched) <= self._ambiguous
            and not self._has_full_name(masked_title + " " + masked_summary)
        ):
            tags.append("acronym_only")
            score = min(score, 0.6)
        if title_hits and self._counterparty_only(masked_title):
            tags.append("counterparty")
            score = min(score, 0.5)
        if ROUNDUP_RE.search(title) or is_digest(title):
            tags.append("roundup")
            score = min(score, 0.5 if title_hits else 0.3)
        return score, matched, tags

    def confirm(self, title: str | None, summary: str | None, body: str | None) -> tuple[float, list[str], list[str]]:
        """Re-score with enrichment text. Body counts like summary for confirmation."""
        extra = " ".join(t for t in (summary, (body or "")[:5000]) if t)
        return self.score(title, extra or None)
