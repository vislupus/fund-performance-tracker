"""Provider interface."""
from __future__ import annotations

import datetime as dt
from typing import Protocol

from ..config import Fund
from ..db import Database
from ..http import Http

Series = list[tuple[dt.date, float]]


class ProviderError(RuntimeError):
    """Raised when a provider cannot deliver data for a fund (logged, not fatal)."""


class Provider(Protocol):
    name: str

    def fetch(self, fund: Fund, start: dt.date, end: dt.date, http: Http, db: Database) -> Series:
        ...
