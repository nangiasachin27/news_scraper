"""Enrichment for kept items only (runs after relevance/dedup, so cost scales with
signal, not with raw feed volume).

Media items
  1. Google News redirect URLs are decoded to the publisher URL (googlenewsdecoder).
  2. The publisher page is fetched once; from it we take
     - ``summary``: og:description / meta description. This is the lede, and it is
       usually present even on paywalled pages.
     - ``body``: main text via trafilatura (often truncated or absent behind paywalls).
  ``meta.text_source`` records what the sentiment engine actually has:
  "body" | "summary" | "title_only".

Filings
  PDF text for attachments of ``attachment_tiers`` filings (results, presentations).
  Scanned PDFs yield nothing and are flagged for OCR.

All optional deps degrade to a recorded per-item error, never an exception.
"""
from __future__ import annotations

import html as htmllib
import io
import logging
import re
from typing import Callable
from urllib.parse import urlsplit

from .config import Settings
from .models import Article, Attachment

log = logging.getLogger(__name__)

HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_META_TAG_RE = re.compile(r"<meta\s[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_DESCRIPTION_KEYS = ("og:description", "description", "twitter:description")


def meta_description(page: str) -> str | None:
    found: dict[str, str] = {}
    for tag in _META_TAG_RE.findall(page[:200_000]):  # meta tags live in <head>
        attrs = {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3))
                 for m in _ATTR_RE.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in _DESCRIPTION_KEYS and attrs.get("content") and key not in found:
            found[key] = attrs["content"]
    for key in _DESCRIPTION_KEYS:
        if found.get(key):
            text = re.sub(r"\s+", " ", htmllib.unescape(found[key])).strip()
            return text or None
    return None


def _default_google_decoder(url: str) -> str | None:
    from googlenewsdecoder import gnewsdecoder  # optional dependency

    result = gnewsdecoder(url)
    if isinstance(result, dict) and (result.get("success") or result.get("status")):
        return result.get("decoded_url")
    return None


def _referer_for(url: str) -> dict[str, str] | None:
    host = urlsplit(url).netloc.lower()
    if "nseindia.com" in host:
        return {"Referer": "https://www.nseindia.com/"}
    if "bseindia.com" in host:
        return {"Referer": "https://www.bseindia.com/"}
    return None


def is_google_news(url: str | None) -> bool:
    return bool(url) and urlsplit(url).netloc.lower().endswith("news.google.com")


def text_source(a: Article) -> str:
    if a.body:
        return "body"
    if a.summary:
        return "summary"
    return "title_only"


class Enricher:
    def __init__(self, http, settings: Settings, google_decoder: Callable[[str], str | None] | None = None):
        self.http = http
        self.settings = settings
        self.google_decoder = google_decoder or _default_google_decoder

    @property
    def active(self) -> bool:
        return self.settings.fetch_attachments or self.settings.fetch_article_body

    def enrich(self, articles: list[Article]) -> None:
        s = self.settings
        for a in articles:
            if s.fetch_attachments and a.source_type != "news" and a.impact_tier in s.attachment_tiers:
                for att in a.attachments:
                    if att.kind == "pdf" and att.text is None and att.error is None:
                        self._pdf_text(att)
            already_tried = bool(a.summary and a.meta.get("body_error"))  # set by reuse_enrichment
            if s.fetch_article_body and a.source_type == "news" and a.url and not a.body and not already_tried:
                self._article(a)

    # ---- media -------------------------------------------------------------
    def _resolve_url(self, a: Article) -> str | None:
        if not is_google_news(a.url):
            return a.url
        limiter = getattr(self.http, "limiter", None)
        if limiter is not None:
            limiter.wait("https://news.google.com/")  # decoder makes its own Google calls
        try:
            decoded = self.google_decoder(a.url)
        except ImportError as exc:
            a.meta["body_error"] = f"googlenewsdecoder import failed: {exc}"[:300]
            return None
        except Exception as exc:
            a.meta["body_error"] = f"google decode failed: {type(exc).__name__}: {exc}"[:300]
            return None
        if not decoded or is_google_news(decoded):
            a.meta["body_error"] = "google decode returned no publisher URL"
            return None
        a.meta["google_news_url"] = a.url
        a.url = decoded
        return decoded

    def _article(self, a: Article) -> None:
        url = self._resolve_url(a)
        if not url:
            return
        try:
            page = self.http.get(url, headers={"Accept": HTML_ACCEPT}).text
        except Exception as exc:
            a.meta["body_error"] = f"fetch failed: {type(exc).__name__}: {exc}"[:300]
            return
        if not a.summary:
            a.summary = meta_description(page)
        try:
            import trafilatura
        except ImportError as exc:
            # Report the real cause: a broken sub-dependency (e.g. lxml_html_clean)
            # also raises ImportError and is not the same as "not installed".
            a.meta["body_error"] = f"trafilatura import failed: {exc} (summary from meta tags only)"[:300]
            return
        try:
            body = trafilatura.extract(page, include_comments=False, include_tables=False)
        except Exception as exc:
            a.meta["body_error"] = f"extract failed: {type(exc).__name__}: {exc}"[:300]
            return
        if body and len(body) > len(a.summary or "") + 40:
            a.body = body[: self.settings.max_text_chars]
            a.meta.pop("body_error", None)
        else:
            a.meta["body_error"] = "no extractable body (paywall or JS-rendered)"

    # ---- filings -----------------------------------------------------------
    def _download(self, url: str) -> bytes:
        resp = self.http.get(url, headers=_referer_for(url), stream=True)
        buf = bytearray()
        for chunk in resp.iter_content(65536):
            buf.extend(chunk)
            if len(buf) > self.settings.max_attachment_bytes:
                raise ValueError(f"exceeds max_attachment_bytes={self.settings.max_attachment_bytes}")
        return bytes(buf)

    def _pdf_text(self, att: Attachment) -> None:
        try:
            from pypdf import PdfReader
        except ImportError:
            att.error = "pypdf not installed"
            return
        try:
            reader = PdfReader(io.BytesIO(self._download(att.url)))
            parts: list[str] = []
            total = 0
            for page in reader.pages[: self.settings.max_pdf_pages]:
                text = page.extract_text() or ""
                parts.append(text)
                total += len(text)
                if total >= self.settings.max_text_chars:
                    break
            text = "\n".join(parts).strip()
            if text:
                att.text = text[: self.settings.max_text_chars]
            else:
                att.error = "no extractable text (likely scanned; needs OCR)"
        except Exception as exc:
            att.error = f"{type(exc).__name__}: {exc}"[:300]
            log.debug("pdf extraction failed for %s: %s", att.url, exc)
