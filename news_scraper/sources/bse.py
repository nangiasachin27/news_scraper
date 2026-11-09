"""BSE corporate announcements (the API behind bseindia.com/corporates/ann.html).

Requires the BSE scrip code, resolved via ISIN join in TickerResolver.
"""
from __future__ import annotations

import re

from ..models import Article, Attachment
from ..textutils import strip_html
from ..timeutils import exchange_time
from .base import Source, register

BSE_ANN_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
ATTACH_LIVE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
ATTACH_HIST = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"

# "Reliance Industries Ltd - 500325 - Outcome of Board Meeting" -> "Outcome of Board Meeting"
_SUBJECT_PREFIX_RE = re.compile(r"^.*?\s-\s*\d{6}\s*-\s*")


def clean_bse_subject(subject: str) -> str:
    return _SUBJECT_PREFIX_RE.sub("", subject or "", count=1).strip()


@register
class BSEAnnouncements(Source):
    name = "bse_announcements"
    requires = ("bse_code",)

    def fetch(self, ticker, start, end, notes):
        rows: list[dict] = []
        page = 1
        while page <= self.settings.bse_max_pages:
            data = self.http.bse_get_json(
                BSE_ANN_URL,
                params={
                    "pageno": page,
                    "strCat": -1,
                    "strPrevDate": start.strftime("%Y%m%d"),
                    "strScrip": ticker.bse_code,
                    "strSearch": "P",
                    "strToDate": end.strftime("%Y%m%d"),
                    "strType": "C",
                    "subcategory": -1,
                },
            )
            batch = (data or {}).get("Table") or [] if isinstance(data, dict) else []
            if not batch:
                break
            rows.extend(batch)
            try:
                total_pages = int(batch[0].get("TotalPageCnt") or 1)
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages:
                break
            page += 1
        else:
            notes.append(f"stopped at bse_max_pages={self.settings.bse_max_pages}; results may be truncated")

        out: list[Article] = []
        for r in rows:
            ts, tmeta = exchange_time(r, "DissemDT", "NEWS_DT", "DT_TM")
            headline = strip_html(r.get("HEADLINE"))
            title = clean_bse_subject(strip_html(r.get("NEWSSUB"))) or headline
            if not ts or not title:
                continue
            attachments: list[Attachment] = []
            att_name = (r.get("ATTACHMENTNAME") or "").strip()
            if att_name:
                base = ATTACH_HIST if str(r.get("PDFFLAG")) == "1" else ATTACH_LIVE
                kind = att_name.rsplit(".", 1)[-1].lower() if "." in att_name else None
                attachments.append(Attachment(url=base + att_name, name=att_name, kind=kind))
            tags = ["bse_critical"] if str(r.get("CRITICALNEWS") or "0") not in ("0", "", "None") else []
            out.append(
                Article(
                    source=self.name,
                    source_type="exchange_filing",
                    title=title,
                    summary=headline if headline and headline != title else None,
                    url=attachments[0].url if attachments else None,
                    published_at=ts,
                    publisher="BSE",
                    attachments=attachments,
                    tags=tags,
                    meta={
                        "exchange": "BSE",
                        "exchange_id": r.get("NEWSID"),
                        "bse_category": r.get("CATEGORYNAME"),
                        "bse_subcategory": r.get("SUBCATNAME"),
                        **tmeta,
                    },
                )
            )
        return out
