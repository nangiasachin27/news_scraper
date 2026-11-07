import csv
from datetime import date, datetime

import pytest

from news_scraper.timeutils import IST
from sentiment_backtest.core import BacktestSettings
from sentiment_backtest.export import export_rows, main as export_main
from scrape_pairs import load_pairs

from .test_timing_and_backtest import _art, _write_sent

MON, TUE, WED = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "sent"
    _write_sent(r, MON, "AAA", [])
    _write_sent(r, TUE, "AAA", [
        _art(1, "AAA plant shut after fire", datetime(2026, 3, 3, 11, 0, 7, tzinfo=IST), -0.8),
        _art(2, "AAA faces probe", datetime(2026, 3, 3, 12, 10, 9, tzinfo=IST), -0.7),
        _art(3, "AAA late news", datetime(2026, 3, 3, 15, 20, 1, tzinfo=IST), 0.9),  # after the 15:15 cut-off
    ])
    _write_sent(r, TUE, "BBB", [])
    _write_sent(r, WED, "AAA", [])
    return r


def test_rows_are_point_in_time_and_flag_negative(root):
    cfg = BacktestSettings(sentiment_dir=root, half_life_hours=0)
    rows = {(r["date"], r["symbol"]): r for r in export_rows(TUE, WED, cfg)}
    tue = rows[("2026-03-03", "AAA")]
    assert tue["n"] == 2 and tue["negative"] == 1  # the 15:20 item is not in Tuesday's row
    wed = rows[("2026-03-04", "AAA")]
    assert wed["n"] == 1 and wed["negative"] == 0 and wed["score"] == 0.9  # ...it lands on Wednesday
    assert rows[("2026-03-03", "BBB")]["n"] == 0  # scraped, no news: a row with n = 0
    assert ("2026-03-04", "BBB") not in rows      # never scraped: no row at all
    assert ("2026-03-02", "AAA") not in rows      # outside the range


def test_cli_writes_csv(root, tmp_path, capsys):
    out = tmp_path / "gate" / "s.csv"
    assert export_main(["--start", "2026-03-03", "--end", "2026-03-04", "--out", str(out),
                        "--sentiment-dir", str(root)]) == 0
    with open(out, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 3 and rows[0]["decision_time"] == "15:15"
    assert "flagged negative" in capsys.readouterr().out


def test_no_folders_in_range_is_an_error(root, tmp_path):
    assert export_main(["--start", "2026-05-01", "--out", str(tmp_path / "x.csv"), "--sentiment-dir", str(root)]) == 2


def test_load_pairs_groups_by_date(tmp_path):
    p = tmp_path / "pairs.csv"
    p.write_text("Date,Symbol\n2026-03-03,tcs\n2026-03-03,M&M\n2026-03-02,TCS\n2026-03-03,TCS\n", encoding="utf-8")
    assert load_pairs(p) == {MON: ["TCS"], TUE: ["M&M", "TCS"]}


def test_single_day_export_looks_back_to_previous_session(tmp_path):
    """A fresh daily run has only today's folder: Monday must still see Friday-evening news."""
    import json

    root = tmp_path / "sent"
    _write_sent(root, MON, "AAA", [
        _art(1, "AAA fraud alleged", datetime(2026, 2, 28, 11, 0, 7, tzinfo=IST), -0.9),   # Saturday
        _art(2, "AAA auditor quits", datetime(2026, 2, 27, 18, 0, 7, tzinfo=IST), -0.8),   # Friday evening
    ])
    f = root / MON.isoformat() / "AAA.json"
    data = json.loads(f.read_text())
    data["input"] = {"window": {"start": "2026-02-27T15:30:00+05:30", "end": "2026-03-02T15:30:00+05:30"}}
    f.write_text(json.dumps(data))
    row = export_rows(MON, MON, BacktestSettings(sentiment_dir=root, half_life_hours=0))[0]
    assert row["n"] == 2 and row["negative"] == 1


def test_daily_sentiment_end_to_end(tmp_path, monkeypatch, capsys):
    import json

    import daily_sentiment
    import run_pipeline

    DAY = date(2026, 3, 10)  # an ordinary trading day (3 March 2026 is Holi)
    uni = tmp_path / "universe.json"
    uni.write_text(json.dumps({"instruments": {}, "lists": {"nifty500": ["AAA", "BBB", "M&M"]}}))
    root = tmp_path / "sent"
    seen = {}

    def fake_pipeline(argv):
        seen["argv"] = argv
        _write_sent(root, DAY, "AAA", [_art(1, "AAA probe", datetime(2026, 3, 10, 11, 0, 7, tzinfo=IST), -0.9),
                                       _art(2, "AAA raid", datetime(2026, 3, 10, 12, 0, 7, tzinfo=IST), -0.7)])
        _write_sent(root, DAY, "BBB", [])
        return 1  # one ticker incomplete

    monkeypatch.setattr(run_pipeline, "main", fake_pipeline)
    monkeypatch.setattr(daily_sentiment, "BacktestSettings", lambda: BacktestSettings(sentiment_dir=root))
    out = tmp_path / "gate"
    assert daily_sentiment.main(["--date", "2026-03-10", "--wrsi-universe", str(uni), "--out-dir", str(out),
                                 "--", "--workers", "2"]) == 0
    assert seen["argv"][:2] == ["--start", "2026-03-10"] and "M&M" in seen["argv"]
    assert seen["argv"][-2:] == ["--workers", "2"] and "--units" in seen["argv"]
    with open(out / "sentiment_2026-03-10.csv", newline="") as fh:
        rows = {r["symbol"]: r for r in csv.DictReader(fh)}
    assert rows["AAA"]["negative"] == "1" and rows["BBB"]["n"] == "0" and "M&M" not in rows
    text = capsys.readouterr().out
    assert "1 flagged negative" in text and "no row" in text and "M&M" in text


def test_daily_sentiment_skips_holidays(tmp_path, monkeypatch):
    import daily_sentiment
    import run_pipeline

    monkeypatch.setattr(run_pipeline, "main", lambda argv: pytest.fail("must not scrape on a weekend"))
    assert daily_sentiment.main(["--date", "2026-03-07", "--tickers", "TCS", "--out-dir", str(tmp_path)]) == 0
    assert not list(tmp_path.glob("*.csv"))


def test_wrsi_universe_list_missing(tmp_path):
    import json

    import daily_sentiment

    uni = tmp_path / "u.json"
    uni.write_text(json.dumps({"lists": {"default": ["A"]}}))
    with pytest.raises(ValueError, match="nifty500"):
        daily_sentiment.wrsi_tickers(uni, "nifty500")
