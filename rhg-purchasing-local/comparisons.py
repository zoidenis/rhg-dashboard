"""Comparison periods and budget benchmark.

Purchases are compared against equivalent, weekday-aligned periods so a Monday
is always compared with a Monday. Budget comes from the G/L budget entries of
the cost-of-goods accounts configured in Settings; actual cost of goods comes
from the matching G/L entries, because a budget is a consumption figure and
purchases are not the same thing.
"""
from calendar import monthrange
from datetime import date, timedelta

import config

OFFSETS = [
    (7, "Previous week", "The same weekdays, 7 days earlier."),
    (14, "Two weeks ago", "The same weekdays, 14 days earlier."),
    (21, "Three weeks ago", "The same weekdays, 21 days earlier."),
    (28, "Same period previous month", "The same weekdays, 28 days earlier (4 weeks), so trading days match."),
    (364, "Same period previous year", "The same weekdays, 364 days earlier (52 weeks), so trading days match."),
]
ROLLING = [7, 14, 21, 28]


def _cost(r):
    c = float(r.get("Cost_Amount_Actual") or 0)
    if config.INCLUDE_EXPECTED_COST:
        c += float(r.get("Cost_Amount_Expected") or 0)
    return c


def total(rows, location=None):
    return sum(_cost(r) for r in rows if not location or r.get("Location_Code") == location)


def shifted(c_from, c_to, days):
    return c_from - timedelta(days=days), c_to - timedelta(days=days)


def _add_months(d, months):
    y, m = divmod((d.year * 12 + d.month - 1) + months, 12)
    return date(y, m + 1, min(d.day, monthrange(y, m + 1)[1]))


def specs(c_from, c_to, mode):
    """Comparison periods for the selected mode, always covering the same number of days."""
    mode = (mode or "week").lower()
    span = (c_to - c_from).days
    out = []
    if mode == "month":
        full_month = c_to.day == monthrange(c_to.year, c_to.month)[1]
        plan = [(1, "Previous month", "The same days of the previous month."),
                (2, "Two months ago", "The same days, two months earlier."),
                (3, "Three months ago", "The same days, three months earlier."),
                (12, "Same period previous year", "The same month a year earlier.")]
        for n, label, method in plan:
            p_from = _add_months(c_from, -n).replace(day=1)
            month_end = p_from.replace(day=monthrange(p_from.year, p_from.month)[1])
            p_to = month_end if full_month else min(p_from + timedelta(days=span), month_end)
            out.append({"offset": n, "label": label, "method": method,
                        "from": p_from, "to": p_to, "rolling": n in (1, 2, 3)})
        return out, "Rolling three-month average"
    if mode in ("ytd", "year"):
        for n, label, method in [(1, "Previous year", "The same days a year earlier."),
                                 (2, "Two years ago", "The same days two years earlier.")]:
            p_from = date(c_from.year - n, 1, 1)
            try:
                p_to = c_to.replace(year=c_to.year - n)
            except ValueError:
                p_to = date(c_to.year - n, 2, 28)
            out.append({"offset": n, "label": label, "method": method,
                        "from": p_from, "to": p_to, "rolling": False})
        return out, None
    for days, label, method in OFFSETS:
        p_from, p_to = shifted(c_from, c_to, days)
        out.append({"offset": days, "label": label, "method": method,
                    "from": p_from, "to": p_to, "rolling": days in ROLLING})
    return out, "Rolling four-week average"


def build(current_total, periods, rolling_label=None):
    """periods: [{offset, label, method, from, to, total, rolling}] -> comparison rows."""
    rows = []
    for p in periods:
        pct = ((current_total - p["total"]) / p["total"] * 100) if p["total"] else None
        rows.append({**p, "difference": current_total - p["total"], "pct": pct})
    roll = [p["total"] for p in periods if p.get("rolling") and p["total"]]
    rolling = sum(roll) / len(roll) if roll and rolling_label else None
    if rolling:
        rows.append({
            "offset": None, "label": rolling_label, "from": None, "to": None,
            "method": f"Average of the {len(roll)} equivalent earlier periods.",
            "total": rolling, "difference": current_total - rolling,
            "pct": (current_total - rolling) / rolling * 100,
        })
    return rows


def prorate_budget(budget_rows, c_from, c_to):
    """G/L budget rows are dated (usually one row per month). Take the share of each
    month that falls inside the period, so a week is compared with a week of budget."""
    days = (c_to - c_from).days + 1
    amount = 0.0
    detail = []
    for r in budget_rows:
        try:
            d = date.fromisoformat((r.get("Date") or "")[:10])
        except ValueError:
            continue
        month_days = monthrange(d.year, d.month)[1]
        m_start, m_end = date(d.year, d.month, 1), date(d.year, d.month, month_days)
        overlap = (min(c_to, m_end) - max(c_from, m_start)).days + 1
        if overlap <= 0:
            continue
        share = float(r.get("Amount") or 0) * overlap / month_days
        amount += share
        detail.append({"account": r.get("G_L_Account_No"), "month": f"{d.year}-{d.month:02d}",
                       "budget_month": float(r.get("Amount") or 0), "days_used": overlap, "share": share})
    return {"amount": abs(amount), "days": days, "detail": detail[:50]}
