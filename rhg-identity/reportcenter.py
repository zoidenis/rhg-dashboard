"""Reports center: every report pulse sends, in one place, to see which are on, which are paused, which are failing
or late, when each goes next, how the last sends went and who receives it, and to pause or resume one.

What it gathers (each keeps its own settings, schedule and log where it was made):
  * Finance and Purchasing: the emails each tower sends on its own (their /internal/reports/catalog, reportcat.py);
  * the scheduled screens (reports.py): any screen of pulse sent by email by the report sender;
  * the Executive Action Center's daily email to the owners, and Unicorn's morning brief.
Each item gets a state worked out the same way for all: Paused, Failing (the last send, or part of it, failed),
Skipped (the last run sent nothing: nobody to send to, or nothing to report), Late (on, and nothing went for longer
than its schedule allows), Not sent yet (on, never sent), Active.
"""
from datetime import datetime, timedelta

import requests

import actioncenter
import auth
import config
import reports
import store
import unicorn

TOWER_PERM = {"finance": "finance.settings.manage", "purchasing": "purchasing.settings.manage"}
VIEW_PERMS = ("reports.manage", "finance.settings.manage", "purchasing.settings.manage", "actioncenter.manage",
              "admin.users.manage")
# where each email is set: the tab of Settings (static/settings.html) that holds its card
SETTINGS_TAB = {("finance", "arap_daily"): "em-arap", ("finance", "negative_daily"): "em-negative",
                ("finance", "stockstop_daily"): "em-stockstop", ("finance", "sevenrooms_daily"): "em-sevenrooms",
                ("finance", "fiscal_daily"): "conn-tax", ("finance", "register_owners"): "em-owners-fin",
                ("finance", "cost_weekly"): "em-cost", ("finance", "sales_weekly"): "em-sales",
                ("finance", "brand_weekly"): "em-brand", ("finance", "brand_monthly"): "em-brand",
                ("finance", "pl_monthly"): "em-pl", ("purchasing", "purchasing_daily"): "em-purchasing",
                ("purchasing", "purchasing_weekly"): "em-purchasing", ("purchasing", "savings_monthly"): "em-purchasing",
                ("purchasing", "price_changes"): "em-pricechg", ("purchasing", "register_owners"): "em-owners-pur"}
STATES = ("failing", "late", "skipped", "waiting", "ok", "combined", "paused", "unknown")


def can_view(user):
    return any(auth.can(user, p) for p in VIEW_PERMS)


def _dt(s):
    try:
        return datetime.fromisoformat(str(s)[:19])
    except (TypeError, ValueError):
        return None


def _gap_days(schedule):
    """How many days may pass between two sends of a schedule, plus a margin."""
    if "days" in schedule:
        days = sorted(schedule.get("days") or [])
        if not days:
            return None
        gaps = [((days[(i + 1) % len(days)] - d) % 7) or 7 for i, d in enumerate(days)]
        return max(gaps) + 1
    if "weekly" in schedule:
        return 8
    if "monthly" in schedule:
        return 35
    return None


def health(it, now):
    """The item's state and the counts of its last seven days, from its runs (tests left out)."""
    real = [r for r in it.get("runs") or [] if not r.get("test")]
    week = [r for r in real if (_dt(r["at"]) or now) >= now - timedelta(days=7)]
    it["sent_7d"] = sum(r["status"] == "sent" for r in week)
    it["failed_7d"] = sum(r["status"] == "failed" for r in week)
    it["skipped_7d"] = sum(r["status"] == "skipped" for r in week)
    last = real[0] if real else None
    batch = [r for r in real if last and r["at"][:10] == last["at"][:10]]       # the sends of the same day
    sent = next((r for r in real if r["status"] == "sent"), None)
    it["last"] = last
    it["last_sent_at"] = sent["at"] if sent else None
    it["last_batch"] = {"sent": sum(r["status"] == "sent" for r in batch),
                        "failed": sum(r["status"] == "failed" for r in batch),
                        "skipped": sum(r["status"] == "skipped" for r in batch),
                        "to": len({a.lower() for r in batch if r["status"] == "sent" for a in r.get("to") or []}) or
                        sum(r.get("to_n") or 0 for r in batch if r["status"] == "sent")}
    gap = _gap_days(it.get("schedule") or {})
    if it.get("error"):
        st = "unknown"
    elif not it.get("enabled"):
        st = "paused"
    elif it.get("combined"):
        st = "combined"
    elif it.get("logged") is False:
        st = "ok"
    elif not last:
        st = "waiting"
    elif it["last_batch"]["failed"]:
        st = "failing"
    elif not it["last_batch"]["sent"] and it["last_batch"]["skipped"]:
        st = "skipped"
    elif gap and (now - (_dt(last["at"]) or now)) > timedelta(days=gap):
        st = "late"
    else:
        st = "ok"
    it["state"] = st
    return it


# ---------------------------------------------------------------- the towers
def _tower(app):
    _, base = actioncenter.TOWERS[app]
    r = requests.get(base + "/internal/reports/catalog", timeout=25, headers={"X-Service-Token": config.SERVICE_TOKEN})
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json()


def _routing():
    d = actioncenter._directory() or {}
    labels = {x["key"]: x["label"] for x in d.get("roles") or []}
    out = {}
    for k, v in (d.get("routing") or {}).items():
        out[k] = {"to": [labels.get(x, x) for x in v.get("to") or []], "cc": [labels.get(x, x) for x in v.get("cc") or []]}
    return out


def _tower_items(app, routing, towers):
    label = actioncenter.TOWERS[app][0]
    try:
        cat = _tower(app)
        towers[app] = {"name": label, "ok": True, "mail": cat.get("mail"), "read_at": cat.get("read_at")}
    except Exception as exc:                                        # noqa: BLE001
        towers[app] = {"name": label, "ok": False, "error": str(exc)[:200]}
        return []
    out = []
    for x in cat.get("items") or []:
        for r in x.get("runs") or []:
            r["to_n"], r["cc_n"] = len(r.get("to") or []), len(r.get("cc") or [])
        out.append(x | {"id": f"{app}:{x['key']}", "app": app, "tower": label, "kind": "email",
                        "roles": routing.get(x.get("routing") or "") if x.get("routing") else None,
                        "link": "/settings#" + SETTINGS_TAB.get((app, x["key"]), "reports-center")})
    return out


# ---------------------------------------------------------------- scheduled screens, the Action Center, Unicorn
def _screen_items(user):
    v = reports.view(user)
    runs = {}
    for r in v.get("runs") or []:
        runs.setdefault(r.get("report"), []).append(r)
    names = {u["username"]: u.get("name") or u["username"] for u in v.get("people") or []}
    out = []
    for r in v.get("reports") or []:
        f = r.get("freq")
        sch = {"daily": {"days": list(range(7))}, "weekdays": {"days": list(range(5))},
               "weekly": {"weekly": r.get("day", 0)}, "monthly": {"monthly": r.get("day") or "last day"}}.get(f, {})
        sch = sch | {"time": r.get("time") or ""}
        rr = []
        for x in runs.get(r["id"], [])[:25]:
            st = x.get("status")
            rr.append({"at": str(x.get("finished_at") or x.get("claimed_at") or x.get("at") or "")[:19],
                       "status": "sent" if st == "sent" else "failed" if st in ("failed", "partial") else "skipped",
                       "partial": st == "partial", "target": r.get("company") or "", "to": [], "cc": [],
                       "to_n": x.get("to") or 0, "cc_n": 0, "test": bool(x.get("test")),
                       "actor": names.get(x.get("by"), x.get("by") or "scheduler"), "detail": str(x.get("detail") or "")[:300]})
        rr.sort(key=lambda z: z["at"], reverse=True)
        app = r.get("app") or "platform"
        out.append({"id": "screen:" + r["id"], "key": r["id"], "app": app,
                    "tower": {"finance": "Finance", "purchasing": "Purchasing"}.get(app, "Platform"),
                    "kind": "screen", "name": r.get("name") or r.get("label"), "screen": r.get("label"),
                    "company": r.get("company"), "freq": {"weekdays": "daily"}.get(f, f), "schedule": sch,
                    "enabled": bool(r.get("enabled")), "next_run": r.get("next"), "runs": rr,
                    "people": [names.get(u, u) for u in r.get("recipients") or []], "emails": r.get("emails") or [],
                    "queued": bool(r.get("queued")), "missing": bool(r.get("missing")),
                    "error": "The screen is no longer in pulse." if r.get("missing") else None,
                    "created_by": names.get(r.get("created_by"), r.get("created_by")), "created_at": r.get("created_at"),
                    "link": "/settings#scheduled"})
    return out, v.get("reporter") or {}


def _digest_item(now):
    s = actioncenter.settings()
    g, last = s.get("digest") or {}, s.get("last_digest") or {}
    runs = [{"at": str(last.get("at") or "")[:19], "status": x.get("status") if x.get("status") in ("sent", "failed") else "sent",
             "target": x.get("owner") or "", "to": [x.get("owner")] if x.get("owner") else [], "cc": x.get("cc") or [],
             "to_n": 1, "cc_n": len(x.get("cc") or []), "test": bool(last.get("test")), "actor": last.get("by") or "scheduler",
             "detail": (f"{x.get('items', 0)} items, {x.get('overdue', 0)} past the deadline" if x.get("status") == "sent"
                        else str(x.get("detail") or ""))} for x in last.get("sent") or []]
    nxt = None
    if g.get("enabled"):
        hh, mm = (int(v) for v in (g.get("time") or "08:30").split(":"))
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        done = str(last.get("at") or "")[:10] == now.date().isoformat() and not last.get("test")
        if done or now > t + timedelta(hours=6):
            t += timedelta(days=1)
        nxt = t.isoformat(timespec="minutes")
    return {"id": "platform:ac_digest", "key": "ac_digest", "app": "platform", "tower": "Platform", "kind": "email",
            "name": "Executive Action Center · the owners' daily email", "freq": "daily",
            "schedule": {"days": list(range(7)), "time": g.get("time") or "08:30"}, "enabled": bool(g.get("enabled")),
            "next_run": nxt, "runs": runs, "link": "/settings#ac-matrix",
            "note": "One email per owner of open items, every register and Unicorn; escalations in Cc."}


def _brief_item():
    st = unicorn.state()
    rows = store.q("SELECT day, state, tries, at, error FROM unicorn_runs ORDER BY day DESC LIMIT 25")
    runs = [{"at": str(r[3] or r[0])[:19], "status": "sent" if r[1] == "done" else "failed" if r[1] == "failed" else "skipped",
             "target": r[0], "to": [], "cc": [], "to_n": 0, "cc_n": 0, "test": False, "actor": "Unicorn",
             "detail": str(r[4] or ("Running now" if r[1] == "running" else ""))[:300]} for r in rows]
    t = str(st.get("time") or "08:00")
    return {"id": "platform:unicorn_brief", "key": "unicorn_brief", "app": "platform", "tower": "Platform",
            "kind": "brief", "name": "Unicorn · morning brief", "freq": "daily",
            "schedule": {"days": list(range(7)), "time": t}, "enabled": bool(st.get("enabled")), "next_run": None,
            "runs": runs, "link": "/unicorn#brief",
            "note": "" if st.get("enabled") else "Unicorn has no API key on the server.", "fixed": True}


# ---------------------------------------------------------------- what the page asks
def collect(user):
    now = datetime.now()
    towers, items, problems = {}, [], []
    try:
        routing = _routing()
    except Exception:                                               # noqa: BLE001
        routing = {}
    for app in actioncenter.TOWERS:
        if auth.can(user, TOWER_PERM[app]) or auth.can(user, "reports.manage") or auth.can(user, "admin.users.manage"):
            items += _tower_items(app, routing, towers)
    reporter = {}
    if auth.can(user, "reports.manage"):
        try:
            screens, reporter = _screen_items(user)
            items += screens
        except Exception as exc:                                    # noqa: BLE001
            problems.append(f"Scheduled screens: {str(exc)[:160]}")
    if auth.can(user, "actioncenter.manage") or auth.can(user, "admin.users.manage"):
        for build in (lambda: _digest_item(now), _brief_item):
            try:
                items.append(build())
            except Exception as exc:                                # noqa: BLE001
                problems.append(str(exc)[:160])
    for it in items:
        health(it, now)
        it["can_toggle"] = _can_toggle(user, it)
        it["can_send"] = it["kind"] == "screen" and auth.can(user, "reports.manage") and not it.get("missing")
    order = {s: i for i, s in enumerate(STATES)}
    items.sort(key=lambda x: (order.get(x["state"], 9), x["tower"], x["name"] or ""))
    return {"items": items, "towers": towers, "reporter": reporter, "problems": problems,
            "read_at": now.isoformat(timespec="seconds")}


def _can_toggle(user, it):
    if it.get("fixed") or it.get("error") and it["kind"] != "screen":
        return False
    if it["kind"] == "screen":
        return auth.can(user, "reports.manage")
    if it["id"] == "platform:ac_digest":
        return auth.can(user, "actioncenter.manage")
    return bool(it.get("toggle")) and auth.can(user, TOWER_PERM.get(it["app"], "-"))


def toggle(user, iid, enabled):
    actor = user["username"]
    app, _, key = str(iid or "").partition(":")
    if app == "screen":
        if not auth.can(user, "reports.manage"):
            raise PermissionError("Your role cannot schedule reports.")
        reports.set_enabled(key, enabled, actor)
    elif iid == "platform:ac_digest":
        if not auth.can(user, "actioncenter.manage"):
            raise PermissionError("Your role cannot change the Action Center's emails.")
        g = actioncenter.settings()["digest"]
        actioncenter.save_settings({"digest": {**g, "enabled": bool(enabled)}}, actor)
    elif app in actioncenter.TOWERS:
        if not auth.can(user, TOWER_PERM[app]):
            raise PermissionError(f"Your role cannot change the settings of {actioncenter.TOWERS[app][0]}.")
        _, base = actioncenter.TOWERS[app]
        r = requests.post(base + "/internal/reports/toggle", timeout=30, headers={"X-Service-Token": config.SERVICE_TOKEN},
                          json={"key": key, "enabled": bool(enabled), "actor": actor})
        j = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
        if not r.ok:
            raise ValueError(j.get("error") or f"{actioncenter.TOWERS[app][0]} answered HTTP {r.status_code}.")
        store.audit("report resumed" if enabled else "report paused", actor, iid, {"tower": app})
    else:
        raise ValueError("This report cannot be turned on or off here.")
    return True


def send(user, iid, test):
    app, _, key = str(iid or "").partition(":")
    if app != "screen":
        raise ValueError("Send it from its own settings.")
    if not auth.can(user, "reports.manage"):
        raise PermissionError("Your role cannot schedule reports.")
    return reports.send_now(key, user["username"], bool(test))
