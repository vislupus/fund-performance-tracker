"""Import of manually downloaded CSV files from data/manual/."""
from __future__ import annotations

import csv
import io
import logging
import re
from pathlib import Path

from ..util import dedupe_sorted, parse_date, parse_number
from .base import Series

log = logging.getLogger(__name__)

ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b")


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    counts = {d: sample.count(d) for d in (";", "\t", ",", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def parse_csv_text(text: str) -> Series:
    """Parse any CSV with a date column and a price/NAV column.

    Column detection: header names containing date/dátum/дата and
    price/nav/árfolyam/цена/close; otherwise first column = date, second = value.
    """
    text = text.lstrip("﻿")
    if not text.strip():
        return []
    delim = _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [c.strip().strip('"').lower() for c in rows[0]]
    date_idx = None
    val_idx = None
    for i, h in enumerate(header):
        if date_idx is None and any(k in h for k in ("date", "dátum", "datum", "дата")):
            date_idx = i
    for i, h in enumerate(header):
        if i == date_idx:
            continue
        if val_idx is None and any(k in h for k in ("árfolyam", "arfolyam", "price", "nav", "цена", "close", "стойност")):
            val_idx = i
    data_rows = rows[1:] if (date_idx is not None or val_idx is not None or parse_date(rows[0][0]) is None) else rows
    if date_idx is None:
        date_idx = 0
    if val_idx is None:
        val_idx = 1 if len(header) > 1 else 0
    decimal = "," if delim in (";", "\t") else None
    out: Series = []
    for r in data_rows:
        if len(r) <= max(date_idx, val_idx):
            continue
        d = parse_date(r[date_idx])
        v = parse_number(r[val_idx], decimal=decimal)
        if d and v:
            out.append((d, v))
    return dedupe_sorted(out)


def import_manual_files(manual_dir: Path, db, known_isins: set[str], run_at: str) -> dict[str, int]:
    """Import every CSV in manual_dir whose file name contains a known ISIN."""
    imported: dict[str, int] = {}
    if not manual_dir.exists():
        return imported
    for path in sorted(manual_dir.glob("*.csv")):
        m = ISIN_RE.search(path.name.upper())
        if not m or m.group(1) not in known_isins:
            log.warning("manual file %s skipped: no known ISIN in the file name", path.name)
            continue
        isin = m.group(1)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = path.read_text(encoding="latin-2")
        rows = parse_csv_text(text)
        n = db.insert_navs(isin, rows, "manual")
        imported[isin] = imported.get(isin, 0) + len(rows)
        db.log(run_at, isin, "manual", "ok" if rows else "empty", n,
               rows[0][0] if rows else None, rows[-1][0] if rows else None, path.name)
        log.info("manual: %s <- %s (%d rows, %d new)", isin, path.name, len(rows), n)
    return imported
