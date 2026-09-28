"""Document history the app keeps itself.

Business Central does not record when a purchase order changed status, and it
deletes requisition worksheet lines the moment they are carried out into an
order. So the app snapshots what it sees on every refresh and derives the
timings from its own history. Everything lives in data/history.json.
"""
import json
import threading
from datetime import datetime, timedelta

import config

HISTORY_FILE = config.BASE_DIR / "data" / "history.json"
_lock = threading.Lock()
KEEP_DAYS = 120


def _now():
    return datetime.now().replace(microsecond=0)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _hours(a, b):
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600, 1)


def _load():
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("orders", {})
    data.setdefault("requisitions", {})
    return data


def _save(data):
    HISTORY_FILE.parent.mkdir(exist_ok=True)
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
    tmp.replace(HISTORY_FILE)


def _prune(data, now):
    cutoff = _iso(now - timedelta(days=KEEP_DAYS))
    for bucket in ("orders", "requisitions"):
        data[bucket] = {k: v for k, v in data[bucket].items() if (v.get("last_seen") or "") >= cutoff}


def track_orders(company, pos):
    """Records first sight and status changes of open purchase orders.

    Returns {order_no: {first_seen, status_since, hours_in_status, released_after_hours}}.
    """
    now = _now()
    out = {}
    with _lock:
        data = _load()
        orders = data["orders"]
        for p in pos:
            key = f"{company}|{p['no']}"
            rec = orders.get(key)
            if rec is None:
                rec = orders[key] = {"first_seen": _iso(now), "status": p["status"],
                                     "status_since": _iso(now), "released_at": None}
            if rec.get("status") != p["status"]:
                rec["status"] = p["status"]
                rec["status_since"] = _iso(now)
                if p["status"] == "Released" and not rec.get("released_at"):
                    rec["released_at"] = _iso(now)
            if p["status"] == "Released" and not rec.get("released_at"):
                rec["released_at"] = rec.get("status_since")
            if p["received_not_invoiced"] > 0 and not rec.get("receipt_seen"):
                rec["receipt_seen"] = _iso(now)
            rec["last_seen"] = _iso(now)
            out[p["no"]] = {
                "first_seen": rec["first_seen"],
                "status_since": rec["status_since"],
                "hours_in_status": _hours(_parse(rec["status_since"]), now),
                "hours_tracked": _hours(_parse(rec["first_seen"]), now),
                "released_after_hours": _hours(_parse(rec["first_seen"]), _parse(rec.get("released_at"))),
                "awaiting_invoice_hours": _hours(_parse(rec.get("receipt_seen")), now),
            }
        _prune(data, now)
        _save(data)
    return out


def track_requisitions(company, lines):
    """Records requisition worksheet lines and how long they stay open.

    A line that disappears was carried out into an order (or deleted); the time
    it was open is the closest measurable proxy for requisition-to-PO time,
    because BC keeps no record of the line afterwards.
    """
    now = _now()
    seen, out, closed = set(), {}, []
    with _lock:
        data = _load()
        reqs = data["requisitions"]
        for l in lines:
            key = f"{company}|{l['key']}"
            seen.add(key)
            rec = reqs.get(key)
            if rec is None:
                rec = reqs[key] = {"first_seen": _iso(now), "item": l.get("item"),
                                   "description": l.get("description"), "location": l.get("location"),
                                   "vendor": l.get("vendor"), "quantity": l.get("quantity"),
                                   "user": l.get("user"), "closed_at": None}
            rec["last_seen"] = _iso(now)
            out[l["key"]] = {"first_seen": rec["first_seen"],
                             "hours_open": _hours(_parse(rec["first_seen"]), now)}
        # lines that vanished since the previous refresh were carried out or deleted
        for key, rec in reqs.items():
            if not key.startswith(f"{company}|") or rec.get("closed_at") or key in seen:
                continue
            if rec.get("last_seen") and rec["last_seen"] < _iso(now - timedelta(seconds=30)):
                rec["closed_at"] = rec["last_seen"]
                rec["hours_open"] = _hours(_parse(rec["first_seen"]), _parse(rec["last_seen"]))
        for key, rec in reqs.items():
            if key.startswith(f"{company}|") and rec.get("closed_at"):
                closed.append({"key": key.split("|", 1)[1], "item": rec.get("item"),
                               "description": rec.get("description"), "location": rec.get("location"),
                               "vendor": rec.get("vendor"), "user": rec.get("user"),
                               "first_seen": rec["first_seen"], "closed_at": rec["closed_at"],
                               "hours_open": rec.get("hours_open")})
        _prune(data, now)
        _save(data)
    closed.sort(key=lambda c: c["closed_at"], reverse=True)
    return out, closed[:100]


def stats():
    with _lock:
        data = _load()
    closed = [r for r in data["requisitions"].values() if r.get("closed_at") and r.get("hours_open") is not None]
    return {
        "tracked_orders": len(data["orders"]),
        "tracked_requisitions": len(data["requisitions"]),
        "closed_requisitions": len(closed),
        "avg_requisition_hours": round(sum(r["hours_open"] for r in closed) / len(closed), 1) if closed else None,
        "since": min((r.get("first_seen") for r in data["orders"].values()), default=None),
    }
