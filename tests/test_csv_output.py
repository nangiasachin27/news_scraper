import csv
import json
import os
from dataclasses import replace
from datetime import date

from news_scraper.__main__ import main as cli
from news_scraper.csv_export import COMBINED_NAME, export
from news_scraper.pipeline import NewsPipeline
from news_scraper.writer import ARTICLE_CSV_FIELDS

RUN_DATE = date(2026, 11, 5)


def _read(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def test_pipeline_writes_csv_next_to_json(settings, fake_http):
    s = NewsPipeline(replace(settings, write_csv=True), http=fake_http).run(RUN_DATE, ["RELIANCE.NS"])[0]
    payload = json.loads(s.path.read_text(encoding="utf-8"))
    rows = _read(s.path.with_suffix(".csv"))
    assert list(rows[0]) == ARTICLE_CSV_FIELDS
    assert [r["id"] for r in rows] == [a["id"] for a in payload["articles"]]
    assert {r["symbol"] for r in rows} == {"RELIANCE"} and {r["date"] for r in rows} == {"2026-11-05"}
    q2 = next(r for r in rows if "Q2 profit" in r["title"])
    assert q2["also_reported_by"] == "Mint" and q2["category"] == "results"
    filing = next(r for r in rows if r["source"] == "nse_announcements")
    assert filing["attachments"].count("; ") == 1  # two attachment URLs, joined
    assert open(s.path.with_suffix(".csv"), "rb").read(3) == b"\xef\xbb\xbf"  # BOM for Excel


def test_csv_off_by_default(settings, fake_http):
    s = NewsPipeline(settings, http=fake_http).run(RUN_DATE, ["RELIANCE.NS"])[0]
    assert s.path.exists() and not s.path.with_suffix(".csv").exists()


def test_export_csv_backfills_and_combines(settings, fake_http):
    NewsPipeline(settings, http=fake_http).run(RUN_DATE, ["RELIANCE.NS", "TCS"])
    folder = settings.output_dir / "2026-11-05"
    st = export(settings.output_dir, RUN_DATE, RUN_DATE)
    assert st["files"] == 2 and st["written"] == 3  # 2 per-ticker + _articles.csv
    combined = _read(folder / COMBINED_NAME)
    assert {r["symbol"] for r in combined} == {"RELIANCE", "TCS"} and len(combined) == st["rows"]
    # up to date -> per-ticker files not rewritten, combined is
    assert export(settings.output_dir, RUN_DATE, RUN_DATE)["written"] == 1
    # JSON newer than CSV -> rewritten
    j = folder / "TCS.json"
    os.utime(j, (j.stat().st_atime, (folder / "TCS.csv").stat().st_mtime + 5))
    assert export(settings.output_dir, RUN_DATE, RUN_DATE)["written"] == 2


def test_export_csv_cli_single_file(settings, fake_http, tmp_path, capsys):
    NewsPipeline(settings, http=fake_http).run(RUN_DATE, ["RELIANCE.NS", "TCS"])
    out = tmp_path / "all.csv"
    rc = cli(["export-csv", "--start", "2026-11-01", "--end", "2026-11-06", "--tickers", "TCS",
              "--output-dir", str(settings.output_dir), "--out", str(out)])
    assert rc == 0 and {r["symbol"] for r in _read(out)} == {"TCS"}
    assert not (settings.output_dir / "2026-11-05" / COMBINED_NAME).exists()  # --out leaves folders alone
    assert "-> " in capsys.readouterr().out
    assert cli(["export-csv", "--start", "2026-01-01", "--output-dir", str(settings.output_dir)]) == 1  # nothing found


def test_run_pipeline_csv_flag_parses():
    import run_pipeline

    assert run_pipeline.build_parser().parse_args(["--start", "2026-11-05", "--csv"]).csv
