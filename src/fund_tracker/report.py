"""Builds the data model for the HTML report and renders it with Jinja2."""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import __version__, analytics as an
from .config import DATA_DIR, DOCS_DIR, Fund, Settings
from .db import Database
from .transactions import load_transactions

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
GROUP_LABELS = {"portfolio": "Портфейл", "watchlist": "Наблюдавани", "benchmark": "Бенчмарк"}


def _fx_to_eur(fund: Fund, settings: Settings) -> float:
    if fund.currency == "BGN":
        return 1.0 / settings.bgn_per_eur
    return 1.0


def bgn_series_to_eur(series: an.Series, bgn_per_eur: float) -> tuple[an.Series, dt.date | None]:
    """Convert a BGN NAV series to EUR.

    Bulgarian funds switched to EUR on 2026-01-01 and some data sources re-denominated
    part of their history. If the series contains a one-day drop by the factor
    1/1.95583 (±3 %), everything before that day is treated as BGN and everything
    from it on as EUR already. Otherwise the whole series is treated as BGN.
    Returns the EUR series and the detected switch date (None = no switch found).
    """
    target = 1.0 / bgn_per_eur
    switch_idx = None
    for i in range(1, len(series)):
        prev, cur = series[i - 1][1], series[i][1]
        if prev > 0 and abs(cur / prev - target) < 0.03 * target:
            switch_idx = i
            break
    if switch_idx is None:
        return [(d, v / bgn_per_eur) for d, v in series], None
    converted = [(d, v / bgn_per_eur) for d, v in series[:switch_idx]] + series[switch_idx:]
    return converted, series[switch_idx][0]


def build_fund_model(fund: Fund, series: an.Series, settings: Settings, today: dt.date,
                     transactions: list[dict] | None) -> dict:
    switch_date = None
    if fund.currency == "BGN" and series:
        eur_series, switch_date = bgn_series_to_eur(series, settings.bgn_per_eur)
        # returns/risk must not see the re-denomination jump either
        series = eur_series
    else:
        eur_series = series
    monthly = an.monthly_returns(series)
    yearly = an.yearly_returns(series)
    periods = an.period_returns(series)
    fee = fund.entry_fee_pct if settings.apply_entry_fee else 0.0
    month_end_dates = [d for d, _ in an.month_ends(eur_series)]
    dca_full = an.dca_simulate(eur_series, settings.monthly_amount, settings.purchase_day, None, today, fee,
                               value_dates=month_end_dates)
    dca_plan = an.dca_simulate(eur_series, settings.monthly_amount, settings.purchase_day,
                               settings.my_plan_start, today, fee, value_dates=month_end_dates)
    dca_currency = "EUR" if fund.currency in ("EUR", "BGN") else fund.currency
    model = {
        "isin": fund.isin,
        "name": fund.name,
        "currency": fund.currency,
        "nav_currency": "EUR" if fund.currency == "BGN" else fund.currency,
        "dca_currency": dca_currency,
        "group": fund.group,
        "group_label": GROUP_LABELS.get(fund.group, fund.group),
        "manager": fund.manager,
        "inception": fund.inception.isoformat() if fund.inception else None,
        "entry_fee_pct": fund.entry_fee_pct,
        "n_points": len(series),
        "first_date": series[0][0].isoformat() if series else None,
        "last_date": series[-1][0].isoformat() if series else None,
        "last_nav": series[-1][1] if series else None,
        "nav": [(d.isoformat(), round(v, 6)) for d, v in an.downsample(series)],
        "nav_m": [(d.isoformat(), round(v, 6)) for d, v in an.month_ends(series)],
        "monthly": monthly,
        "yearly": yearly,
        "periods": periods,
        "risk": {
            "max_drawdown": an.max_drawdown(series) if series else None,
            "volatility": an.volatility_from_monthly(monthly),
            **an.best_worst_month(monthly),
        },
        "dca_full": dca_full.to_dict(),
        "dca_plan": dca_plan.to_dict(),
        "real": an.real_holdings(transactions, eur_series) if transactions else None,
    }
    if fund.currency == "BGN":
        if switch_date:
            model["fx_note"] = (f"Цените до {switch_date} са в BGN и са преизчислени в EUR по {settings.bgn_per_eur}; "
                                f"от {switch_date} източникът ги дава в EUR. Всички показатели са в EUR.")
        else:
            model["fx_note"] = f"NAV в BGN, преизчислен в EUR по {settings.bgn_per_eur}. Всички показатели са в EUR."
    if series and fund.inception and series[0][0] < fund.inception - dt.timedelta(days=60):
        model["history_note"] = (f"Източникът дава история от {series[0][0]}, преди официалния старт на класа "
                                 f"({fund.inception}) – вероятно история на предшественик (напр. слят фонд).")
    return model


def build_portfolio_model(funds: list[dict], series_by_isin: dict[str, an.Series], settings: Settings,
                          today: dt.date) -> dict:
    """Aggregate of the 'my plan' DCA over the portfolio funds in EUR."""
    members = [f for f in funds if f["group"] == "portfolio" and f["dca_currency"] == "EUR" and f["dca_plan"]["n_purchases"]]
    if not members:
        return {"members": [], "invested": 0, "value": 0}
    invested = sum(f["dca_plan"]["invested"] for f in members)
    value = sum(f["dca_plan"]["value"] for f in members)
    cashflows: list[tuple[dt.date, float]] = []
    as_of = None
    for f in members:
        for p in f["dca_plan"]["purchases"]:
            cashflows.append((dt.date.fromisoformat(p["date"]), -p["amount"]))
        d = dt.date.fromisoformat(f["dca_plan"]["as_of"])
        as_of = d if as_of is None or d > as_of else as_of
    cashflows.append((as_of, value))
    # value series on the union of month ends + purchase dates
    grid: set[dt.date] = set()
    for f in members:
        for d, _, _ in f["dca_plan"]["value_series"]:
            grid.add(dt.date.fromisoformat(d))
    grid_sorted = sorted(grid)
    if len(grid_sorted) > 400:
        me = {}
        for d in grid_sorted:
            me[(d.year, d.month)] = d
        grid_sorted = sorted(set(me.values()) | {grid_sorted[-1]})
    series_out = []
    for d in grid_sorted:
        tot_v = tot_i = 0.0
        for f in members:
            vs = f["dca_plan"]["value_series"]
            # last point on or before d
            row = None
            for item in vs:
                if item[0] <= d.isoformat():
                    row = item
                else:
                    break
            if row:
                tot_v += row[1]
                tot_i += row[2]
        series_out.append((d.isoformat(), round(tot_v, 2), round(tot_i, 2)))
    real_members = [f for f in funds if f.get("real")]
    real = None
    if real_members:
        r_inv = sum(f["real"]["invested"] for f in real_members)
        r_val = sum(f["real"]["value"] for f in real_members)
        r_cf = []
        for f in real_members:
            for row in f["real"]["rows"]:
                r_cf.append((dt.date.fromisoformat(row["date"]), -row["amount"]))
        r_as_of = max(dt.date.fromisoformat(f["real"]["as_of"]) for f in real_members)
        r_cf.append((r_as_of, r_val))
        real = {"invested": r_inv, "value": r_val, "pnl": r_val - r_inv,
                "pnl_pct": (r_val / r_inv - 1) * 100 if r_inv else None, "xirr": an.xirr(r_cf),
                "as_of": r_as_of.isoformat(), "n_funds": len(real_members)}
    return {
        "members": [f["isin"] for f in members],
        "invested": invested, "value": value, "pnl": value - invested,
        "pnl_pct": (value / invested - 1) * 100 if invested else None,
        "xirr": an.xirr(cashflows), "as_of": as_of.isoformat() if as_of else None,
        "value_series": series_out, "real": real,
    }


def build_model(db: Database, funds: list[Fund], settings: Settings, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    transactions = load_transactions(DATA_DIR / "transactions.csv")
    fund_models = []
    series_by_isin: dict[str, an.Series] = {}
    for fund in funds:
        series = db.nav_series(fund.isin)
        series_by_isin[fund.isin] = series
        fund_models.append(build_fund_model(fund, series, settings, today, transactions.get(fund.isin)))
    years = sorted({y["year"] for f in fund_models for y in f["yearly"]})
    logs = [dict(r) for r in db.last_run_logs()]
    status = {}
    for r in logs:
        status.setdefault(r["isin"], []).append(r)
    for f in fund_models:
        f["sources"] = db.nav_sources(f["isin"])
        f["last_run"] = status.get(f["isin"], [])
        ok = any(r["status"] == "ok" for r in f["last_run"])
        f["fetch_status"] = "ok" if ok else ("failed" if f["last_run"] else "never")
    portfolio = build_portfolio_model(fund_models, series_by_isin, settings, today)
    order = {"portfolio": 0, "watchlist": 1, "benchmark": 2}
    fund_models.sort(key=lambda f: (order.get(f["group"], 9), funds.index(next(x for x in funds if x.isin == f["isin"]))))
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "last_run_at": db.last_run_at(),
        "version": __version__,
        "settings": {
            "monthly_amount": settings.monthly_amount, "purchase_day": settings.purchase_day,
            "my_plan_start": settings.my_plan_start.isoformat(), "title": settings.title,
            "default_compare": settings.default_compare, "bgn_per_eur": settings.bgn_per_eur,
        },
        "years": years,
        "funds": fund_models,
        "portfolio": portfolio,
        "has_transactions": bool(transactions),
    }


def chart_payload(model: dict) -> dict:
    """Only what the page's JavaScript needs (keeps the HTML small)."""
    funds = []
    for f in model["funds"]:
        funds.append({
            "isin": f["isin"], "name": f["name"], "group": f["group"], "currency": f["currency"],
            "dca_currency": f["dca_currency"], "nav_currency": f.get("nav_currency", f["currency"]),
            "nav": f["nav"], "nav_m": f["nav_m"],
            "dca_full": {"value_series": f["dca_full"]["value_series"]},
            "dca_plan": {"value_series": f["dca_plan"]["value_series"]},
        })
    return {"settings": model["settings"], "funds": funds,
            "portfolio": {"value_series": model["portfolio"].get("value_series", [])}}


def heat_class(value: float | None, scale: str = "m") -> str:
    """CSS class for the diverging heatmap: p1..p4 (positive) / n1..n4 (negative) / z (near zero)."""
    if value is None:
        return ""
    thresholds = (1, 3, 7) if scale == "m" else (3, 10, 20)
    a = abs(value)
    if a < 0.05:
        return "z"
    level = 1 + sum(1 for t in thresholds if a >= t)
    return ("p" if value > 0 else "n") + str(level)


def render_html(model: dict, out_path: Path | None = None) -> Path:
    out_path = out_path or DOCS_DIR / "index.html"
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html"]))
    env.filters["money"] = lambda v, nd=2: ("—" if v is None else f"{v:,.{nd}f}".replace(",", " "))
    env.filters["pct"] = lambda v, nd=2: ("—" if v is None else f"{v:+.{nd}f}%")
    env.filters["num"] = lambda v, nd=4: ("—" if v is None else f"{v:.{nd}f}")
    env.globals["heat_class"] = heat_class
    tpl = env.get_template("report.html.j2")
    data_json = json.dumps(chart_payload(model), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = tpl.render(m=model, data_json=data_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    log.info("report written to %s (%.0f kB)", out_path, out_path.stat().st_size / 1024)
    return out_path


def export_csv(db: Database, funds: list[Fund], out_dir: Path | None = None) -> list[Path]:
    """One CSV per fund (date;nav;source) – handy for Excel."""
    out_dir = out_dir or DATA_DIR / "csv"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for fund in funds:
        rows = db.conn.execute("SELECT date, nav, source FROM nav WHERE isin=? ORDER BY date", (fund.isin,)).fetchall()
        if not rows:
            continue
        path = out_dir / f"{fund.isin}.csv"
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("date;nav;source\n")
            for r in rows:
                fh.write(f"{r['date']};{r['nav']};{r['source']}\n")
        written.append(path)
    return written
