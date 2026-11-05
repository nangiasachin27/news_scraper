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
  tagged ``roundup``.

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
        if ROUNDUP_RE.search(title):
            tags.append("roundup")
            score = min(score, 0.5 if title_hits else 0.3)
        return score, matched, tags

    def confirm(self, title: str | None, summary: str | None, body: str | None) -> tuple[float, list[str], list[str]]:
        """Re-score with enrichment text. Body counts like summary for confirmation."""
        extra = " ".join(t for t in (summary, (body or "")[:5000]) if t)
        return self.score(title, extra or None)
