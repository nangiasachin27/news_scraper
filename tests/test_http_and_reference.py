from dataclasses import replace

import pytest

from news_scraper.http import HttpClient
from news_scraper.ticker_resolver import TickerResolver

from .conftest import FakeHttp

BSE_EQUITY_CSV = """Security Code,Issuer Name,Security Id,Security Name,Status,Group,Face Value,ISIN No,Industry,Instrument
500325,Reliance Industries Ltd,RELIANCE,RELIANCE INDUSTRIES LTD.,Active,A,10.00,INE002A01018,Refineries & Marketing,Equity
500875,ITC Ltd,ITC,ITC LTD.,Active,A,1.00,INE154A01025,Diversified FMCG,Equity
999999,Delisted Co Ltd,DEADCO,DEADCO LTD.,Delisted,Z,10.00,INE000X01010,,Equity
"""


def test_manual_bse_csv_used_and_api_not_called(settings):
    (settings.reference_dir / "bse_scrip_list.json").unlink()
    (settings.reference_dir / "bse_scrip_list.csv").write_text(BSE_EQUITY_CSV, encoding="utf-8")
    http = FakeHttp({})  # any HTTP call would raise AssertionError
    resolver = TickerResolver(settings, http)
    info = resolver.resolve("RELIANCE")
    assert info.bse_code == "500325"
    assert resolver.resolve("500875").symbol == "ITC"
    assert not any("bseindia" in url for url, _ in http.calls)


def test_manual_csv_skips_inactive(settings):
    (settings.reference_dir / "bse_scrip_list.json").unlink()
    (settings.reference_dir / "bse_scrip_list.csv").write_text(BSE_EQUITY_CSV, encoding="utf-8")
    from news_scraper.ticker_resolver import UnknownTickerError

    with pytest.raises(UnknownTickerError):
        TickerResolver(settings, FakeHttp({})).resolve("999999")


def test_refresh_does_not_retry_failed_download_twice(settings):
    http = FakeHttp({"nsearchives": OSError("blocked"), "ListofScripData": OSError("blocked")})
    status = TickerResolver(settings, http).refresh()
    # cached fixtures exist, so copies are still usable
    assert status == {"NSE equity list": True, "BSE scrip list": True}
    assert sum("ListofScripData" in u for u, _ in http.calls) == 1
    assert sum("nsearchives" in u for u, _ in http.calls) == 1


def test_refresh_reports_unavailable(settings):
    for f in settings.reference_dir.iterdir():
        f.unlink()
    http = FakeHttp({"nsearchives": OSError("blocked"), "ListofScripData": OSError("blocked")})
    assert TickerResolver(settings, http).refresh() == {"NSE equity list": False, "BSE scrip list": False}


# ---- HttpClient retry semantics (session stubbed) ------------------------------
class _Resp:
    def __init__(self, status, body=b"{}"):
        self.status_code, self.content, self.headers = status, body, {}

    def raise_for_status(self):
        if self.status_code >= 400:
            err = RuntimeError(f"HTTP {self.status_code}")
            err.response = self
            raise err

    def json(self):
        import json

        return json.loads(self.content)


class _Session:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0
        self.cookies = type("C", (), {"clear": lambda self: None})()

    def get(self, url, **kw):
        self.calls += 1
        return _Resp(self.statuses.pop(0))


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = HttpClient(replace(settings, max_retries=2, http_backend="requests"))
    return c


def test_retries_5xx_then_succeeds(client):
    s = _Session([503, 502, 200])
    client._local.session, client._local.nse_ready = s, False
    assert client.get_json("https://x.example/a") == {}
    assert s.calls == 3


def test_does_not_retry_403(client):
    s = _Session([403, 200])
    client._local.session, client._local.nse_ready = s, False
    with pytest.raises(RuntimeError):
        client.get("https://x.example/a")
    assert s.calls == 1


def test_html_bot_page_rejected_as_json(client):
    class S(_Session):
        def get(self, url, **kw):
            return _Resp(200, b"<html>Access Denied</html>")

    client._local.session, client._local.nse_ready = S([]), False
    with pytest.raises(ValueError, match="bot-check"):
        client.get_json("https://x.example/a")


def test_nse_warmup_falls_back_to_next_page(client):
    statuses = {"https://www.nseindia.com/": 403}

    class S(_Session):
        def get(self, url, **kw):
            self.calls += 1
            return _Resp(statuses.get(url, 200), b"[]")

    s = S([])
    client._local.session, client._local.nse_ready = s, False
    assert client.nse_get_json("https://www.nseindia.com/api/x") == []
    assert client._local.nse_ready is True
