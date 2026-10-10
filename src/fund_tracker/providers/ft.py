"""Financial Times markets data (markets.ft.com).

1. Resolve the FT "xid" of the share class by searching the ISIN:
   https://markets.ft.com/data/searchapi/searchsecurities?query=<ISIN>
2. Download history (HTML table rows wrapped in JSON):
   https://markets.ft.com/data/equities/ajax/get-historical-prices?startDate=YYYY/MM/DD&endDate=YYYY/MM/DD&symbol=<xid>
Max ~12 years per request, so long ranges are chunked.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from bs4 import BeautifulSoup

from ..config import Fund
from ..db import Database
from ..http import Http
from ..util import dedupe_sorted, parse_date, parse_number
from .base import ProviderError, Series

log = logging.getLogger(__name__)

SEARCH_URL = "https://markets.ft.com/data/searchapi/searchsecurities"
HISTORY_URL = "https://markets.ft.com/data/equities/ajax/get-historical-prices"
CHUNK_YEARS = 10


def _pick_security(fund: Fund, securities: list[dict]) -> dict | None:
    """Choose the listing matching the fund's currency (symbol looks like 'LU0106235533:EUR')."""
    want_symbol = (fund.ids.get("ft_symbol") or "").upper()
    if want_symbol:
        for s in securities:
            if str(s.get("symbol", "")).upper() == want_symbol:
                return s
    cur = fund.currency.upper()
    isin_matches = [s for s in securities if fund.isin in str(s.get("symbol", "")).upper()
                    or fund.isin in str(s.get("name", "")).upper()]
    pool = isin_matches or securities
    for s in pool:
        if str(s.get("symbol", "")).upper().endswith(":" + cur):
            return s
    return pool[0] if pool else None


def resolve_xid(fund: Fund, http: Http, db: Database) -> str:
    cached = db.get_provider_id(fund.isin, "ft")
    if cached:
        return cached
    query = fund.ids.get("ft_symbol") or fund.isin
    resp = http.get(SEARCH_URL, params={"query": query},
                    headers={"Accept": "application/json", "Referer": "https://markets.ft.com/data/funds"})
    try:
        payload = resp.json()
    except Exception as exc:
        raise ProviderError(f"FT search returned non-JSON ({exc})") from exc
    securities = (payload.get("data") or {}).get("security") or []
    if not securities:
        raise ProviderError(f"FT search found nothing for {query}")
    sec = _pick_security(fund, securities)
    if not sec or not sec.get("xid"):
        raise ProviderError(f"FT search found no usable listing for {query}")
    xid = str(sec["xid"])
    db.set_provider_id(fund.isin, "ft", xid, f"{sec.get('symbol')} {sec.get('name')}")
    log.info("FT: %s -> xid %s (%s)", fund.isin, xid, sec.get("symbol"))
    return xid


def parse_history_html(html: str) -> Series:
    soup = BeautifulSoup(html, "lxml")
    rows: Series = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        date_cell = cells[0]
        spans = date_cell.find_all("span")
        date_text = spans[0].get_text(strip=True) if spans else date_cell.get_text(strip=True)
        d = parse_date(date_text)
        if d is None:
            continue
        # Funds: Date | Open | High | Low | Close | Volume ; sometimes Date | Price
        close_idx = 4 if len(cells) >= 5 else 1
        value = parse_number(cells[close_idx].get_text(strip=True))
        if value is None and len(cells) >= 2:
            value = parse_number(cells[1].get_text(strip=True))
        if value:
            rows.append((d, value))
    return dedupe_sorted(rows)


class FTProvider:
    name = "ft"

    def fetch(self, fund: Fund, start: dt.date, end: dt.date, http: Http, db: Database) -> Series:
        xid = resolve_xid(fund, http, db)
        out: Series = []
        chunk_end = end
        while chunk_end >= start:
            chunk_start = max(start, chunk_end.replace(year=chunk_end.year - CHUNK_YEARS))
            resp = http.get(
                HISTORY_URL,
                params={
                    "startDate": chunk_start.strftime("%Y/%m/%d"),
                    "endDate": chunk_end.strftime("%Y/%m/%d"),
                    "symbol": xid,
                },
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                         "Referer": f"https://markets.ft.com/data/funds/tearsheet/historical?s={fund.isin}:{fund.currency}"},
            )
            try:
                html = resp.json().get("html", "")
            except Exception:
                html = resp.text if "<tr" in resp.text else ""
            part = parse_history_html(html)
            out.extend(part)
            if chunk_start == start:
                break
            chunk_end = chunk_start - dt.timedelta(days=1)
        out = dedupe_sorted(out)
        if not out:
            raise ProviderError(f"FT returned no rows for xid {xid}")
        return out
