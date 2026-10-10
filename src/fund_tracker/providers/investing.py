"""Investing.com data via the mobile app API (aappapi.investing.com) with a
TradingView-style fallback (tvc6.investing.com).

Search:   https://aappapi.investing.com/search_by_type.php?section=quotes&string=<ISIN>&lang_ID=1&include_pair_attr=true
History:  https://aappapi.investing.com/get_screen.php?screen_ID=63&pair_ID=<id>&lang_ID=1&date_from=DDMMYYYY&date_to=DDMMYYYY
Fallback: https://tvc6.investing.com/<random>/0/0/0/0/history?symbol=<id>&from=<ts>&to=<ts>&resolution=D
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from ..config import Fund
from ..db import Database
from ..http import Http
from ..util import dedupe_sorted, parse_number
from .base import ProviderError, Series

log = logging.getLogger(__name__)

SEARCH_URL = "https://aappapi.investing.com/search_by_type.php"
SCREEN_URL = "https://aappapi.investing.com/get_screen.php"
APP_HEADERS = {
    "x-meta-ver": "14",
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json",
}
TVC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://tvc-invdn-com.investing.com/",
    "Content-Type": "application/json",
}
CHUNK_DAYS = 365


FUND_TYPES = ("fund", "etf", "фонд", "alap")
NAME_STOPWORDS = {"FUND", "FUNDS", "EUR", "USD", "ACC", "DIST", "THE", "AND", "CLASS", "(C)", "A2", "AT"}


def _name_tokens(name: str) -> list[str]:
    cleaned = name.replace("–", " ").replace("-", " ").replace("(", " ").replace(")", " ").upper()
    return [t for t in cleaned.split() if len(t) > 2 and t not in NAME_STOPWORDS]


def _score_quote(fund: Fund, q: dict, explicit_query: bool) -> int:
    """Score a search hit. Returns < 0 when the hit must be rejected.

    A hit is acceptable only if it carries the ISIN, or (when the user supplied an
    explicit search query) it is a fund/ETF whose name matches the fund name.
    Without this, the ISIN search happily returns e.g. the OTP Bank share for an
    OTP fund, because the name contains "OTP".
    """
    text = " ".join(str(q.get(k, "")) for k in ("search_main_text", "search_main_longtext", "search_main_subtext")).upper()
    subtext = str(q.get("search_main_subtext", "")).lower()
    pair_type = str(q.get("pair_type", "")).lower()
    is_fund = any(t in subtext for t in FUND_TYPES) or pair_type in {"fund", "funds", "etf"}
    tokens = _name_tokens(fund.name)
    name_hits = sum(1 for t in tokens if t in text)
    score = 0
    if fund.isin in text:
        score += 100
    elif not explicit_query:
        return -1
    elif not is_fund or name_hits < max(2, len(tokens) // 2):
        return -1
    if fund.currency.upper() in text:
        score += 3
    if is_fund:
        score += 2
    return score + name_hits


def resolve_pair_id(fund: Fund, http: Http, db: Database) -> str:
    if fund.ids.get("investing_pair_id"):
        return str(fund.ids["investing_pair_id"])
    cached = db.get_provider_id(fund.isin, "investing")
    if cached:
        return cached
    queries = [(fund.isin, False)]
    if fund.ids.get("investing_query"):
        queries.append((str(fund.ids["investing_query"]), True))
    best: tuple[int, dict] | None = None
    rejected = 0
    for query, explicit in queries:
        resp = http.get(SEARCH_URL, params={"section": "quotes", "string": query, "lang_ID": 1,
                                            "include_pair_attr": "true"}, headers=APP_HEADERS)
        try:
            quotes = (resp.json().get("data") or {}).get("quotes") or []
        except Exception as exc:
            raise ProviderError(f"Investing search returned non-JSON ({exc})") from exc
        for q in quotes:
            if not q.get("pair_ID"):
                continue
            s = _score_quote(fund, q, explicit)
            if s < 0:
                rejected += 1
                continue
            if best is None or s > best[0]:
                best = (s, q)
        if best and best[0] >= 100:
            break
    if best is None:
        raise ProviderError(f"Investing.com search found no listing carrying ISIN {fund.isin}"
                            f" ({rejected} unrelated hits rejected; set ids.investing_pair_id or ids.investing_query)")
    q = best[1]
    pair_id = str(q["pair_ID"])
    label = f"{q.get('search_main_text')} | {q.get('search_main_longtext')} | {q.get('search_main_subtext')}"
    db.set_provider_id(fund.isin, "investing", pair_id, label)
    log.info("Investing: %s -> pair_ID %s (%s, score %d)", fund.isin, pair_id, label, best[0])
    return pair_id


def _fetch_screen(http: Http, pair_id: str, start: dt.date, end: dt.date) -> Series:
    out: Series = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + dt.timedelta(days=CHUNK_DAYS))
        resp = http.get(SCREEN_URL, params={"screen_ID": 63, "pair_ID": pair_id, "lang_ID": 1,
                                            "date_from": chunk_start.strftime("%d%m%Y"),
                                            "date_to": chunk_end.strftime("%d%m%Y")}, headers=APP_HEADERS)
        try:
            payload = resp.json()
            items = payload["data"][0]["screen_data"]["data"]
        except Exception:
            items = []
        for item in items:
            try:
                d = dt.datetime.fromtimestamp(int(item["date"]), dt.timezone.utc).date()
            except Exception:
                continue
            v = parse_number(str(item.get("price", "")))
            if v:
                out.append((d, v))
        chunk_start = chunk_end + dt.timedelta(days=1)
    return dedupe_sorted(out)


def _fetch_tvc(http: Http, pair_id: str, start: dt.date, end: dt.date) -> Series:
    out: Series = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + dt.timedelta(days=6000))
        url = f"https://tvc6.investing.com/{uuid.uuid4().hex}/0/0/0/0/history"
        params = {
            "symbol": pair_id,
            "from": int(dt.datetime.combine(chunk_start, dt.time.min, dt.timezone.utc).timestamp()),
            "to": int(dt.datetime.combine(chunk_end + dt.timedelta(days=1), dt.time.min, dt.timezone.utc).timestamp()),
            "resolution": "D",
        }
        resp = http.get(url, params=params, headers=TVC_HEADERS)
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if payload.get("s") == "ok":
            for t, c in zip(payload.get("t", []), payload.get("c", [])):
                if c:
                    out.append((dt.datetime.fromtimestamp(int(t), dt.timezone.utc).date(), float(c)))
        chunk_start = chunk_end + dt.timedelta(days=1)
    return dedupe_sorted(out)


class InvestingProvider:
    name = "investing"

    def fetch(self, fund: Fund, start: dt.date, end: dt.date, http: Http, db: Database) -> Series:
        pair_id = resolve_pair_id(fund, http, db)
        errors = []
        try:
            rows = _fetch_screen(http, pair_id, start, end)
            if rows:
                return rows
            errors.append("app API returned no rows")
        except Exception as exc:
            errors.append(f"app API: {exc}")
        try:
            rows = _fetch_tvc(http, pair_id, start, end)
            if rows:
                return rows
            errors.append("tvc API returned no rows")
        except Exception as exc:
            errors.append(f"tvc API: {exc}")
        raise ProviderError(f"Investing.com pair {pair_id}: " + "; ".join(errors))
