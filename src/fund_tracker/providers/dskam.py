"""DSK Asset Management (dskam.bg) – NAV per unit history via the site's REST API.

    GET https://dskam.bg/restapi/dsk/GetMutualFundUnitValues?MutualFundID=<id>&DateFrom=dd.MM.yyyy&DateTo=dd.MM.yyyy
    -> {"Total": n, "Items": [{"Date": "...", "NetAssetValuePerUnit": 0.9041, "NetAssetValue": ..., "ReferenceIndex": ...}, ...]}

The MutualFundID comes from the "Инвестиционен помощник" form on the homepage
(5 = ДСК Баланс, 20 = ДСК Растеж, 15 = ДСК Стандарт, 90 = ДСК Динамика, ...)
and is configured per fund as ``ids.dskam_id`` in config/funds.yaml.
"""
from __future__ import annotations

import datetime as dt
import logging

from ..config import Fund
from ..db import Database
from ..http import Http
from ..util import dedupe_sorted, parse_date, parse_number
from .base import ProviderError, Series

log = logging.getLogger(__name__)

API_URL = "https://dskam.bg/restapi/dsk/GetMutualFundUnitValues"
DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%m/%d/%Y")
CHUNK_YEARS = 5
HEADERS = {"Accept": "application/json, text/javascript, */*", "Referer": "https://dskam.bg/",
           "X-Requested-With": "XMLHttpRequest"}


def parse_items(payload: dict) -> Series:
    rows: Series = []
    for item in (payload or {}).get("Items") or []:
        raw_date = item.get("Date") or item.get("date") or ""
        d = parse_date(str(raw_date)[:10]) if str(raw_date)[:4].isdigit() else parse_date(str(raw_date))
        if d is None and isinstance(raw_date, str) and raw_date.startswith("/Date("):
            try:  # ASP.NET "/Date(1700000000000)/"
                d = dt.datetime.fromtimestamp(int(raw_date[6:19]) / 1000, dt.timezone.utc).date()
            except ValueError:
                d = None
        v = item.get("NetAssetValuePerUnit")
        v = parse_number(str(v)) if v is not None else None
        if d and v:
            rows.append((d, v))
    return dedupe_sorted(rows)


class DskamProvider:
    name = "dskam"

    def fetch(self, fund: Fund, start: dt.date, end: dt.date, http: Http, db: Database) -> Series:
        fund_id = fund.ids.get("dskam_id")
        if not fund_id:
            raise ProviderError("no ids.dskam_id configured for this fund")
        out: Series = []
        errors: list[str] = []
        fmt_used: str | None = None
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, dt.date(chunk_start.year + CHUNK_YEARS, 12, 31))
            got = None
            for fmt in ([fmt_used] if fmt_used else DATE_FORMATS):
                params = {"MutualFundID": str(fund_id), "DateFrom": chunk_start.strftime(fmt), "DateTo": chunk_end.strftime(fmt)}
                try:
                    resp = http.get(API_URL, params=params, headers=HEADERS, allow_statuses=(200, 204))
                except Exception as exc:
                    errors.append(f"{fmt}: {exc}")
                    continue
                if resp.status_code == 204 or not resp.text.strip():
                    got = []
                    continue
                try:
                    payload = resp.json()
                except Exception:
                    errors.append(f"{fmt}: non-JSON answer")
                    continue
                rows = parse_items(payload)
                if rows:
                    got = rows
                    fmt_used = fmt
                    break
                got = got or []
            if got:
                out.extend(got)
            chunk_start = chunk_end + dt.timedelta(days=1)
        out = dedupe_sorted(out)
        if not out:
            raise ProviderError(f"dskam.bg returned no rows for MutualFundID={fund_id}" + (f" ({'; '.join(errors)})" if errors else ""))
        return out
