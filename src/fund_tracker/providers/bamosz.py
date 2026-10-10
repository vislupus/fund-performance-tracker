"""BAMOSZ (Hungarian fund association) – best effort.

The fund page https://www.bamosz.hu/alapoldal?isin=<ISIN> is a Liferay portal. It
shows the latest price in a table and offers a CSV download of the price history
behind a portlet resource URL whose exact parameters are not documented. This
provider therefore:

1. loads the page and looks for any link/form that mentions a download
   (letölt / csv / p_p_resource_id) and tries it with sensible date parameters,
   parsing whatever CSV comes back;
2. if that fails, falls back to the latest price shown on the page, so the
   monthly run still accumulates one data point per month.

If this provider keeps failing, download the CSV manually from the fund page
and drop it into data/manual/<ISIN>.csv – it is imported automatically.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from ..config import Fund
from ..db import Database
from ..http import Http
from ..util import dedupe_sorted, parse_date, parse_number
from .base import ProviderError, Series
from .manual_csv import parse_csv_text

log = logging.getLogger(__name__)

PAGE_URLS = ("https://www.bamosz.hu/alapoldal?isin={isin}", "https://www.bamosz.hu/en/alapoldal?isin={isin}")
DOWNLOAD_HINTS = ("letolt", "letölt", "csv", "p_p_resource_id", "download", "export")


def _candidate_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    found: list[str] = []
    for tag in soup.find_all(["a", "form"]):
        href = tag.get("href") or tag.get("action") or ""
        text = tag.get_text(" ", strip=True).lower()
        if any(h in href.lower() for h in DOWNLOAD_HINTS) or any(h in text for h in ("letölt", "download", "csv")):
            if href and not href.startswith("javascript"):
                found.append(urljoin(base_url, href))
    # also scan inline scripts for resource URLs
    for m in re.finditer(r"""["'](https?://www\.bamosz\.hu/[^"']*p_p_resource_id[^"']*)["']""", html):
        found.append(m.group(1).replace("&amp;", "&"))
    seen: list[str] = []
    for u in found:
        if u not in seen:
            seen.append(u)
    return seen


def _with_params(url: str, extra: dict) -> str:
    parts = urlparse(url)
    q = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    q.update(extra)
    return urlunparse(parts._replace(query=urlencode(q)))


def _latest_from_page(html: str) -> Series:
    soup = BeautifulSoup(html, "lxml")
    price = None
    date = None
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) < 2:
                continue
            label = cells[0].lower()
            if price is None and ("árfolyam" in label or "price" in label or "nav" in label):
                price = parse_number(cells[1])
            if date is None and ("dátum" in label or "date" in label):
                date = parse_date(cells[1])
    if price and date:
        return [(date, price)]
    return []


class BamoszProvider:
    name = "bamosz"

    def fetch(self, fund: Fund, start: dt.date, end: dt.date, http: Http, db: Database) -> Series:
        html = ""
        page_url = ""
        last_error = ""
        for template in PAGE_URLS:
            page_url = template.format(isin=fund.isin)
            try:
                resp = http.get(page_url, headers={"Accept-Language": "hu,en;q=0.8"})
                html = resp.text
                if fund.isin in html:
                    break
            except Exception as exc:
                last_error = str(exc)
        if not html:
            raise ProviderError(f"BAMOSZ page not reachable ({last_error})")

        for link in _candidate_links(html, page_url):
            url = _with_params(link, {
                "isin": fund.isin,
                "kezdoDatum": start.strftime("%Y.%m.%d"),
                "vegDatum": end.strftime("%Y.%m.%d"),
                "elvalaszto": ";",
            })
            try:
                resp = http.get(url)
            except Exception as exc:
                log.debug("BAMOSZ candidate %s failed: %s", url, exc)
                continue
            text = resp.text
            if "<html" in text[:500].lower():
                continue
            rows = parse_csv_text(text)
            if len(rows) >= 2:
                log.info("BAMOSZ: history for %s via %s (%d rows)", fund.isin, url, len(rows))
                return rows

        latest = _latest_from_page(html)
        if latest:
            self.name = "bamosz-latest"
            log.info("BAMOSZ: only latest price for %s (%s)", fund.isin, latest[0][0])
            return latest
        raise ProviderError("BAMOSZ: no download link found and no latest price parsed")
