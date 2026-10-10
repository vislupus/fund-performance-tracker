"""Loading of data/transactions.csv (real purchases)."""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from .util import parse_date, parse_number


def load_transactions(path: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8-sig") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    for row in reader:
        isin = (row.get("isin") or "").strip().upper()
        d = parse_date(row.get("date") or "")
        amount = parse_number(row.get("amount") or "")
        if not isin or not d or not amount:
            continue
        out.setdefault(isin, []).append({
            "date": d, "amount": amount,
            "units": parse_number(row.get("units") or ""),
            "price": parse_number(row.get("price") or ""),
            "note": (row.get("note") or "").strip(),
        })
    return out
