"""Scheduled reports: any screen (tab) of pulse sent by email, on a frequency, to the people chosen.

A report is a screen of the catalogue (screens.py), a company, an optional period, a frequency and the people who
receive it. The people are accounts of pulse, chosen by the locations they work in (their access scope). Each one
receives the screen as they themselves see it: the report sender (rhg-reporter, a headless browser) opens the screen
with a short read-only session of that person (auth.report_session), takes a picture and a PDF of it and emails
them. So nobody receives a figure their own access does not show, and a person limited to some restaurants receives
those restaurants only.

People who see the same thing (same role, same access, same language) are rendered once and share one email.

Kept in data/reports.json: the reports, the runs (last 300), what was sent for each scheduled slot, the queue of
"send now" requests and when the report sender last called. Nothing is sent from here: identity prepares the runs,
the report sender claims them (/internal/reports/claim), renders, sends through the Purchasing mail server and says
how it went (/internal/reports/done).
"""
import json
import secrets
import threading
from datetime import date, datetime, timedelta
from urllib.parse import quote

import auth
import config
import screens
import store

FILE = config.BASE_DIR / "data" / "reports.json"
_lock = threading.RLock()

FREQUENCIES = {"daily": "Every day", "weekdays": "Every working day (Mon–Fri)", "weekly": "Every week",
               "monthly": "Every month"}
PERIODS = {"screen": "As the screen opens", "prev_month": "Previous month", "mtd": "Month to date",
           "ytd": "Year to date"}
PERIOD_APPS = ("finance", "platform")          # the towers that take a period in the address
WINDOW_HOURS = 6                               # a slot missed by more than this is not sent late
RETRY_MINUTES, MAX_TRIES, LEASE_MINUTES = 20, 3, 25
MAX_RECIPIENTS = 60
NOT_REPORTS = {"finance.s.settings", "purchasing.s.settings"}
PUBLIC_URL = (config.get("PUBLIC_URL", "https://dashboard.rhg.al") or "").rstrip("/")


# ---------------------------------------------------------------- storage
def _read():
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    for k, v in (("reports", {}), ("runs", []), ("done", {}), ("tries", {}), ("queue", []), ("active", {}),
                 ("reporter", {})):
        d.setdefault(k, v)
    return d


def _write(d):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(FILE)


def _now():
    return datetime.now().replace(microsecond=0)


# ---------------------------------------------------------------- the catalogue of reports
def catalogue():
    """Every screen that can be sent: [{key, app, tower, menu, tab, label, kind, view, bar, tab_key, period}]."""
    out = []
    for t in screens.catalogue():
        for m in t["menus"]:
            items = [(x["key"], x["tab"], x["label"], x["kind"]) for x in m["tabs"]] or \
                    [(m["key"], None, None, m["kind"])]
            for key, tab, tlabel, kind in items:
                if key in NOT_REPORTS:
                    continue
                parts = [t["label"], m["label"]] + ([tlabel] if tlabel and tlabel != m["label"] else [])
                out.append({"key": key, "app": t["app"], "tower": t["label"], "menu": m["label"],
                            "tab": tlabel or "", "label": " › ".join(parts), "kind": kind or "company",
                            "view": m["view"], "bar": m["bar"], "tab_key": tab,
                            "period": t["app"] in PERIOD_APPS})
    return out


def _screen(key):
    return next((s for s in catalogue() if s["key"] == key), None)


_sq = {"key": None, "words": {}}


def tr(text, lang):
    """The Albanian of a label (i18n/sq.json), for the subject and the email."""
    if lang != "sq":
        return text
    f = config.BASE_DIR / "i18n" / "sq.json"
    try:
        k = f.stat().st_mtime
        if _sq["key"] != k:
            _sq.update(key=k, words=json.loads(f.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return text
    return _sq["words"].get(text) or text


def title(screen, lang="en"):
    return " › ".join(tr(p, lang) for p in [screen["menu"]] + ([screen["tab"]] if screen["tab"] else []))


# ---------------------------------------------------------------- who may receive a report
def _covers(scope, company, kind):
    """Does an access scope cover the company (all of it for a company screen)? (bool, locations or "*")"""
    comps = scope.get("companies", "*")
    if comps != "*" and company not in comps:
        return False, None
    locs = (scope.get("locations") or {}).get(company, "*")
    if kind == "company" and locs != "*":
        return False, locs
    return True, locs


def people(screen_key=None, company=None):
    """The accounts with what the report tool needs: name, email, role, language, their locations of each company
    and the screens they hold. With a screen and a company: whether each may receive it, and why not."""
    s = _screen(screen_key) if screen_key else None
    out = []
    for row in store.q("SELECT * FROM users ORDER BY name"):
        u = auth.public(row)
        held = {p for p in u["permissions"] if ".s." in p}
        e = {"username": u["username"], "name": u["name"], "email": u["email"] or "", "role": u["role"],
             "active": u["active"], "lang": u["lang"], "scope": u["scope"], "screens": sorted(held)}
        if s and company:
            why = ""
            ok, _locs = _covers(u["scope"], company, s["kind"])
            if not u["active"]:
                why = "Account disabled"
            elif not (u["email"] or "").strip():
                why = "No email address on the account"
            elif s["key"] not in held:
                why = "The role does not open this screen"
            elif not ok:
                why = ("This screen shows the whole company; the account covers some locations only"
                       if _locs not in (None, "*") else f"The account does not cover {company}")
            e["eligible"], e["why"] = not why, why
        out.append(e)
    return out


def cut_of(r, s):
    """(company, [codes]) when the report is cut to some locations (a location screen only), else None."""
    locs = r.get("locations") or []
    return (r["company"], locs) if locs and s and s["kind"] == "location" else None


def _eligible(row, s, company, cut=None):
    u = auth.public(row)
    if not u["active"]:
        return None, "Account disabled"
    if not (u["email"] or "").strip():
        return None, "No email address"
    if s["key"] not in u["permissions"]:
        return None, "The role does not open this screen"
    ok, _l = _covers(u["scope"], company, s["kind"])
    if not ok:
        return None, f"The access does not cover {company}" + (" in full" if _l not in (None, "*") else "")
    if cut:
        sc = auth.narrow(u["scope"], *cut)
        if not sc:
            return None, "Does not work at the report's locations"
        u = dict(u, scope=sc)                    # what this person will see of the report
    return u, ""


# ---------------------------------------------------------------- reports
def _clean(body, old=None):
    s = _screen(str(body.get("screen") or ""))
    if not s:
        raise ValueError("Choose the screen to send.")
    company = str(body.get("company") or "").strip()[:80]           # as Business Central names it
    if not company:
        raise ValueError("Choose the company.")
    freq = body.get("freq") if body.get("freq") in FREQUENCIES else None
    if not freq:
        raise ValueError("Choose how often it goes.")
    hh, _, mm = str(body.get("time") or "08:00").partition(":")
    try:
        hh, mm = int(hh), int(mm or 0)
        assert 0 <= hh < 24 and 0 <= mm < 60
    except (ValueError, AssertionError):
        raise ValueError("The time must be HH:MM.") from None
    day = 0
    if freq == "weekly":
        try:
            day = max(0, min(6, int(body.get("day") or 0)))
        except (TypeError, ValueError):
            day = 0
    elif freq == "monthly":
        try:
            day = int(body.get("day") or 1)
        except (TypeError, ValueError):
            day = 1
        day = day if 1 <= day <= 28 else 0          # 0 = the last day of the month
    period = body.get("period") if body.get("period") in PERIODS and s["period"] else "screen"
    names = []
    for x in body.get("recipients") or []:
        x = str(x).strip().lower()[:80]
        if x and x not in names:
            names.append(x)
    if len(names) > MAX_RECIPIENTS:
        raise ValueError(f"At most {MAX_RECIPIENTS} people per report.")
    locs = sorted({str(x).strip()[:40] for x in (body.get("locations") or []) if str(x).strip()})[:100]
    now = _now().isoformat()
    enabled = bool(body.get("enabled", True))
    r = {"screen": s["key"], "company": company, "freq": freq, "day": day, "time": f"{hh:02d}:{mm:02d}",
         "period": period, "recipients": names, "locations": locs,
         "name": str(body.get("name") or "").strip()[:120], "note": str(body.get("note") or "").strip()[:600],
         "pdf": bool(body.get("pdf", True)), "enabled": enabled}
    if old:
        r.update(id=old["id"], created_at=old.get("created_at"), created_by=old.get("created_by"),
                 armed_at=old.get("armed_at") if old.get("enabled") or not enabled else now)
    else:
        r.update(id="r" + secrets.token_hex(5), created_at=now, armed_at=now)
    return r


def save(body, actor):
    with _lock:
        d = _read()
        old = d["reports"].get(str(body.get("id") or "")) if body.get("id") else None
        if body.get("id") and not old:
            raise ValueError("That report no longer exists.")
        r = _clean(body, old)
        if not old:
            r["created_by"] = actor
        r.update(updated_at=_now().isoformat(), updated_by=actor)
        d["reports"][r["id"]] = r
        _write(d)
    store.audit("report saved", actor, r["id"], {"screen": r["screen"], "company": r["company"], "freq": r["freq"],
                                                 "time": r["time"], "recipients": len(r["recipients"]),
                                                 "enabled": r["enabled"]})
    return r


def delete(rid, actor):
    with _lock:
        d = _read()
        r = d["reports"].pop(str(rid), None)
        d["queue"] = [q for q in d["queue"] if q["report"] != rid]
        _write(d)
    if r:
        store.audit("report deleted", actor, rid, {"screen": r["screen"], "company": r["company"]})
    return bool(r)


# ---------------------------------------------------------------- the schedule
def _hm(r):
    hh, _, mm = r.get("time", "08:00").partition(":")
    return int(hh), int(mm)


def _month_day(y, m, day):
    last = ((date(y, m, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
    return date(y, m, min(day, last) if day else last)


def _slot_on(r, d):
    """The time the report goes on day d, or None when it does not go that day."""
    f, wd = r["freq"], d.weekday()
    if f == "weekdays" and wd > 4 or f == "weekly" and wd != r.get("day", 0) \
            or f == "monthly" and d != _month_day(d.year, d.month, r.get("day", 1)):
        return None
    hh, mm = _hm(r)
    return datetime(d.year, d.month, d.day, hh, mm)


def last_slot(r, now):
    for i in range(0, 32):
        t = _slot_on(r, now.date() - timedelta(days=i))
        if t and t <= now:
            return t
    return None


def next_slot(r, now):
    for i in range(0, 33):
        t = _slot_on(r, now.date() + timedelta(days=i))
        if t and t > now:
            return t
    return None


def _key(r, t):
    return f"{r['id']}@{t.isoformat(timespec='minutes')}"


def due(d, now):
    """[(report, slot)] whose slot has come, within the window, not yet sent or tried too often."""
    out = []
    for r in d["reports"].values():
        if not r.get("enabled") or not r.get("recipients"):
            continue
        t = last_slot(r, now)
        if not t or now - t > timedelta(hours=WINDOW_HOURS) or t.isoformat() < (r.get("armed_at") or ""):
            continue
        k = _key(r, t)
        if k in d["done"] or any(a.get("key") == k for a in d["active"].values()):
            continue
        tr_ = d["tries"].get(k) or {}
        if tr_.get("count", 0) >= MAX_TRIES or tr_.get("at") and \
                (now - datetime.fromisoformat(tr_["at"])) < timedelta(minutes=RETRY_MINUTES):
            continue
        out.append((r, t))
    return out


# ---------------------------------------------------------------- runs
def address(r, s, now=None):
    """The address of the screen, with the company and the period where the tower takes them."""
    now = now or _now()
    first = now.date().replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    co = quote(r["company"])
    if s["app"] == "finance":
        q = f"?company={co}"
        if r.get("period") == "prev_month":
            q += f"&mode=month&period={prev.isoformat()}"
        elif r.get("period") == "mtd":
            q += f"&mode=month&period={first.isoformat()}"
        elif r.get("period") == "ytd":
            q += f"&mode=ytd&period={first.isoformat()}"
        return f"/finance/{q}#{s['view']}"
    if s["app"] == "purchasing":
        return f"/purchasing/?company={co}#{s['view']}"
    if s["view"] == "executive":
        q = f"?company={co}&year={now.year}&tab={s['tab_key'] or 'overview'}"
        if r.get("period") == "prev_month":
            q = f"?company={co}&year={prev.year}&mode=month&end={prev.strftime('%Y-%m')}&tab={s['tab_key']}"
        elif r.get("period") == "mtd":
            q += f"&mode=month&end={first.strftime('%Y-%m')}"
        elif r.get("period") == "ytd":
            q += "&mode=ytd"
        return "/executive" + q
    return "/"


def period_label(r, now=None):
    now = now or _now()
    first = now.date().replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    return {"prev_month": prev.strftime("%m.%Y"), "mtd": f"{first.strftime('%m.%Y')} (to date)",
            "ytd": f"{now.year} (year to date)"}.get(r.get("period"), "")


def _groups(r, s, only=None):
    """[{as, lang, to: [{username, name, email}]}] and [{username, why}] of those left out. People with the same
    role, access and language see the same screen, so they share one rendering and one email."""
    groups, skipped = {}, []
    cut = cut_of(r, s)
    for name in only or r["recipients"]:
        row = auth.find(name)
        if not row:
            skipped.append({"username": name, "why": "No such account"})
            continue
        u, why = _eligible(row, s, r["company"], cut)
        if not u:
            skipped.append({"username": name, "why": why})
            continue
        k = (u["role"], json.dumps(u["scope"], sort_keys=True), u["lang"])
        g = groups.setdefault(k, {"as": u["username"], "lang": u["lang"], "to": []})
        g["to"].append({"username": u["username"], "name": u["name"], "email": u["email"].strip()})
    return list(groups.values()), skipped


def _make_run(d, r, slot, actor, test_to=None):
    s = _screen(r["screen"])
    if not s:
        return None
    groups, skipped = _groups(r, s, [test_to] if test_to else None)
    now = _now()
    run = {"id": "x" + secrets.token_hex(6), "report": r["id"], "key": _key(r, slot) if slot else None,
           "slot": slot.isoformat(timespec="minutes") if slot else None, "by": actor, "test": bool(test_to),
           "claimed_at": now.isoformat(), "status": "claimed", "skipped": skipped, "groups": []}
    tokens = []
    for g in groups:
        tok = auth.report_session(g["as"], cut_of(r, s))
        if not tok:
            skipped += [{"username": x["username"], "why": "Account disabled"} for x in g["to"]]
            continue
        tokens.append(tok)
        run["groups"].append({"as": g["as"], "lang": g["lang"], "to": g["to"], "token": tok})
    run["tokens"] = tokens
    return run, s


def claim(reporter_info=None):
    """For the report sender: the runs to do now (scheduled slots that have come, and "send now" requests), each
    with its screen address and, per group of recipients, a short session to open it with."""
    now = _now()
    out = []
    with _lock:
        d = _read()
        d["reporter"] = {"seen_at": now.isoformat(), **({k: str(v)[:80] for k, v in (reporter_info or {}).items()})}
        # runs claimed earlier and never answered: their sessions end, the slot may be tried again
        for rid, a in list(d["active"].items()):
            if now - datetime.fromisoformat(a["claimed_at"]) > timedelta(minutes=LEASE_MINUTES):
                auth.end_report_sessions(a.get("tokens"))
                d["active"].pop(rid, None)
                _log(d, {**a, "status": "failed", "detail": "The report sender did not answer.",
                         "finished_at": now.isoformat()})
        todo = [(r, t, "scheduler", None) for r, t in due(d, now)]
        for q in d["queue"]:
            r = d["reports"].get(q["report"])
            if r:
                todo.append((r, None, q["by"], q.get("test_to")))
        d["queue"] = []
        for r, t, by, test_to in todo:
            made = _make_run(d, r, t, by, test_to)
            if not made:
                continue
            run, s = made
            if t:
                k = run["key"]
                c = (d["tries"].get(k) or {}).get("count", 0)
                d["tries"][k] = {"count": c + 1, "at": now.isoformat()}
            d["active"][run["id"]] = {k: v for k, v in run.items() if k != "groups"} | {
                "groups": [{k: v for k, v in g.items() if k != "token"} for g in run["groups"]]}
            if not run["groups"]:
                d["active"].pop(run["id"], None)
                _log(d, {**run, "status": "failed", "detail": "Nobody of the report can receive it now.",
                         "finished_at": now.isoformat(), "tokens": []})
                continue
            out.append({"run": run["id"], "test": run["test"], "address": address(r, s, now),
                        "public": PUBLIC_URL + address(r, s, now), "app": s["app"], "bar": s["bar"],
                        "tab": s["tab_key"], "screen": s["key"], "company": r["company"],
                        "period": " · ".join(x for x in (period_label(r, now), ", ".join(
                            (cut_of(r, s) or ("", []))[1])) if x), "note": r.get("note") or "", "pdf": r.get("pdf", True),
                        "name": r.get("name") or "",
                        "groups": [{"as": g["as"], "lang": g["lang"], "token": g["token"], "to": g["to"],
                                    "title": r.get("name") or title(s, g["lang"]),
                                    "tower": tr(s["tower"], g["lang"])} for g in run["groups"]]})
        _write(d)
    return {"runs": out}


def _log(d, run):
    run = {k: v for k, v in run.items() if k != "tokens"}
    d["runs"] = (d["runs"] + [run])[-300:]


def done(run_id, results, detail=""):
    """The report sender's answer: results = [{as, status: sent|failed, detail, emails}]."""
    now = _now()
    with _lock:
        d = _read()
        a = d["active"].pop(str(run_id), None)
        if not a:
            return False
        auth.end_report_sessions(a.get("tokens"))
        res = {str(x.get("as")): x for x in results or []}
        for g in a["groups"]:
            x = res.get(g["as"]) or {"status": "failed", "detail": "No answer for this group."}
            g.update(status=x.get("status") if x.get("status") in ("sent", "failed") else "failed",
                     detail=str(x.get("detail") or "")[:300])
        sent = [g for g in a["groups"] if g["status"] == "sent"]
        a["status"] = "sent" if len(sent) == len(a["groups"]) else "partial" if sent else "failed"
        a.update(finished_at=now.isoformat(), detail=str(detail or "")[:300])
        if a.get("key") and a["status"] == "sent":
            d["done"][a["key"]] = now.isoformat()
            d["tries"].pop(a["key"], None)
            d["done"] = dict(list(d["done"].items())[-2000:])
        _log(d, a)
        _write(d)
    store.audit("report sent" if a["status"] == "sent" else "report failed", "reporter", a["report"],
                {"run": run_id, "status": a["status"], "test": a.get("test"),
                 "to": sum(len(g["to"]) for g in sent)})
    return True


def send_now(rid, actor, test):
    """Queues the report for the report sender: to its people, or (test) to the person asking only."""
    with _lock:
        d = _read()
        r = d["reports"].get(str(rid))
        if not r:
            raise ValueError("That report no longer exists.")
        if test:
            row = auth.find(actor)
            sc_ = _screen(r["screen"])
            u, why = _eligible(row, sc_, r["company"], cut_of(r, sc_)) if row else (None, "No account")
            if not u:
                raise ValueError(f"The test goes to you, as you see the screen: {why}.")
        elif not r["recipients"]:
            raise ValueError("Choose who receives it first.")
        if len(d["queue"]) >= 20:
            raise ValueError("Too many reports are waiting to go; try again in a minute.")
        d["queue"].append({"report": r["id"], "by": actor, "test_to": actor if test else None,
                           "at": _now().isoformat()})
        _write(d)
    store.audit("report send now", actor, rid, {"test": bool(test)})
    return {"queued": True}


# ---------------------------------------------------------------- for the report tool
def view(user):
    now = _now()
    with _lock:
        d = _read()
    seen = d["reporter"].get("seen_at")
    online = bool(seen) and now - datetime.fromisoformat(seen) < timedelta(minutes=3)
    last = {}
    for run in d["runs"]:
        last[run["report"]] = run
    out = []
    for r in d["reports"].values():
        s = _screen(r["screen"])
        nxt = next_slot(r, now) if r.get("enabled") else None
        lr = last.get(r["id"])
        out.append(r | {"label": s["label"] if s else r["screen"] + " (no longer in pulse)",
                        "app": s["app"] if s else "", "missing": not s,
                        "next": nxt.isoformat(timespec="minutes") if nxt else None,
                        "queued": any(q["report"] == r["id"] for q in d["queue"]) or any(
                            a["report"] == r["id"] for a in d["active"].values()),
                        "last": {k: lr.get(k) for k in ("status", "finished_at", "test", "detail", "by")} | {
                            "sent_to": sum(len(g["to"]) for g in lr.get("groups", []) if g.get("status") == "sent"),
                            "skipped": lr.get("skipped") or [],
                            "errors": [g.get("detail") for g in lr.get("groups", []) if g.get("status") != "sent"]}
                        if lr else None})
    out.sort(key=lambda x: (x["label"], x["company"]))
    return {"reports": out, "catalogue": catalogue(), "frequencies": FREQUENCIES, "periods": PERIODS,
            "reporter": {"online": online, "seen_at": seen, "queue": len(d["queue"]) + len(d["active"])},
            "runs": [{k: v for k, v in x.items() if k not in ("groups",)} | {
                "to": sum(len(g["to"]) for g in x.get("groups", []) if g.get("status") == "sent")}
                for x in d["runs"][-40:][::-1]],
            "people": people(), "me": user["username"]}
