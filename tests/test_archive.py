import io
import json
import zipfile
from datetime import date
from types import SimpleNamespace

import pytest

import archive_release as ar

D = date(2026, 11, 12)


def _day(root, d=D):
    for folder, names in (("data/news", ["TCS.json", "M&M.json"]), ("data/sentiment", ["TCS.json", "_summary.csv"]),
                          ("data/sentiment_title", ["TCS.json"])):
        p = root / folder / d.isoformat()
        p.mkdir(parents=True, exist_ok=True)
        for n in names:
            (p / n).write_text(json.dumps({"f": n, "body": "full article text"}), encoding="utf-8")
    (root / "data/news" / d.isoformat() / "TCS.json.tmp").write_text("partial")  # never archived


def test_pack_layout(tmp_path):
    _day(tmp_path)
    extra = tmp_path / "sentiment_2026-11-12.csv"
    extra.write_text("date,symbol\n")
    z, counts = ar.pack(D, tmp_path, tmp_path / "out", [extra, tmp_path / "missing.log"])
    assert z.name == "news_2026-11-12.zip"
    assert counts == {"data/news": 2, "data/sentiment": 2, "data/sentiment_title": 1, "extras": 1}
    names = set(zipfile.ZipFile(z).namelist())
    assert "data/news/2026-11-12/M&M.json" in names and "extras/sentiment_2026-11-12.csv" in names
    assert "MANIFEST.json" in names and not any(n.endswith(".tmp") for n in names)


def test_pack_without_news_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        ar.pack(D, tmp_path, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*"))


def test_upload_creates_monthly_release_once(tmp_path, monkeypatch):
    calls, releases = [], set()

    def fake_gh(*args, check=True):
        calls.append(args)
        if args[:2] == ("release", "view"):
            return SimpleNamespace(returncode=0 if args[2] in releases else 1, stderr="")
        if args[:2] == ("release", "create"):
            releases.add(args[2])
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(ar, "_gh", fake_gh)
    _day(tmp_path)
    z, _ = ar.pack(D, tmp_path, tmp_path / "out")
    assert ar.upload(z, D, "me/repo") == "news-archive-2026-11"
    ar.upload(z, D, "me/repo")
    creates = [c for c in calls if c[:2] == ("release", "create")]
    assert len(creates) == 1 and "--prerelease" in creates[0]
    uploads = [c for c in calls if c[:2] == ("release", "upload")]
    assert len(uploads) == 2 and all("--clobber" in u for u in uploads)


def test_restore_round_trip(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _day(src)
    _day(src, date(2026, 12, 1))
    z1, _ = ar.pack(D, src, tmp_path / "out")
    z2, _ = ar.pack(date(2026, 12, 1), src, tmp_path / "out")
    blobs = {"https://dl.test/u1": z1.read_bytes(), "https://dl.test/u2": z2.read_bytes()}
    releases = {
        "news-archive-2026-11": [{"name": z1.name, "browser_download_url": "https://dl.test/u1", "size": 10}],
        "news-archive-2026-12": [{"name": z2.name, "browser_download_url": "https://dl.test/u2", "size": 20},
                                 {"name": "notes.txt", "browser_download_url": "https://dl.test/x", "size": 1}],
    }

    def fake_api(url):
        tag = url.rsplit("/", 1)[-1]
        if tag not in releases:
            raise ar.urllib.error.HTTPError(url, 404, "nf", {}, None)
        return {"assets": releases[tag]}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ar, "_api", fake_api)
    monkeypatch.setattr(ar.urllib.request, "urlopen", lambda req, timeout=0: Resp(blobs[req.full_url]))
    dest = tmp_path / "dest"
    assert [d for d, *_ in ar.assets_between(date(2026, 10, 1), date(2026, 12, 31), "me/repo")] == [D, date(2026, 12, 1)]
    assert ar.restore(date(2026, 11, 1), date(2026, 12, 31), dest, "me/repo") == [D, date(2026, 12, 1)]
    assert json.loads((dest / "data/news/2026-11-12/TCS.json").read_text())["body"] == "full article text"
    assert ar.restore(D, D, dest, "me/repo") == []  # already present
    assert ar.restore(D, D, dest, "me/repo", overwrite=True) == [D]


def test_restore_rejects_unsafe_zip(tmp_path, monkeypatch):
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.txt", "x")

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ar, "assets_between", lambda s, e, r: [(D, "https://dl.test/bad", 1)])
    monkeypatch.setattr(ar.urllib.request, "urlopen", lambda req, timeout=0: Resp(bad.getvalue()))
    with pytest.raises(ValueError, match="unsafe"):
        ar.restore(D, D, tmp_path / "dest")
    assert not (tmp_path / "evil.txt").exists()


def test_daily_sentiment_reports_date_for_later_steps(tmp_path, monkeypatch):
    import daily_sentiment

    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert daily_sentiment.main(["--date", "2026-03-07", "--tickers", "TCS", "--out-dir", str(tmp_path)]) == 0
    assert out.read_text() == "date=2026-03-07\ntrading_day=false\n"
