"""Exchange rates for the Group view, read from Business Central (Currency Exchange Rates, page 483), never typed.

The rates are those of the company whose local currency is the reporting currency (SALT, in ALL): a row says
that from its Starting Date, Exchange Rate Amount units of the currency are worth Relational Exch. Rate Amount
units of that company's local currency (relational currency blank = local currency). From them:
  * closing rate at a date: the row in force on that date (the latest Starting Date on or before it);
  * average rate of a period: the rate in force on each day, averaged over the days (a rate entered on the
    10th counts from the 10th).
Proposed method, to be approved by Finance: the result at the average rate of the period, the balance sheet at
the closing rate. Until then the converted figures are Pilot. Nothing is written to Business Central.
"""
import re
from datetime import date, timedelta


def _n(k):
    return re.sub(r"[^a-z]", "", str(k).lower())


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


COLS = {"currency": ["currencycode"], "starting": ["startingdate"],
        "amount": ["exchangerateamount"], "relational": ["relationalexchrateamount", "relationalexchangerateamount"],
        "rel_currency": ["relationalcurrencycode"]}


def parse(rows):
    """{currency: [(starting date, rate in local currency per 1 unit)]} sorted by date; and what was not usable."""
    out, skipped = {}, 0
    if not rows:
        return out, skipped, []
    keys = {_n(k): k for k in rows[0].keys() if not str(k).startswith("@")}
    col = {role: next((keys[c] for c in cands if c in keys), None) for role, cands in COLS.items()}
    missing = [r for r in ("currency", "starting", "amount", "relational") if not col[r]]
    if missing:
        return out, len(rows), [f"Currency Exchange Rates has no {', '.join(missing)} column (found: {', '.join(keys.values())})"]
    for r in rows:
        cur = str(r.get(col["currency"]) or "").strip().upper()
        try:
            d = date.fromisoformat(str(r.get(col["starting"]) or "")[:10])
        except ValueError:
            skipped += 1
            continue
        amt, rel = _f(r.get(col["amount"])), _f(r.get(col["relational"]))
        relcur = str(r.get(col["rel_currency"]) or "").strip() if col["rel_currency"] else ""
        if not cur or amt <= 0 or rel <= 0 or relcur:
            skipped += 1
            continue
        out.setdefault(cur, []).append((d, rel / amt))
    for v in out.values():
        v.sort()
    return out, skipped, []


def closing(series, on):
    """(rate, starting date) in force on a date, or (None, None)."""
    best = None
    for d, r in series or []:
        if d <= on:
            best = (r, d)
        else:
            break
    return best or (None, None)


def average(series, a, b):
    """The average of the rate in force on each day of [a, b]; None when a day has no rate."""
    if not series or b < a:
        return None
    total, n, d = 0.0, 0, a
    while d <= b:
        r, _ = closing(series, d)
        if r is None:
            return None
        total += r
        n += 1
        d += timedelta(days=1)
    return total / n
