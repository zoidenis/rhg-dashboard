"""Reports center (identity): every email this tower sends on its own schedule, in one shape.

Each report keeps its own settings, schedule and run log; this only reads them and puts them side by side, so the
Reports center can show what is on, what is paused, when each goes next, how the last sends went and who gets it.
The same file sits in Finance and in Purchasing; keep the two alike.

An item: {key, name, group, freq, schedule, enabled, next_run, runs, view, card, routing, toggle, note}
  schedule: {"days": [0..6], "time"} | {"weekly": day, "time"} | {"monthly": "first working day" | day, "working", "time"}
  runs: the last sends, newest first: {at, status: sent|failed|skipped, target, to, cc, test, actor, detail}
"""
from datetime import datetime

MAX_RUNS = 25


def _hhmm(t, default):
    t = str(t or "")
    return t if len(t) == 5 and t[2] == ":" and t.replace(":", "").isdigit() else default


def days_schedule(days, time, default="08:00"):
    return {"days": sorted({int(d) for d in days if 0 <= int(d) <= 6}), "time": _hhmm(time, default)}


def weekly_schedule(day, time, default="08:00"):
    try:
        d = max(0, min(6, int(float(day or 0))))
    except (TypeError, ValueError):
        d = 0
    return {"weekly": d, "time": _hhmm(time, default)}


def monthly_schedule(day, time, working=False, default="08:00"):
    return {"monthly": day, "working": bool(working), "time": _hhmm(time, default)}


def _addresses(x):
    if isinstance(x, str):
        x = [p for p in x.replace(";", ",").split(",")]
    return [str(a).strip() for a in (x or []) if str(a or "").strip()][:40]


def run(e, target=None):
    """One send of a report's log, whatever the report wrote: its time, how it went, to whom."""
    e = e or {}
    st = str(e.get("status") or "sent").lower()
    st = "sent" if st in ("sent", "ok") else "skipped" if st == "skipped" else "failed"
    tgt = target if target is not None else (e.get("company") or e.get("target") or e.get("brand") or e.get("label")
                                             or e.get("owner") or "")
    return {"at": str(e.get("ts") or e.get("at") or "")[:19], "status": st, "target": str(tgt)[:120],
            "to": _addresses(e.get("to")), "cc": _addresses(e.get("cc")), "test": bool(e.get("test")),
            "actor": str(e.get("actor") or e.get("by") or "scheduler")[:80], "detail": str(e.get("detail") or "")[:300]}


def runs(entries, keep=None, target=None):
    out = [run(e, target(e) if target else None) for e in (entries or []) if isinstance(e, dict) and (keep is None or keep(e))]
    out.sort(key=lambda r: r["at"], reverse=True)
    return out[:MAX_RUNS]


def item(key, name, group, freq, schedule, enabled, next_run, entries, view, card, routing=None, toggle=None, note="",
         **extra):
    """toggle: (section, field) of the tower's settings that turns the email on and off."""
    return {"key": key, "name": name, "group": group, "freq": freq, "schedule": schedule, "enabled": bool(enabled),
            "next_run": next_run if enabled else None, "runs": entries, "view": view, "card": card,
            "routing": routing, "toggle": list(toggle) if toggle else None, "note": note} | extra


def safe(build, key, name, group, view, card):
    """An item whose state could not be read stays in the list, with the reason."""
    try:
        return build()
    except Exception as exc:                                       # noqa: BLE001
        return item(key, name, group, "", {}, False, None, [], view, card, note="", error=str(exc)[:200])


def catalog(items, mail, tower):
    return {"tower": tower, "items": items, "mail": mail, "read_at": datetime.now().isoformat(timespec="seconds")}


def toggle(items, key, enabled, save, actor):
    """Turns one email on or off through the tower's own settings (save(patch, actor)). Returns the item's key."""
    it = next((x for x in items if x["key"] == key), None)
    if not it or not it.get("toggle"):
        raise ValueError("This email cannot be turned on or off here; open its settings.")
    section, field = it["toggle"]
    save({section: {field: bool(enabled)}}, actor)
    return key
