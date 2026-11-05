import json
from news_scraper.config import Settings
from news_scraper.http import HttpClient

h = HttpClient(Settings())
print("backend:", h.backend)

def show(label, data):
    rows = data if isinstance(data, list) else (data.get("data") or data.get("Table") or []) if isinstance(data, dict) else []
    print(f"\n=== {label}: type={type(data).__name__}, rows={len(rows)}")
    if isinstance(data, dict):
        print("top-level keys:", list(data)[:10])
    if rows:
        print("first row keys:", list(rows[0])[:25])
        print(json.dumps(rows[0], indent=1, ensure_ascii=False)[:800])

try:
    show("NSE announcements 30d", h.nse_get_json(
        "https://www.nseindia.com/api/corporate-announcements",
        {"index": "equities", "symbol": "RELIANCE", "from_date": "05-10-2026", "to_date": "04-11-2026"},
        referer="https://www.nseindia.com/companies-listing/corporate-filings-announcements"))
except Exception as e:
    print("NSE FAILED:", type(e).__name__, e)

try:
    show("NSE announcements, no date filter", h.nse_get_json(
        "https://www.nseindia.com/api/corporate-announcements",
        {"index": "equities", "symbol": "RELIANCE"},
        referer="https://www.nseindia.com/companies-listing/corporate-filings-announcements"))
except Exception as e:
    print("NSE (no dates) FAILED:", type(e).__name__, e)

try:
    show("BSE announcements 30d", h.bse_get_json(
        "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w",
        {"pageno": 1, "strCat": -1, "strPrevDate": "20261005", "strScrip": "500325",
         "strSearch": "P", "strToDate": "20261104", "strType": "C", "subcategory": -1}))
except Exception as e:
    print("BSE FAILED:", type(e).__name__, e)