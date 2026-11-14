"""Purchasing periods (analytics.periods_for) and the comparisons of each mode."""
from datetime import date
import unittest

import analytics
import comparisons

T = date(2026, 11, 14)


def per(start, mode, end=None):
    r = analytics.periods_for(start, mode, today=T, end=end)
    return (r[0], r[1]), (r[2], r[3])


class Periods(unittest.TestCase):
    def test_week(self):
        self.assertEqual(per(None, "week"), ((date(2026, 11, 9), T), (date(2026, 11, 2), date(2026, 11, 7))))

    def test_month_running_and_finished(self):
        self.assertEqual(per(date(2026, 11, 1), "month")[1], (date(2026, 10, 1), date(2026, 10, 14)))
        self.assertEqual(per(date(2026, 2, 1), "month")[1], (date(2026, 1, 1), date(2026, 1, 31)))

    def test_quarter(self):
        self.assertEqual(per(date(2026, 10, 1), "quarter"), ((date(2026, 10, 1), T), (date(2026, 7, 1), date(2026, 8, 14))))
        self.assertEqual(per(date(2026, 1, 1), "quarter")[1], (date(2025, 10, 1), date(2025, 12, 31)))

    def test_range(self):
        self.assertEqual(per(date(2026, 11, 1), "range", date(2026, 11, 10)),
                         ((date(2026, 11, 1), date(2026, 11, 10)), (date(2026, 10, 22), date(2026, 10, 31))))
        cur, _ = per(date(2026, 11, 1), "range", date(2027, 1, 1))     # never past today
        self.assertEqual(cur[1], T)

    def test_last_year(self):
        self.assertEqual(per(date(2026, 11, 1), "month+ly")[1], (date(2025, 11, 1), date(2025, 11, 14)))
        self.assertEqual(analytics.split_mode("bogus+ly"), ("week", "last_year"))

    def test_comparison_specs(self):
        sp, _ = comparisons.specs(date(2026, 10, 1), T, "quarter")
        self.assertEqual((sp[0]["from"], sp[0]["to"]), (date(2026, 7, 1), date(2026, 8, 14)))
        sp, roll = comparisons.specs(date(2026, 11, 1), date(2026, 11, 10), "range")
        self.assertEqual((sp[0]["from"], sp[0]["to"]), (date(2026, 10, 22), date(2026, 10, 31)))
        self.assertTrue(roll)


if __name__ == "__main__":
    unittest.main()
