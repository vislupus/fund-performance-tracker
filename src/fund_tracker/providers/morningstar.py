"""Legacy Morningstar time-series endpoint (used by Portfolio Performance and others).

Search SecId:  https://www.morningstar.co.uk/uk/util/SecuritySearch.ashx?q=<ISIN>&limit=10
History:       https://lt.morningstar.com/api/rest.svc/timeseries_price/t92wz0sj7c?id=<SecId>]2]0]FOALL$$ALL
                 &currencyId=EUR&idtype=Morningstar&frequency=daily&startDate=YYYY-MM-DD&outputType=COMPACTJSON
This endpoint is undocumented and may stop working; it is only a fallback.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from ..config import Fund
from ..db import Database
from ..http import Http
from ..util import dedupe_sorted
from .base import ProviderError, Series

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.morningstar.co.uk/uk/util/SecuritySearch.ashx"
HISTORY_URLS = (
    "https://lt.morningstar.com/api/rest.svc/timeseries_price/t92wz0sj7c",
    "https://tools.morningstar.co.uk/api/rest.svc/timeseries_price/t92wz0sj7c",
)


def resolve_secid(fund: Fund, http: Http, db: Database) -> str:
    if fund.ids.get("morningstar_secid"):
        return str(fund.ids["morningstar_secid"])
    cached = db.get_provider_id(fund.isin, "morningstar")
    if cached:
        return cached
    resp = http.get(SEARCH_URL, params={"q": fund.isin, "limit": 10, "preferedList": ""})
    m = re.search(r'"i"\s*:\s*"([A-Z0-9]{8,12})"', resp.text)
    if not m:
        raise ProviderError(f"Morningstar search found nothing for {fund.isin}")
    secid = m.group(1)
    db.set_provider_id(fund.isin, "morningstar", secid, resp.text.split("|")[0][:120])
    return secid


class MorningstarProvider:
    name = "morningstar"

    def fetch(self, fund: Fund, start: dt.date, end: dt.date, http: Http, db: Database) -> Series:
        secid = resolve_secid(fund, http, db)
        params = {
            "id": f"{secid}]2]0]FOALL$$ALL",
            "currencyId": fund.currency,
            "idtype": "Morningstar",
            "frequency": "daily",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "outputType": "COMPACTJSON",
        }
        errors = []
        for url in HISTORY_URLS:
            try:
                resp = http.get(url, params=params, headers={"Accept": "application/json"})
                data = resp.json()
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                continue
            rows: Series = []
            if isinstance(data, list):
                for item in data:
                    try:
                        ts, value = item[0], item[1]
                        rows.append((dt.datetime.fromtimestamp(int(ts) / 1000, dt.timezone.utc).date(), float(value)))
                    except Exception:
                        continue
            elif isinstance(data, dict):
                for sec in (data.get("TimeSeries") or {}).get("Security") or []:
                    for h in sec.get("HistoryDetail") or []:
                        try:
                            rows.append((dt.date.fromisoformat(h["EndDate"][:10]), float(h["Value"])))
                        except Exception:
                            continue
            rows = dedupe_sorted(rows)
            if rows:
                return rows
            errors.append(f"{url}: empty")
        raise ProviderError("Morningstar: " + "; ".join(errors))
