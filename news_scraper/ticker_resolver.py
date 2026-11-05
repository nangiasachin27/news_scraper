"""Resolve any user input (RELIANCE, RELIANCE.NS, RELIANCE.BO, 500325, INE002A01018)
into a TickerInfo carrying NSE symbol, BSE scrip code, ISIN and match aliases.

NSE and BSE lists are joined on ISIN, cached under ``reference_dir`` and refreshed
when older than ``reference_max_age_days``. If a download fails, a stale cache is
used; with no cache at all, ``config/aliases.yaml`` entries still resolve (offline
mode), but BSE sources are skipped unless the override supplies ``bse_code``.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import Settings
from .models import TickerInfo

log = logging.getLogger(__name__)

NSE_EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
BSE_SCRIP_LIST_URL = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
NSE_HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master"
NSE_CACHE = "nse_equity_list.csv"
BSE_CACHE = "bse_scrip_list.json"
BSE_MANUAL_CSV = "bse_scrip_list.csv"  # user-supplied export from bseindia.com

ISIN_RE = re.compile(r"^IN[A-Z0-9]{9}\d$")
_SUFFIX_RE = re.compile(r"[\s,.]*\b(limited|ltd)\b\.?\s*$", re.I)


class UnknownTickerError(LookupError):
    pass


def clean_company_name(name: str) -> str:
    cleaned = _SUFFIX_RE.sub("", name or "").strip(" ,.")
    return re.sub(r"\s+", " ", cleaned)


def _dedupe(values, case_insensitive: bool) -> list[str]:
    seen, out = set(), []
    for v in values:
        v = (v or "").strip()
        key = v.lower() if case_insensitive else v
        if v and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def load_overrides(path: Path) -> dict[str, dict[str, Any]]:
    if not path or not Path(path).exists():
        return {}
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping of SYMBOL -> settings")
    out: dict[str, dict[str, Any]] = {}
    for sym, cfg in raw.items():
        cfg = cfg or {}
        for key in ("aliases", "strict_aliases", "exclude_terms", "ambiguous_aliases"):
            val = cfg.get(key, [])
            if isinstance(val, str):
                val = [val]
            cfg[key] = [str(v) for v in val]
        out[str(sym).upper()] = cfg
    return out


class TickerResolver:
    def __init__(self, settings: Settings, http) -> None:
        self.settings = settings
        self.http = http
        self._lock = threading.RLock()
        self._loaded = False
        self._nse: dict[str, dict] = {}
        self._nse_by_isin: dict[str, dict] = {}
        self._bse_by_code: dict[str, dict] = {}
        self._bse_by_isin: dict[str, dict] = {}
        self._bse_by_symbol: dict[str, dict] = {}
        self._overrides: dict[str, dict] = {}

    # ---- reference data ----------------------------------------------------
    @property
    def _nse_path(self) -> Path:
        return Path(self.settings.reference_dir) / NSE_CACHE

    @property
    def _bse_path(self) -> Path:
        return Path(self.settings.reference_dir) / BSE_CACHE

    def _is_stale(self, path: Path) -> bool:
        if not path.exists():
            return True
        return (time.time() - path.stat().st_mtime) > self.settings.reference_max_age_days * 86400

    _CONSEQUENCE = {
        "NSE equity list": "NSE symbols resolve only via config/aliases.yaml",
        "BSE scrip list": (
            "BSE codes unresolved, so bse_announcements is skipped. Fix: download the Equity list "
            "from bseindia.com (Markets > Equity > List of Scrips > Active > Download) and save it "
            f"as <reference_dir>/{BSE_MANUAL_CSV}, or set bse_code per ticker in aliases.yaml"
        ),
    }

    def _ensure(self, path: Path, downloader: Callable[[Path], None], label: str, force: bool) -> bool:
        """Download if forced or stale. Returns True if a usable copy exists afterwards."""
        if not force and not self._is_stale(path):
            return True
        try:
            downloader(path)
            log.info("downloaded %s -> %s", label, path)
            return True
        except Exception as exc:  # network/format failure must not kill the run
            if path.exists():
                log.warning("%s refresh failed (%s); using cached copy", label, exc)
                return True
            log.warning("%s unavailable (%s). %s", label, exc, self._CONSEQUENCE.get(label, ""))
            return False

    def _download_nse(self, path: Path) -> None:
        resp = self.http.get(NSE_EQUITY_LIST_URL, headers={"Referer": "https://www.nseindia.com/"})
        text = resp.content.decode("utf-8-sig")
        if "SYMBOL" not in text[:200].upper():
            raise ValueError("unexpected NSE equity list format")
        _atomic_write(path, text.encode("utf-8"))

    def _download_bse(self, path: Path) -> None:
        data = self.http.bse_get_json(
            BSE_SCRIP_LIST_URL,
            params={"Group": "", "Scripcode": "", "industry": "", "segment": "Equity", "status": "Active"},
        )
        rows = data if isinstance(data, list) else (data or {}).get("Table", [])
        if not rows:
            raise ValueError("empty BSE scrip list")
        _atomic_write(path, json.dumps(rows, ensure_ascii=False).encode("utf-8"))

    @property
    def _bse_manual_path(self) -> Path:
        return Path(self.settings.reference_dir) / BSE_MANUAL_CSV

    def refresh(self) -> dict[str, bool]:
        """Force re-download. Returns {label: usable_copy_available}."""
        with self._lock:
            Path(self.settings.reference_dir).mkdir(parents=True, exist_ok=True)
            status = {"NSE equity list": self._ensure(self._nse_path, self._download_nse, "NSE equity list", True)}
            if self._bse_manual_path.exists():
                log.info("using manual %s; skipping BSE API download", self._bse_manual_path.name)
                status["BSE scrip list"] = True
            else:
                status["BSE scrip list"] = self._ensure(self._bse_path, self._download_bse, "BSE scrip list", True)
            self._loaded = False
            self._load(download=False)  # don't immediately retry what just failed
            return status

    def refresh_holidays(self) -> int:
        """Write NSE capital-market trading holidays to ``holidays_file``."""
        data = self.http.nse_get_json(NSE_HOLIDAY_URL, params={"type": "trading"})
        rows = (data or {}).get("CM", []) if isinstance(data, dict) else []
        from .timeutils import parse_exchange_dt

        lines = ["# NSE capital-market trading holidays (generated by refresh-reference)"]
        for r in rows:
            dt = parse_exchange_dt(r.get("tradingDate"))
            if dt:
                lines.append(f"{dt.date().isoformat()}  # {r.get('description', '').strip()}")
        if len(lines) == 1:
            raise ValueError("NSE holiday API returned no CM rows")
        _atomic_write(Path(self.settings.holidays_file), ("\n".join(lines) + "\n").encode("utf-8"))
        return len(lines) - 1

    def _load(self, download: bool = True) -> None:
        with self._lock:
            if self._loaded:
                return
            Path(self.settings.reference_dir).mkdir(parents=True, exist_ok=True)
            manual_bse = self._bse_manual_path.exists()
            if download:
                self._ensure(self._nse_path, self._download_nse, "NSE equity list", force=False)
                if not manual_bse:
                    self._ensure(self._bse_path, self._download_bse, "BSE scrip list", force=False)
            if self._nse_path.exists():
                self._parse_nse(self._nse_path)
            self._bse_by_code.clear()
            self._bse_by_isin.clear()
            self._bse_by_symbol.clear()
            if self._bse_path.exists():
                self._ingest_bse_rows(json.loads(self._bse_path.read_text(encoding="utf-8")))
            if manual_bse:
                # Manual export wins for any code present in both.
                self._ingest_bse_rows(self._read_bse_csv(self._bse_manual_path))
            self._overrides = load_overrides(self.settings.aliases_file)
            self._loaded = True

    def _parse_nse(self, path: Path) -> None:
        self._nse.clear()
        self._nse_by_isin.clear()
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                row = {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
                sym = row.get("SYMBOL", "").upper()
                if not sym:
                    continue
                rec = {
                    "symbol": sym,
                    "name": row.get("NAME OF COMPANY", ""),
                    "isin": row.get("ISIN NUMBER") or None,
                    "series": row.get("SERIES", ""),
                }
                if sym in self._nse and self._nse[sym]["series"] == "EQ":
                    continue
                self._nse[sym] = rec
                if rec["isin"]:
                    self._nse_by_isin[rec["isin"]] = rec

    @staticmethod
    def _read_bse_csv(path: Path) -> list[dict]:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))

    def _ingest_bse_rows(self, rows: list[dict]) -> None:
        """Accepts both the API JSON (SCRIP_CD, scrip_id, ISIN_NUMBER, ...) and the
        website's Equity.csv export (Security Code, Security Id, ISIN No, ...)."""

        def pick(low: dict, *keys: str) -> str:
            for k in keys:
                v = low.get(k)
                if v not in (None, ""):
                    return str(v).strip()
            return ""

        for r in rows:
            low = {str(k).strip().lower(): v for k, v in r.items() if k is not None}
            code = pick(low, "scrip_cd", "security code", "scrip code", "scripcode")
            if not code.isdigit():
                continue
            status = pick(low, "status").lower()
            if status and status != "active":
                continue
            rec = {
                "code": code,
                "symbol": pick(low, "scrip_id", "security id", "scrip id").upper() or None,
                "name": pick(low, "issuer_name", "issuer name", "scrip_name", "security name"),
                "isin": pick(low, "isin_number", "isin no", "isin").upper() or None,
            }
            self._bse_by_code[code] = rec
            if rec["isin"]:
                self._bse_by_isin[rec["isin"]] = rec
            if rec["symbol"]:
                self._bse_by_symbol[rec["symbol"]] = rec

    # ---- resolution ----------------------------------------------------------
    def resolve(self, raw: str) -> TickerInfo:
        self._load()
        key = raw.strip().upper()
        nse_rec: dict | None = None
        bse_rec: dict | None = None

        if key.endswith(".NS"):
            key = key[:-3]
        elif key.endswith(".BO"):
            bse_rec = self._bse_by_symbol.get(key[:-3]) or self._bse_by_code.get(key[:-3])
            if bse_rec is None:
                raise UnknownTickerError(f"{raw!r}: BSE symbol not in reference list")
        elif key.isdigit() and len(key) == 6:
            bse_rec = self._bse_by_code.get(key)
            if bse_rec is None:
                raise UnknownTickerError(f"{raw!r}: BSE scrip code not in reference list")
        elif ISIN_RE.match(key):
            nse_rec = self._nse_by_isin.get(key)
            bse_rec = self._bse_by_isin.get(key)
            if nse_rec is None and bse_rec is None:
                raise UnknownTickerError(f"{raw!r}: ISIN not in reference lists")

        if bse_rec and nse_rec is None and bse_rec.get("isin"):
            nse_rec = self._nse_by_isin.get(bse_rec["isin"])
        if nse_rec is None and bse_rec is None:
            nse_rec = self._nse.get(key)
            if nse_rec is None:
                bse_rec = self._bse_by_symbol.get(key)
        if nse_rec and bse_rec is None and nse_rec.get("isin"):
            bse_rec = self._bse_by_isin.get(nse_rec["isin"])

        if nse_rec is None and bse_rec is None:
            override = self._overrides.get(key)
            if override is None:
                raise UnknownTickerError(
                    f"{raw!r} not found in NSE/BSE reference lists "
                    "(run `refresh-reference`, or add it to config/aliases.yaml)"
                )
            log.warning("%s resolved from aliases.yaml only (reference lists unavailable)", key)
            return self._build(
                symbol=key,
                name=override.get("company_name") or key,
                isin=override.get("isin"),
                bse_code=str(override["bse_code"]) if override.get("bse_code") else None,
                bse_symbol=None,
                nse_listed=True,
                override=override,
            )

        symbol = nse_rec["symbol"] if nse_rec else (bse_rec.get("symbol") or bse_rec["code"])
        override = self._overrides.get(symbol, {})
        name = (nse_rec or {}).get("name") or (bse_rec or {}).get("name") or symbol
        isin = (nse_rec or {}).get("isin") or (bse_rec or {}).get("isin")
        bse_code = bse_rec["code"] if bse_rec else (str(override["bse_code"]) if override.get("bse_code") else None)
        return self._build(
            symbol=symbol,
            name=override.get("company_name") or name,
            isin=isin,
            bse_code=bse_code,
            bse_symbol=(bse_rec or {}).get("symbol"),
            nse_listed=nse_rec is not None,
            override=override,
        )

    @staticmethod
    def _build(*, symbol, name, isin, bse_code, bse_symbol, nse_listed, override) -> TickerInfo:
        clean = clean_company_name(name)
        aliases = list(override.get("aliases", []))
        strict = list(override.get("strict_aliases", []))
        # A name equal to the symbol (offline fallback) adds nothing and, matched
        # case-insensitively, would turn "RELIANCE" into a bare "reliance" alias.
        if clean and clean.upper() != symbol.upper():
            # Short all-caps names (ITC, ACC) collide with ordinary text when matched
            # case-insensitively, so they are matched case-sensitively.
            (strict if len(clean) <= 4 and clean.isupper() else aliases).append(clean)
        if override.get("use_symbol_alias", True) and len(symbol) >= 3 and symbol.isalpha():
            strict.append(symbol)
        return TickerInfo(
            symbol=symbol,
            company_name=name,
            isin=isin,
            bse_code=bse_code,
            bse_symbol=bse_symbol,
            nse_listed=nse_listed,
            aliases=_dedupe(aliases, case_insensitive=True),
            strict_aliases=_dedupe(strict, case_insensitive=False),
            exclude_terms=_dedupe(override.get("exclude_terms", []), case_insensitive=True),
            ambiguous_aliases=_dedupe(override.get("ambiguous_aliases", []), case_insensitive=False),
        )
