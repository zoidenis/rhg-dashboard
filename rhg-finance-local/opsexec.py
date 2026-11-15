"""Operations & Food Safety: Crunchtime Ops Execution (checklists, food safety logs, waste and yield, sensors) by
restaurant, for the executives (overview), the operational managers (checklists) and the food safety manager (food
safety, waste and yield).

Forms are recognised by their title (RHG names them consistently; the rules are below, KINDS): temperature logs,
frying-oil checks, product traceability (receiving temperatures and whether the product was fit), spoiled product,
yield (firo: gross and net weight after handling), staff consumption, the daily receiving report; every other form of
the Operations category is a checklist (opening, closing, cleaning, kitchen and service procedures). Each kind is read
with the same rules for every location, so a figure means the same thing everywhere.

Temperatures: a reading is a number answer whose question names a temperature, with the equipment type chosen just
before it in the form (cooler or freezer). Out of range: a cooler above 5 °C, a freezer above −10 °C (RHG's own limits,
as the form asks them; changeable on the Connection card). Sensors read in °F and are taken to °C.
Checklists: the tasks due in the period (Ops Execution assigns a task for each recurring checklist): done, done late,
still open past their due time. Completion = done / due.
Nothing is written to Crunchtime; no name of a person is kept or shown.
"""
import re
import threading
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import crunchtime
import locintel

KINDS = [  # (kind, title pattern) in order: the first that matches
    ("temp", re.compile(r"temperature log|temperatur", re.I)),
    ("oil", re.compile(r"vaj gatim|kontroll vaj|riciklim vaj|frying oil", re.I)),
    ("trace", re.compile(r"gjurmueshm|traceab|pranimin e lendes", re.I)),
    ("spoil", re.compile(r"spoil|asgjesim", re.I)),
    ("firo", re.compile(r"\bfiro\b", re.I)),
    ("staff", re.compile(r"konsum stafi|staff consumption", re.I)),
    ("receiving", re.compile(r"receiving kpi|raporti ditor i menaxherit te stokut", re.I)),
]
KIND_LABEL = {"temp": "Temperature logs", "oil": "Frying oil checks", "trace": "Product traceability",
              "spoil": "Spoiled product", "firo": "Yield (firo)", "staff": "Staff consumption",
              "receiving": "Receiving reports", "safety": "Other food safety forms", "hr": "HR audits",
              "checklist": "Checklists and procedures", "other": "Other forms"}
_jobs, _lock = {}, threading.Lock()


def kind_of(t):
    title, cat = t.get("title") or "", (t.get("category_name") or "").strip().lower()
    for k, rx in KINDS:
        if rx.search(title):
            return k
    if cat == "safety":
        return "safety"
    if cat == "human resources":
        return "hr"
    if cat == "operations":
        return "checklist"
    return "other"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _yes(v):
    return v is True or str(v).lower() == "true"


def _no(v):
    return v is False or str(v).lower() == "false"


def _sel(v):
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return str(v or "")


def equip_type(text):
    t = (text or "").lower()
    if re.search(r"ngrir|freez", t):
        return "freezer"
    if re.search(r"fresk|cool|frigorifer|fridge|ftoh", t):
        return "cooler"
    return "other"


# ---------------------------------------------------------------- parsers of each kind (on slim submissions)
def parse_temp(ans, lim):
    """[(equipment type, °C, out?)]"""
    out, eq = [], "other"
    for t, title, v in ans:
        if t == "select" and re.search(r"pajisj|equipment", title, re.I):
            eq = equip_type(_sel(v))
        elif t in ("number", "temperature", "equipment_temperature") and re.search(r"temperat", title, re.I):
            x = _num(v)
            if x is None:
                continue
            bad = (eq == "cooler" and x > lim["cooler_max"]) or (eq == "freezer" and x > lim["freezer_max"])
            out.append((eq, x, bad))
    return out


def parse_oil(ans):
    """(checks answered, over the limit): a question about the oil measurement answered Yes = over 23 (to change)."""
    n = over = 0
    for t, title, v in ans:
        if t == "yesno" and re.search(r"vaj|oil|23", title, re.I) and v not in (None, ""):
            n += 1
            over += 1 if _yes(v) else 0
    return n, over


def _blocks(ans, start_rx):
    """Groups the answers of a repeated block (one product each) that starts with a question matching start_rx."""
    blocks, cur = [], None
    for a in ans:
        if a[0] in ("text", "select") and start_rx.search(a[1]):
            cur = {"start": a}
            blocks.append(cur)
            cur["rows"] = [a]
        elif cur is not None:
            cur["rows"].append(a)
    return blocks


def parse_trace(ans):
    """[(product, supplier, °C, fit: True / False / None)]"""
    out = []
    for b in _blocks(ans, re.compile(r"^produkt", re.I)):
        prod = _sel(b["start"][2]).strip()
        sup, temp, fit = "", None, None
        for t, title, v in b["rows"][1:]:
            if t == "text" and re.search(r"furnitor|supplier", title, re.I):
                sup = str(v or "").strip()
            elif t in ("number", "temperature") and re.search(r"temperat", title, re.I):
                temp = _num(v)
            elif t == "yesno" and re.search(r"pershtatsh|suitable|fit", title, re.I) and v not in (None, ""):
                fit = _yes(v)
        if prod or temp is not None:
            out.append((prod[:80], sup[:80], temp, fit))
    return out


def parse_spoil(ans):
    """[(product, quantity, unit, reason)]"""
    out = []
    for b in _blocks(ans, re.compile(r"emertimi|product", re.I)):
        prod = _sel(b["start"][2]).strip()
        qty, unit, why = None, "", ""
        for t, title, v in b["rows"][1:]:
            if t == "number" and qty is None:
                qty = _num(v)
            elif t == "select" and re.search(r"njesi|unit|list", title, re.I):
                unit = _sel(v).strip()
            elif t == "text" and re.search(r"arsye|reason|problem", title, re.I):
                why = str(v or "").strip()
        if prod and qty:
            out.append((prod[:80], qty, unit[:10], why[:120]))
    return out


def parse_firo(ans):
    """(product, supplier, invoice, gross kg, net kg) or None."""
    prod = sup = inv = ""
    gross = net = None
    for t, title, v in ans:
        if t == "select" and re.search(r"produkt", title, re.I):
            prod = re.sub(r"^\s*\d+\)\s*", "", _sel(v)).strip()
        elif t == "text" and re.search(r"furnitor|supplier", title, re.I):
            sup = str(v or "").strip()
        elif re.search(r"numri i fatur|number of the bill|invoice", title, re.I):
            inv = str(v or "").strip()
        elif t == "number" and re.search(r"bruto|gross", title, re.I):
            gross = _num(v)
        elif t == "number" and re.search(r"neto|net", title, re.I):
            net = _num(v)
    if gross and net is not None and gross > 0:
        return (prod[:80], sup[:80], inv[:30], gross, net)
    return None


def parse_receiving(ans):
    keys = {"invoices": r"numri i faturave", "posted": r"fatura t[eë] postuara", "missing": r"fatura t[eë] munguar",
            "tr_sent": r"transferta te derguara", "tr_posted": r"transferta te postuara"}
    out = {}
    for t, title, v in ans:
        if t != "number":
            continue
        for k, rx in keys.items():
            if re.search(rx, title, re.I) and _num(v) is not None:
                out[k] = out.get(k, 0) + _num(v)
    return out


# ---------------------------------------------------------------- the build
def places(settings=None):
    reg = {r["ref"]: r for r in locintel.registry(settings) if r.get("ref")}
    return reg, crunchtime.mapping(crunchtime.info().get("map"))


def place_of(name, reg, m):
    ref = m.get((name or "").strip().upper())
    if ref is None:
        return "", "support"
    if not ref:
        return "", "left out"
    return (ref, "mapped") if ref in reg else (ref, "needs review")


TZ = ZoneInfo("Europe/Tirane")


def _local(when):
    """An ISO time of Crunchtime (UTC when it carries no zone) as Tirana's local time, 'YYYY-MM-DDTHH:MM'."""
    try:
        t = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return str(when or "")[:16]
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(TZ).strftime("%Y-%m-%dT%H:%M")


def _age_h(when):
    """Hours since an ISO time of Crunchtime (UTC when it carries no zone); 0 when it cannot be read."""
    try:
        t = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def build(a, b, progress=None, today=None):
    today = today or date.today()
    lim = crunchtime.limits(crunchtime.info().get("thresholds"))
    reg, m = places()
    if progress:
        progress("Crunchtime: forms and locations")
    T = crunchtime.templates()
    L = {x["id"]: x["name"] for x in crunchtime.locations()}
    forms = []
    for t in T:
        k = kind_of(t)
        last = ((t.get("date_last_submitted") or {}).get("$date") or 0) / 1000
        forms.append({"id": t.get("id"), "title": t.get("title") or "", "category": (t.get("category_name") or "").strip(),
                      "kind": k, "subs": t.get("num_submissions") or 0,
                      "last": datetime.fromtimestamp(last).date().isoformat() if last else None,
                      "deleted": bool(t.get("deleted"))})
    active = [f for f in forms if not f["deleted"] and f["last"] and f["last"] >= (a - timedelta(days=1)).isoformat()
              and f["kind"] in ("temp", "oil", "trace", "spoil", "firo", "staff", "receiving", "safety")]
    loc = defaultdict(lambda: {"temp_n": 0, "temp_out": 0, "temp_by": defaultdict(lambda: [0, 0, None, None]),
                               "temp_days": defaultdict(lambda: [0, 0]), "temp_subs": 0,
                               "oil_n": 0, "oil_over": 0, "trace_n": 0, "trace_unfit": 0, "trace_temps": [],
                               "spoil": [], "firo": [], "staff_subs": 0, "receiving": defaultdict(float), "receiving_subs": 0,
                               "safety_subs": defaultdict(int), "forms": defaultdict(int)})
    suppliers = defaultdict(lambda: [0, 0, []])       # receiving checks by supplier: products checked, unfit, temperatures
    errors = []
    for i, f in enumerate(active):
        if progress:
            progress(f"Crunchtime: {f['title'][:40]} ({i + 1} of {len(active)})")
        try:
            subs = crunchtime.submissions(f["id"], a, b)
        except crunchtime.Error as exc:
            errors.append(f"{f['title'][:40]}: {exc}")
            continue
        for lid, lname, when, _task, ans in subs:
            name = lname or L.get(lid) or ""
            x = loc[name or "No location"]
            x["forms"][f["title"]] += 1
            day = (when or "")[:10]
            if f["kind"] == "temp":
                x["temp_subs"] += 1
                for eq, c, bad in parse_temp(ans, lim):
                    x["temp_n"] += 1
                    x["temp_out"] += 1 if bad else 0
                    e = x["temp_by"][eq]
                    e[0] += 1
                    e[1] += 1 if bad else 0
                    e[2] = c if e[2] is None else min(e[2], c)
                    e[3] = c if e[3] is None else max(e[3], c)
                    d = x["temp_days"][day]
                    d[0] += 1
                    d[1] += 1 if bad else 0
            elif f["kind"] == "oil":
                n, over = parse_oil(ans)
                x["oil_n"] += n
                x["oil_over"] += over
            elif f["kind"] == "trace":
                for prod, sup, c, fit in parse_trace(ans):
                    x["trace_n"] += 1
                    x["trace_unfit"] += 1 if fit is False else 0
                    if c is not None:
                        x["trace_temps"].append(c)
                    s = suppliers[sup or "Not written"]
                    s[0] += 1
                    s[1] += 1 if fit is False else 0
                    if c is not None:
                        s[2].append(c)
            elif f["kind"] == "spoil":
                for prod, q, unit, why in parse_spoil(ans):
                    x["spoil"].append([day, prod, q, unit, why])
            elif f["kind"] == "firo":
                r = parse_firo(ans)
                if r:
                    x["firo"].append([day] + list(r))
            elif f["kind"] == "staff":
                x["staff_subs"] += 1
            elif f["kind"] == "receiving":
                x["receiving_subs"] += 1
                for k, v in parse_receiving(ans).items():
                    x["receiving"][k] += v
            elif f["kind"] == "safety":
                x["safety_subs"][f["title"]] += 1
    # tasks: the checklists due
    if progress:
        progress("Crunchtime: tasks")
    try:
        TK = crunchtime.tasks(a, b)
    except crunchtime.Error as exc:
        TK = []
        errors.append(f"tasks: {exc}")
    now = datetime.now().isoformat()[:16]
    tk = defaultdict(lambda: {"due": 0, "done": 0, "late": 0, "overdue": 0, "by": defaultdict(lambda: [0, 0, 0, 0]),
                              "days": defaultdict(lambda: [0, 0])})
    for lid, lname, title, proj, due, done, late, open_, deleted in TK:
        if deleted:
            continue
        x = tk[lname or L.get(lid) or "No location"]
        x["due"] += 1
        x["done"] += 1 if done else 0
        x["late"] += 1 if done and late else 0
        over = open_ and not done and due and due < now
        x["overdue"] += 1 if over else 0
        key = re.sub(r"\s+", " ", title).strip()[:90]
        y = x["by"][key]
        y[0] += 1
        y[1] += 1 if done else 0
        y[2] += 1 if done and late else 0
        y[3] += 1 if over else 0
        d = x["days"][(due or "")[:10]]
        d[0] += 1
        d[1] += 1 if done else 0
    # sensors: live
    try:
        S = crunchtime.sensors()
    except crunchtime.Error as exc:
        S = []
        errors.append(f"sensors: {exc}")
    sens = []
    for s in S:
        acc = s.get("account") or {}
        eq = equip_type((s.get("placement") or {}).get("name") or s.get("name"))
        f_ = _num(s.get("last_reading_value"))
        c = round((f_ - 32) * 5 / 9, 1) if f_ is not None else None
        bad = c is not None and ((eq == "cooler" and c > lim["cooler_max"]) or (eq == "freezer" and c > lim["freezer_max"]))
        last = s.get("last_reading_date") or ""
        stale = _age_h(last) > 3
        sens.append({"name": s.get("name") or "", "location": (acc.get("name") or "").strip(), "placement": (s.get("placement") or {}).get("name") or "",
                     "type": eq, "c": c, "out": bad, "online": bool(s.get("is_online")), "enabled": bool(s.get("is_enabled")),
                     "stale": stale, "battery": s.get("battery"), "last": _local(last) if last else ""})
    # by restaurant
    names = sorted(set(loc) | set(tk) | {s["location"] for s in sens} | set(L.values()))
    rows, support = [], []
    for n in names:
        ref, how = place_of(n, reg, m)
        r = reg.get(ref) or {}
        x, t = loc.get(n), tk.get(n)
        ss = [s for s in sens if s["location"] == n]
        for s_ in ss:
            s_["restaurant"] = r.get("name") or n
        firo = (x or {}).get("firo") or []
        g = sum(f[4] for f in firo)
        item = {
            "location": n, "ref": ref if how == "mapped" else "", "how": how, "name": r.get("name") or n,
            "company": r.get("company") or "", "bc": r.get("bc") or "", "dept": r.get("dept") or "",
            "tasks": {k: t[k] for k in ("due", "done", "late", "overdue")} if t else {"due": 0, "done": 0, "late": 0, "overdue": 0},
            "completion": round(t["done"] / t["due"] * 100, 1) if t and t["due"] else None,
            "checklists": sorted(([k, v[0], v[1], v[2], v[3]] for k, v in (t["by"] if t else {}).items()), key=lambda z: (z[2] / z[1] if z[1] else 1, -z[1])),
            "task_days": sorted([[k, v[0], v[1]] for k, v in (t["days"] if t else {}).items() if k]),
            "temp_n": (x or {}).get("temp_n", 0), "temp_out": (x or {}).get("temp_out", 0),
            "temp_pct_in": round((1 - x["temp_out"] / x["temp_n"]) * 100, 1) if x and x["temp_n"] else None,
            "temp_by": {k: v for k, v in ((x or {}).get("temp_by") or {}).items()},
            "temp_days": sorted([[k, v[0], v[1]] for k, v in ((x or {}).get("temp_days") or {}).items() if k]),
            "oil_n": (x or {}).get("oil_n", 0), "oil_over": (x or {}).get("oil_over", 0),
            "trace_n": (x or {}).get("trace_n", 0), "trace_unfit": (x or {}).get("trace_unfit", 0),
            "spoil": sorted((x or {}).get("spoil") or [], reverse=True), "firo": sorted(firo, reverse=True),
            "yield_pct": round(sum(f[5] for f in firo) / g * 100, 1) if g else None,
            "staff_subs": (x or {}).get("staff_subs", 0),
            "receiving": dict((x or {}).get("receiving") or {}), "receiving_subs": (x or {}).get("receiving_subs", 0),
            "safety": dict((x or {}).get("safety_subs") or {}), "forms": dict((x or {}).get("forms") or {}),
            "sensors": ss, "sensors_out": sum(1 for s in ss if s["out"]), "sensors_off": sum(1 for s in ss if not s["online"] or s["stale"])}
        (rows if how in ("mapped", "needs review") else support).append(item)
    rows.sort(key=lambda r: r["name"])
    sup_rows = sorted(([k, v[0], v[1], round(sum(v[2]) / len(v[2]), 1) if v[2] else None, max(v[2]) if v[2] else None]
                       for k, v in suppliers.items()), key=lambda z: (-z[2], -z[1]))
    by_kind = defaultdict(lambda: [0, 0, 0])
    for f in forms:
        if f["deleted"]:
            continue
        z = by_kind[f["kind"]]
        z[0] += 1
        z[1] += 1 if f["last"] and f["last"] >= (today - timedelta(days=30)).isoformat() else 0
        z[2] += f["subs"]
    return {"available": True, "period": {"from": a.isoformat(), "to": b.isoformat()}, "limits": lim,
            "locations": rows, "support": support, "sensors": sens, "suppliers": sup_rows[:200],
            "forms": sorted(forms, key=lambda f: (f["kind"], f["title"])), "kinds": {k: by_kind[k] for k in by_kind},
            "kind_label": KIND_LABEL, "errors": errors, "read_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "definitions": DEFINITIONS}


DEFINITIONS = {
    "completion": "Checklist completion = tasks done / tasks due in the period (Ops Execution assigns a task for each "
                  "recurring checklist and procedure). Done late: done after the due time. Overdue: still open past "
                  "its due time.",
    "temp": "Temperature readings: every temperature answered in the temperature logs, with the equipment chosen before "
            "it. Out of range: a cooler above the cooler limit, a freezer above the freezer limit (RHG's form: 5 °C and "
            "−10 °C; changeable on the Connection card). Other equipment is counted, not judged.",
    "sensors": "Sensors: the last reading of each Monnit sensor (Ops Execution reads them in °F; shown in °C), judged with "
               "the same limits. A sensor offline, or with no reading for three hours, is listed apart. The history of "
               "readings is not in Crunchtime's public API: only the live value is shown.",
    "oil": "Frying oil: each measurement answered; Yes to \"does the meter show more than 23?\" means the oil is over the "
           "limit and must be changed.",
    "trace": "Product traceability: each product checked on arrival, its temperature and whether it was fit to use; "
             "by supplier on the Food safety tab.",
    "spoil": "Spoiled product: each product written off in the spoil forms, with quantity, unit and reason, as entered.",
    "yield": "Yield (firo) = net weight after handling / gross weight received, of the products processed.",
    "people": "No name of a submitter, assignee or receiver is kept or shown; photos and signatures stay in Crunchtime.",
}


def cut(r, allowed):
    if allowed is None or not isinstance(r, dict) or not isinstance(r.get("locations"), list):
        return r
    A = {str(x).strip().upper() for x in allowed}
    keep = [L for L in r["locations"] if {str(L.get(k) or "").upper() for k in ("ref", "bc", "dept")} & A]
    names = {L["location"] for L in keep}
    return dict(r, locations=keep, support=[], sensors=[s for s in r.get("sensors") or [] if s["location"] in names],
                suppliers=[], cut=True)


def held_request(key, fn, refresh=False):
    with _lock:
        j = _jobs.get(key)
        if j and j["state"] == "running":
            return dict(j.get("value") or {}, pending=not j.get("value"), reading=True, stage=j.get("stage", ""))
        if j and j["state"] == "done" and not refresh and time.time() - j["at"] < 600:
            return j["value"]
        if j and j["state"] == "failed" and not refresh and time.time() - j["at"] < 60:
            return {"available": False, "error": j["error"]}
        last = (j or {}).get("value")
        j = _jobs[key] = {"state": "running", "at": time.time(), "stage": "starting", "value": last}

    def go():
        try:
            v = fn(lambda st: j.update(stage=st))
            j.update(state="done", at=time.time(), value=v)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            j.update(state="failed", at=time.time(), error=str(exc)[:300])
    threading.Thread(target=go, daemon=True, name="opsexec").start()
    return dict(last, reading=True, stage="starting") if last else {"pending": True, "stage": "starting"}
