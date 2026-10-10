"""Provider registry and fetch orchestration."""
from __future__ import annotations

import datetime as dt
import logging

from ..config import Fund, Settings
from ..db import Database
from ..http import Http
from .bamosz import BamoszProvider
from .base import ProviderError, Series
from .ft import FTProvider
from .investing import InvestingProvider
from .morningstar import MorningstarProvider

log = logging.getLogger(__name__)

REGISTRY = {
    "ft": FTProvider,
    "investing": InvestingProvider,
    "bamosz": BamoszProvider,
    "morningstar": MorningstarProvider,
}


def fetch_window(db: Database, fund: Fund, settings: Settings, today: dt.date) -> tuple[dt.date, dt.date, bool]:
    first, last, n = db.nav_range(fund.isin)
    if last is None:
        return settings.history_start, today, True
    return last - dt.timedelta(days=settings.refresh_overlap_days), today, False


def update_fund(db: Database, http: Http, fund: Fund, settings: Settings, run_at: str,
                today: dt.date | None = None, full: bool = False) -> dict:
    """Try providers in order until one returns data. Every attempt is logged."""
    today = today or dt.date.today()
    start, end, initial = fetch_window(db, fund, settings, today)
    if full:
        start, initial = settings.history_start, True
    result = {"isin": fund.isin, "status": "failed", "provider": None, "rows": 0, "errors": []}
    for pname in fund.provider_order:
        cls = REGISTRY.get(pname)
        if cls is None:
            result["errors"].append(f"{pname}: unknown provider")
            continue
        provider = cls()
        try:
            rows: Series = provider.fetch(fund, start, end, http, db)
        except ProviderError as exc:
            msg = str(exc)
            db.log(run_at, fund.isin, pname, "failed", 0, None, None, msg)
            result["errors"].append(f"{pname}: {msg}")
            log.warning("%s %s failed: %s", fund.isin, pname, msg)
            continue
        except Exception as exc:  # unexpected – keep going with the next provider
            msg = f"{type(exc).__name__}: {exc}"
            db.log(run_at, fund.isin, pname, "error", 0, None, None, msg)
            result["errors"].append(f"{pname}: {msg}")
            log.exception("%s %s crashed", fund.isin, pname)
            continue
        if not rows:
            db.log(run_at, fund.isin, pname, "empty", 0, None, None, "no rows")
            result["errors"].append(f"{pname}: no rows")
            continue
        source = provider.name
        inserted = db.insert_navs(fund.isin, rows, source)
        db.log(run_at, fund.isin, source, "ok", inserted, rows[0][0], rows[-1][0],
               f"{len(rows)} rows fetched ({'initial' if initial else 'incremental'}), {inserted} new")
        result.update(status="ok", provider=source, rows=len(rows), inserted=inserted,
                      first=rows[0][0].isoformat(), last=rows[-1][0].isoformat())
        log.info("%s: %d rows via %s (%d new)", fund.isin, len(rows), source, inserted)
        # A "latest price only" result is still a success, but keep trying for history
        if source.endswith("-latest"):
            continue
        break
    return result
