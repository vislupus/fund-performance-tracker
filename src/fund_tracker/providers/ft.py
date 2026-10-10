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
import time

from bs4 import BeautifulSoup

from ..config import Fund
from ..db import Database
from ..http import Http
from ..util import dedupe_sorted, parse_date, parse_number
from .base import ProviderError, Series

log = logging.getLogger(__name__)

SEARCH_URL = "https://markets.ft.com/data/searchapi/searchsecurities"
HISTORY_URL = "https://markets.ft.com/data/equities/ajax/get-historical-prices"
CHUNK_YEARS = 5


def _pick_security(fund: Fund, securities: list[dict]) -> dict | None:
    """Choose the listing matching the fund's currency (symbol looks like 'LU0106235533:EUR').

    Without an explicit ft_symbol hint, only listings whose symbol or name carries the
    ISIN are acceptable: FT's search returns unrelated instruments for unknown ISINs.
    """
    want_symbol = (fund.ids.get("ft_symbol") or "").upper()
    if want_symbol:
        for s in securities:
            if str(s.get("symbol", "")).upper() == want_symbol:
                return s
        return None
    cur = fund.currency.upper()
    pool = [s for s in securities if fund.isin in str(s.get("symbol", "")).upper()
            or fund.isin in str(s.get("name", "")).upper()]
    for s in pool:
        if str(s.get("symbol", "")).upper().endswith(":" + cur):
            return s
    return pool[0] if pool else None


def _search(http: Http, query: str) -> list[dict]:
    resp = http.get(SEARCH_URL, params={"query": query},
                    headers={"Accept": "application/json", "Referer": "https://markets.ft.com/data/funds"})
    try:
        payload = resp.json()
    except Exception as exc:
        raise ProviderError(f"FT search returned non-JSON ({exc})") from exc
    return (payload.get("data") or {}).get("security") or []


def resolve_xid(fund: Fund, http: Http, db: Database) -> str:
    cached = db.get_provider_id(fund.isin, "ft")
    if cached:
        return cached
    hint = fund.ids.get("ft_symbol")
    securities = _search(http, hint or fund.isin)
    if not securities and not hint:
        # Some funds are not indexed by ISIN; try the name and keep only hits whose symbol carries the ISIN
        securities = [s for s in _search(http, fund.name) if fund.isin in str(s.get("symbol", "")).upper()]
    if not securities:
        raise ProviderError(f"FT search found nothing for {hint or fund.isin}")
    sec = _pick_security(fund, securities)
    if not sec or not sec.get("xid"):
        found = ", ".join(str(x.get("symbol")) for x in securities[:5])
        raise ProviderError(f"FT search found no listing carrying {hint or fund.isin} (hits: {found})")
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
        value = parse_number(cells[close_idx].get_text(strip=True), decimal=".")
        if value is None and len(cells) >= 2:
            value = parse_number(cells[1].get_text(strip=True), decimal=".")
        if value:
            rows.append((d, value))
    return dedupe_sorted(rows)


def _years_back(date: dt.date, years: int) -> dt.date:
    try:
        return date.replace(year=date.year - years)
    except ValueError:  # 29 February
        return date.replace(year=date.year - years, day=28)


class FTProvider:
    name = "ft"

    def _fetch_chunk(self, fund: Fund, xid: str, start: dt.date, end: dt.date, http: Http) -> Series:
        resp = http.get(
            HISTORY_URL,
            params={"startDate": start.strftime("%Y/%m/%d"), "endDate": end.strftime("%Y/%m/%d"), "symbol": xid},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                     "Referer": f"https://markets.ft.com/data/funds/tearsheet/historical?s={fund.isin}:{fund.currency}"},
        )
        try:
            html = resp.json().get("html", "")
        except Exception:
            html = resp.text if "<tr" in resp.text else ""
        return parse_history_html(html)

    def _fetch_chunk_with_retry(self, fund: Fund, xid: str, start: dt.date, end: dt.date, http: Http) -> Series:
        rows = self._fetch_chunk(fund, xid, start, end, http)
        if rows or (end - start).days < 400:
            return rows
        # An empty answer for a multi-year window is usually a hiccup: wait, then ask for two halves
        time.sleep(2)
        mid = start + (end - start) / 2
        return dedupe_sorted(self._fetch_chunk(fund, xid, start, mid, http)
                             + self._fetch_chunk(fund, xid, mid + dt.timedelta(days=1), end, http))

    def fetch(self, fund: Fund, start: dt.date, end: dt.date, http: Http, db: Database) -> Series:
        xid = resolve_xid(fund, http, db)
        out: Series = []
        chunk_end = end
        empty_streak = 0
        while chunk_end >= start:
            chunk_start = max(start, _years_back(chunk_end, CHUNK_YEARS))
            part = self._fetch_chunk_with_retry(fund, xid, chunk_start, chunk_end, http)
            out.extend(part)
            empty_streak = empty_streak + 1 if not part else 0
            if chunk_start == start or (out and empty_streak >= 2):
                break  # two empty windows below the data we already have = before inception
            chunk_end = chunk_start - dt.timedelta(days=1)
        out = dedupe_sorted(out)
        if not out:
            raise ProviderError(f"FT returned no rows for xid {xid}")
        return out
