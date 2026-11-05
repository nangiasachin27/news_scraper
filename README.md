# news_scraper — per-ticker financial news for NSE/BSE equities

Takes tickers + a date, writes one JSON per ticker into a date folder:

```
data/news/2026-11-05/RELIANCE.json
data/news/2026-11-05/TCS.json
...
```

These files are the input contract for the sentiment engine.

## Quick start (Windows)

```powershell
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt          # tzdata is required on Windows for zoneinfo
python -m news_scraper diagnose          # probe every upstream endpoint (run this first)
python -m news_scraper refresh-reference # NSE/BSE symbol lists + NSE holiday calendar
python -m news_scraper run --tickers RELIANCE TCS INFY --date 2026-11-05 -v
python -m news_scraper run --universe universe.example.txt     # date defaults to today IST
python -m news_scraper resolve M&M                             # debug alias matching
python -m pytest                                               # offline tests
```

Ticker input accepts `RELIANCE`, `RELIANCE.NS`, `RELIANCE.BO`, `500325` (BSE code) or the ISIN.
Output filename is `{symbol}.json` using the NSE symbol; change `Settings.filename_format`
(`{symbol}`, `{bse_code}`, `{isin}`) if the sentiment engine wants yfinance-style names.

## Pipeline

```
resolve ticker ─► compute window ─► sources (each isolated) ─► trim to window
   ─► relevance (news only) ─► classify (category, impact_tier) ─► drop noise
   ─► dedup (cross-exchange, syndication) ─► optional enrichment ─► merge with existing file ─► atomic write
```

| Source | What | Date-range capable | Requires |
|---|---|---|---|
| `nse_announcements` | Corporate filings (Reg 30 disclosures, outcomes, press releases) | yes | NSE listing |
| `nse_financial_results` | Structured results filings (+ XBRL link) | yes (trimmed locally) | NSE listing |
| `nse_board_meetings` | Board meeting intimations (timestamped at announcement) | yes | NSE listing |
| `nse_corporate_actions` | Ex-date events, tagged `mechanical_price_adjustment` | yes | NSE listing |
| `bse_announcements` | BSE filings; merged into NSE copies when duplicated | yes | BSE scrip code |
| `google_news` | Ticker search across Indian media | yes (~100 items/query cap) | — |
| `google_news_sites` | Same query per outlet via `site:` (Moneycontrol, ET, BS, Reuters) | yes | — |
| `yahoo_finance` | Yahoo per-ticker news; `relatedTickers`-tagged items kept as `source_tagged` | no (latest ~25) | — |
| `publisher_rss` | ET, Moneycontrol, Mint, BS, BusinessLine, FE, CNBC-TV18 rolling feeds | **no — latest items only** | — |

## Key design decisions

**Session window (default).** Folder `D` holds everything from the previous trading day's
15:30 close to `D`'s 15:30 close — i.e. the information set between two closing prices.
Results declared after market on `D` land in `D+1`, which is when they affect price.
`--window calendar` gives plain 00:00–24:00 IST. Holidays come from `config/nse_holidays.txt`
(populated by `refresh-reference`); without it only weekends are skipped.

**Relevance.** Filings are relevant by construction (queried by symbol). News must match an alias:
`exclude_terms` are masked *before* matching (Reliance Power never counts for RELIANCE),
`strict_aliases` are case-sensitive (RIL, L&T, ITC), and market round-ups are capped at the
threshold and tagged `roundup`. Group companies sharing a brand need entries in `config/aliases.yaml`.

**Impact tiers** (`classifier.py`) are a weighting prior for the sentiment engine, not a direction call.
Noise rules run first because zero-information filings often contain high-impact keywords
("newspaper publication of financial results"). Noise tier is dropped by default (`--keep-noise`).

**Source status in every file.** `sources.<name>.ok / error / raw_count / in_window` lets the
sentiment engine distinguish "no news" from "source was down" — treat those differently.

**Merge on re-run.** Re-running a date unions with the existing file by article id and keeps
`first_seen_at`. Rolling RSS feeds drop items within hours, so intraday re-runs accumulate
rather than lose coverage. `--no-merge` overwrites.

## Output schema (v1.0)

```jsonc
{
  "schema_version": "1.0",
  "ticker": {"symbol": "RELIANCE", "company_name": "...", "isin": "INE002A01018", "bse_code": "500325",
             "nse_listed": true, "aliases": [...], "strict_aliases": [...], "exclude_terms": [...]},
  "date": "2026-11-05",
  "window": {"mode": "session", "start": "2026-11-04T15:30:00+05:30", "end": "2026-11-05T15:30:00+05:30"},
  "generated_at": "...",
  "sources": {"nse_announcements": {"ok": true, "skipped": false, "raw_count": 3, "in_window": 2,
                                    "elapsed_ms": 812, "error": null, "notes": []}, "...": {}},
  "stats": {"in_window": 11, "dropped_irrelevant": 2, "dropped_noise": 1, "merged_duplicates": 2, "kept": 6},
  "article_count": 6,
  "articles": [{
    "id": "9f2c1a...",                 // stable across runs
    "source": "nse_announcements",
    "source_type": "exchange_filing",  // exchange_filing | financial_result | board_meeting | corporate_action | news
    "title": "...", "summary": "...", "body": null,
    "url": "...", "publisher": "NSE",
    "published_at": "2026-11-05T09:12:45+05:30",
    "first_seen_at": "...",
    "category": "results",             // results, dividend, m_and_a, order_win, credit_rating, ...
    "impact_tier": "high",             // high | medium | low | noise | unclassified
    "relevance_score": 1.0,            // news: 1.0 title, 0.6 summary, 0.5/0.3 round-up
    "matched_terms": [],
    "attachments": [{"url": "...pdf", "name": "...", "kind": "pdf", "text": null, "error": null}],
    "also_reported_by": ["bse_announcements"],   // or other publishers for syndicated news
    "tags": [],                        // roundup | counterparty | bse_critical | mechanical_price_adjustment
    "meta": {"exchange": "NSE", "exchange_id": "111", "cross_listing": {"bse_announcements": "b1"}}
  }]
}
```

## Exchange access (403s)

NSE and BSE sit behind Akamai bot protection, which fingerprints the TLS handshake.
Plain `requests` gets HTTP 403 whatever headers it sends. The client therefore uses
`curl_cffi` (Chrome TLS impersonation) when installed — `http backend: curl_cffi` in
`diagnose` output confirms it. If the BSE scrip-list API still refuses, download the
Equity list from bseindia.com (Markets → Equity → List of Scrips → Active → Download)
and save it as `data/reference/bse_scrip_list.csv`; the API download is then skipped.
Persistent 403s with curl_cffi usually mean an IP-level block (some cloud/VPN ranges):
run from a residential/office connection, or slow down via `Settings.host_min_interval`.

## Known limitations

* NSE/BSE endpoints are the unofficial JSON APIs behind their websites. They change without notice
  and throttle hard (rate limits default to 1 req/s per host). Field access is defensive, but a
  silent schema change shows up as `raw_count: 0` on liquid names — monitor that.
* Article text: Google News redirects are decoded (`googlenewsdecoder`), then the publisher page
  gives `summary` (og/meta description — usually present even behind paywalls) and `body`
  (trafilatura — often absent behind paywalls). `meta.text_source` = body | summary | title_only
  tells the sentiment engine what it has; `stats.text_sources` counts them. `--no-body` skips this.
* Counterparty mentions ("X bags order from RELIANCE") are tagged `counterparty` at score 0.5.
* Rolling RSS feeds are useless for backfills; historical runs rely on exchanges + Google News.
* Scanned PDF attachments yield no text (flagged `needs OCR`).
* Classification is regex. Every rule is one line and traceable; swap `classify()` for a model later.

## Extending

New source: subclass `sources.base.Source`, decorate with `@register`, implement `fetch`, add its
name to `Settings.enabled_sources`. See the docstring in `sources/base.py`.

---

# sentiment_engine — FinBERT over the news files

```powershell
python -m pip install torch --index-url https://download.pytorch.org/whl/cu124   # CUDA build first
python -m pip install -r requirements-sentiment.txt
python -m sentiment_engine check                       # loads model, scores 3 sample sentences
python -m sentiment_engine run --date 2026-11-02 -v    # needs data/news/2026-11-02/ from news_scraper
```

Writes `data/sentiment/<date>/<SYMBOL>.json` and `data/sentiment/<date>/_summary.csv` (one row per ticker).

**Scoring.** FinBERT is sentence-level, so each article is scored as separate units — title (0.5),
summary (0.3), first 8 body sentences averaged (0.2) — renormalised over the units present.
Unit sentiment `s = p_pos − p_neg ∈ [−1, 1]`.

**Weighting** (all in `sentiment_engine/config.py`): article weight = source_type × impact_tier ×
relevance × tag multipliers × syndication (+25%/outlet, cap 4) × recency (12h half-life to window end).
Filings are weighted 0.4: their text is procedural and FinBERT reads it as neutral whatever the
numbers say. Ex-date corporate actions and noise-tier items get weight 0.

**Aggregate.** Weighted mean of article scores; neutral flow dilutes toward 0 by design.
`insufficient` when total weight < 0.5; `data_quality.degraded` when any scraper source failed —
treat that as "unknown", not "neutral". `drivers` lists the top contributors.

**Swapping the model.** `--model yiyanghkust/finbert-tone`, or implement the `Scorer` protocol
(`score(texts) -> [{positive, negative, neutral}]`) in `sentiment_engine/scorers.py`.

**Not yet:** results sentiment from numbers (XBRL YoY/QoQ deltas), event-study calibration of the
weights against forward returns.

---

# run_pipeline.py — scrape + sentiment over a date range

```powershell
copy universe.example.json universe.json          # edit the ticker list
python run_pipeline.py --start 2026-10-01 --end 2026-11-04
python run_pipeline.py --start 2026-11-02 --tickers RELIANCE TCS     # single date, explicit tickers
python run_pipeline.py --start 2026-10-01 --end 2026-10-31 --skip-existing   # resume a backfill
python run_pipeline.py --start 2026-10-01 --end 2026-10-31 --skip-scrape     # re-score only
```

Output: `data/sentiment/summary_<start>_<end>.csv` — one row per (date, ticker), columns
`date, symbol, status, scrape_error` + the per-date summary columns.
`status`: `ok` | `no_news_file` | `sentiment_failed` | `not_scored` (with `--skip-sentiment`).

* Trading days only. Nothing is lost: each window starts at the previous trading day's close,
  so weekend/holiday news lands in the next session's file.
* Dates older than `--rolling-days` (2) skip `publisher_rss` / `yahoo_finance` (no past data).
* NSE results / board-meeting responses are cached per ticker for the run (full-history endpoints).

**Unattended / overnight runs**
* *Complete* = news file exists and every source expected for that date reported ok.
  `--skip-existing` skips only complete ticker-dates; degraded files are redone (merged).
* Outage cooldown: a source that worked earlier but now fails for *every* ticker on a date is
  treated as a block: sleep `--cooldown` (10 min, doubling, `--max-cooldowns` 3) and retry the date.
* `--passes` (3): incomplete ticker-dates are retried after `--pass-cooldown` (15 min). Sources that
  never worked in the run are reported, not retried forever.
* Google: windows padded by a day (after:/before: inclusivity is undocumented); a query hitting the
  ~100 cap is re-run per calendar day; a partial `site:` failure marks the source not-ok (items kept).
* Windows is kept awake for the run (`--allow-sleep` to disable; a closed lid may still sleep).
  `--log-file` tees all output plus INFO logs to a file.
* Rate-limit bound: ~2 NSE + 5 Google (+ ~1 per kept Google article for link decoding) requests per
  ticker-date. `--google-interval 1.5` lowers block risk at the cost of time.
