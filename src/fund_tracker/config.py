"""Loading of config/funds.yaml and config/settings.yaml."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
DB_PATH = DATA_DIR / "funds.sqlite"

DEFAULT_PROVIDERS = {
    "HU": ["ft", "investing", "bamosz", "morningstar"],
    "BG": ["dskam", "ft", "investing"],
    "*": ["ft", "investing", "morningstar"],
}


@dataclass
class Fund:
    isin: str
    name: str
    currency: str = "EUR"
    group: str = "portfolio"
    inception: dt.date | None = None
    manager: str = ""
    entry_fee_pct: float = 0.0
    providers: list[str] = field(default_factory=list)
    ids: dict[str, Any] = field(default_factory=dict)

    @property
    def provider_order(self) -> list[str]:
        if self.providers:
            return list(self.providers)
        return list(DEFAULT_PROVIDERS.get(self.isin[:2], DEFAULT_PROVIDERS["*"]))

    @property
    def short_name(self) -> str:
        return self.name


@dataclass
class Settings:
    monthly_amount: float = 10.0
    purchase_day: int = 15
    my_plan_start: dt.date = dt.date(2025, 10, 15)
    apply_entry_fee: bool = True
    title: str = "Fund Performance Tracker"
    language: str = "bg"
    default_compare: int = 6
    bgn_per_eur: float = 1.95583
    refresh_overlap_days: int = 45
    history_start: dt.date = dt.date(1995, 1, 1)
    raw: dict[str, Any] = field(default_factory=dict)


def _to_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def load_funds(path: Path | None = None) -> list[Fund]:
    path = path or CONFIG_DIR / "funds.yaml"
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    funds: list[Fund] = []
    seen: set[str] = set()
    for item in raw.get("funds", []):
        isin = str(item["isin"]).strip().upper()
        if isin in seen:
            raise ValueError(f"Duplicate ISIN in funds.yaml: {isin}")
        seen.add(isin)
        funds.append(
            Fund(
                isin=isin,
                name=str(item.get("name") or isin).strip(),
                currency=str(item.get("currency") or "EUR").upper(),
                group=str(item.get("group") or "portfolio"),
                inception=_to_date(item.get("inception")),
                manager=str(item.get("manager") or ""),
                entry_fee_pct=float(item.get("entry_fee_pct") or 0.0),
                providers=[str(p) for p in (item.get("providers") or [])],
                ids={str(k): v for k, v in (item.get("ids") or {}).items()},
            )
        )
    return funds


def load_settings(path: Path | None = None) -> Settings:
    path = path or CONFIG_DIR / "settings.yaml"
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    dca = raw.get("dca", {}) or {}
    report = raw.get("report", {}) or {}
    data = raw.get("data", {}) or {}
    s = Settings(raw=raw)
    s.monthly_amount = float(dca.get("monthly_amount", s.monthly_amount))
    s.purchase_day = int(dca.get("purchase_day", s.purchase_day))
    s.my_plan_start = _to_date(dca.get("my_plan_start")) or s.my_plan_start
    s.apply_entry_fee = bool(dca.get("apply_entry_fee", s.apply_entry_fee))
    s.title = str(report.get("title", s.title))
    s.language = str(report.get("language", s.language))
    s.default_compare = int(report.get("default_compare", s.default_compare))
    s.bgn_per_eur = float(data.get("bgn_per_eur", s.bgn_per_eur))
    s.refresh_overlap_days = int(data.get("refresh_overlap_days", s.refresh_overlap_days))
    s.history_start = _to_date(data.get("history_start")) or s.history_start
    return s
