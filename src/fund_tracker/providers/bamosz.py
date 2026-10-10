"""BAMOSZ (Hungarian fund association) – https://www.bamosz.hu/alapoldal?isin=<ISIN>

The fund page (a Liferay/JSF portal) embeds:
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
DATE_HEADERS = ("dátum", "datum", "date")
PRICE_HEADERS = ("árfolyam", "arfolyam", "unit price", "price")


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
        page_url = PAGE_URL.format(isin=fund.isin)
        resp = http.get(page_url, headers={"Accept-Language": "hu,en;q=0.8"})
        html = resp.text
        if fund.isin not in html:
            raise ProviderError("BAMOSZ page does not mention the ISIN (unknown fund?)")

        rows = dedupe_sorted(parse_chart_data(html) + parse_history_table(html))
        if rows:
            log.info("BAMOSZ: %s page carries %d rows (%s – %s)", fund.isin, len(rows), rows[0][0], rows[-1][0])

        # ask for the full interval through the chart form (best effort)
        if start < (rows[0][0] if rows else end):
            payload = chart_form_payload(html, start, end)
            if payload:
                action, data = payload
                try:
                    r2 = http._session.post(action, data=data, timeout=http.timeout,
                                            headers={"Referer": page_url, "Accept-Language": "hu,en;q=0.8"})
                    if r2.status_code == 200:
                        more = dedupe_sorted(parse_chart_data(r2.text) + parse_history_table(r2.text))
                        if more and (not rows or more[0][0] < rows[0][0]):
                            log.info("BAMOSZ: %s chart form gave %d rows from %s", fund.isin, len(more), more[0][0])
                            rows = dedupe_sorted(rows + more)
                except Exception as exc:
                    log.debug("BAMOSZ chart form post failed: %s", exc)

        if rows:
            return rows
        latest = parse_latest(html)
        if latest:
            self.name = "bamosz-latest"
            return latest
        raise ProviderError("BAMOSZ: page parsed but no prices found")
