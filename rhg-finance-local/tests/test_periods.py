"""Comparison periods of the Finance tower (server.periods)."""
from datetime import date

import server

TODAY = date(2026, 11, 14)


def per(anchor, mode):
    r = server.periods(anchor, mode, today=TODAY)
    return (r[0], r[1]), (r[2], r[3]), (r[4], r[5])


def test_quarter_to_date_same_days_of_previous_quarter():
    cur, prev, ly = per("2026-11-14", "quarter")
    assert cur == (date(2026, 10, 1), date(2026, 11, 14))
    assert prev == (date(2026, 7, 1), date(2026, 8, 14))
    assert ly == (date(2025, 10, 1), date(2025, 11, 14))


def test_finished_quarter_against_whole_previous_quarter():
    assert per("2026-08-15", "quarter")[1] == (date(2026, 4, 1), date(2026, 6, 30))
    assert per("2026-02-10", "quarter")[1] == (date(2025, 10, 1), date(2025, 12, 31))
    assert per("2026-05-20", "quarter")[1] == (date(2026, 1, 1), date(2026, 3, 31))


def test_month_to_date_and_finished_month():
    assert per("2026-11-03", "month")[1] == (date(2026, 10, 1), date(2026, 10, 14))
    assert per("2026-02-10", "month")[1] == (date(2026, 1, 1), date(2026, 1, 31))
    assert per("2026-03-10", "month")[1] == (date(2026, 2, 1), date(2026, 2, 28))


def test_range_and_ytd():
    cur, prev, _ = per("2026-11-01..2026-11-10", "range")
    assert cur == (date(2026, 11, 1), date(2026, 11, 10))
    assert prev == (date(2026, 10, 22), date(2026, 10, 31))
    assert per("2026-05-01", "ytd")[1] == (date(2025, 1, 1), date(2025, 11, 14))
