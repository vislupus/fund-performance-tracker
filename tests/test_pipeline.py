"""End-to-end test with synthetic NAV data: DB -> model -> HTML, no network."""
import datetime as dt
import math
import tempfile
import unittest
from pathlib import Path

from fund_tracker.config import Fund, Settings, load_funds, load_settings
from fund_tracker.db import Database
from fund_tracker.report import build_model, render_html


def synthetic(start: dt.date, days: int, drift: float, seed: int):
    out = []
    v = 100.0
    x = seed
    for i in range(days):
        d = start + dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        x = (x * 1103515245 + 12345) % (2 ** 31)
        noise = (x / 2 ** 31 - 0.5) * 0.02
        v *= 1 + drift / 252 + noise
        out.append((d, round(v, 4)))
    return out


class PipelineTest(unittest.TestCase):
    def test_config_loads(self):
        funds = load_funds()
        self.assertGreaterEqual(len(funds), 10)
        self.assertEqual(len({f.isin for f in funds}), len(funds))
        settings = load_settings()
        self.assertEqual(settings.purchase_day, 15)
        self.assertEqual(funds[0].provider_order, ["ft", "investing", "bamosz", "morningstar"])

    def test_db_priority_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.sqlite")
            funds = [
                Fund("LU0000000001", "Test Equity", "EUR", "portfolio"),
                Fund("HU0000000002", "Test Bond", "EUR", "portfolio"),
                Fund("BG0000000003", "Test BGN", "BGN", "watchlist"),
                Fund("IE0000000004", "Bench", "EUR", "benchmark"),
                Fund("LU0000000005", "Empty", "EUR", "watchlist"),
            ]
            db.upsert_funds(funds)
            today = dt.date(2026, 10, 1)
            db.insert_navs("LU0000000001", synthetic(dt.date(2019, 6, 21), 2660, 0.08, 1), "ft")
            db.insert_navs("HU0000000002", synthetic(dt.date(2023, 11, 29), 1040, 0.03, 2), "investing")
            db.insert_navs("BG0000000003", synthetic(dt.date(2006, 1, 2), 7570, 0.04, 3), "ft")
            db.insert_navs("IE0000000004", synthetic(dt.date(2009, 9, 25), 6220, 0.10, 4), "ft")
            # priority: manual beats ft for the same date, investing does not
            n = db.insert_navs("LU0000000001", [(dt.date(2020, 1, 2), 1.0)], "investing")
            self.assertEqual(n, 0)
            n = db.insert_navs("LU0000000001", [(dt.date(2020, 1, 2), 1.0)], "manual")
            self.assertEqual(n, 1)
            self.assertEqual(db.nav_sources("LU0000000001")["manual"], 1)
            db.log("2026-10-01T06:00:00+00:00", "LU0000000001", "ft", "ok", 10, dt.date(2026, 9, 1), dt.date(2026, 9, 30), "x")
            db.log("2026-10-01T06:00:00+00:00", "LU0000000005", "ft", "failed", 0, None, None, "nothing")

            settings = Settings(my_plan_start=dt.date(2025, 10, 15))
            model = build_model(db, funds, settings, today)
            self.assertEqual(len(model["funds"]), 5)
            eq = next(f for f in model["funds"] if f["isin"] == "LU0000000001")
            self.assertEqual(eq["fetch_status"], "ok")
            self.assertTrue(eq["dca_full"]["n_purchases"] > 80)
            self.assertTrue(eq["dca_plan"]["n_purchases"] >= 11)
            self.assertIsNotNone(eq["periods"]["5y"]["ann"])
            empty = next(f for f in model["funds"] if f["isin"] == "LU0000000005")
            self.assertEqual(empty["n_points"], 0)
            self.assertEqual(empty["fetch_status"], "failed")
            self.assertEqual(len(model["portfolio"]["members"]), 2)
            self.assertTrue(math.isfinite(model["portfolio"]["xirr"]))
            out = render_html(model, Path(tmp) / "index.html")
            html = out.read_text(encoding="utf-8")
            self.assertIn("Test Equity", html)
            self.assertIn("Моят портфейл", html)
            self.assertIn('id="data"', html)
            db.close()


if __name__ == "__main__":
    unittest.main()


class BgnSwitchTest(unittest.TestCase):
    def test_detects_redenomination_jump(self):
        from fund_tracker.report import bgn_series_to_eur
        series = [(dt.date(2025, 12, 29), 1.6736), (dt.date(2025, 12, 30), 1.6740),
                  (dt.date(2026, 1, 5), 0.8560), (dt.date(2026, 1, 6), 0.8590)]
        out, switch = bgn_series_to_eur(series, 1.95583)
        self.assertEqual(switch, dt.date(2026, 1, 5))
        self.assertAlmostEqual(out[0][1], 1.6736 / 1.95583)
        self.assertAlmostEqual(out[2][1], 0.8560)

    def test_no_jump_means_all_bgn(self):
        from fund_tracker.report import bgn_series_to_eur
        series = [(dt.date(2025, 12, 29), 1.6736), (dt.date(2025, 12, 30), 1.6800), (dt.date(2026, 1, 5), 1.6900)]
        out, switch = bgn_series_to_eur(series, 1.95583)
        self.assertIsNone(switch)
        self.assertAlmostEqual(out[-1][1], 1.69 / 1.95583)
