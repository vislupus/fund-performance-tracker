"""Provider orchestration with a fake HTTP client (no network)."""
import datetime as dt
import logging
import json
import tempfile
import unittest
from pathlib import Path

from fund_tracker.config import Fund, Settings
from fund_tracker.db import Database
from fund_tracker.providers import update_fund
from fund_tracker.providers.base import ProviderError
from fund_tracker.providers.ft import FTProvider
from fund_tracker.providers.investing import InvestingProvider


logging.disable(logging.CRITICAL)


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.status_code = 200

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeHttp:
    """Routes requests by URL substring; `fail` lists substrings that raise."""

    def __init__(self, routes, fail=()):
        self.routes = routes
        self.fail = fail
        self.calls = []

    def get(self, url, params=None, headers=None, allow_statuses=(200,)):
        self.calls.append((url, params))
        for key in self.fail:
            if key in url:
                raise RuntimeError(f"HTTP 403 for {url}")
        for key, handler in self.routes.items():
            if key in url:
                return handler(url, params) if callable(handler) else FakeResponse(handler)
        raise RuntimeError(f"no route for {url}")


def ft_rows(dates_prices):
    html = ""
    for d, p in dates_prices:
        html += (f'<tr><td><span class="a">{d.strftime("%A, %B %d, %Y")}</span></td>'
                 f'<td>{p}</td><td>{p}</td><td>{p}</td><td>{p}</td><td><span>--</span></td></tr>')
    return {"html": html}


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.sqlite")
        self.fund = Fund("LU0106235533", "Schroder ISF Euro Bond A Acc EUR", "EUR", "portfolio")
        self.db.upsert_funds([self.fund])
        self.settings = Settings(history_start=dt.date(2024, 1, 1))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_ft_picks_currency_listing_and_parses(self):
        search = {"data": {"security": [
            {"xid": "111", "symbol": "LU0106235533:USD", "name": "Schroder ISF Euro Bond A Acc USD"},
            {"xid": "222", "symbol": "LU0106235533:EUR", "name": "Schroder ISF Euro Bond A Acc EUR"},
        ]}}
        rows = [(dt.date(2024, 1, 2), 27.5), (dt.date(2024, 1, 3), 27.6)]
        http = FakeHttp({"searchsecurities": search, "get-historical-prices": ft_rows(rows)})
        out = FTProvider().fetch(self.fund, dt.date(2024, 1, 1), dt.date(2024, 1, 31), http, self.db)
        self.assertEqual(out, rows)
        self.assertEqual(self.db.get_provider_id("LU0106235533", "ft"), "222")
        # second call uses the cached xid (no search request)
        http2 = FakeHttp({"get-historical-prices": ft_rows(rows)})
        FTProvider().fetch(self.fund, dt.date(2024, 1, 1), dt.date(2024, 1, 31), http2, self.db)
        self.assertTrue(all("searchsecurities" not in u for u, _ in http2.calls))

    def test_ft_symbol_hint(self):
        fund = Fund("IE00B4L5Y983", "iShares Core MSCI World", "EUR", "benchmark", ids={"ft_symbol": "EUNL:GER:EUR"})
        search = {"data": {"security": [
            {"xid": "1", "symbol": "IWDA:LSE:USD", "name": "iShares Core MSCI World"},
            {"xid": "2", "symbol": "EUNL:GER:EUR", "name": "iShares Core MSCI World"},
        ]}}
        http = FakeHttp({"searchsecurities": search, "get-historical-prices": ft_rows([(dt.date(2024, 1, 2), 80.0)])})
        FTProvider().fetch(fund, dt.date(2024, 1, 1), dt.date(2024, 1, 31), http, self.db)
        self.assertEqual(self.db.get_provider_id(fund.isin, "ft"), "2")

    def test_investing_search_and_screen(self):
        search = {"data": {"quotes": [
            {"pair_ID": 900, "search_main_text": "0P00000ABC", "search_main_longtext": "Some Other Fund", "search_main_subtext": "Fund - Luxembourg"},
            {"pair_ID": 901, "search_main_text": "LU0106235533", "search_main_longtext": "Schroder ISF Euro Bond A Acc EUR", "search_main_subtext": "Fund - Luxembourg"},
        ]}}
        ts = int(dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc).timestamp())
        screen = {"data": [{"screen_data": {"data": [{"date": ts, "price": "27.55"}, {"date": ts + 86400, "price": "27,60"}]}}]}
        http = FakeHttp({"search_by_type": search, "get_screen": screen})
        out = InvestingProvider().fetch(self.fund, dt.date(2024, 1, 1), dt.date(2024, 1, 31), http, self.db)
        self.assertEqual(out, [(dt.date(2024, 1, 2), 27.55), (dt.date(2024, 1, 3), 27.6)])
        self.assertEqual(self.db.get_provider_id("LU0106235533", "investing"), "901")

    def test_investing_falls_back_to_tvc(self):
        search = {"data": {"quotes": [{"pair_ID": 5, "search_main_text": "LU0106235533", "search_main_longtext": "x", "search_main_subtext": "y"}]}}
        ts = int(dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc).timestamp())
        http = FakeHttp({"search_by_type": search, "get_screen": {"data": []},
                         "tvc6.investing.com": {"s": "ok", "t": [ts], "c": [27.0]}})
        out = InvestingProvider().fetch(self.fund, dt.date(2024, 1, 1), dt.date(2024, 1, 31), http, self.db)
        self.assertEqual(out, [(dt.date(2024, 1, 2), 27.0)])

    def test_update_fund_falls_through_providers_and_logs(self):
        search = {"data": {"quotes": [{"pair_ID": 5, "search_main_text": "LU0106235533", "search_main_longtext": "x", "search_main_subtext": "y"}]}}
        ts = int(dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc).timestamp())
        screen = {"data": [{"screen_data": {"data": [{"date": ts, "price": "27.55"}]}}]}
        http = FakeHttp({"search_by_type": search, "get_screen": screen}, fail=("markets.ft.com",))
        run_at = "2026-10-01T06:00:00+00:00"
        res = update_fund(self.db, http, self.fund, self.settings, run_at, today=dt.date(2024, 1, 31))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["provider"], "investing")
        logs = self.db.last_run_logs()
        self.assertEqual([(r["provider"], r["status"]) for r in logs], [("ft", "error"), ("investing", "ok")])
        self.assertEqual(self.db.nav_range("LU0106235533")[2], 1)
        # incremental window starts refresh_overlap_days before the last stored date
        http2 = FakeHttp({"get_screen": lambda u, p: FakeResponse({"data": [{"screen_data": {"data": []}}]}),
                          "tvc6.investing.com": {"s": "no_data"}}, fail=("markets.ft.com",))
        res2 = update_fund(self.db, http2, self.fund, self.settings, run_at, today=dt.date(2024, 3, 1))
        self.assertEqual(res2["status"], "failed")
        date_from = [p for u, p in http2.calls if "get_screen" in u][0]["date_from"]
        self.assertEqual(date_from, (dt.date(2024, 1, 2) - dt.timedelta(days=45)).strftime("%d%m%Y"))

    def test_all_fail(self):
        http = FakeHttp({}, fail=("markets.ft.com", "investing.com", "morningstar"))
        res = update_fund(self.db, http, self.fund, self.settings, "2026-10-01T06:00:00+00:00", today=dt.date(2024, 1, 31))
        self.assertEqual(res["status"], "failed")
        self.assertEqual(len(res["errors"]), 3)


if __name__ == "__main__":
    unittest.main()
