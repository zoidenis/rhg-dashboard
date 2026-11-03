"""The rule that spreads the costs kept outside the restaurants (head office, marketing, IT, the central kitchen and
warehouse, anything posted without a restaurant department) over the restaurants, so a restaurant can show a
Contribution. Finance owns it: until it is Approved, a restaurant shows its result before central costs, marked
Partial, and its Contribution is Content required.

The pool is everything of the company that is not on a restaurant department of the Location intelligence registry:
pool result = company EBITDA − Σ of the restaurants' results before central costs. A restaurant's Contribution =
its result before central costs + its share × the pool result, so the Contributions add up to the company EBITDA.
Shares: by net revenue of the period (the proposal), or fixed percentages Finance sets per restaurant.
"""
import json
import threading
from datetime import datetime

import store

FILE = store.DATA_DIR / "allocation.json"
METHODS = {"revenue": "By net revenue of the period", "fixed": "Fixed percentages"}
STATUSES = ("Proposed", "Reviewed", "Approved")
_lock = threading.Lock()


def _norm(company):
    return str(company or "").strip().upper()


def _read():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(d):
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FILE)


def rule(company):
    """The rule of a company; the proposal (by revenue, Proposed) when none was saved."""
    with _lock:
        r = _read().get(_norm(company))
    if r:
        return r
    return {"company": company, "method": "revenue", "shares": {}, "status": "Proposed", "proposal": True,
            "approved_by": "", "approved_at": "", "note": "", "history": []}


def save(company, method, shares, actor, action="save", note=""):
    """action: save (→ Reviewed), approve (→ Approved, needs a note and fixed shares adding up to 100), reset."""
    if method not in METHODS:
        raise ValueError("Unknown method.")
    clean = {}
    for k, v in (shares or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f:
            clean[str(k).strip().upper()] = round(f, 4)
    if action == "approve":
        if not str(note or "").strip():
            raise ValueError("Approving needs a note: who decided and on what basis.")
        if method == "fixed" and abs(sum(clean.values()) - 100) > 0.01:
            raise ValueError(f"The fixed shares add up to {sum(clean.values()):g}%, not 100%.")
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        d = _read()
        key = _norm(company)
        old = d.get(key) or {}
        if action == "reset":
            d.pop(key, None)
            _write(d)
            store.audit("allocation", company, actor, {"status": old.get("status")}, None, "back to the proposal")
            return rule(company)
        r = {"company": company, "method": method, "shares": clean if method == "fixed" else {},
             "status": "Approved" if action == "approve" else "Reviewed",
             "approved_by": actor if action == "approve" else "", "approved_at": now if action == "approve" else "",
             "note": str(note or "")[:500] if action == "approve" else old.get("note", ""),
             "history": (old.get("history") or [])[-30:] + [{"at": now, "by": actor, "action": action,
                                                              "method": method, "note": str(note or "")[:300]}]}
        d[key] = r
        _write(d)
    store.audit("allocation", company, actor, {"status": old.get("status"), "method": old.get("method")},
                {"status": r["status"], "method": method}, note)
    return r


def public(company):
    r = rule(company)
    return {k: r.get(k) for k in ("method", "shares", "status", "approved_by", "approved_at", "note", "proposal")} | \
        {"method_label": METHODS.get(r.get("method"), ""), "approved": r.get("status") == "Approved"}


def shares(r, revenue):
    """{restaurant: share 0..1}. revenue: {restaurant: net revenue of the period}."""
    if r.get("method") == "fixed":
        return {k: v / 100 for k, v in (r.get("shares") or {}).items()}
    pos = {k: v for k, v in revenue.items() if v and v > 0}
    tot = sum(pos.values())
    return {k: v / tot for k, v in pos.items()} if tot else {}


def apply(r, company_ebitda, direct, revenue):
    """direct, revenue: {restaurant: result before central costs, net revenue}. Returns (pool result,
    {restaurant: allocated share of it}, {restaurant: contribution}); contributions only when Approved."""
    pool = company_ebitda - sum(direct.values())
    sh = shares(r, revenue)
    alloc = {k: pool * sh.get(k, 0.0) for k in direct}
    contrib = {k: direct[k] + alloc[k] for k in direct} if r.get("status") == "Approved" else {}
    return pool, alloc, contrib
