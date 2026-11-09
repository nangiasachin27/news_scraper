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
Multi-story digests are tagged `roundup` too. Quote pages, company landing pages and
share-price live blogs are dropped (`stats.dropped_quote_page`): they aren't news, and
their text changes all day. Sponsorship and CSR brand uses ("Jaguar TCS Racing", "TCS
Foundation") are `exclude_terms`.

**Story grouping.** After near-identical titles are merged, headlines about one event in
different words ("Jaleel named accused in TCS Nashik case" from six outlets) are grouped:
3+ shared distinctive words making up 40%+ of the shorter headline's
(`Settings.story_min_shared` / `story_min_overlap`, off with `story_grouping=False`). The
earliest item is kept, other outlets go to `also_reported_by` (the sentiment engine's
syndication bonus, capped), merged titles to `meta.story_titles`.

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
* Article pages are **not fetched by default** (`fetch_article_body=False`). The Jan–Sep 2026
  backtest found headline-only scoring carries the signal, and skipping pages makes runs much
  faster (no Google-link decoding, no paywalls). News items then have `summary` only where the
  RSS feed supplies one, and `body: null`. Pass `--with-body` (news_scraper or run_pipeline) to
  decode Google News links and fetch publisher pages: `summary` from og/meta description, `body`
  from trafilatura (often absent behind paywalls). `meta.text_source` = body | summary | title_only
  and `stats.text_sources` show what each run got. `--no-body` is still accepted (the default).
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

**Scoring.** Both CLIs score **headlines only** by default (`--units title`): that is the setting the
Jan–Sep 2026 backtest validated, for news and filings alike. `--units all` scores each article as
separate units — title (0.5), summary (0.3), first 8 body sentences averaged (0.2) — renormalised
over the units present; `--units title,summary` drops bodies. (`SentimentSettings` used directly
from Python still defaults to all three units.)
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

---

# Backtesting: sentiment_backtest

**The timestamp problem.** For older articles Google News gives a date only, which the feed
renders as midnight US Pacific time: 07:00 GMT under PDT, 08:00 GMT under PST (12:30 / 13:30
IST). The date is the *Pacific* date, so the article may have come out at any moment in the
next 24 hours, up to 12:29 / 13:29 IST the following day. Taken literally, news published
after the close on day D looks as if it was known before D's close: lookahead. Live runs get
real times. Exchange APIs can return date-only fields too (e.g. `05-Nov-2026`).

**How the scraper handles it now.** Every date-only item is flagged
`meta.time_precision: "date"` (original value in `meta.reported_published_at`, source field in
`meta.time_field` for exchange rows) and stored at the **latest moment it could have appeared**:
Google items at placeholder + 23:59:59 (12:29:59 / 13:29:59 IST next day), exchange items at
23:59:59 IST of their date. Both fall into the session window they cannot postdate.
`Settings.date_only_policy = "keep"` restores the raw placeholder for Google items.

Re-running a date also re-applies these rules to items carried over from the existing file
and drops any that no longer belong to the window (`stats.dropped_out_of_window`), so files
written by older code are cleaned on the next run.

**How the backtester stays honest.** Every item gets a `known_at`: its timestamp when exact,
the latest moment it could have appeared when date-only (`news_scraper.timeutils.known_at`).
A signal for a decision uses only items known before it:

| Decision | Signal uses items known in | Returns measured |
|---|---|---|
| open, 09:15 on T | [09:15 previous session, 09:15 T) | open T → close T; open T → close T+4 |
| close, 15:15 on T | [15:15 previous session, 15:15 T) | close T → close T+1; close T → close T+5 |

Media news therefore reaches the backtest up to a day later than production: the backtest is a
**conservative lower bound** on the live system. Files scored before the precision flag existed
are handled by detecting placeholder times (whole UTC hours for media, exactly 00:00:00 IST for
filings, 23:59:59 IST from the previous rule); ex-dividend headlines are zero-weighted.

```powershell
python -m pip install -r requirements-backtest.txt
python fetch_prices.py data\sentiment\summary_2026-01-01_2026-09-22.csv
python -m sentiment_backtest --start 2026-01-01 --end 2026-09-22 --prices prices.csv --exclude RELIANCE,M&M
```

Output in `data\backtest\<start>_<end>\`: `metrics.csv` (rank correlation, the negative-news gate
and the strongly-positive bucket, each against the rest, with 95% intervals and p-values from
resampling 5-day blocks), `stability.csv` (does the effect keep its sign in each of 3
sub-periods), `signals.csv`, `report.json`.

**Validation protocol**
1. Freeze the rule before looking (the gate: score ≤ −0.25, weight ≥ 1.0, more negative than
   positive articles). Report the frozen rule first; treat anything tuned afterwards as a
   hypothesis for the next period, not a result.
2. Require the effect in every sub-period, not just overall (`stability.csv`).
3. Headline-only robustness: article bodies of old news are downloaded long after publication
   and may have been edited. Re-score headlines only into a separate folder and re-run:
   ```powershell
   python run_pipeline.py --start 2026-01-01 --end 2026-09-22 --skip-scrape --units title --sentiment-out data\sentiment_title
   python -m sentiment_backtest --start 2026-01-01 --end 2026-09-22 --prices prices.csv --sentiment-dir data\sentiment_title --out-dir data\backtest_title
   ```
4. Parity on live data: after a few months of daily runs, `--parity` recomputes every signal
   with media timestamps cut back to dates (as a backfill sees them) and reports how closely
   the two agree. High agreement means backfill results transfer to production.
5. Forward test: keep the rule frozen and re-run the backtest on each new month of live data.

## Materiality experiment (zero-shot)

FinBERT scores tone, not importance: "LPG shortage forces Infosys to limit catering services"
reads as strongly negative. With `--materiality`, a zero-shot NLI model (`facebook/bart-large-mnli`
by default) rates each news **headline** on "news that could move the company's share price" vs
"routine news with no effect on the share price". The article weight is multiplied by
`0.1 + 0.9 × materiality`; filings are left alone. Labels, template and floor were fixed before
any result was seen (`sentiment_engine/scorers.py`, `sentiment_engine/config.py`).

The factor is stored in each article's `weight_factors`, so one scoring run gives both arms:

```powershell
python -m sentiment_engine check --materiality          # downloads the model (~1.6 GB), scores sample headlines
python run_pipeline.py --start 2026-01-01 --end 2026-09-22 --skip-scrape --units title --materiality --sentiment-out data\sentiment_title_mat
python -m sentiment_backtest --start 2026-01-01 --end 2026-09-22 --prices prices.csv --exclude "RELIANCE,M&M" --sentiment-dir data\sentiment_title_mat --out-dir data\backtest_mat_on
python -m sentiment_backtest --start 2026-01-01 --end 2026-09-22 --prices prices.csv --exclude "RELIANCE,M&M" --sentiment-dir data\sentiment_title_mat --out-dir data\backtest_mat_off --no-materiality
```

Adopt it only if `backtest_mat_on` beats `backtest_mat_off` in every sub-period (stability.csv),
not just overall. It stays off by default until then.

# Export for trading systems: sentiment_backtest.export

```powershell
python -m sentiment_backtest.export --start 2026-01-01 --end 2026-10-31 --out sentiment_2026-01-01_2026-10-31.csv
```

One row per scraped ticker-day: `date, symbol, decision_time, score, weight, n, n_neg, n_pos,
date_only_share, negative`. Rows are computed by the backtester's own signal code, so they are
point-in-time: the row for day T uses only news known before 15:15 IST on T (date-only
timestamps count at the latest moment they could have appeared). `negative` is the frozen rule (score ≤ −0.25,
weight ≥ 1.0, n_neg > n_pos). A scraped day with no news has a row with `n = 0`; a ticker-day
that was never scraped has **no row**, and consumers should treat it as unknown.

wrsi_trader_new's `sentiment` filter reads these files (see its `config/filters/sentiment.json`).

## Scraping only what a backtest needs: scrape_pairs.py

Backfilling all of Nifty 500 every day is hundreds of thousands of Google queries. A trading
backtest only asks about the candidates its other filters let through, so scrape those:

```powershell
python scrape_pairs.py pairs.csv --dry-run          # plan: dates x tickers
python scrape_pairs.py pairs.csv --skip-existing    # options after the file go to run_pipeline.py
```

`pairs.csv` needs `date,symbol` columns; wrsi_trader_new's `scripts/sentiment_check.py pairs`
writes it from a backtest journal.

## Daily run for wrsi_trader_new: daily_sentiment.py + GitHub workflow

```powershell
python daily_sentiment.py --wrsi-universe ..\wrsi_trader_new\config\universe.json --out-dir ..\wrsi_trader_new\sentiment_gate
python daily_sentiment.py --date 2026-11-06 --tickers TCS INFY "M&M" --out-dir out -- --workers 4   # after --: run_pipeline options
```

It checks the date is an NSE trading day, runs `run_pipeline` for that date (headlines only,
`--skip-existing`, short cooldowns), exports `sentiment_<date>.csv` and prints a summary:
coverage, flagged tickers, source failures, and tickers with no row. Run it after 15:30 IST.

`.github/workflows/daily_sentiment.yml` runs it at 15:35 IST on weekdays, commits the CSV to
`wrsi_trader_new/sentiment_gate/`, and keeps the raw news and sentiment JSONs as a 90-day
artifact. Setup:

1. Create a fine-grained PAT with access to `wrsi_trader_new` only and Contents: Read and write.
   Save it as the `WRSI_PUSH_TOKEN` secret in this repo.
2. Move wrsi's `eod` cron after this job finishes, e.g. `"0 12 * * 1-5"` (17:30 IST). A Nifty 500
   run takes roughly 45–90 minutes, and GitHub's cron can start late. A missing CSV only means
   the filter treats that day as unknown and lets every candidate through.
3. Run it once with *Run workflow* and read the job summary. NSE and BSE often block cloud IPs.
   If their sources fail for most tickers, filings are missing from the signal; a self-hosted
   runner on your own PC avoids that.
