from datetime import date, datetime, timedelta

import pytest

from news_scraper.classifier import classify
from news_scraper.dedup import dedupe
from news_scraper.models import Article, TickerInfo
from news_scraper.relevance import RelevanceMatcher
from news_scraper.sources.bse import clean_bse_subject
from news_scraper.textutils import canonical_url
from news_scraper.ticker_resolver import TickerResolver, UnknownTickerError, clean_company_name
from news_scraper.timeutils import IST, compute_window, parse_exchange_dt

from .conftest import FakeHttp


# ---- time -------------------------------------------------------------------
def test_session_window_monday_spans_weekend():
    start, end = compute_window(date(2026, 11, 9), "session")  # Monday
    assert start == datetime(2026, 11, 6, 15, 30, tzinfo=IST)  # Friday close
    assert end == datetime(2026, 11, 9, 15, 30, tzinfo=IST)


def test_session_window_skips_holiday():
    start, _ = compute_window(date(2026, 11, 6), "session", holidays=frozenset({date(2026, 11, 5)}))
    assert start.date() == date(2026, 11, 4)


def test_calendar_window():
    start, end = compute_window(date(2026, 11, 4), "calendar")
    assert (end - start) == timedelta(days=1) and start.hour == 0


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("05-Nov-2026 09:12:45", datetime(2026, 11, 5, 9, 12, 45, tzinfo=IST)),
        ("2026-11-05 09:12:45", datetime(2026, 11, 5, 9, 12, 45, tzinfo=IST)),
        ("2026-11-05T09:13:10.17", datetime(2026, 11, 5, 9, 13, 10, 170000, tzinfo=IST)),
        ("06-Nov-2026", datetime(2026, 11, 6, tzinfo=IST)),
        ("-", None),
        ("garbage", None),
    ],
)
def test_parse_exchange_dt(raw, expected):
    assert parse_exchange_dt(raw) == expected


# ---- resolver ---------------------------------------------------------------
@pytest.mark.parametrize("raw", ["RELIANCE", "reliance.ns", "500325", "RELIANCE.BO", "INE002A01018"])
def test_resolver_inputs_converge(settings, raw):
    info = TickerResolver(settings, FakeHttp({})).resolve(raw)
    assert (info.symbol, info.bse_code, info.isin) == ("RELIANCE", "500325", "INE002A01018")
    assert "Reliance Industries" in info.aliases and "RIL" in info.strict_aliases
    assert "Reliance Power" in info.exclude_terms


def test_resolver_short_name_is_strict(settings):
    info = TickerResolver(settings, FakeHttp({})).resolve("ITC")
    assert "ITC" in info.strict_aliases
    assert "ITC" not in info.aliases  # case-insensitive "itc" would be noisy


def test_resolver_bse_only(settings):
    info = TickerResolver(settings, FakeHttp({})).resolve("590099")
    assert info.symbol == "BSEONLY" and info.nse_listed is False and info.bse_code == "590099"


def test_resolver_offline_fallback_has_no_bare_symbol_alias(settings):
    for f in settings.reference_dir.iterdir():
        f.unlink()
    from dataclasses import replace

    offline = replace(settings, reference_dir=settings.reference_dir / "empty")
    info = TickerResolver(offline, FakeHttp({"nseindia": OSError("down"), "bseindia": OSError("down")})).resolve("RELIANCE")
    assert info.aliases[0] == "Reliance Industries"
    assert "RELIANCE" not in info.aliases  # bare symbol never a case-insensitive alias
    assert info.bse_code is None


def test_resolver_unknown(settings):
    with pytest.raises(UnknownTickerError):
        TickerResolver(settings, FakeHttp({})).resolve("NOPE")


def test_clean_company_name():
    assert clean_company_name("Tata Consultancy Services Limited") == "Tata Consultancy Services"
    assert clean_company_name("ITC Ltd.") == "ITC"


# ---- relevance --------------------------------------------------------------
@pytest.fixture
def reliance():
    return TickerInfo(
        symbol="RELIANCE",
        company_name="Reliance Industries Limited",
        aliases=["Reliance Industries", "Reliance"],  # deliberately loose alias
        strict_aliases=["RIL"],
        ambiguous_aliases=["RIL"],
        exclude_terms=["Reliance Power", "Reliance Infrastructure"],
    )


def test_exclusion_masks_sibling_company(reliance):
    score, matched, _ = RelevanceMatcher(reliance).score("Reliance Power shares hit upper circuit")
    assert score == 0.0 and matched == []


def test_strict_alias_case_sensitive(reliance):
    m = RelevanceMatcher(reliance)
    assert m.score("RIL shares jump 3%")[0] == 0.6  # matched, but acronym-only until confirmed
    assert m.score("RIL shares jump 3%", "Reliance Industries rose")[0] == 1.0
    assert m.score("April thrill for markets")[0] == 0.0


def test_summary_only_and_roundup(reliance):
    m = RelevanceMatcher(reliance)
    assert m.score("Oil marketing stocks rally", "Reliance Industries led gains")[0] == 0.6
    score, _, tags = m.score("Stocks to watch: Reliance Industries, TCS, Infosys")
    assert score == 0.5 and tags == ["roundup"]


# ---- classifier -------------------------------------------------------------
@pytest.mark.parametrize(
    "text, source_type, expected",
    [
        ("Copy of Newspaper Publication of financial results", "exchange_filing", ("newspaper_publication", "noise")),
        ("Outcome of Board Meeting - unaudited financial results Q2", "exchange_filing", ("results", "high")),
        ("Board Meeting Intimation for financial results", "exchange_filing", ("board_meeting", "medium")),
        ("Compliances-Certificate under Reg. 74 (5)", "exchange_filing", ("compliance_filing", "noise")),
        ("Board recommends final dividend of Rs 10", "exchange_filing", ("dividend", "high")),
        ("Company bags Rs 2,000 crore order from NHAI", "news", ("order_win", "high")),
        ("Reliance Industries Q2 profit rises 9%", "news", ("results", "high")),
        ("Newspaper reports on the company's expansion", "news", ("general_news", "unclassified")),
        ("Some random filing", "exchange_filing", ("other_filing", "low")),
    ],
)
def test_classify(text, source_type, expected):
    assert classify(text, source_type) == expected


# ---- dedup / misc -----------------------------------------------------------
def _filing(source, title, summary, minute, ex_id):
    return Article(
        source=source, source_type="exchange_filing", title=title, summary=summary,
        published_at=datetime(2026, 11, 5, 9, minute, tzinfo=IST), meta={"exchange_id": ex_id},
    )


def test_cross_exchange_dedup(settings):
    text = "Outcome of board meeting approving unaudited results for quarter ended September 2026"
    nse = _filing("nse_announcements", "Outcome of Board Meeting", text, 12, "1")
    bse = _filing("bse_announcements", "Outcome of Board Meeting", text, 14, "b1")
    other = _filing("bse_announcements", "Updates", "Allotment of 1,200 shares under ESOP", 20, "b2")
    out = dedupe([bse, nse, other], settings)
    assert len(out) == 2
    merged = next(a for a in out if a.source == "nse_announcements")
    assert merged.also_reported_by == ["bse_announcements"]
    assert merged.meta["cross_listing"] == {"bse_announcements": "b1"}


def test_bse_subject_cleaning():
    assert clean_bse_subject("Reliance Industries Ltd - 500325 - Outcome of Board Meeting") == "Outcome of Board Meeting"
    assert clean_bse_subject("No prefix here") == "No prefix here"


def test_canonical_url_strips_tracking():
    a = canonical_url("https://www.livemint.com/x/story/?utm_source=rss&id=5#top")
    b = canonical_url("http://livemint.com/x/story?id=5")
    assert a == b
