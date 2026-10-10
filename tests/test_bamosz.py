import datetime as dt
import unittest

from fund_tracker.providers.bamosz import chart_form_payload, parse_chart_data, parse_history_table, parse_latest

HTML = """
<html><body>
<table class="dataTable"><tr><td>Alap neve:</td><td>OTP Multi-Asset Konzervatív</td></tr>
<tr><td>ISIN:</td><td>HU0000729793</td></tr><tr><td>Devizanem:</td><td>EUR</td></tr>
<tr><td>Árfolyam:</td><td>1,182102</td></tr><tr><td>Dátum:</td><td>2026.10.08.</td></tr></table>
<form id="A3225:j_idt8" method="post" action="/web/guest/alapoldal;jsessionid=X?a=1&amp;b=2">
<input type="hidden" name="A3225:j_idt8" value="A3225:j_idt8">
<input type="text" name="A3225:j_idt8:startDate_input" value="2025.10.10">
<input type="text" name="A3225:j_idt8:endDate_input" value="2026.10.10">
<input type="hidden" name="A3225:j_idt8:chartDataValue" value="[[1791417600000, 1.1821],[1791331200000, 1.1840]]">
<input type="submit" name="A3225:j_idt8:j_idt30" value="Frissít">
<input type="hidden" name="javax.faces.ViewState" value="123:456">
</form>
<table class="dataTable2"><tr><th>Dátum</th><th>Árfolyam</th><th>Nettó eszközérték</th></tr>
<tr><td>2026.10.08.</td><td>1,182102</td><td>24 342 871</td></tr>
<tr><td>2026.10.07.</td><td>1,183992</td><td>24 386 445</td></tr></table>
</body></html>
"""


class BamoszParsersTest(unittest.TestCase):
    def test_chart_data(self):
        rows = parse_chart_data(HTML)
        self.assertEqual(rows, [(dt.date(2026, 10, 7), 1.184), (dt.date(2026, 10, 8), 1.1821)])

    def test_history_table(self):
        rows = parse_history_table(HTML)
        self.assertEqual(rows, [(dt.date(2026, 10, 7), 1.183992), (dt.date(2026, 10, 8), 1.182102)])

    def test_latest(self):
        self.assertEqual(parse_latest(HTML), [(dt.date(2026, 10, 8), 1.182102)])

    def test_form_payload(self):
        action, data = chart_form_payload(HTML, dt.date(2022, 8, 26), dt.date(2026, 10, 10))
        self.assertTrue(action.startswith("https://www.bamosz.hu/web/guest/alapoldal;jsessionid=X?a=1&b=2"))
        self.assertEqual(data["A3225:j_idt8:startDate_input"], "2022.08.26")
        self.assertEqual(data["A3225:j_idt8:endDate_input"], "2026.10.10")
        self.assertEqual(data["A3225:j_idt8:j_idt30"], "Frissít")
        self.assertEqual(data["javax.faces.ViewState"], "123:456")
        self.assertIn("A3225:j_idt8:chartDataValue", data)
