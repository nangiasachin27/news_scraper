"""Rule-based event classification -> (category, impact_tier).

Ordering matters and is first-match-wins:

1. NOISE rules first. Many zero-information filings contain high-impact keywords:
   "Newspaper publication of financial results" is a results-keyword filing that
   carries no new information (the results were filed hours earlier).
2. Intimations before outcomes: "Board meeting to consider financial results" is a
   date announcement, not results.
3. High-impact event rules, then medium/low.

Noise rules are not applied to media items; a news story that mentions a newspaper
is not a newspaper-publication filing.

Tiers are a prior for the sentiment engine (weighting / filtering), not a verdict on
direction. This is deliberately transparent regex so a false positive can be traced
to a single line; an ML classifier can replace ``classify`` without touching callers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    category: str
    tier: str
    pattern: re.Pattern


def _r(category: str, tier: str, *alternatives: str) -> Rule:
    return Rule(category, tier, re.compile("|".join(f"(?:{a})" for a in alternatives), re.I))


RULES: tuple[Rule, ...] = (
    # ---- 1. noise (filings only) ------------------------------------------
    _r("newspaper_publication", "noise", r"newspaper", r"\bpublication\b.{0,40}\b(advertisement|notice)\b"),
    _r("share_certificate", "noise", r"share certificates?", r"\bduplicate\b.{0,20}\bcertificate"),
    _r(
        "compliance_filing",
        "noise",
        r"74\s*\(\s*5\s*\)",
        r"regulation\s*74\b",
        r"compliance certificate",
        r"secretarial compliance",
        r"regulation\s*(7|40)\s*\(\s*(3|9|10)\s*\)",
        r"investor (grievance|complaint)",
        r"statement of deviation",
    ),
    _r("trading_window", "noise", r"trading window"),
    # ---- 2. low-information procedural ------------------------------------
    _r("esop_allotment", "low", r"\besops?\b", r"\besos\b", r"\besps\b", r"employee stock (option|purchase)"),
    _r(
        "analyst_meet",
        "low",
        r"analysts?\s*(/|and)?\s*institutional investors? meet",
        r"(analyst|investor)s? (meet|call|conference|day)\b",
        r"earnings (call|conference)",
        r"con[- ]?call",
    ),
    _r("transcript", "low", r"transcript", r"audio (recording|link)", r"video recording"),
    _r(
        "board_meeting",
        "medium",
        r"(prior |advance )?intimation of (the )?board meeting",
        r"board meeting intimation",
        r"notice of (the )?board meeting",
        r"board meeting (to be held|to consider|scheduled)",
    ),
    # ---- 3. high impact -----------------------------------------------------
    _r(
        "results",
        "high",
        r"financial results?",
        r"\b(un-?)?audited\b.{0,40}\bresults",
        r"quarterly results",
        r"\bresults? for the (quarter|half|year|period)",
        r"\bq[1-4]\s?(fy)?\s?'?\d{2}\b.{0,60}\b(profit|loss|revenue|results|earnings|ebitda|margin)",
        r"\b(q[1-4]|quarterly|annual)\s+(net\s+)?(profit|loss|revenue|earnings|results)",
        r"\b(net )?profit (rises|falls|jumps|drops|surges|slumps|declines|climbs|up|down|grows)\b",
        r"\bearnings (beat|miss)",
    ),
    _r("dividend", "high", r"\bdividend"),
    _r("buyback", "high", r"buy\s?-?\s?back"),
    _r(
        "bonus_split",
        "high",
        r"bonus (issue|shares)",
        r"stock split",
        r"sub-?division of (equity )?shares",
        r"split of (equity )?shares",
    ),
    _r("block_deal", "medium", r"block deal", r"bulk deal", r"offer for sale", r"\bofs\b"),
    _r(
        "m_and_a",
        "high",
        r"\bacqui(re|res|red|ring|sition)",
        r"amalgamation",
        r"\bmergers?\b",
        r"demerger",
        r"scheme of arrangement",
        r"takeover",
        r"open offer",
        r"stake (sale|purchase|acquisition)",
        r"divest",
        r"joint venture",
    ),
    _r(
        "fund_raising",
        "high",
        r"\bqip\b",
        r"qualified institutions? placement",
        r"preferential (issue|allotment)",
        r"rights issue",
        r"\bncds?\b",
        r"non-convertible debentures?",
        r"fund ?rais",
        r"commercial paper",
    ),
    _r(
        "order_win",
        "high",
        r"\borders? (win|wins|worth|received|inflow|bagged)",
        r"\bbags?\b.{0,40}\b(order|contract)",
        r"\bwins?\b.{0,40}\b(order|contract|deal)\b",
        r"letter of (award|intent)",
        r"contract (win|awarded|worth)",
    ),
    _r(
        "credit_rating",
        "high",
        r"credit rating",
        r"rating (upgrade|downgrade|reaffirm|revision|action)",
        r"\b(crisil|icra|care ratings|careedge|india ratings|moody'?s|fitch)\b",
    ),
    _r(
        "management_change",
        "high",
        r"\bresign",
        r"cessation",
        r"appoint(ment|s|ed)?\b.{0,60}\b(ceo|cfo|coo|md|managing director|chairman|chairperson|whole[- ]time director|chief)",
        r"\bsteps down\b",
        r"\bnew (ceo|cfo|md|chairman)\b",
    ),
    _r(
        "regulatory_legal",
        "high",
        r"\bsebi\b.{0,40}\b(order|notice|penalty|probe|ban|bars)",
        r"show[- ]cause",
        r"\bpenalt(y|ies)\b",
        r"\bnclt\b",
        r"\bnclat\b",
        r"insolvency",
        r"\blitigation",
        r"\b(raid|raids|search and seizure)\b",
        r"\bfraud\b",
        r"payment default",
        r"\bdefault(ed|s)? on\b",
        r"tax demand",
        r"\bgst\b.{0,30}\b(demand|notice|order)",
        r"income tax.{0,30}\b(order|demand|notice)",
        r"\b(supreme|high) court\b",
    ),
    _r("pledge", "high", r"\bpledg", r"encumbrance"),
    # ---- 4. medium / low ------------------------------------------------------
    _r(
        "insider_disclosure",
        "medium",
        r"\bsast\b",
        r"regulation\s*29",
        r"regulation\s*7\s*\(\s*2\s*\)",
        r"insider trading",
        r"\bpit\b",
    ),
    _r(
        "business_update",
        "medium",
        r"business update",
        r"operational update",
        r"production (numbers|update|volume)",
        r"(sales|auto|dispatch) (volume|numbers|figures)",
        r"monthly sales",
        r"\bguidance\b",
        r"\bcapex\b",
        r"capacity (expansion|addition)",
        r"new (plant|facility)",
        r"\blaunch(es|ed)?\b",
    ),
    _r(
        "analyst_rating",
        "medium",
        r"target price",
        r"price target",
        r"\b(upgrades?|downgrades?)\b",
        r"\b(buy|sell|hold|overweight|underweight|neutral|accumulate) (rating|call)\b",
        r"brokerages?",
    ),
    _r("investor_presentation", "medium", r"investor presentation", r"press release", r"media release"),
    _r(
        "agm_egm",
        "low",
        r"\b(agm|egm)\b",
        r"annual general meeting",
        r"postal ballot",
        r"e-?voting",
        r"record date",
        r"book closure",
    ),
    _r("general_update", "low", r"\bupdates?\b", r"clarification", r"regulation\s*30\b", r"\bdisclosure\b"),
)


MECHANICAL_TAG = "mechanical_price_adjustment"

# Media headlines about an exchange price adjustment rather than company news.
_MECHANICAL_RE = re.compile(
    r"\bex[- ]?divid(e|en)d|\bturns? ex[- ]?(dividend|date|bonus|split)|\bex[- ]?date\b|\bex[- ]?bonus\b|\bex[- ]?split\b"
    r"|\bprice adjusts?\b|\badjusts? (for|to) (the )?(dividend|bonus|split)|\brecord date\b",
    re.I,
)

# Headlines that report a price move that has already happened.
_PRICE_RECAP_RE = re.compile(
    r"\b(shares?|stocks?|scrip|nifty|sensex|index)\b.{0,60}\b(surge[sd]?|soar(s|ed)?|jump(s|ed)?|rall(y|ies|ied)|"
    r"gain(s|ed)?|rise[sn]?|rose|climb(s|ed)?|fall(s|en)?|fell|drop(s|ped)?|slump(s|ed)?|plunge[sd]?|tumble[sd]?|"
    r"crash(es|ed)?|slide[sd]?|sink(s)?|sank|decline[sd]?|tank(s|ed)?|dive[sd]?|end(s|ed)? (higher|lower))"
    r"|\b(top (gainers|losers)|stocks? to watch|buzzing stocks?|share price (live|today)|stock price live|"
    r"52-week (high|low)|(upper|lower) circuit)\b|\b(up|down) \d+(\.\d+)?\s?%",
    re.I,
)


def is_mechanical(title: str | None) -> bool:
    return bool(_MECHANICAL_RE.search(title or ""))


def is_price_recap(title: str | None) -> bool:
    return bool(_PRICE_RECAP_RE.search(title or ""))


def classify(text: str, source_type: str) -> tuple[str, str]:
    is_news = source_type == "news"
    for rule in RULES:
        if is_news and rule.tier == "noise":
            continue
        if rule.pattern.search(text or ""):
            return rule.category, rule.tier
    if is_news:
        return "general_news", "unclassified"
    return "other_filing", "low"
