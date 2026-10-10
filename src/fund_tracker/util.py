"""Small parsing helpers shared by providers."""
from __future__ import annotations

import datetime as dt
import re

_DATE_FORMATS = (
    "%Y-%m-%d", "%Y.%m.%d.", "%Y.%m.%d", "%Y/%m/%d", "%d.%m.%Y", "%d/%m/%Y",
    "%d-%m-%Y", "%b %d, %Y", "%B %d, %Y", "%A, %B %d, %Y", "%d %b %Y", "%d %B %Y",
    "%m/%d/%Y", "%Y%m%d",
)


def parse_date(text: str) -> dt.date | None:
    text = (text or "").strip().strip('"')
    if not text:
        return None
    if re.fullmatch(r"\d{10}", text):
        return dt.datetime.fromtimestamp(int(text), dt.timezone.utc).date()
    if re.fullmatch(r"\d{13}", text):
        return dt.datetime.fromtimestamp(int(text) / 1000, dt.timezone.utc).date()
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    return None


def parse_number(text: str) -> float | None:
    """Parse '1 234,56', '1,234.56', '1234.56', '12,34' etc."""
    if text is None:
        return None
    s = str(text).strip().strip('"').replace(" ", "").replace(" ", "")
    if not s or s in {"-", "–", "n/a", "N/A"}:
        return None
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # single comma: decimal comma unless it looks like a thousands separator (e.g. 1,234)
        head, tail = s.rsplit(",", 1)
        if len(tail) == 3 and head.replace("-", "").isdigit() and len(head.replace("-", "")) <= 3:
            s = head + tail
        else:
            s = head + "." + tail
    try:
        return float(s)
    except ValueError:
        return None


def dedupe_sorted(rows: list[tuple[dt.date, float]]) -> list[tuple[dt.date, float]]:
    seen: dict[dt.date, float] = {}
    for d, v in rows:
        if d is not None and v is not None and v > 0:
            seen[d] = v
    return sorted(seen.items())
