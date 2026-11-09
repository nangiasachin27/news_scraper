"""Timestamp fixes: no item may be stamped earlier than it could have been public.

1. Date-only exchange fields are dates, not midnight.
2. Google News date-only items get the latest bound of a US Pacific date (see
   test_timing_and_backtest.py for the bound itself).
3. Merging with an existing file re-applies the rules to carried-over items and drops
   anything that no longer belongs to the window.
"""
import json
from dataclasses import replace
from datetime import date, datetime

from news_scraper.pipeline import NewsPipeline
from news_scraper.timeutils import (
    IST,
    compute_window,
    exchange_time,
    normalize_article_time,
    parse_exchange_dt_precise,
)
from news_scraper.writer import merge_with_existing, trim_to_window
from sentiment_engine.engine import time_precision

from .conftest import DEFAULT_ROUTES, FakeHttp

THU, FRI = date(2026, 11, 5), date(2026, 11, 6)


def _run(settings, d, routes, sources):
    s = replace(settings, enabled_sources=sources)
    summary = NewsPipeline(s, http=FakeHttp(routes)).run(d, ["RELIANCE"])[0]
    return json.loads(summary.path.read_text("utf-8"))


# ---- fix 1: exchange date-only fields ------------------------------------------------
def test_parse_exchange_dt_precise_flags_date_only():
    assert parse_exchange_dt_precise("05-Nov-2026")[1] == "date"
    assert parse_exchange_dt_precise("05-11-2026")[1] == "date"
    assert parse_exchange_dt_precise("2026-11-05")[1] == "date"
    assert parse_exchange_dt_precise("05-Nov-2026 18:45:12") == (datetime(2026, 11, 5, 18, 45, 12, tzinfo=IST), "exact")
    assert parse_exchange_dt_precise("2026-11-05T09:13:10.17")[1] == "exact"
    assert parse_exchange_dt_precise("2026-11-05T09:13:10+05:30")[1] == "exact"
    assert parse_exchange_dt_precise("-") == (None, None)


def test_exchange_time_prefers_a_field_with_a_time():
    row = {"broadCastDate": "05-Nov-2026", "exchdisstime": "05-Nov-2026 18:45:12"}
    ts, meta = exchange_time(row, "broadCastDate", "exchdisstime", "filingDate")
    assert ts == datetime(2026, 11, 5, 18, 45, 12, tzinfo=IST)
    assert meta == {"time_field": "exchdisstime"}


def test_exchange_time_date_only_goes_to_end_of_day():
    ts, meta = exchange_time({"filingDate": "05-Nov-2026"}, "broadCastDate", "filingDate")
    assert ts == datetime(2026, 11, 5, 23, 59, 59, tzinfo=IST)
    assert meta == {"time_field": "filingDate", "time_precision": "date", "reported_published_at": "05-Nov-2026"}
    assert exchange_time({"x": "-"}, "x") == (None, {})


def test_date_only_nse_result_lands_in_next_session(settings):
    routes = dict(DEFAULT_ROUTES)
    routes["corporates-financial-results"] = [
        {"symbol": "RELIANCE", "consolidated": "Consolidated", "audited": "Un-Audited",
         "relatingTo": "Second Quarter", "toDate": "30-Sep-2026", "seqNumber": "9100",
         "broadCastDate": "05-Nov-2026"},  # date only: may have been after the close
    ]
    thu = _run(settings, THU, routes, ["nse_financial_results"])
    assert thu["articles"] == []  # was stamped 00:00 Thu before the fix
    fri = _run(settings, FRI, routes, ["nse_financial_results"])
    [res] = fri["articles"]
    assert res["published_at"] == "2026-11-05T23:59:59+05:30"
    assert res["meta"]["time_precision"] == "date"
    assert res["meta"]["time_field"] == "broadCastDate"


def test_date_only_bse_filing_lands_in_next_session(settings):
    routes = dict(DEFAULT_ROUTES)
    row = dict(DEFAULT_ROUTES["AnnSubCategoryGetData"]["Table"][0])
    row.pop("DissemDT")
    row["NEWS_DT"] = "2026-11-05"
    routes["AnnSubCategoryGetData"] = {"Table": [row]}
    assert _run(settings, THU, routes, ["bse_announcements"])["articles"] == []
    [a] = _run(settings, FRI, routes, ["bse_announcements"])["articles"]
    assert a["meta"]["time_field"] == "NEWS_DT" and a["meta"]["time_precision"] == "date"


def test_exact_exchange_times_unchanged(settings):
    thu = _run(settings, THU, DEFAULT_ROUTES, ["nse_announcements", "bse_announcements"])
    times = {a["published_at"] for a in thu["articles"]}
    assert "2026-11-05T09:12:45+05:30" in times
    assert all("time_precision" not in a["meta"] for a in thu["articles"])


# ---- fix 3: merge re-applies the rules and trims to the window ------------------------
def _seed(settings, d, articles):
    path = settings.output_dir / d.isoformat() / "RELIANCE.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"articles": articles}), encoding="utf-8")
    return path


def _old_google(id_, published_at, meta=None):
    return {"id": id_, "source": "google_news", "source_type": "news", "title": f"old {id_}",
            "published_at": published_at, "meta": meta or {}, "first_seen_at": "2026-11-05T16:00:00+05:30"}


def test_rerun_removes_legacy_placeholder_from_wrong_day(settings):
    routes = dict(DEFAULT_ROUTES)
    routes["news.google.com"] = b"<rss><channel></channel></rss>"
    _seed(settings, THU, [
        _old_google("legacy", "2026-11-05T12:30:00+05:30"),       # unflagged placeholder (pre-fix file)
        _old_google("exact", "2026-11-05T10:41:07+05:30"),        # genuine exact item
    ])
    thu = _run(settings, THU, routes, ["google_news"])
    ids = {a["id"] for a in thu["articles"]}
    assert ids == {"exact"}  # placeholder now belongs to Fri (latest bound Fri 12:29:59)
    assert thu["stats"]["dropped_out_of_window"] == 1


def test_rerun_upgrades_old_end_of_day_bound_and_keeps_it(settings):
    routes = dict(DEFAULT_ROUTES)
    routes["news.google.com"] = b"<rss><channel></channel></rss>"
    _seed(settings, FRI, [_old_google("prev", "2026-11-05T23:59:59+05:30", {
        "time_precision": "date", "reported_published_at": "2026-11-05T12:30:00+05:30"})])
    [a] = _run(settings, FRI, routes, ["google_news"])["articles"]
    assert a["published_at"] == "2026-11-06T12:29:59+05:30"
    assert a["first_seen_at"] == "2026-11-05T16:00:00+05:30"


def test_rerun_moves_legacy_midnight_filing_out_of_day(settings):
    routes = dict(DEFAULT_ROUTES)
    routes["corporate-announcements"] = []
    _seed(settings, THU, [{"id": "f1", "source": "nse_announcements", "source_type": "exchange_filing",
                           "title": "Outcome of board meeting", "published_at": "2026-11-05T00:00:00+05:30",
                           "meta": {"exchange": "NSE"}}])
    thu = _run(settings, THU, routes, ["nse_announcements"])
    assert thu["articles"] == [] and thu["stats"]["dropped_out_of_window"] == 1


def test_trim_and_merge_units(tmp_path):
    start, end = compute_window(THU)
    path = tmp_path / "X.json"
    path.write_text(json.dumps({"articles": [
        _old_google("in", "2026-11-05T10:00:00+05:30"),
        _old_google("out", "2026-11-03T10:00:00+05:30"),
    ]}))
    merged = merge_with_existing(path, [])
    kept, dropped = trim_to_window(merged, start, end)
    assert [a["id"] for a in kept] == ["in"] and [a["id"] for a in dropped] == ["out"]


def test_keep_policy_leaves_news_untouched():
    art = _old_google("x", "2026-11-05T12:30:00+05:30")
    assert normalize_article_time(art, "keep") is False
    assert art["published_at"] == "2026-11-05T12:30:00+05:30"


def test_normalize_is_idempotent_on_current_items():
    news = _old_google("n", "2026-11-06T12:29:59+05:30",
                       {"time_precision": "date", "reported_published_at": "2026-11-05T12:30:00+05:30"})
    filing = {"source": "bse_announcements", "source_type": "exchange_filing",
              "published_at": "2026-11-05T23:59:59+05:30",
              "meta": {"time_precision": "date", "reported_published_at": "2026-11-05"}}
    action = {"source": "nse_corporate_actions", "source_type": "corporate_action",
              "published_at": "2026-11-05T09:15:00+05:30", "meta": {}}
    for art in (news, filing, action):
        before = json.dumps(art, sort_keys=True)
        assert normalize_article_time(art) is False
        assert json.dumps(art, sort_keys=True) == before


def test_engine_flags_legacy_midnight_filings():
    assert time_precision({"source_type": "exchange_filing", "published_at": "2026-11-05T00:00:00+05:30"}) == "date"
    assert time_precision({"source_type": "exchange_filing", "published_at": "2026-11-05T09:12:45+05:30"}) == "exact"
    assert time_precision({"source_type": "corporate_action", "published_at": "2026-11-05T00:00:00+05:30"}) == "exact"
