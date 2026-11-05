"""Probe each upstream endpoint with the real client and report pass/fail.

Run when a source starts failing; paste the output when reporting an issue.
Uses RELIANCE (NSE) / 500325 (BSE) over the last 7 days as a known-active probe.
"""
from __future__ import annotations

import time
from datetime import timedelta

from .config import Settings
from .http import HttpClient, cffi_requests, status_of
from .sources._rss import parse_feed
from .sources.bse import BSE_ANN_URL
from .ticker_resolver import BSE_SCRIP_LIST_URL, NSE_EQUITY_LIST_URL, NSE_HOLIDAY_URL
from .timeutils import now_ist

NSE_API = "https://www.nseindia.com/api"


def _count(data) -> str:
    if isinstance(data, list):
        return f"{len(data)} rows"
    if isinstance(data, dict):
        for k in ("data", "Table", "CM"):
            if isinstance(data.get(k), list):
                return f"{len(data[k])} rows ({k})"
        return f"dict keys={list(data)[:5]}"
    return type(data).__name__


def _google_probe(http: HttpClient, site: str | None = None) -> str:
    q = '"Reliance Industries"' + (f" site:{site}" if site else "")
    params = {"q": q, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    items = parse_feed(http.get("https://news.google.com/rss/search", params=params).content)
    return f"{len(items)} items" + (f" ({site})" if site else "")


def _yahoo_probe(http: HttpClient) -> str:
    from .sources.yahoo import YAHOO_SEARCH_URL

    data = http.get_json(YAHOO_SEARCH_URL, params={"q": "RELIANCE.NS", "quotesCount": 0, "newsCount": 10},
                         headers={"Accept": "application/json", "Referer": "https://finance.yahoo.com/"})
    news = data.get("news") or []
    tagged = sum("RELIANCE.NS" in [str(t).upper() for t in n.get("relatedTickers") or []] for n in news)
    return f"{len(news)} stories, {tagged} tagged RELIANCE.NS"


def run_diagnostics(settings: Settings, http: HttpClient) -> int:
    end = now_ist()
    start = end - timedelta(days=7)
    dmy = lambda d: d.strftime("%d-%m-%Y")  # noqa: E731
    ymd = lambda d: d.strftime("%Y%m%d")  # noqa: E731

    probes = [
        ("NSE warmup (cookies)", lambda: (http._nse_warmup(force=True), "cookies set")[1]),
        ("NSE equity list CSV", lambda: f"{len(http.get(NSE_EQUITY_LIST_URL).content)} bytes"),
        ("NSE announcements", lambda: _count(http.nse_get_json(
            f"{NSE_API}/corporate-announcements",
            {"index": "equities", "symbol": "RELIANCE", "from_date": dmy(start), "to_date": dmy(end)}))),
        ("NSE financial results", lambda: _count(http.nse_get_json(
            f"{NSE_API}/corporates-financial-results",
            {"index": "equities", "symbol": "RELIANCE", "period": "Quarterly"}))),
        ("NSE board meetings", lambda: _count(http.nse_get_json(
            f"{NSE_API}/corporate-board-meetings", {"index": "equities", "symbol": "RELIANCE"}))),
        ("NSE corporate actions", lambda: _count(http.nse_get_json(
            f"{NSE_API}/corporates-corporateActions",
            {"index": "equities", "symbol": "RELIANCE", "from_date": dmy(start - timedelta(days=180)),
             "to_date": dmy(end)}))),
        ("NSE holidays", lambda: _count(http.nse_get_json(NSE_HOLIDAY_URL, {"type": "trading"}))),
        ("BSE scrip list", lambda: _count(http.bse_get_json(
            BSE_SCRIP_LIST_URL,
            {"Group": "", "Scripcode": "", "industry": "", "segment": "Equity", "status": "Active"}))),
        ("BSE announcements", lambda: _count(http.bse_get_json(BSE_ANN_URL, {
            "pageno": 1, "strCat": -1, "strPrevDate": ymd(start), "strScrip": "500325", "strSearch": "P",
            "strToDate": ymd(end), "strType": "C", "subcategory": -1}))),
        ("Google News RSS", lambda: _google_probe(http)),
        ("Google News site:", lambda: _google_probe(http, settings.google_news_sites[0] if settings.google_news_sites else None)),
        ("Yahoo Finance news", lambda: _yahoo_probe(http)),
    ]
    for name, url in settings.publisher_feeds.items():
        probes.append((f"RSS: {name}", lambda url=url: f"{len(parse_feed(http.get(url).content))} items"))

    print(f"http backend : {http.backend}" + ("" if cffi_requests else "  <-- install curl_cffi"))
    print(f"impersonate  : {settings.impersonate if http.backend == 'curl_cffi' else 'n/a'}\n")
    failures = 0
    for label, fn in probes:
        t0 = time.perf_counter()
        try:
            detail = fn()
            mark = "PASS"
        except Exception as exc:
            failures += 1
            code = status_of(exc)
            detail = f"{'HTTP ' + str(code) + ' ' if code else ''}{type(exc).__name__}: {str(exc)[:140]}"
            mark = "FAIL"
        ms = int((time.perf_counter() - t0) * 1000)
        print(f"{mark}  {label:<24} {ms:>6} ms  {detail}")
    print(f"\n{len(probes) - failures}/{len(probes)} endpoints reachable")
    return 1 if failures else 0
