import csv
import json
from datetime import date

import pytest

import run_pipeline as rp
from sentiment_engine.config import SentimentSettings

from .conftest import DEFAULT_ROUTES, FakeHttp
from .test_sentiment import KeywordScorer

THU, FRI, SAT, MON = date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 9)


@pytest.fixture
def sent(tmp_path):
    return SentimentSettings(output_dir=tmp_path / "sentiment")


def _run(settings, sent, http, **kw):
    kw.setdefault("today", FRI)
    kw.setdefault("scorer_factory", KeywordScorer)
    return rp.run(kw.pop("start", THU), kw.pop("end", SAT), kw.pop("tickers", ["RELIANCE", "TCS"]),
                  settings=settings, sent_settings=sent, http=http, **kw)


# ---- inputs -------------------------------------------------------------------------
def test_dates_skip_weekends_and_holidays():
    assert rp.dates_in_range(THU, MON, frozenset(), False) == [THU, FRI, MON]
    assert rp.dates_in_range(THU, MON, frozenset({FRI}), False) == [THU, MON]
    assert len(rp.dates_in_range(THU, MON, frozenset(), True)) == 5


@pytest.mark.parametrize("name, content", [
    ("u.json", '["RELIANCE", "tcs", "TCS", " INFY "]'),
    ("u.json", '{"tickers": ["RELIANCE", "tcs", "INFY"]}'),
    ("u.txt", "RELIANCE\ntcs  # comment\n\nTCS\nINFY\n"),
])
def test_load_universe_formats_and_dedup(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    assert rp.load_universe(p) == ["RELIANCE", "tcs", "INFY"]


def test_load_universe_rejects_bad_json(tmp_path):
    p = tmp_path / "u.json"
    p.write_text('{"symbols": ["X"]}', encoding="utf-8")
    with pytest.raises(ValueError):
        rp.load_universe(p)


# ---- end to end -----------------------------------------------------------------------
def test_full_range(settings, sent):
    http = FakeHttp(DEFAULT_ROUTES)
    results, rows, csv_path, unresolved = _run(settings, sent, http, tickers=["RELIANCE", "500325", "TCS", "NOPE"])
    assert [r.date for r in results] == [THU, FRI]  # Saturday skipped
    assert list(unresolved) == ["NOPE"]  # 500325 deduped into RELIANCE
    for d in ("2026-11-05", "2026-11-06"):
        assert (settings.output_dir / d / "RELIANCE.json").exists()
        assert (sent.output_dir / d / "_summary.csv").exists()

    assert csv_path.name == "summary_2026-11-05_2026-11-07.csv"
    file_rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    assert [(r["date"], r["symbol"]) for r in file_rows] == [
        ("2026-11-05", "RELIANCE"), ("2026-11-05", "TCS"), ("2026-11-06", "RELIANCE"), ("2026-11-06", "TCS")]
    assert all(r["status"] == "ok" for r in file_rows)
    by_key = {(r["date"], r["symbol"]): r for r in file_rows}
    assert by_key[("2026-11-05", "RELIANCE")]["score"] != ""  # fixture news sits in Thursday's window
    assert by_key[("2026-11-05", "TCS")]["insufficient"] == "True"  # nothing relevant for TCS

    # static NSE endpoints fetched once per ticker, not once per ticker-date
    results_calls = [u for u, _ in http.calls if "corporates-financial-results" in u]
    assert len(results_calls) == 2 * len(settings.nse_result_periods)  # 2 tickers


def test_historical_dates_drop_rolling_sources(settings, sent):
    _run(settings, sent, FakeHttp(DEFAULT_ROUTES), today=date(2026, 12, 1), scorer_factory=None)
    news = json.loads((settings.output_dir / "2026-11-05" / "RELIANCE.json").read_text("utf-8"))
    assert "publisher_rss" not in news["sources"] and "nse_announcements" in news["sources"]


def test_recent_dates_keep_rolling_sources(settings, sent):
    _run(settings, sent, FakeHttp(DEFAULT_ROUTES), scorer_factory=None)
    news = json.loads((settings.output_dir / "2026-11-05" / "RELIANCE.json").read_text("utf-8"))
    assert "publisher_rss" in news["sources"]


def test_skip_existing_makes_no_scrape_requests(settings, sent):
    _run(settings, sent, FakeHttp(DEFAULT_ROUTES))
    http2 = FakeHttp({})  # any request would raise
    results, rows, _, _ = _run(settings, sent, http2, skip_existing=True)
    assert http2.calls == []
    assert all(r.skipped_complete == 2 and not r.scraped for r in results)
    assert all(r["status"] == "ok" for r in rows)


def test_skip_sentiment_writes_no_combined_csv(settings, sent):
    _, rows, csv_path, _ = _run(settings, sent, FakeHttp(DEFAULT_ROUTES), scorer_factory=None)
    assert csv_path is None and {r["status"] for r in rows} == {"not_scored"}


def test_skip_scrape_on_missing_folder_reports_status(settings, sent):
    results, rows, csv_path, _ = _run(settings, sent, FakeHttp({}), skip_scrape=True)
    assert {r["status"] for r in rows} == {"sentiment_failed"}
    assert rows[0]["scrape_error"] == "missing"
    assert csv_path.exists()


def test_sentiment_failure_on_one_date_isolated(settings, sent):
    class FailOnce(KeywordScorer):
        def score(self, texts):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("CUDA out of memory")
            return super().score(texts)

    _, rows, _, _ = _run(settings, sent, FakeHttp(DEFAULT_ROUTES), scorer_factory=FailOnce)
    status = {(r["date"], r["status"]) for r in rows}
    assert status == {("2026-11-05", "sentiment_failed"), ("2026-11-06", "ok")}


def test_per_date_summary_merges_across_runs(settings, sent):
    _run(settings, sent, FakeHttp(DEFAULT_ROUTES), end=THU)
    _run(settings, sent, FakeHttp(DEFAULT_ROUTES), end=THU, tickers=["RELIANCE"])
    rows = list(csv.DictReader(open(sent.output_dir / "2026-11-05" / "_summary.csv", encoding="utf-8")))
    assert [r["symbol"] for r in rows] == ["RELIANCE", "TCS"]  # TCS kept from the first run


def test_cli_rejects_bad_input(tmp_path, capsys):
    assert rp.main(["--start", "2026-11-06", "--end", "2026-11-05"]) == 2
    assert rp.main(["--start", "2026-11-05", "--universe", str(tmp_path / "missing.json")]) == 2
    assert rp.main(["--start", "2026-11-05", "--tickers", "X", "--skip-scrape", "--skip-sentiment"]) == 2
    assert rp.main(["--start", "2099-01-01", "--tickers", "X", "--skip-sentiment"]) == 2


def test_weekend_only_range_does_no_work(settings, sent):
    http = FakeHttp({})
    results, rows, csv_path, _ = _run(settings, sent, http, start=SAT, end=SAT)
    assert results == [] and rows == [] and csv_path is None and http.calls == []


# ---- completeness / unattended behaviour ----------------------------------------------
def _ok_routes():
    return dict(DEFAULT_ROUTES)


def test_incompleteness_reasons(settings, tmp_path):
    p = tmp_path / "X.json"
    assert rp.incompleteness(p, ["a"]) == "missing"
    p.write_text(json.dumps({"sources": {"a": {"ok": True, "skipped": False},
                                         "b": {"ok": False, "skipped": False},
                                         "c": {"ok": True, "skipped": True}}}), encoding="utf-8")
    assert rp.incompleteness(p, ["a", "c"]) is None
    assert rp.incompleteness(p, ["a", "b"]) == "failed: b"
    assert rp.incompleteness(p, ["a", "b"], ignore={"b"}) is None
    assert rp.incompleteness(p, ["a", "d"]) == "not scraped with: d"


def test_skip_existing_rescrapes_degraded_files(settings, sent):
    routes = _ok_routes()
    routes["AnnSubCategoryGetData"] = ConnectionError("BSE down")
    _run(settings, sent, FakeHttp(routes), end=THU, scorer_factory=None)
    news = json.loads((settings.output_dir / "2026-11-05" / "RELIANCE.json").read_text("utf-8"))
    assert news["sources"]["bse_announcements"]["ok"] is False

    http2 = FakeHttp(_ok_routes())
    results, *_ = _run(settings, sent, http2, end=THU, scorer_factory=None, skip_existing=True)
    assert results[0].scraped == {"RELIANCE", "TCS"}  # both degraded -> redone
    news = json.loads((settings.output_dir / "2026-11-05" / "RELIANCE.json").read_text("utf-8"))
    assert news["sources"]["bse_announcements"]["ok"] is True


def test_outage_cooldown_retries_same_date(settings, sent):
    """Google works on Thursday, fails for every ticker on Friday until a cooldown
    passes: Friday must be retried in place, not written off as degraded."""
    google_ok = DEFAULT_ROUTES["news.google.com"]
    sleeps = []

    def google(url, params):
        friday = "after:2026-11-04" in params["q"]  # Friday's window is padded back to 11-04
        if friday and not sleeps:
            raise ConnectionError("429 Too Many Requests")
        return google_ok

    routes = _ok_routes()
    routes["news.google.com"] = google
    results, *_ = _run(settings, sent, FakeHttp(routes), scorer_factory=None,
                       max_cooldowns=3, cooldown_minutes=10, sleep=sleeps.append)
    assert sleeps == [600]  # exactly one 10-minute cooldown, then success
    assert all(not r.incomplete for r in results)


def test_outage_cooldowns_double_then_move_on(settings, sent):
    google_ok = DEFAULT_ROUTES["news.google.com"]

    def google(url, params):
        if "after:2026-11-04" in params["q"]:
            raise ConnectionError("blocked all night")
        return google_ok

    routes = _ok_routes()
    routes["news.google.com"] = google
    sleeps = []
    results, *_ = _run(settings, sent, FakeHttp(routes), scorer_factory=None,
                       max_cooldowns=3, cooldown_minutes=10, sleep=sleeps.append)
    assert sleeps == [600, 1200, 2400]
    assert set(results[1].incomplete) == {"RELIANCE", "TCS"} and not results[0].incomplete


def test_never_working_source_no_cooldown_no_endless_retry(settings, sent):
    routes = _ok_routes()
    routes["AnnSubCategoryGetData"] = ConnectionError("403 from this IP")
    sleeps = []
    http = FakeHttp(routes)
    results, *_ = _run(settings, sent, http, scorer_factory=None, max_cooldowns=3,
                       passes=3, pass_cooldown_minutes=15, sleep=sleeps.append)
    assert sleeps == []  # broken from the start: no cooldowns, no retry passes
    bse_calls = [u for u, _ in http.calls if "AnnSubCategoryGetData" in u]
    assert len(bse_calls) == 4  # 2 tickers x 2 dates, once each
    assert all(r.incomplete for r in results)  # still reported as incomplete


def test_retry_pass_redoes_transient_failures(settings, sent):
    from dataclasses import replace

    calls = {"n": 0}
    real = DEFAULT_ROUTES["AnnSubCategoryGetData"]

    def flaky_bse(url, params):
        calls["n"] += 1
        if calls["n"] == 2:  # one transient failure, after BSE has already worked
            raise ConnectionError("reset")
        return real

    routes = _ok_routes()
    routes["AnnSubCategoryGetData"] = flaky_bse
    sleeps = []
    results, *_ = _run(replace(settings, max_workers=1), sent, FakeHttp(routes), scorer_factory=None,
                       passes=2, pass_cooldown_minutes=15, sleep=sleeps.append)
    assert sleeps == [900]  # one retry pass after a 15-minute wait
    assert all(not r.incomplete for r in results)
