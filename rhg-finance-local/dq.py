"""Finance tower → Data quality & reconciliation: the checks that say whether the figures of the KPI library can be
trusted for one company and period, and the frozen months.

Every check states what is compared with what, the difference and its status (Pass, Difference, Content required,
Not read). Nothing is corrected here and nothing is written to Business Central.

A month is frozen on request (Finance → Settings permission): the trial balance of the month (net change of every
account in the month and its balance at the month end) and the last G/L entry number at that moment are kept. Every
later look compares Business Central with that snapshot: an account whose figure moved was posted to after the
freeze, and the entries numbered above the frozen one with a posting date up to the month end are those postings
(entry numbers only grow, so this needs no creation time, which the G/L web service does not publish).
"""
import json
import threading
from datetime import datetime

import kpilib
import store

SNAP_FILE = store.DATA_DIR / "dq_snapshots.json"
_lock = threading.Lock()
STATUSES = ("Pass", "Difference", "Content required", "Not read", "Info")


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- snapshots of frozen months
def _read():
    try:
        return json.loads(SNAP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(d):
    tmp = SNAP_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
    tmp.replace(SNAP_FILE)


def _key(company, period):
    return f"{str(company).strip().upper()}|{period}"


def snapshot(company, period):
    with _lock:
        return _read().get(_key(company, period))


def snapshots(company):
    pre = f"{str(company).strip().upper()}|"
    with _lock:
        d = _read()
    return sorted(({**v, "period": k.split("|", 1)[1]} for k, v in d.items() if k.startswith(pre)),
                  key=lambda x: x["period"], reverse=True)


def tb_of(rows):
    """{account: [net change of the period, balance at its end]} of the posting accounts."""
    out = {}
    for r in rows or []:
        if r.get("Account_Type") not in (None, "Posting"):
            continue
        no = str(r.get("No") or "")
        net, bal = round(_f(r.get("Net_Change")), 2), round(_f(r.get("Balance_at_Date")), 2)
        if no and (net or bal):
            out[no] = [net, bal]
    return out


def freeze(company, period, date_from, date_to, rows, entry_no, actor, names=None):
    snap = {"at": datetime.now().isoformat(timespec="seconds"), "by": actor, "from": date_from, "to": date_to,
            "entry_no": entry_no, "accounts": tb_of(rows),
            "names": {str(r.get("No")): r.get("Name") or "" for r in rows or [] if r.get("No")} if names is None
            else names}
    with _lock:
        d = _read()
        d[_key(company, period)] = snap
        _write(d)
    store.audit("dq_freeze", f"{company}|{period}", actor, None,
                {"entry_no": entry_no, "accounts": len(snap["accounts"])}, "month frozen")
    return snap


def unfreeze(company, period, actor):
    with _lock:
        d = _read()
        old = d.pop(_key(company, period), None)
        _write(d)
    if old:
        store.audit("dq_freeze", f"{company}|{period}", actor, {"entry_no": old.get("entry_no")}, None,
                    "freeze removed")
    return bool(old)


def compare(snap, rows):
    """The accounts whose net change of the month or balance at its end is no longer what was frozen."""
    now = tb_of(rows)
    names = dict(snap.get("names") or {})
    names.update({str(r.get("No")): r.get("Name") or "" for r in rows or [] if r.get("No")})
    out = []
    for no in sorted(set(now) | set(snap["accounts"])):
        a, b = snap["accounts"].get(no, [0.0, 0.0]), now.get(no, [0.0, 0.0])
        dn, db = round(b[0] - a[0], 2), round(b[1] - a[1], 2)
        if abs(dn) >= 0.5 or abs(db) >= 0.5:
            out.append({"account": no, "name": names.get(no, ""), "net_frozen": a[0], "net_now": b[0],
                        "net_change": dn, "bal_frozen": a[1], "bal_now": b[1], "bal_change": db})
    out.sort(key=lambda x: -max(abs(x["net_change"]), abs(x["bal_change"])))
    return out


# ---------------------------------------------------------------- the checks
def row(key, check, compared, expected, found, status, detail="", link="", unit="money"):
    diff = None
    if isinstance(expected, (int, float)) and isinstance(found, (int, float)):
        diff = round(found - expected, 2)
    return {"key": key, "check": check, "compared": compared, "expected": expected, "found": found,
            "difference": diff, "status": status, "detail": detail, "link": link, "unit": unit}


def ledger_check(key, label, gl, ledger, materiality, link, what, gl_what):
    if gl is None:
        return row(key, label, f"{gl_what} against {what}", None, None, "Content required",
                   f"No account is mapped to {gl_what} (Settings → KPI mapping).", link)
    if ledger is None:
        return row(key, label, f"{gl_what} against {what}", None, None, "Not read",
                   f"{what} could not be read from Business Central.", link)
    d = gl - ledger
    return row(key, label, f"{gl_what} (G/L) against {what}, today", ledger, gl,
               "Pass" if abs(d) < max(1.0, materiality) else "Difference",
               "" if abs(d) < 1 else
               "The ledger entries post to accounts other than the mapped ones (advances, group companies, other "
               "posting groups), or entries were posted to the G/L directly. The difference needs an explanation.",
               link)


def findings(period, checks, late):
    """Failed checks of a month for the Exceptions register (source "data_quality"); subject starts with the
    period so a later look at the same month closes what is fixed."""
    out = []
    for c in checks:
        if c["status"] != "Difference":
            continue
        value = abs(c["difference"] if c["difference"] is not None else (c["found"] or 0))
        out.append({"subject": f"{period} {c['key']}", "kind": c["key"], "location": "",
                    "title": f"{period}: {c['check']}", "detail": (c["compared"] + ". " + (c["detail"] or "")).strip(),
                    "severity": "critical" if c["key"] in ("bs_check", "pl_check") else "warning",
                    "value": value})
    for x in late[:20]:
        out.append({"subject": f"{period} late {x['account']}", "kind": "late_posting", "location": "",
                    "title": f"{period}: {x['account']} {x['name']} changed after the month was frozen",
                    "detail": f"Net change of the month {x['net_frozen']:,.0f} → {x['net_now']:,.0f}; balance at the "
                              f"month end {x['bal_frozen']:,.0f} → {x['bal_now']:,.0f}.",
                    "severity": "warning", "value": max(abs(x["net_change"]), abs(x["bal_change"]))})
    return out


def unmapped_rows(P, B):
    """The accounts with a figure that no line of the mapping takes: income statement (net change of the period)
    and balance sheet (balance at its end)."""
    out = [{"account": no, "name": n, "statement": "Income statement", "value": v}
           for no, n, v, _l in (P or {}).get("unmapped", []) if abs(v) >= 0.5]
    out += [{"account": no, "name": n, "statement": "Balance sheet", "value": v}
            for no, n, v, _l in (B or {}).get("unmapped", []) if abs(v) >= 0.5]
    out.sort(key=lambda x: -abs(x["value"]))
    return out


def budget_check(budget_rows, m, P):
    """The budget of the period through the same mapping: its total by line, and the budget on accounts the mapping
    does not take."""
    if not budget_rows:
        return None
    acc = {}
    for b in budget_rows:
        no = str(b.get("G_L_Account_No") or "")
        acc[no] = acc.get(no, 0.0) + _f(b.get("Amount"))
    Bp = kpilib.pl_lines([{"No": no, "Net_Change": v, "Income_Balance": "Income Statement", "Account_Type": "Posting"}
                          for no, v in acc.items()], m)
    off = [(no, v) for no, v in acc.items() if kpilib.classifier(m)(no) not in kpilib.PL_LINES and abs(v) >= 0.5]
    return {"rows": len(budget_rows), "accounts": len(acc), "net_profit": Bp["net_profit"],
            "revenue": Bp["lines"]["revenue"], "unmapped": sorted(off, key=lambda x: -abs(x[1]))[:20],
            "unmapped_value": round(sum(v for _no, v in off), 2)}
