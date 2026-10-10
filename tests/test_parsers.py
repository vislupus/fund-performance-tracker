import datetime as dt
import unittest

from fund_tracker.providers.ft import parse_history_html
from fund_tracker.providers.manual_csv import parse_csv_text
from fund_tracker.util import parse_date, parse_number


class NumberDateTest(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(parse_number("1 234,56"), 1234.56)
        self.assertEqual(parse_number("1,234.56"), 1234.56)
        self.assertEqual(parse_number("12,34"), 12.34)
        self.assertEqual(parse_number("1,234"), 1234.0)
        self.assertEqual(parse_number("0,9876"), 0.9876)
        self.assertEqual(parse_number("€ 101.25"), 101.25)
        self.assertIsNone(parse_number("n/a"))

    def test_dates(self):
        self.assertEqual(parse_date("2024.01.15."), dt.date(2024, 1, 15))
        self.assertEqual(parse_date("2024-01-15"), dt.date(2024, 1, 15))
        self.assertEqual(parse_date("Jan 15, 2024"), dt.date(2024, 1, 15))
        self.assertEqual(parse_date("Monday, January 15, 2024"), dt.date(2024, 1, 15))
        self.assertEqual(parse_date("15.01.2024"), dt.date(2024, 1, 15))
        self.assertIsNone(parse_date("hello"))


class CsvTest(unittest.TestCase):
    def test_bamosz_style(self):
        text = "Dátum;Árfolyam;Nettó eszközérték\n2024.01.15.;1,0234;123456\n2024.01.16.;1,0250;123999\n"
        rows = parse_csv_text(text)
        self.assertEqual(rows, [(dt.date(2024, 1, 15), 1.0234), (dt.date(2024, 1, 16), 1.025)])

    def test_investing_style(self):
        text = '"Date","Price","Open","High","Low","Change %"\n"Jan 16, 2024","27.68","27.68","27.68","27.68","0.11%"\n"Jan 15, 2024","27.65","27.65","27.65","27.65","-0.04%"\n'
        rows = parse_csv_text(text)
        self.assertEqual(rows[0], (dt.date(2024, 1, 15), 27.65))
        self.assertEqual(rows[1], (dt.date(2024, 1, 16), 27.68))

    def test_headerless(self):
        rows = parse_csv_text("2024-01-15,10.5\n2024-01-16,10.6\n")
        self.assertEqual(len(rows), 2)

    def test_empty(self):
        self.assertEqual(parse_csv_text(""), [])


class FTParserTest(unittest.TestCase):
    def test_rows(self):
        html = ('<tr><td><span class="mod-ui-hide-small-below">Monday, January 15, 2024</span>'
                '<span class="mod-ui-hide-medium-above">Mon, Jan 15, 2024</span></td>'
                '<td>27.65</td><td>27.65</td><td>27.65</td><td>27.66</td><td><span>--</span></td></tr>'
                '<tr><td><span>Tuesday, January 16, 2024</span></td><td>27.70</td></tr>')
        rows = parse_history_html(html)
        self.assertEqual(rows, [(dt.date(2024, 1, 15), 27.66), (dt.date(2024, 1, 16), 27.70)])


if __name__ == "__main__":
    unittest.main()


class DecimalSeparatorTest(unittest.TestCase):
    def test_explicit_decimal_comma(self):
        self.assertEqual(parse_number("1,003", decimal=","), 1.003)
        self.assertEqual(parse_number("1.234,5", decimal=","), 1234.5)
        self.assertEqual(parse_number("1,003", decimal="."), 1003.0)
        self.assertEqual(parse_number("27.60", decimal="."), 27.6)

    def test_bamosz_three_decimals(self):
        from fund_tracker.providers.bamosz import parse_download_csv
        text = '"Dátum";"Árfolyam"\n"2023/01/12";"1,002031"\n"2023/01/13";"1,003"\n"2023/01/16";"1,00294"\n'
        rows = parse_download_csv(text)
        self.assertEqual([v for _, v in rows], [1.002031, 1.003, 1.00294])

    def test_manual_semicolon_file_uses_decimal_comma(self):
        rows = parse_csv_text("Dátum;Árfolyam\n2023.01.13.;1,003\n2023.01.16.;1,00294\n")
        self.assertEqual(rows[0][1], 1.003)
