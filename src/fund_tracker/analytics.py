"""Return, risk and dollar-cost-averaging (DCA) calculations on NAV series.

All functions are pure and work on a list of (date, nav) tuples sorted by date.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
from dataclasses import dataclass, field

Series = list[tuple[dt.date, float]]


# ---------------------------------------------------------------- lookups
def _dates(series: Series) -> list[dt.date]:
    return [d for d, _ in series]


def nav_on_or_before(series: Series, date: dt.date) -> tuple[dt.date, float] | None:
    dates = _dates(series)
    i = bisect.bisect_right(dates, date)
    return series[i - 1] if i > 0 else None


def nav_on_or_after(series: Series, date: dt.date) -> tuple[dt.date, float] | None:
    dates = _dates(series)
    i = bisect.bisect_left(dates, date)
    return series[i] if i < len(series) else None


def month_ends(series: Series) -> Series:
    """Last observation of each calendar month."""
    out: Series = []
    for d, v in series:
        if out and (out[-1][0].year, out[-1][0].month) == (d.year, d.month):
            out[-1] = (d, v)
        else:
            out.append((d, v))
    return out


def year_ends(series: Series) -> Series:
    out: Series = []
    for d, v in series:
        if out and out[-1][0].year == d.year:
            out[-1] = (d, v)
        else:
            out.append((d, v))
    return out


# ---------------------------------------------------------------- returns
def pct(a: float, b: float) -> float:
    return (b / a - 1.0) * 100.0


def monthly_returns(series: Series) -> list[dict]:
    """Return per calendar month: last NAV of month vs last NAV of previous month.
    The first month in the data uses the first available NAV as the base (partial)."""
    me = month_ends(series)
    out: list[dict] = []
    if not me:
        return out
    prev_val = series[0][1]
    prev_date = series[0][0]
    for d, v in me:
        partial = prev_date.month == d.month and prev_date.year == d.year
        out.append({"year": d.year, "month": d.month, "ret": pct(prev_val, v), "partial": partial,
                    "date": d.isoformat()})
        prev_val, prev_date = v, d
    return out


def yearly_returns(series: Series) -> list[dict]:
    ye = year_ends(series)
    out: list[dict] = []
    if not ye:
        return out
    prev_val = series[0][1]
    prev_date = series[0][0]
    today = series[-1][0]
    for d, v in ye:
        partial = prev_date.year == d.year or (d.year == today.year and not (d.month == 12 and d.day >= 28))
        out.append({"year": d.year, "ret": pct(prev_val, v), "partial": partial,
                    "from": prev_date.isoformat(), "to": d.isoformat()})
        prev_val, prev_date = v, d
    return out


def _shift_years(date: dt.date, years: int) -> dt.date:
    try:
        return date.replace(year=date.year - years)
    except ValueError:  # 29 Feb
        return date.replace(year=date.year - years, day=28)


def _shift_months(date: dt.date, months: int) -> dt.date:
    y, m = divmod(date.month - 1 - months, 12)
    y = date.year + y
    m += 1
    day = min(date.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return dt.date(y, m, day)


def period_returns(series: Series) -> dict[str, dict]:
    """Cumulative and annualised returns for standard windows ending at the last NAV."""
    out: dict[str, dict] = {}
    if len(series) < 2:
        return out
    last_date, last_nav = series[-1]
    first_date, first_nav = series[0]
    windows = {
        "1m": _shift_months(last_date, 1), "3m": _shift_months(last_date, 3), "6m": _shift_months(last_date, 6),
        "ytd": dt.date(last_date.year - 1, 12, 31),
        "1y": _shift_years(last_date, 1), "3y": _shift_years(last_date, 3), "5y": _shift_years(last_date, 5),
        "10y": _shift_years(last_date, 10),
    }
    for key, start in windows.items():
        if start < first_date:
            out[key] = {"cum": None, "ann": None, "available": False}
            continue
        base = nav_on_or_before(series, start)
        if base is None:
            out[key] = {"cum": None, "ann": None, "available": False}
            continue
        cum = pct(base[1], last_nav)
        years = (last_date - base[0]).days / 365.25
        ann = ((last_nav / base[1]) ** (1 / years) - 1) * 100 if years >= 1 else None
        out[key] = {"cum": cum, "ann": ann, "available": True, "from": base[0].isoformat()}
    years = (last_date - first_date).days / 365.25
    out["inception"] = {
        "cum": pct(first_nav, last_nav),
        "ann": ((last_nav / first_nav) ** (1 / years) - 1) * 100 if years >= 1 else None,
        "available": True, "from": first_date.isoformat(),
    }
    return out


# ---------------------------------------------------------------- risk
def max_drawdown(series: Series) -> dict:
    """Largest peak-to-trough fall (in %), with peak/trough dates and the recovery date if any."""
    peak_val = -math.inf
    peak_date = None
    worst = 0.0
    worst_peak = worst_trough = None
    worst_peak_val = None
    for d, v in series:
        if v > peak_val:
            peak_val, peak_date = v, d
        dd = (v / peak_val - 1) * 100
        if dd < worst:
            worst, worst_peak, worst_trough, worst_peak_val = dd, peak_date, d, peak_val
    recovered = None
    if worst_trough is not None:
        for d, v in series:
            if d > worst_trough and v >= worst_peak_val:
                recovered = d
                break
    return {"dd": worst, "peak": worst_peak.isoformat() if worst_peak else None,
            "trough": worst_trough.isoformat() if worst_trough else None,
            "recovered": recovered.isoformat() if recovered else None}


def volatility_from_monthly(monthly: list[dict], months: int | None = None, min_months: int = 6) -> float | None:
    """Annualised standard deviation of monthly returns; `months` limits it to the last N months."""
    rets = [m["ret"] / 100 for m in monthly if not m.get("partial")]
    if months:
        if len(rets) < min(months, max(min_months, 12)):
            return None
        rets = rets[-months:]
    if len(rets) < min_months:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(12) * 100


def best_worst_month(monthly: list[dict]) -> dict:
    full = [m for m in monthly if not m.get("partial")]
    if not full:
        return {"best": None, "worst": None, "positive_share": None}
    best = max(full, key=lambda m: m["ret"])
    worst = min(full, key=lambda m: m["ret"])
    pos = sum(1 for m in full if m["ret"] > 0) / len(full) * 100
    return {"best": {"ret": best["ret"], "date": best["date"]},
            "worst": {"ret": worst["ret"], "date": worst["date"]}, "positive_share": pos}


# ---------------------------------------------------------------- XIRR
def xnpv(rate: float, cashflows: list[tuple[dt.date, float]]) -> float:
    t0 = cashflows[0][0]
    return sum(cf / (1 + rate) ** ((d - t0).days / 365.0) for d, cf in cashflows)


def xirr(cashflows: list[tuple[dt.date, float]]) -> float | None:
    """Annualised money-weighted return (in %). Negative = outflows (purchases)."""
    cashflows = sorted(cashflows)
    if len(cashflows) < 2 or not any(cf < 0 for _, cf in cashflows) or not any(cf > 0 for _, cf in cashflows):
        return None
    if (cashflows[-1][0] - cashflows[0][0]).days < 1:
        return None
    lo, hi = -0.9999, 10.0
    f_lo, f_hi = xnpv(lo, cashflows), xnpv(hi, cashflows)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = xnpv(mid, cashflows)
        if abs(f_mid) < 1e-9:
            break
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return ((lo + hi) / 2) * 100


# ---------------------------------------------------------------- DCA
@dataclass
class DCAResult:
    purchases: list[dict] = field(default_factory=list)
    invested: float = 0.0
    units: float = 0.0
    value: float = 0.0
    as_of: dt.date | None = None
    last_nav: float | None = None
    pnl: float = 0.0
    pnl_pct: float | None = None
    xirr: float | None = None
    avg_price: float | None = None
    value_series: list[tuple[dt.date, float, float]] = field(default_factory=list)  # date, value, invested
    yearly: list[dict] = field(default_factory=list)
    skipped_months: int = 0

    def to_dict(self) -> dict:
        return {
            "purchases": self.purchases, "invested": self.invested, "units": self.units, "value": self.value,
            "as_of": self.as_of.isoformat() if self.as_of else None, "last_nav": self.last_nav,
            "pnl": self.pnl, "pnl_pct": self.pnl_pct, "xirr": self.xirr, "avg_price": self.avg_price,
            "value_series": [(d.isoformat(), round(v, 4), round(i, 2)) for d, v, i in self.value_series],
            "yearly": self.yearly, "skipped_months": self.skipped_months,
            "n_purchases": len(self.purchases),
            "first_purchase": self.purchases[0]["date"] if self.purchases else None,
        }


def purchase_dates(start: dt.date, end: dt.date, day: int) -> list[dt.date]:
    """The intended purchase date (e.g. the 15th) of every month from start to end."""
    out: list[dt.date] = []
    y, m = start.year, start.month
    while True:
        last_day = [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
        d = dt.date(y, m, min(day, last_day))
        if d > end:
            break
        if d >= start:
            out.append(d)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def dca_simulate(series: Series, amount: float, day: int, start: dt.date | None = None,
                 end: dt.date | None = None, entry_fee_pct: float = 0.0,
                 value_dates: list[dt.date] | None = None) -> DCAResult:
    """Buy `amount` every month on `day` (or the next day with a NAV).

    The first purchase is on the first intended date >= start (default: first NAV date).
    """
    res = DCAResult()
    if not series:
        return res
    first_nav_date = series[0][0]
    last_nav_date = series[-1][0]
    start = max(start or first_nav_date, first_nav_date)
    end = min(end or last_nav_date, last_nav_date)
    if start > end:
        return res
    fee = max(entry_fee_pct, 0.0) / 100.0
    units = 0.0
    invested = 0.0
    cashflows: list[tuple[dt.date, float]] = []
    for intended in purchase_dates(start, end, day):
        hit = nav_on_or_after(series, intended)
        # the purchase must happen inside the same month, otherwise the month is skipped
        if hit is None or (hit[0].year, hit[0].month) != (intended.year, intended.month):
            res.skipped_months += 1
            continue
        d, nav = hit
        net = amount * (1 - fee)
        u = net / nav
        units += u
        invested += amount
        cashflows.append((d, -amount))
        res.purchases.append({"date": d.isoformat(), "intended": intended.isoformat(), "nav": nav,
                              "amount": amount, "units": u, "units_total": units,
                              "invested_total": invested, "value": units * nav})
    if not res.purchases:
        return res
    first_purchase = dt.date.fromisoformat(res.purchases[0]["date"])
    # value series on every NAV date from the first purchase (or on requested dates)
    purchases_by_date: dict[dt.date, tuple[float, float]] = {}
    for p in res.purchases:
        purchases_by_date[dt.date.fromisoformat(p["date"])] = (p["units_total"], p["invested_total"])
    run_units = run_inv = 0.0
    pi = 0
    purchase_list = sorted(purchases_by_date.items())
    for d, nav in series:
        if d < first_purchase:
            continue
        while pi < len(purchase_list) and purchase_list[pi][0] <= d:
            run_units, run_inv = purchase_list[pi][1]
            pi += 1
        res.value_series.append((d, run_units * nav, run_inv))
    if value_dates:
        keep = set(value_dates)
        filtered = [row for row in res.value_series if row[0] in keep]
        # always keep purchase dates and the last point
        extra = [row for row in res.value_series if row[0] in purchases_by_date or row is res.value_series[-1]]
        merged = {row[0]: row for row in filtered + extra}
        res.value_series = sorted(merged.values())
    res.units = units
    res.invested = invested
    res.as_of, res.last_nav = series[-1]
    res.value = units * res.last_nav
    res.pnl = res.value - invested
    res.pnl_pct = res.pnl / invested * 100 if invested else None
    res.avg_price = invested / units if units else None
    res.xirr = xirr(cashflows + [(res.as_of, res.value)]) if res.as_of > first_purchase else None
    # per-year summary
    by_year: dict[int, dict] = {}
    for p in res.purchases:
        y = int(p["date"][:4])
        by_year.setdefault(y, {"year": y, "invested_year": 0.0})
        by_year[y]["invested_year"] += p["amount"]
    prev_value = 0.0
    full_series = [(d, u, i) for d, u, i in _value_on_all_dates(series, purchase_list, first_purchase)]
    ye = {}
    for d, v, i in full_series:
        ye[d.year] = (d, v, i)
    for y in sorted(by_year):
        d, v, i = ye.get(y, (None, 0.0, 0.0))
        row = by_year[y]
        row.update({"as_of": d.isoformat() if d else None, "invested_total": i, "value": v,
                    "pnl_total": v - i, "pnl_total_pct": (v / i - 1) * 100 if i else None,
                    "gain_year": v - prev_value - row["invested_year"]})
        prev_value = v
        res.yearly.append(row)
    return res


def _value_on_all_dates(series: Series, purchase_list, first_purchase: dt.date):
    run_units = run_inv = 0.0
    pi = 0
    for d, nav in series:
        if d < first_purchase:
            continue
        while pi < len(purchase_list) and purchase_list[pi][0] <= d:
            run_units, run_inv = purchase_list[pi][1]
            pi += 1
        yield d, run_units * nav, run_inv


def dca_value_on(res: DCAResult, series: Series, date: dt.date) -> tuple[float, float]:
    """(value, invested) of a DCA position on a given date."""
    units = invested = 0.0
    for p in res.purchases:
        if dt.date.fromisoformat(p["date"]) <= date:
            units, invested = p["units_total"], p["invested_total"]
        else:
            break
    nav = nav_on_or_before(series, date)
    return (units * nav[1] if nav else 0.0), invested


# ---------------------------------------------------------------- misc
def index_to_base(series: Series, base_date: dt.date, base: float = 100.0) -> Series:
    start = nav_on_or_after(series, base_date)
    if start is None:
        return []
    return [(d, v / start[1] * base) for d, v in series if d >= start[0]]


def downsample(series: Series, max_points: int = 900) -> Series:
    """Keep every k-th point plus the last point so charts stay light."""
    if len(series) <= max_points:
        return series
    k = math.ceil(len(series) / max_points)
    out = series[::k]
    if out[-1] != series[-1]:
        out.append(series[-1])
    return out


def real_holdings(transactions: list[dict], series: Series) -> dict | None:
    """Value and XIRR of real purchases (date, amount, units, price) for one fund."""
    if not transactions or not series:
        return None
    units = invested = 0.0
    cashflows = []
    rows = []
    for t in sorted(transactions, key=lambda x: x["date"]):
        d = t["date"]
        amount = float(t["amount"])
        price = t.get("price")
        u = t.get("units")
        if not u:
            if not price:
                hit = nav_on_or_before(series, d) or nav_on_or_after(series, d)
                price = hit[1] if hit else None
            u = amount / price if price else None
        if u is None:
            continue
        units += float(u)
        invested += amount
        cashflows.append((d, -amount))
        rows.append({"date": d.isoformat(), "amount": amount, "units": float(u),
                     "price": float(price) if price else amount / float(u)})
    if not rows:
        return None
    as_of, last_nav = series[-1]
    value = units * last_nav
    return {"rows": rows, "invested": invested, "units": units, "value": value, "pnl": value - invested,
            "pnl_pct": (value / invested - 1) * 100 if invested else None,
            "xirr": xirr(cashflows + [(as_of, value)]), "as_of": as_of.isoformat(),
            "avg_price": invested / units if units else None}
