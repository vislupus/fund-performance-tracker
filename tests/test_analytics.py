import datetime as dt
import math
import unittest

from fund_tracker import analytics as an


def daily_series(start: dt.date, days: int, fn):
    out = []
    for i in range(days):
        d = start + dt.timedelta(days=i)
        if d.weekday() < 5:
            out.append((d, fn(i)))
    return out


class ReturnsTest(unittest.TestCase):
    def test_monthly_and_yearly_returns(self):
        # 1% per calendar month growth, compounding, from 2023-01-01
        def nav(i):
            d = dt.date(2023, 1, 1) + dt.timedelta(days=i)
            months = (d.year - 2023) * 12 + d.month - 1
            return 100 * (1.01 ** months)
        s = daily_series(dt.date(2023, 1, 1), 730, nav)
        monthly = an.monthly_returns(s)
        full = [m for m in monthly if not m["partial"]]
        self.assertTrue(all(abs(m["ret"] - 1.0) < 1e-9 for m in full))
        yearly = an.yearly_returns(s)
        self.assertEqual(yearly[0]["year"], 2023)
        self.assertTrue(yearly[0]["partial"])  # first year starts at the first NAV
        self.assertAlmostEqual(yearly[1]["ret"], (1.01 ** 12 - 1) * 100, places=6)

    def test_period_returns(self):
        s = daily_series(dt.date(2020, 1, 1), 365 * 4, lambda i: 100 + i * 0.1)
        pr = an.period_returns(s)
        self.assertTrue(pr["1y"]["available"])
        self.assertFalse(pr["5y"]["available"])
        self.assertGreater(pr["1y"]["cum"], 0)
        self.assertIsNotNone(pr["3y"]["ann"])
        self.assertIsNone(pr["1m"]["ann"])  # too short to annualise

    def test_max_drawdown(self):
        s = [(dt.date(2024, 1, i + 1), v) for i, v in enumerate([100, 110, 99, 95, 120, 100, 130])]
        dd = an.max_drawdown(s)
        self.assertAlmostEqual(dd["dd"], (100 / 120 - 1) * 100)
        self.assertEqual(dd["peak"], "2024-01-05")
        self.assertEqual(dd["trough"], "2024-01-06")
        self.assertEqual(dd["recovered"], "2024-01-07")

    def test_volatility(self):
        monthly = [{"ret": r, "partial": False} for r in [1, -1] * 6]
        vol = an.volatility_from_monthly(monthly)
        self.assertAlmostEqual(vol, math.sqrt(12 / 11) * math.sqrt(12), places=6)


class XirrTest(unittest.TestCase):
    def test_single_cashflow_doubles_in_a_year(self):
        cf = [(dt.date(2023, 1, 1), -100.0), (dt.date(2024, 1, 1), 200.0)]
        self.assertAlmostEqual(an.xirr(cf), 100.0, places=3)

    def test_monthly_contributions(self):
        cf = [(dt.date(2023, m, 15), -10.0) for m in range(1, 13)]
        cf.append((dt.date(2023, 12, 31), 120.0))
        self.assertAlmostEqual(an.xirr(cf), 0.0, places=5)
        cf[-1] = (dt.date(2023, 12, 31), 125.0)
        self.assertGreater(an.xirr(cf), 5)

    def test_no_solution(self):
        self.assertIsNone(an.xirr([(dt.date(2023, 1, 1), -100.0)]))


class DCATest(unittest.TestCase):
    def test_purchase_on_15th_or_next_trading_day(self):
        s = daily_series(dt.date(2024, 1, 1), 120, lambda i: 10.0)
        res = an.dca_simulate(s, 10, 15)
        dates = [p["date"] for p in res.purchases]
        # Jan 15 2024 is Monday, Feb 15 Thursday, Mar 15 Friday, Apr 15 Monday
        self.assertEqual(dates, ["2024-01-15", "2024-02-15", "2024-03-15", "2024-04-15"])
        self.assertAlmostEqual(res.invested, 40)
        self.assertAlmostEqual(res.units, 4.0)
        self.assertAlmostEqual(res.value, 40)
        self.assertAlmostEqual(res.pnl, 0)

    def test_weekend_rolls_forward(self):
        s = daily_series(dt.date(2024, 6, 1), 30, lambda i: 5.0)  # 15 June 2024 is a Saturday
        res = an.dca_simulate(s, 10, 15)
        self.assertEqual(res.purchases[0]["date"], "2024-06-17")

    def test_entry_fee_and_start(self):
        s = daily_series(dt.date(2024, 1, 1), 200, lambda i: 2.0)
        res = an.dca_simulate(s, 10, 15, start=dt.date(2024, 3, 1), entry_fee_pct=2.0)
        self.assertEqual(res.purchases[0]["date"], "2024-03-15")
        self.assertAlmostEqual(res.purchases[0]["units"], 10 * 0.98 / 2.0)
        self.assertAlmostEqual(res.invested, 10 * len(res.purchases))
        self.assertLess(res.value, res.invested)

    def test_value_series_and_yearly(self):
        s = daily_series(dt.date(2023, 1, 1), 400, lambda i: 1.0 + i * 0.001)
        res = an.dca_simulate(s, 10, 15)
        self.assertEqual(res.value_series[0][0].isoformat(), res.purchases[0]["date"])
        self.assertEqual(len(res.yearly), 2)
        self.assertAlmostEqual(res.yearly[0]["invested_year"], 120)
        self.assertGreater(res.xirr, 0)
        d = res.to_dict()
        self.assertEqual(d["n_purchases"], len(res.purchases))

    def test_empty(self):
        self.assertEqual(an.dca_simulate([], 10, 15).purchases, [])
        s = daily_series(dt.date(2024, 1, 1), 10, lambda i: 1.0)
        self.assertEqual(an.dca_simulate(s, 10, 15, start=dt.date(2025, 1, 1)).purchases, [])


class MiscTest(unittest.TestCase):
    def test_index_and_downsample(self):
        s = daily_series(dt.date(2020, 1, 1), 3000, lambda i: 50 + i * 0.01)
        idx = an.index_to_base(s, dt.date(2021, 1, 1))
        self.assertAlmostEqual(idx[0][1], 100)
        ds = an.downsample(s, 500)
        self.assertLessEqual(len(ds), 501)
        self.assertEqual(ds[-1], s[-1])

    def test_real_holdings(self):
        s = daily_series(dt.date(2024, 1, 1), 100, lambda i: 2.0)
        tx = [{"date": dt.date(2024, 1, 15), "amount": 10, "units": None, "price": None},
              {"date": dt.date(2024, 2, 15), "amount": 10, "units": 4.0, "price": 2.5}]
        r = an.real_holdings(tx, s)
        self.assertAlmostEqual(r["units"], 9.0)
        self.assertAlmostEqual(r["invested"], 20)
        self.assertAlmostEqual(r["value"], 18)


if __name__ == "__main__":
    unittest.main()
