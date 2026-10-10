import datetime as dt
import tempfile
import unittest
from pathlib import Path

from fund_tracker.config import Fund
from fund_tracker.db import Database
from fund_tracker.providers.dskam import DskamProvider, parse_items
from test_providers import FakeHttp, FakeResponse


class DskamTest(unittest.TestCase):
    def test_parse_items_formats(self):
        payload = {"Total": 3, "Items": [
            {"Date": "2024-01-15T00:00:00", "NetAssetValuePerUnit": 0.9041},
            {"Date": "16.01.2024", "NetAssetValuePerUnit": "0,9050"},
            {"Date": "/Date(1705536000000)/", "NetAssetValuePerUnit": 0.906},
        ]}
        rows = parse_items(payload)
        self.assertEqual(rows, [(dt.date(2024, 1, 15), 0.9041), (dt.date(2024, 1, 16), 0.905), (dt.date(2024, 1, 18), 0.906)])

    def test_fetch_with_date_format_fallback(self):
        calls = []
        def api(url, params):
            calls.append(dict(params))
            if "." not in params["DateFrom"]:
                return FakeResponse({"Total": 0, "Items": []})
            return FakeResponse({"Total": 1, "Items": [{"Date": "2024-01-15T00:00:00", "NetAssetValuePerUnit": 0.9}]})
        http = FakeHttp({"GetMutualFundUnitValues": api})
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.sqlite")
            fund = Fund("BG9000004051", "ДСК Баланс", "BGN", "watchlist", ids={"dskam_id": 5})
            rows = DskamProvider().fetch(fund, dt.date(2024, 1, 1), dt.date(2024, 2, 1), http, db)
            self.assertEqual(rows, [(dt.date(2024, 1, 15), 0.9)])
            self.assertEqual(calls[0]["MutualFundID"], "5")
            self.assertEqual(calls[0]["DateFrom"], "01.01.2024")
            db.close()

    def test_requires_id(self):
        from fund_tracker.providers.base import ProviderError
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.sqlite")
            with self.assertRaises(ProviderError):
                DskamProvider().fetch(Fund("BG0000000001", "x", "BGN"), dt.date(2024, 1, 1), dt.date(2024, 2, 1), FakeHttp({}), db)
            db.close()
