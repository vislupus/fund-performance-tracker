"""BAMOSZ (Hungarian fund association) – https://www.bamosz.hu

Primary route: the bulk download servlet behind the "Letöltések" page,
    POST https://www.bamosz.hu/bamosz-public-letoltes-portlet/data.download
with selectedAlap=<ISIN>, selectedData=arfolyam, an interval and a separator.
It returns a semicolon separated file (ISO-8859-2) with a metadata preamble
followed by a "Dátum";"Árfolyam" table covering the whole life of the fund.
The server sends a bogus ``Content-Encoding: ISO-8859-2`` header which makes
curl refuse the body, so this call is made with plain ``requests``.
Second route: GET .../bamosz-public-alapoldal-portlet/kuka.download?isin=..
Fallback: the fund page https://www.bamosz.hu/alapoldal?isin=<ISIN> embeds:
  * a hidden input ``chartDataValue`` with ``[[epoch_ms, price], ...]`` for the
    selected chart interval (default: the last year);
  * a ``dataTable2`` table with the same interval row by row
    (Dátum | Árfolyam | Nettó eszközérték | ...).
To get older data the JSF chart form is re-posted with an earlier start date;
when that fails the provider still returns the last year, so the database fills
up month by month. The latest price from the summary table is the last resort.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Fund
from ..db import Database
from ..http import Http
from ..util import dedupe_sorted, parse_date, parse_number
from .base import ProviderError, Series

log = logging.getLogger(__name__)

PAGE_URL = "https://www.bamosz.hu/alapoldal?isin={isin}"
BULK_URL = "https://www.bamosz.hu/bamosz-public-letoltes-portlet/data.download"
FUND_DL_URL = "https://www.bamosz.hu/bamosz-public-alapoldal-portlet/kuka.download"
PLAIN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Encoding": "identity",
    "Accept-Language": "hu,en;q=0.8",
}
DATE_HEADERS = ("dátum", "datum", "date")
PRICE_HEADERS = ("árfolyam", "arfolyam", "unit price", "price")


def parse_download_csv(text: str) -> Series:
    """Parse the BAMOSZ download file: metadata lines, then "Dátum";"Árfolyam";... rows."""
    rows: Series = []
    in_table = False
    date_idx = price_idx = None
    for line in text.splitlines():
        cells = [c.strip().strip('"') for c in re.split(r"[;\t]", line)]
        if not in_table:
            lowered = [c.lower() for c in cells]
            if any(any(k == c or k in c for k in DATE_HEADERS) for c in lowered) and \
               any(any(k in c for k in PRICE_HEADERS) for c in lowered) and len(cells) >= 2:
                date_idx = next(i for i, c in enumerate(lowered) if any(k in c for k in DATE_HEADERS))
                price_idx = next(i for i, c in enumerate(lowered) if any(k in c for k in PRICE_HEADERS))
                in_table = True
            continue
        if len(cells) <= max(date_idx, price_idx):
            continue
        d = parse_date(cells[date_idx])
        v = parse_number(cells[price_idx])
        if d and v:
            rows.append((d, v))
    return dedupe_sorted(rows)


def _plain_request(method: str, url: str, start: dt.date, end: dt.date, isin: str, timeout: int = 60) -> str:
    """Use `requests` (curl rejects the server's bogus Content-Encoding header)."""
    import requests

    if method == "POST":
        data = {"selectedAlap": isin, "selectedData": "arfolyam", "selectedHozams": "",
                "selectedOption": "idointervallumraVonatkozoStatisztika",
                "intervallumraDateStart": start.strftime("%Y.%m.%d"), "intervallumraDateEnd": end.strftime("%Y.%m.%d"),
                "idopontraDate": end.strftime("%Y.%m.%d"), "sortDirection": "inc", "separator": "pontosvesszo"}
        resp = requests.post(url, data=data, headers=dict(PLAIN_HEADERS, Referer="https://www.bamosz.hu/letoltesek"), timeout=timeout)
    else:
        resp = requests.get(url, params={"isin": isin, "separator": "pontosvesszo"},
                            headers=dict(PLAIN_HEADERS, Referer=PAGE_URL.format(isin=isin)), timeout=timeout)
    if resp.status_code != 200:
        raise ProviderError(f"BAMOSZ {method} {url} -> HTTP {resp.status_code}")
    raw = resp.content
    for enc in ("iso-8859-2", "utf-8", "cp1250"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def parse_chart_data(html: str) -> Series:
    """[[1791417600000, 1.1821], ...] from the hidden chartDataValue input."""
    rows: Series = []
    for m in re.finditer(r'name="[^"]*chartDataValue"[^>]*value="(\[\[[^"]*\]\])"', html):
        for ts, val in re.findall(r"\[\s*(\d{12,13})\s*,\s*([\d.]+)\s*\]", m.group(1)):
            try:
                d = dt.datetime.fromtimestamp(int(ts) / 1000, dt.timezone.utc).date()
                rows.append((d, float(val)))
            except (ValueError, OverflowError):
                continue
    if not rows:  # value="..." might come before name="..."
        m = re.search(r'value="(\[\[[^"]*\]\])"[^>]*name="[^"]*chartDataValue"', html)
        if m:
            for ts, val in re.findall(r"\[\s*(\d{12,13})\s*,\s*([\d.]+)\s*\]", m.group(1)):
                rows.append((dt.datetime.fromtimestamp(int(ts) / 1000, dt.timezone.utc).date(), float(val)))
    return dedupe_sorted(rows)


def parse_history_table(html: str) -> Series:
    """The per-day table (Dátum | Árfolyam | ...)."""
    soup = BeautifulSoup(html, "lxml")
    rows: Series = []
    for table in soup.find_all("table"):
        header = [c.get_text(" ", strip=True).lower() for c in table.find("tr").find_all(["th", "td"])] if table.find("tr") else []
        if not header:
            continue
        date_idx = next((i for i, h in enumerate(header) if any(k in h for k in DATE_HEADERS)), None)
        price_idx = next((i for i, h in enumerate(header) if any(k in h for k in PRICE_HEADERS)), None)
        if date_idx is None or price_idx is None:
            continue
        for tr in table.find_all("tr")[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) <= max(date_idx, price_idx):
                continue
            d = parse_date(cells[date_idx])
            v = parse_number(cells[price_idx])
            if d and v:
                rows.append((d, v))
        if rows:
            break
    return dedupe_sorted(rows)


def parse_latest(html: str) -> Series:
    soup = BeautifulSoup(html, "lxml")
    price = date = None
    for table in soup.find_all("table", class_="dataTable"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) < 2:
                continue
            label = cells[0].lower()
            if price is None and any(k in label for k in PRICE_HEADERS):
                price = parse_number(cells[1])
            if date is None and any(k in label for k in DATE_HEADERS):
                date = parse_date(cells[1])
    return [(date, price)] if price and date else []


def chart_form_payload(html: str, start: dt.date, end: dt.date) -> tuple[str, dict] | None:
    """Rebuild the JSF chart form with a wider interval. Returns (action_url, data)."""
    soup = BeautifulSoup(html, "lxml")
    form = None
    for f in soup.find_all("form"):
        if f.find("input", attrs={"name": re.compile(r"startDate_input$")}):
            form = f
            break
    if form is None:
        return None
    data: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "text").lower()
        if itype in ("submit", "button", "image"):
            continue
        if itype in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        data[name] = inp.get("value", "")
    for sel in form.find_all("select"):
        name = sel.get("name")
        opt = sel.find("option", selected=True) or sel.find("option")
        if name and opt is not None:
            data[name] = opt.get("value", "")
    for key in list(data):
        if key.endswith("startDate_input"):
            data[key] = start.strftime("%Y.%m.%d")
        elif key.endswith("endDate_input"):
            data[key] = end.strftime("%Y.%m.%d")
    # the "Frissít"/"Refresh" submit button
    button = None
    for b in form.find_all(["input", "button"]):
        if (b.get("type") or "").lower() == "submit" or b.name == "button":
            label = (b.get("value") or b.get_text(" ", strip=True) or "").lower()
            if "friss" in label or "refresh" in label:
                button = b
                break
    if button is not None and button.get("name"):
        data[button["name"]] = button.get("value") or button.get_text(strip=True) or ""
    action = form.get("action") or ""
    return urljoin(PAGE_URL.format(isin=""), action.replace("&amp;", "&")), data


class BamoszProvider:
    name = "bamosz"

    def fetch(self, fund: Fund, start: dt.date, end: dt.date, http: Http, db: Database) -> Series:
        errors: list[str] = []
        # 1. bulk download servlet (whole history)
        for method, url in (("POST", BULK_URL), ("GET", FUND_DL_URL)):
            try:
                text = _plain_request(method, url, start, end, fund.isin, timeout=http.timeout + 20)
            except Exception as exc:
                errors.append(f"{method} {url.rsplit('/', 1)[-1]}: {exc}")
                continue
            if fund.isin not in text and "rfolyam" not in text:
                errors.append(f"{method} {url.rsplit('/', 1)[-1]}: unexpected content")
                continue
            rows = parse_download_csv(text)
            if rows:
                log.info("BAMOSZ: %s %s gave %d rows (%s – %s)", fund.isin, url.rsplit('/', 1)[-1], len(rows), rows[0][0], rows[-1][0])
                return [(d, v) for d, v in rows if start <= d <= end] or rows
            errors.append(f"{method} {url.rsplit('/', 1)[-1]}: no rows parsed")

        # 2. the fund page itself (last year embedded)
        page_url = PAGE_URL.format(isin=fund.isin)
        resp = http.get(page_url, headers={"Accept-Language": "hu,en;q=0.8"})
        html = resp.text
        if fund.isin not in html:
            raise ProviderError("BAMOSZ page does not mention the ISIN (unknown fund?); " + "; ".join(errors))
        rows = dedupe_sorted(parse_chart_data(html) + parse_history_table(html))
        if rows:
            log.info("BAMOSZ: %s page carries %d rows (%s – %s); download failed: %s",
                     fund.isin, len(rows), rows[0][0], rows[-1][0], "; ".join(errors))
            return rows
        latest = parse_latest(html)
        if latest:
            self.name = "bamosz-latest"
            return latest
        raise ProviderError("BAMOSZ: no prices found; " + "; ".join(errors))
