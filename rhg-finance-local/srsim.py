"""Sales → SevenRooms ↔ Simphony: the reservations of each restaurant against what Simphony sold.

Sources (both only read)
  * SevenRooms (sevenrooms.py): the reservations of each venue by reservation date: party, guests who came, status
    (booked, seated and the courses, completed, no-show, cancelled), walk-ins, the channel it was booked through, the
    shift, the area and tables, and the Simphony check numbers SevenRooms holds for it (its POS integration fills
    check_numbers and pos_tickets); and the guests' feedback.
  * Simphony: the closed guest checks of each restaurant and business day (kept by Simphony ↔ Billy, the same store)
    and the day's totals (checks, guests, net sales).

A venue is a restaurant of Settings → Locations by the mapping of Connection & mapping ("venue = Simphony
reference"), else by the same name or its external venue id. A seated reservation is linked to the Simphony checks
whose numbers it carries, on the same business day; its spend is what Simphony closed on those checks (with VAT).
Shown: covers booked and seated against Simphony's guests, no-shows and cancellations, the share of the sales and
guests that came with a reservation, the spend per cover of reservations against everyone else, by shift, channel,
lead time and party size, and the feedback. Cases: a seated reservation with no check, with check numbers Simphony
does not have, a no-show or cancelled reservation with a check, a check claimed by two reservations, a reservation
of a closed day left booked.
"""
import io
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import billyrec
import heldjob
import locintel
import sevenrooms
import simphony

KEEP = 3 * 3600
MAX_DAYS = 62
SHOW_ROWS = 3000
XLSX_ROWS = 20000               # reservations in the Excel (the workbook is built in memory)
_held = heldjob.Held("srsim", KEEP)
IN_SERVICE = {"ARRIVED_PARTIAL", "ARRIVED", "ORDER_PLACED", "FIRST_COURSE", "SECOND_COURSE", "THIRD_COURSE", "FOURTH_COURSE",
              "DESSERT", "CHECK_DROPPED", "PAID", "BUS_TABLE", "COMPLETE"}
CAT = {"seated": "Seated", "no_show": "No-show", "cancel": "Cancelled", "no_entry": "No entry", "open": "Not closed in SevenRooms",
       "pending": "Today or later"}
LINK = {"linked": "Linked to a Simphony check", "not_found": "Check numbers Simphony does not have", "no_check": "Seated, no check",
        "shared": "Its check is another reservation's too", "check_on_noshow": "No-show or cancelled, with a check", "": ""}
CASES = ("not_found", "no_check", "shared", "check_on_noshow")
LEAD = [(0, "Same day"), (2, "1–2 days"), (7, "3–7 days"), (30, "8–30 days"), (10 ** 6, "Over 30 days")]
PARTY = [(2, "1–2"), (4, "3–4"), (6, "5–6"), (10, "7–10"), (10 ** 6, "Over 10")]
DWELL = [(60, "Under 1 h"), (90, "1–1½ h"), (120, "1½–2 h"), (180, "2–3 h"), (10 ** 6, "Over 3 h")]
GUEST = ("First visit", "Returning", "Not known")
AHEAD = 14                      # days ahead in the forecast
HIST_WEEKS = 4                  # the same weekday of the weeks before: what the forecast learns from
MIN_HIST_COVERS = 10            # covers on the books at the same lead (a week's average) for the pickup ratio
BIG_PARTY = 8
LINK_FOR_REGISTER = 0.5         # "seated, no check" goes to the register only where SevenRooms links half the tables
on_built = None                 # server.py: the cases of the default read into the exceptions register
DEFINITIONS = {
    "sources": "SevenRooms: the reservations of each venue by reservation date, with the party, the guests who came, "
               "the status, the channel and the Simphony check numbers SevenRooms holds for them, and the guests' "
               "feedback; no name, phone or email of a guest is read into the tower. Simphony: the closed checks of "
               "each business day and the day's guests and net sales. Both only read.",
    "venues": "A SevenRooms venue is the restaurant of Settings → Locations named in Connection & mapping "
              "(\"venue = Simphony reference\"), else the restaurant of the same name or of its external venue id.",
    "link": "A seated reservation is linked to the Simphony checks whose numbers it carries (check_numbers, or the "
            "POS tickets of SevenRooms' Simphony integration), on the same business day. Its spend is what Simphony "
            "closed on them, with VAT. The rest of the day's checks are the walk-ins and the guests not in SevenRooms.",
    "rates": "No-show rate: no-shows over the reservations that should have come (seated and no-shows). Cancellation "
             "rate: cancellations over every reservation. Covers: the guests of a reservation who came (its party "
             "when SevenRooms has no count). Share of guests: seated covers over Simphony's guests of the same days.",
    "cases": "A seated reservation with no check number; check numbers Simphony does not have that day; a no-show or "
             "cancelled reservation with a check; one check claimed by two reservations; a reservation of a day "
             "already closed left booked or confirmed.",
    "guests": "Returning: a guest (by SevenRooms' client, kept only as a code) seated at the restaurant before, within "
              "the days read (this period and the one before it); first visit: not seated there before in them; not "
              "known: a reservation with no client. VIP as SevenRooms marks it. Tags: SevenRooms' reservation tags "
              "(occasions, preferences).",
    "time": "Time at the table: from the first Simphony check opened to the last closed of the reservation's checks, "
            "else from SevenRooms' seated to left time; under 5 minutes or over 10 hours is not counted. Table turns: "
            "seated reservations over the tables used, by day and shift. Hours: the reservations' covers by the hour "
            "of the slot, against the Simphony checks by the hour they were opened.",
    "forecast": "The next 14 days: the reservations on the books now. Each day learns from the same weekday of the "
                "4 weeks before (the days the restaurant was open): covers seated, against covers on the books then at "
                "the same number of days ahead. Forecast covers: on the books × their ratio (the pickup) when 10 covers "
                "or more were on the books then, else on the books + the covers added after; Simphony guests: the "
                "weeks' guests moved by the difference in covers; net sales: the guests × the weeks' net sales per guest.",
    "staff": "By who served the reservation in SevenRooms: covers, the share linked to a Simphony check, the spend "
             "per cover, the cases and the feedback; the Simphony employee numbers are those of the checks linked.",
    "no_show_cost": "No-show covers × the spend per cover of reservations in the same period: the sales the tables "
                    "would have made.",
}


# ---------------------------------------------------------------- venues → restaurants
def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def mapping(text):
    """'venue (its id or name) = Simphony reference' lines → {key: ref}; '#' starts a comment."""
    out = {}
    for line in str(text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, _, v = line.rpartition("=")
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def place(venues, regs, text):
    """[(venue, restaurant or None, how)]: by the mapping, else by the same name, else by the external venue id."""
    M = mapping(text)
    byk = {k.lower(): v for k, v in M.items()}
    regs_by_ref = {r["ref"].upper(): r for r in regs if r.get("ref")}
    by_name = defaultdict(list)
    for r in regs:
        by_name[_norm(r.get("name"))].append(r)
    out = []
    for v in venues:
        hit, how = None, ""
        for k in (v["id"], v["name"], v.get("internal_name")):
            ref = byk.get(str(k or "").lower())
            if ref:
                hit, how = regs_by_ref.get(ref.upper()), "mapped"
                if hit is None:
                    how = "mapped to " + ref + " (not a restaurant of Settings → Locations)"
                break
        if not how:
            for nm in (v.get("internal_name"), v["name"]):
                cand = by_name.get(_norm(nm)) or []
                if len(cand) == 1:
                    hit, how = cand[0], "same name"
                    break
        if not how and v.get("external_venue_id"):
            x = v["external_venue_id"].upper()
            hit = next((r for r in regs if x in {str(r.get(k) or "").upper() for k in ("ref", "bc", "dept")}), None)
            how = "external venue id" if hit else ""
        out.append((v, hit, how or "not mapped"))
    return out


def _cat(r, closed):
    st, simple = (r.get("status") or "").upper(), (r.get("simple") or "").lower()
    if st == "CANCELED" or simple.startswith("cancel"):
        return "cancel"
    if st == "NO_SHOW" or simple.replace("-", " ") == "no show":
        return "no_show"
    if st == "NO_ENTRY" or simple.replace("-", " ") == "no entry":
        return "no_entry"
    if st in IN_SERVICE or simple == "complete":
        return "seated"
    return "open" if closed else "pending"


def _bucket(x, table):
    return next(label for top, label in table if x <= top)


def _hm(s):
    """'7:15 PM', '19:15', '19:15:00' or a date and time → minutes from midnight, or None."""
    m = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?", str(s or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    return h * 60 + mi if h < 24 and mi < 60 else None


def _dwell(x, found):
    """Minutes at the table: the Simphony checks' first opened to last closed, else SevenRooms' seated to left."""
    o = [t for t in (billyrec._ts(c.get("opn")) for c in found) if t]
    c = [t for t in (billyrec._ts(c.get("clsd")) for c in found) if t]
    if o and c:
        m = (max(c) - min(o)) / 60
        if 5 <= m <= 600:
            return round(m), "sim"
    a, b = _hm(x.get("seated")), _hm(x.get("left"))
    if a is not None and b is not None:
        m = b - a + (1440 if b < a else 0)
        if 5 <= m <= 600:
            return m, "sr"
    return None, ""


def _hour(t):
    m = _hm(t)
    return m // 60 if m is not None else None


# ---------------------------------------------------------------- the read
def build(company, frm, to, latest_fn=None, progress=None, today=None):
    today = today or date.today()
    a, b = billyrec.check_range(frm, to, today)
    days = billyrec._dates(a, b)
    before = billyrec._dates(a - timedelta(days=len(days)), a - timedelta(days=1))     # the period before, as long
    say = progress or (lambda *_: None)
    conn = sevenrooms.info()
    if not conn["configured"]:
        raise ValueError("SevenRooms is not connected: enter the API account in Connection & mapping.")
    regs = locintel.registry() if callable(getattr(locintel, "registry", None)) else []
    regs = [r for r in regs if (r.get("company") or "").upper() == (company or "").upper()]
    say("SevenRooms · the venues")
    V = sevenrooms.venues()
    placed = place(V, regs, conn["map"])
    venues = [(v, r) for v, r, _how in placed if r]
    sim_on = bool(simphony.info().get("configured"))
    latest = {}
    for v, r in venues:
        if sim_on and r["ref"] not in latest:
            try:
                latest[r["ref"]] = latest_fn(r["ref"]) if latest_fn else simphony.latest_business_date(r["ref"])
            except Exception:                                                # noqa: BLE001
                latest[r["ref"]] = None
    every = before + days
    hist = billyrec._dates(today - timedelta(days=7 * HIST_WEEKS), today - timedelta(days=1))    # the forecast's past
    ahead = [today + timedelta(days=i) for i in range(AHEAD)]
    sr_days = sorted(set(every) | set(hist) | set(ahead))
    tot_days = sorted(set(every) | set(hist))
    jobs = [("sr", v["id"], d) for v, r in venues for d in sr_days] + [("fb", v["id"], None) for v, r in venues]
    if sim_on:
        refs = sorted({r["ref"] for v, r in venues})
        jobs += [("chk", ref, d) for ref in refs for d in every if not latest.get(ref) or d.isoformat() <= latest[ref]]
        jobs += [("tot", ref, d) for ref in refs for d in tot_days if not latest.get(ref) or d.isoformat() <= latest[ref]]
    errors = []
    say(f"{len(every)} days · {len(jobs):,} reads ({len(venues)} venues)")

    def run(job):
        kind, x, d = job
        if kind == "sr":
            return sevenrooms.read_day(x, d, today)
        if kind == "fb":
            return sevenrooms.read_feedback(x, before[0], b, today)
        if kind == "chk":
            return billyrec.read_checks(x, d.isoformat(), latest.get(x))
        return simphony.daily_totals(x, d.isoformat(), latest.get(x))
    with ThreadPoolExecutor(max_workers=3) as spool, ThreadPoolExecutor(max_workers=6) as ppool:
        futs = {(spool if j[0] in ("sr", "fb") else ppool).submit(run, j): j for j in jobs}
        for n, f in enumerate(as_completed(futs), 1):
            kind, x, d = futs[f]
            try:
                f.result()
            except Exception as exc:                                         # noqa: BLE001
                if len(errors) < 12:
                    errors.append(f"{'SevenRooms' if kind in ('sr', 'fb') else 'Simphony'} {x[:24]} {d or ''}: {str(exc)[:140]}")
            if n % 10 == 0:
                say(f"{len(every)} days · {n:,} of {len(jobs):,} reads")
    if sim_on:
        simphony.flush_days()
    say("matching the reservations with the checks")
    prev = analyse(company, before, venues, placed, regs, latest, today, sim_on)
    r = analyse(company, days, venues, placed, regs, latest, today, sim_on, history=prev.pop("seen"))
    r.pop("seen")
    r["prev_agg"] = prev["agg"]
    r["prev_range"] = {"from": before[0].isoformat(), "to": before[-1].isoformat()}
    say("the next 14 days")
    now = datetime.now() if today == date.today() else datetime.combine(today, datetime.min.time()).replace(hour=12)
    r["forecast"] = forecast(venues, today, now, sim_on)
    r["forecast_at"] = now.isoformat(timespec="minutes")
    r.update(errors=errors, complete=not errors, checked_at=datetime.now().isoformat(timespec="seconds"),
             simphony=sim_on, connection={k: conn[k] for k in ("env", "configured")})
    return r


MIX_KINDS = ("shift", "source", "lead", "party", "area", "guest", "vip", "tag", "dwell", "hour", "simhour", "server", "turns")


def analyse(company, days, venues, placed, regs, latest, today, sim_on, history=None):
    """history: {ref: client codes seated there before these days} (for first visit / returning)."""
    a, b = days[0], days[-1]
    settled = (today - timedelta(days=2)).isoformat()
    rows, agg, cells = [], {}, {}
    mix = {k: defaultdict(lambda: defaultdict(Counter)) for k in MIX_KINDS}      # kind → ref → value → counters
    slots = defaultdict(lambda: defaultdict(set))                                # ref → area → (day, shift, tables)
    clients = Counter()
    seen = defaultdict(set)
    for ref, codes in (history or {}).items():
        seen[ref].update(codes)
    fb_all = []

    def A(ref):
        return agg.setdefault(ref, Counter())
    for v, reg in venues:
        ref = reg["ref"]
        res = [x for x in sevenrooms.held(v["id"], a, b) if not x.get("deleted")]
        chks = billyrec.checks_held(ref, a.isoformat(), b.isoformat()) if sim_on else []
        by_num = defaultdict(list)
        for c in chks:
            by_num[(c["day"], str(c.get("num")))].append(c)
        tot = {}
        for d in days:
            h = simphony.held_day(ref, d.isoformat()) if sim_on else None
            tot[d.isoformat()] = simphony.day_figures(h.get("rvcs") or []) if h else None
        claimed = {}
        res.sort(key=lambda x: (x["date"], x.get("real") or x.get("time") or "", x["id"]))
        for x in res:
            day = x["date"]
            cat = _cat(x, day <= settled)            # a day two days back is closed: left booked is a case
            codes = list(dict.fromkeys([*x["checks"], *(t["code"] for t in x["tickets"] if t["code"]),
                                        *(t["pos_id"] for t in x["tickets"] if t["pos_id"])]))
            found = []
            for k in codes:
                for c in by_num.get((day, k.lstrip("0") or k), []) + by_num.get((day, k), []):
                    if not c.get("cancel") and c not in found:
                        found.append(c)
            link = ""
            if cat == "seated":
                if found:
                    link = "linked"
                    for c in found:
                        key = (c["day"], c["id"])
                        if key in claimed and claimed[key] != x["id"]:
                            link = "shared"
                        claimed.setdefault(key, x["id"])
                elif codes:
                    link = "not_found" if sim_on else ""
                else:
                    link = "no_check"
            elif cat in ("no_show", "cancel") and found:
                link = "check_on_noshow"
            spend = round(sum(c["total"] for c in found), 2) if found else None
            covers = (x["arrived"] if x["arrived"] not in (None, 0) else x["guests"]) if cat == "seated" else 0
            lead = None
            try:
                lead = (date.fromisoformat(day) - date.fromisoformat(x["created"][:10])).days if x.get("created") else None
            except ValueError:
                pass
            dwell, dwell_src = _dwell(x, found) if cat == "seated" else (None, "")
            cl = x.get("client") or ""
            guest = GUEST[2] if not cl else GUEST[1] if cl in seen[ref] else GUEST[0]
            if cl and cat == "seated":
                seen[ref].add(cl)
            k = A(ref)
            k["res"] += 1
            k["cat_" + cat] += 1
            if dwell is not None:
                k["dwell_sum"] += dwell
                k["dwell_n"] += 1
            k["booked_covers"] += x["guests"] if cat != "cancel" else 0
            if cat == "seated":
                k["seated_covers"] += covers
                k["walk_in"] += x["walk_in"]
                k["vip"] += x["vip"]
                if link in ("linked", "shared"):
                    k["linked"] += 1
                    k["linked_covers"] += covers
                    k["linked_sales"] += spend or 0
            if cat == "no_show":
                k["no_show_covers"] += x["guests"]
            if link in CASES:
                k["case_" + link] += 1
                k["cases"] += 1
            if cat == "open":
                k["cases"] += 1
            if x.get("prepay"):
                k["prepay"] += x["prepay"]
            cell = cells.setdefault((ref, day), Counter())
            cell["res"] += 1
            cell[cat] += 1
            cell["covers"] += covers
            cell["cases"] += link in CASES or cat == "open"
            cell["linked_sales"] += (spend or 0) if link in ("linked", "shared") else 0
            linked = link in ("linked", "shared")

            def add(C):
                C["all"] += 1
                if cat == "cancel":
                    C["cancel"] += 1
                    C["cancel_covers"] += x["guests"] or 0
                    return
                C["res"] += 1
                C[cat] += 1
                C["covers"] += covers
                C["booked"] += x["guests"] or 0
                C["sales"] += spend or 0
                C["linked_covers"] += covers if linked else 0
                C["linked"] += linked
                C["cases"] += link in CASES
                if cat == "no_show":
                    C["ns_covers"] += x["guests"] or 0
                if dwell is not None:
                    C["dwell_sum"] += dwell
                    C["dwell_n"] += 1
            if not x["walk_in"]:
                keys = [("shift", x.get("shift") or "–"), ("source", x.get("source") or "–"), ("area", x.get("area") or "–"),
                        ("party", _bucket(x["guests"] or 0, PARTY)),
                        ("lead", _bucket(lead, LEAD) if lead is not None and lead >= 0 else "–"),
                        ("guest", guest), ("vip", "VIP" if x["vip"] else "Not VIP")]
                keys += [("tag", t) for t in dict.fromkeys(x.get("tags") or [])]
                hh = _hour(x.get("time"))
                if hh is not None:
                    keys.append(("hour", f"{date.fromisoformat(day).weekday()}|{hh}"))
                if dwell is not None:
                    keys.append(("dwell", _bucket(dwell, DWELL)))
                for kind, key in keys:
                    add(mix[kind][ref][key])
            if cat != "cancel":                                  # the staff: every table served, walk-ins too
                S = mix["server"][ref][x.get("server") or "–"]
                add(S)
                S["no_check"] += link == "no_check"
                for c in found:
                    if c.get("emp") not in (None, ""):
                        S["emp_" + str(c["emp"])] += 1
            if cat == "seated" and x.get("tables"):
                area = x.get("area") or "–"
                slots[ref][area].add((day, x.get("shift") or "", x["tables"]))
                add(mix["turns"][ref][area])
            if x.get("client") and cat == "seated":
                clients[(ref, x["client"])] += 1
            rows.append({"ref": ref, "day": day, "time": (x.get("time") or "")[:5], "res": x["ref"], "id": x["id"],
                         "guests": x["guests"], "arrived": x["arrived"], "status": x["status_display"] or x["status"],
                         "cat": cat, "walk_in": x["walk_in"], "source": x["source"], "shift": x["shift"], "area": x["area"],
                         "tables": x["tables"], "server": x["server"], "vip": x["vip"], "tags": x["tags"],
                         "checks": codes, "found": [str(c["num"]) for c in found], "spend": spend,
                         "sr_spend": round(sum(t["total"] or 0 for t in x["tickets"]), 2) if x["tickets"] else None,
                         "lead": lead, "link": link, "prepay": x.get("prepay"), "client": x.get("client") or "",
                         "created": x.get("created") or "", "seated": x.get("seated") or "", "left": x.get("left") or "",
                         "duration": x.get("duration"), "real": x.get("real") or "", "dwell": dwell, "dwell_src": dwell_src,
                         "guest": guest, "booked": x.get("guests") or 0,
                         "sim": [{"num": c.get("num"), "opn": c.get("opn") or "", "clsd": c.get("clsd") or "", "total": c["total"],
                                  "emp": c.get("emp"), "rvc": c.get("rvc")} for c in found],
                         "tickets": [{"code": t["code"] or t["pos_id"], "total": t["total"], "start": t["start"], "end": t["end"],
                                      "table": t["table"]} for t in x["tickets"]][:6]})
        # Simphony's side of each day: every check, guests, sales; the checks no reservation took
        for d in days:
            ds = d.isoformat()
            dc = [c for c in chks if c["day"] == ds and not c.get("cancel") and abs(c["total"]) >= 0.01]
            t = tot.get(ds)
            k = A(ref)
            k["sim_checks"] += len(dc)
            k["sim_sales"] += sum(c["total"] for c in dc)
            k["sim_guests"] += (t or {}).get("guests") or 0
            k["sim_net"] += (t or {}).get("net") or 0
            k["sim_days"] += 1 if (dc or t) else 0
            cell = cells.setdefault((ref, ds), Counter())
            cell["sim_checks"] += len(dc)
            cell["sim_sales"] += sum(c["total"] for c in dc)
            cell["sim_guests"] += (t or {}).get("guests") or 0
            for c in dc:
                hh = _hour((c.get("opn") or "")[11:])
                if hh is not None:
                    H = mix["simhour"][ref][f"{d.weekday()}|{hh}"]
                    H["checks"] += 1
                    H["sales"] += c["total"]
        fbs = sevenrooms.feedback_held(v["id"], a, b)
        for f in fbs:
            f["ref"] = ref
            fb_all.append(f)
            k = A(ref)
            if f.get("overall") is not None:
                k["fb_n"] += 1
                k["fb_overall"] += f["overall"]
                k["fb_low"] += f["overall"] <= 2
            for x in ("food", "drinks", "service", "ambience"):
                if f.get(x) is not None:
                    k["fb_n_" + x] += 1
                    k["fb_" + x] += f[x]
    for (ref, cid), n in clients.items():
        A(ref)["guests_known"] += 1
        A(ref)["guests_back"] += n >= 2
    names = {r["ref"]: r for r in regs}
    locs = []
    for ref in sorted(agg):
        R = names.get(ref) or {}
        locs.append({"ref": ref, "name": R.get("name") or ref, "bc": R.get("bc") or "", "dept": R.get("dept") or "",
                     "venues": [v["name"] or v["id"] for v, r in venues if r["ref"] == ref],
                     "cells": {d.isoformat(): _cell(cells.get((ref, d.isoformat()))) for d in days}})
    res_ids = {x["id"]: x for x in rows}
    fb_rows = []
    for f in fb_all:
        x = res_ids.get(f["res"]) or {}
        fb_rows.append({"ref": f["ref"], "day": f["rday"], "received": f["received"], "overall": f.get("overall"),
                        "food": f.get("food"), "drinks": f.get("drinks"), "service": f.get("service"), "ambience": f.get("ambience"),
                        "recommend": f.get("recommend"), "type": f.get("type"), "res": x.get("res") or "", "tables": x.get("tables") or "",
                        "server": x.get("server") or "", "guests": x.get("guests")})
        if x and f.get("overall") is not None:                # the feedback of the tables each one served
            S = mix["server"][f["ref"]][x.get("server") or "–"]
            S["fb_n"] += 1
            S["fb_sum"] += f["overall"]
            S["fb_low"] += f["overall"] <= 2
    fb_rows.sort(key=lambda f: (f["overall"] if f["overall"] is not None else 9, f["day"]))
    for ref, by_area in slots.items():
        for area, S in by_area.items():
            mix["turns"][ref][area]["slots"] = len(S)
    mix_raw = {kind: {ref: {k: dict(c) for k, c in D.items()} for ref, D in by_ref.items()} for kind, by_ref in mix.items()}
    venues_out = [{"id": v["id"], "name": v["name"] or v["id"], "internal_name": v.get("internal_name") or "", "ref": (r or {}).get("ref") or "",
                   "restaurant": (r or {}).get("name") or "", "how": how,
                   "company": (r or {}).get("company") or ""} for v, r, how in placed]
    return {"company": company, "days": [d.isoformat() for d in days], "range": {"from": a.isoformat(), "to": b.isoformat()},
            "agg": {ref: dict(k) for ref, k in agg.items()}, "locations": locs, "rows": rows, "feedback": fb_rows,
            "mix_raw": mix_raw, "venues": venues_out, "seen": {ref: sorted(v) for ref, v in seen.items()}, "cat": CAT, "link": LINK, "cases": list(CASES), "definitions": DEFINITIONS}


def _cell(c):
    c = c or Counter()
    return {k: (round(c[k], 2) if isinstance(c[k], float) else c[k]) for k in
            ("res", "seated", "no_show", "cancel", "open", "covers", "cases", "linked_sales", "sim_checks", "sim_sales", "sim_guests")}


def mixes(r, per_cover=None):
    """The tables by shift, channel, lead time, party, area, guest, VIP, tag, time at the table, hour, server and
    area turns of the restaurants of r (after cut: of the scope). per_cover: the no-show cost per cover."""
    refs = set(r["agg"])
    out = {}
    for kind, by_ref in (r.get("mix_raw") or {}).items():
        D = defaultdict(Counter)
        for ref, vals in by_ref.items():
            if ref in refs:
                for k, c in vals.items():
                    D[k].update(c)
        order = {"lead": [l for _t, l in LEAD] + ["–"], "party": [l for _t, l in PARTY], "dwell": [l for _t, l in DWELL],
                 "guest": list(GUEST), "vip": ["VIP", "Not VIP"]}.get(kind)
        by = "covers" if kind in ("server", "turns") else "checks" if kind == "simhour" else "res"
        out[kind] = _mix(D, order, by=by, top=25 if kind == "tag" else 60 if kind == "server" else None, per_cover=per_cover)
    return out


BASE = ("res", "all", "cancel", "cancel_covers", "seated", "no_show", "covers", "booked", "sales", "linked_covers", "linked", "ns_covers",
        "dwell_sum", "dwell_n", "cases")


def _mix(D, order=None, by="res", top=None, per_cover=None):
    keys = [k for k in order if k in D] if order else sorted(D, key=lambda k: (-D[k][by], k))
    out = []
    for k in keys[:top] if top else keys:
        row, emps = {"key": k}, []
        for f, v in D[k].items():
            if f.startswith("emp_"):
                emps.append((-v, f[4:]))
            else:
                row[f] = round(v, 2) if isinstance(v, float) else v
        for f in BASE:
            row.setdefault(f, 0)
        if emps:
            row["emps"] = [e for _n, e in sorted(emps)[:3]]
        row["ns_cost"] = round(row["ns_covers"] * per_cover, 2) if per_cover else None
        out.append(row)
    return out


# ---------------------------------------------------------------- the next 14 days
def forecast(venues, today, now, sim_on):
    """Each restaurant's next AHEAD days: on the books now, and the forecast learnt from the same weekday of the
    HIST_WEEKS weeks before (see DEFINITIONS["forecast"])."""
    lo = today - timedelta(days=7 * HIST_WEEKS)
    settled = (today - timedelta(days=2)).isoformat()
    vids = defaultdict(list)
    for v, reg in venues:
        vids[reg["ref"]].append(v["id"])
    out = []
    for ref, ids in sorted(vids.items()):
        by_day = defaultdict(list)
        for vid in ids:
            for x in sevenrooms.held(vid, lo, today + timedelta(days=AHEAD - 1)):
                if not x.get("deleted") and not x.get("walk_in"):
                    by_day[x["date"]].append(x)
        past = {}
        for i in range(1, 7 * HIST_WEEKS + 1):
            H = today - timedelta(days=i)
            hs = H.isoformat()
            seated = sum((x["arrived"] if x["arrived"] not in (None, 0) else x["guests"]) or 0
                         for x in by_day[hs] if _cat(x, hs <= settled) == "seated")
            h = simphony.held_day(ref, hs) if sim_on else None
            f = simphony.day_figures(h.get("rvcs") or []) if h else None
            if by_day[hs] or (f and f["guests"]):              # open that day (else closed, or not read)
                past[hs] = (seated, f)
        for i in range(AHEAD):
            P = today + timedelta(days=i)
            ps = P.isoformat()
            book = [x for x in by_day[ps] if _cat(x, False) != "cancel"]
            row = {"ref": ref, "day": ps, "lead": i, "res": len(book), "covers": sum(x["guests"] or 0 for x in book),
                   "vip": sum(1 for x in book if x["vip"]), "big": sum(1 for x in book if (x["guests"] or 0) >= BIG_PARTY),
                   "weeks": 0, "hist_seated": None, "hist_booked": None, "pickup": None, "added": None, "fc_covers": None, "method": "",
                   "hist_guests": None, "fc_guests": None, "fc_net": None, "hist": [],
                   "book": [{"time": (x.get("time") or "")[:5], "res": x.get("ref") or "", "guests": x["guests"] or 0,
                             "vip": x["vip"], "source": x.get("source") or "", "area": x.get("area") or "",
                             "tables": x.get("tables") or "", "shift": x.get("shift") or "", "tags": (x.get("tags") or [])[:4],
                             "status": x.get("status_display") or x.get("status") or "", "created": (x.get("created") or "")[:10]}
                            for x in sorted(book, key=lambda x: (x.get("time") or "", x.get("ref") or ""))[:80]]}
            then, seat, G, N, seat_g = 0, 0, [], 0.0, 0
            for w in range(1, HIST_WEEKS + 1):
                H = P - timedelta(days=7 * w)
                hs = H.isoformat()
                if hs not in past:
                    continue
                cut_at = (datetime.combine(H, now.time()) - timedelta(days=i)).isoformat(timespec="seconds")
                at = lambda t: str(t or "")[:19].replace(" ", "T")
                th = sum(x["guests"] or 0 for x in by_day[hs] if x.get("created") and at(x["created"]) <= cut_at
                         and not (_cat(x, True) == "cancel" and x.get("updated") and at(x["updated"]) <= cut_at))
                then += th
                s_, f = past[hs]
                row["hist"].append({"day": hs, "then": th, "seated": s_, "guests": (f or {}).get("guests"),
                                    "net": round((f or {}).get("net") or 0, 2) if f else None})
                seat += s_
                row["weeks"] += 1
                if f and f["guests"]:
                    G.append(f["guests"])
                    N += f["net"]
                    seat_g += s_
            n = row["weeks"]
            if not n:
                out.append(row)
                continue
            row["hist_seated"], row["hist_booked"] = round(seat / n, 1), round(then / n, 1)
            if then / n >= MIN_HIST_COVERS and row["covers"]:          # enough on the books then: the ratio
                row["pickup"] = round(seat / then, 3)
                fc, row["method"] = row["covers"] * seat / then, "pickup"
            else:                                                    # few then: what was added after, on what is booked
                row["added"] = round((seat - then) / n, 1)
                fc, row["method"] = max(row["covers"], row["covers"] + (seat - then) / n), "added"
            row["fc_covers"] = round(fc, 1)
            if G:
                g = sum(G) / len(G)
                row["hist_guests"] = round(g, 1)
                row["fc_guests"] = round(max(fc, g + fc - seat_g / len(G)), 1)
                row["fc_net"] = round(row["fc_guests"] * N / sum(G), 2) if sum(G) else None
            out.append(row)
    return out


# ---------------------------------------------------------------- the exceptions register
CASE_TITLE = {"not_found": "with check numbers Simphony does not have", "no_check": "seated with no Simphony check",
              "shared": "on a check another reservation has too", "check_on_noshow": "no-show or cancelled, with a check",
              "open": "left booked on a closed day"}
CASE_SEVERITY = {"not_found": "warning", "no_check": "warning", "shared": "info", "check_on_noshow": "warning", "open": "info"}


def findings(r):
    """The cases for exreg.sync (source "sevenrooms"): one by restaurant, day and kind. "Seated, no check" only for
    a restaurant where SevenRooms links at least half the seated tables (else its POS link is not on there)."""
    names = {L["ref"]: (L["name"], L["bc"] or L["ref"]) for L in r["locations"]}
    rate = {ref: kpis({ref: k})["link_rate"] for ref, k in r["agg"].items()}
    groups = defaultdict(list)
    for x in r["rows"]:
        kind = x["link"] if x["link"] in CASES else "open" if x["cat"] == "open" else ""
        if kind == "no_check" and (rate.get(x["ref"]) or 0) < LINK_FOR_REGISTER:
            continue
        if kind:
            groups[(x["ref"], x["day"], kind)].append(x)
    out = []
    for (ref, d, kind), xs in sorted(groups.items()):
        nm, loc = names.get(ref, (ref, ref))
        ex = [f"{x['res'] or x['id']} {x['time']} party {x['guests']}" + (f" checks {', '.join(x['checks'])}" if x["checks"] else "")
              for x in xs[:12]]
        out.append({"subject": f"{d} sr_{kind} {ref}", "kind": kind, "location": loc,
                    "title": f"{nm} {d}: {len(xs)} {'reservation' if len(xs) == 1 else 'reservations'} {CASE_TITLE[kind]}",
                    "detail": "SevenRooms: " + "; ".join(ex) + (" …" if len(xs) > 12 else ""),
                    "severity": CASE_SEVERITY[kind], "value": round(sum(x["spend"] or 0 for x in xs), 2)})
    return out


def in_scope(r):
    days = set(r["days"])
    return lambda x: (x["subject"] or "")[:10] in days


# ---------------------------------------------------------------- the scope, the totals
def _keep_of(r, allowed):
    if allowed is None or not isinstance(r, dict) or not isinstance(r.get("locations"), list):
        return None
    A = {str(x).strip().upper() for x in allowed}
    return {L["ref"] for L in r["locations"] if {str(L.get(k) or "").upper() for k in ("ref", "bc", "dept")} & A}


def cut(r, allowed):
    keep = _keep_of(r, allowed)
    if keep is None:
        return r
    ok = lambda x: x.get("ref") in keep
    return dict(r, locations=[L for L in r["locations"] if L["ref"] in keep], rows=[x for x in r["rows"] if ok(x)],
                feedback=[f for f in r["feedback"] if ok(f)], agg={k: v for k, v in r["agg"].items() if k in keep},
                prev_agg={k: v for k, v in (r.get("prev_agg") or {}).items() if k in keep},
                venues=[v for v in r["venues"] if v["ref"] in keep], forecast=[x for x in r.get("forecast") or [] if ok(x)],
                cut=True)


def kpis(agg):
    t = Counter()
    for k in agg.values():
        t.update(k)
    seated, ns = t["cat_seated"], t["cat_no_show"]
    other_covers = max(0, t["sim_guests"] - t["linked_covers"])
    other_sales = t["sim_sales"] - t["linked_sales"]
    return {"res": t["res"], "booked_covers": t["booked_covers"], "seated": seated, "seated_covers": t["seated_covers"],
            "no_show": ns, "no_show_covers": t["no_show_covers"], "no_show_rate": ns / (seated + ns) if seated + ns else None,
            "cancel": t["cat_cancel"], "cancel_rate": t["cat_cancel"] / t["res"] if t["res"] else None,
            "open": t["cat_open"], "walk_in": t["walk_in"], "vip": t["vip"],
            "linked": t["linked"], "link_rate": t["linked"] / seated if seated else None,
            "linked_sales": round(t["linked_sales"], 2), "sim_sales": round(t["sim_sales"], 2), "sim_guests": t["sim_guests"],
            "sim_checks": t["sim_checks"], "sim_net": round(t["sim_net"], 2),
            "sales_share": t["linked_sales"] / t["sim_sales"] if t["sim_sales"] else None,
            "covers_share": t["seated_covers"] / t["sim_guests"] if t["sim_guests"] else None,
            "per_cover_res": t["linked_sales"] / t["linked_covers"] if t["linked_covers"] else None,
            "per_cover_other": other_sales / other_covers if other_covers and t["sim_guests"] else None,
            "cases": t["cases"], **{c: t["case_" + c] for c in CASES},
            "fb_n": t["fb_n"], "fb_overall": t["fb_overall"] / t["fb_n"] if t["fb_n"] else None, "fb_low": t["fb_low"],
            **{"fb_" + x: (t["fb_" + x] / t["fb_n_" + x] if t["fb_n_" + x] else None) for x in ("food", "drinks", "service", "ambience")},
            "guests_known": t["guests_known"], "guests_back": t["guests_back"], "prepay": round(t["prepay"], 2),
            "dwell": t["dwell_sum"] / t["dwell_n"] if t["dwell_n"] else None, "dwell_n": t["dwell_n"],
            "ns_cost": t["no_show_covers"] * t["linked_sales"] / t["linked_covers"] if t["linked_covers"] else None}


def public(r):
    """The screen: the totals and every restaurant's line, the cases first and then the latest reservations."""
    if not isinstance(r, dict) or "agg" not in r:
        return r
    by = {ref: kpis({ref: k}) for ref, k in r["agg"].items()}
    P = r.get("prev_agg") or {}
    locs = [dict(L, k=by.get(L["ref"]) or kpis({}), kp=kpis({L["ref"]: P[L["ref"]]}) if L["ref"] in P else None) for L in r["locations"]]
    rows = r["rows"]
    cases = [x for x in rows if x["link"] in CASES or x["cat"] == "open"]
    rest = sorted((x for x in rows if not (x["link"] in CASES or x["cat"] == "open")), key=lambda x: (x["day"], x["time"]), reverse=True)
    shown = cases[:SHOW_ROWS] + rest[:max(0, SHOW_ROWS - len(cases))]
    out = {k: v for k, v in r.items() if k not in ("agg", "rows", "mix_raw", "prev_agg")}
    K = kpis(r["agg"])
    by_loc = {ref: mixes({"agg": {ref: k}, "mix_raw": r.get("mix_raw")}, by[ref]["per_cover_res"]) for ref, k in r["agg"].items()}
    out.update(kpis=K, kpis_prev=kpis(P) if "prev_agg" in r else None, mixes=mixes(r, K["per_cover_res"]), loc_mixes=by_loc, locations=locs,
               rows=shown, rows_count=len(rows), cases_count=len(cases), feedback=r["feedback"][:1500],
               forecast=r.get("forecast") or [])
    return out


# ---------------------------------------------------------------- held reads
def key_of(company, frm, to):
    return f"{company}|{frm}|{to}"


def default_range(today=None):
    b = (today or date.today()) - timedelta(days=1)
    return (b - timedelta(days=6)).isoformat(), b.isoformat()


def _after(company):
    """The default read (the last 7 days) puts its cases in the exceptions register; a range of one's own does not."""
    def after(r):
        if on_built and r.get("complete"):
            return on_built(company, r)
        return None
    return after


def request(company, frm=None, to=None, refresh=False, latest_fn=None):
    today = date.today()
    if not (frm and to):
        frm, to = default_range(today)
    a, b = billyrec.check_range(frm, to, today)
    if not sevenrooms.info()["configured"]:
        return {"available": False, "not_connected": True, "range": {"from": a.isoformat(), "to": b.isoformat()},
                "error": "SevenRooms is not connected yet: the API account goes in Connection & mapping."}
    default = (a.isoformat(), b.isoformat()) == default_range(today)
    return _held.request(key_of(company, a.isoformat(), b.isoformat()),
                         lambda progress: build(company, a.isoformat(), b.isoformat(), latest_fn, progress), refresh,
                         after=_after(company) if default else None, note="Reading SevenRooms and Simphony…")


def remember(company, r):
    _held.remember(key_of(company, r["range"]["from"], r["range"]["to"]), r)


def companies():
    """The companies with a restaurant placed for a SevenRooms venue (the daily control run reads them)."""
    conn = sevenrooms.info()
    if not conn["configured"]:
        return []
    regs = locintel.registry() if callable(getattr(locintel, "registry", None)) else []
    return sorted({(r.get("company") or "").upper() for _v, r, _h in place(sevenrooms.venues(), regs, conn["map"]) if r and r.get("company")})


def held(company, frm, to):
    return _held.held(key_of(company, frm, to))


# ---------------------------------------------------------------- the Excel
def render_xlsx(r):
    import xlsxtable
    by = {ref: kpis({ref: k}) for ref, k in r["agg"].items()}
    names = {L["ref"]: L["name"] for L in r["locations"]}
    pc = lambda v: v if v is not None else None
    loc_rows = [[names.get(ref, ref), ref, k["res"], k["booked_covers"], k["seated"], k["seated_covers"], k["no_show"], pc(k["no_show_rate"]),
                 k["cancel"], pc(k["cancel_rate"]), k["walk_in"], k["linked"], pc(k["link_rate"]), k["linked_sales"], k["sim_sales"],
                 pc(k["sales_share"]), k["sim_guests"], pc(k["covers_share"]), k["per_cover_res"], k["per_cover_other"], k["cases"],
                 k["fb_n"], k["fb_overall"]] for ref, k in sorted(by.items(), key=lambda x: names.get(x[0], x[0]))]
    day_rows = [[L["name"], d, c["res"], c["seated"], c["no_show"], c["cancel"], c["covers"], c["sim_guests"], c["linked_sales"],
                 c["sim_sales"], c["sim_checks"], c["cases"]] for L in r["locations"] for d, c in L["cells"].items()]
    res_rows = [[names.get(x["ref"], x["ref"]), x["day"], x["time"], x["res"], x["guests"], x["arrived"], x["status"], r["cat"][x["cat"]],
                 "yes" if x["walk_in"] else "", x["source"], x["shift"], x["area"], x["tables"], x["server"], "yes" if x["vip"] else "",
                 ", ".join(x["checks"]), ", ".join(x["found"]), x["spend"], x["sr_spend"], x["lead"], r["link"].get(x["link"], ""),
                 x["prepay"]] for x in sorted(r["rows"], key=lambda x: (x["ref"], x["day"], x["time"]))[:XLSX_ROWS]]
    K = kpis(r["agg"])
    M = mixes(r, K["per_cover_res"])
    pcv = lambda m: m["sales"] / m["linked_covers"] if m["linked_covers"] else None
    avg = lambda a, n: a / n if n else None
    by_label = {"shift": "Shift", "source": "Booked by", "lead": "Lead time", "party": "Party", "area": "Area", "guest": "Guest",
                "vip": "VIP", "tag": "Tag", "dwell": "Time at the table"}
    mix_rows = [[by_label[kind], m["key"], m["all"], m["res"], m["cancel"], pc(m["cancel"] / m["all"] if m["all"] else None),
                 m["seated"], m["no_show"], pc(m["no_show"] / (m["seated"] + m["no_show"]) if m["seated"] + m["no_show"] else None),
                 m["covers"], m["ns_covers"], m["ns_cost"], m["sales"], pcv(m), avg(m["dwell_sum"], m["dwell_n"])]
                for kind in by_label for m in M.get(kind) or []]
    turn_rows = [[m["key"], m["seated"], m.get("slots", 0), avg(m["seated"], m.get("slots", 0)), m["covers"],
                  avg(m["dwell_sum"], m["dwell_n"])] for m in M.get("turns") or []]
    WD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    hours = defaultdict(dict)
    for kind in ("hour", "simhour"):
        for m in M.get(kind) or []:
            hours[m["key"]][kind] = m
    hour_rows = [[WD[int(k.split("|")[0])], int(k.split("|")[1]), v.get("hour", {}).get("res", 0), v.get("hour", {}).get("booked", 0),
                  v.get("hour", {}).get("covers", 0), v.get("simhour", {}).get("checks", 0), v.get("simhour", {}).get("sales", 0)]
                 for k, v in sorted(hours.items(), key=lambda kv: tuple(int(p) for p in kv[0].split("|")))]
    staff_rows = [[m["key"], m["res"], m["seated"], m["covers"], m["linked"], pc(m["linked"] / m["seated"] if m["seated"] else None),
                   m["sales"], pcv(m), avg(m["dwell_sum"], m["dwell_n"]), m.get("no_check", 0), m["cases"], m.get("fb_n", 0),
                   avg(m.get("fb_sum", 0), m.get("fb_n", 0)), m.get("fb_low", 0), ", ".join(m.get("emps") or [])] for m in M.get("server") or []]
    fc_rows = [[names.get(x["ref"], x["ref"]), x["day"], x["res"], x["covers"], x["vip"], x["big"], x["weeks"], x["hist_booked"],
                x["hist_seated"], x["pickup"], x.get("added"), x["fc_covers"], x["hist_guests"], x["fc_guests"], x["fc_net"], x["method"]]
               for x in r.get("forecast") or []]
    fb_rows = [[names.get(f["ref"], f["ref"]), f["day"], f["received"], f["overall"], f["food"], f["drinks"], f["service"], f["ambience"],
                f["recommend"], f["res"], f["tables"], f["server"]] for f in r["feedback"]]
    ven_rows = [[v["name"], v["internal_name"], v["id"], v["restaurant"], v["ref"], v["how"]] for v in r["venues"]]
    T, N, I, P, D = "text", "num", "int", "pct", "date"
    sheets = [
        {"title": "Restaurants", "header": ["Restaurant", "Simphony ref", "Reservations", "Covers booked", "Seated", "Covers seated", "No-shows",
                                            "No-show rate", "Cancelled", "Cancel rate", "Walk-ins", "Linked to a check", "Linked rate",
                                            "Sales with a reservation", "Simphony sales", "Share of sales", "Simphony guests", "Share of guests",
                                            "Per cover, reservations", "Per cover, the rest", "Cases", "Feedback", "Overall rating"],
         "formats": [T, T, I, I, I, I, I, P, I, P, I, I, P, N, N, P, I, P, N, N, I, I, "qty"], "rows": loc_rows},
        {"title": "Days", "header": ["Restaurant", "Day", "Reservations", "Seated", "No-shows", "Cancelled", "Covers seated", "Simphony guests",
                                     "Sales with a reservation", "Simphony sales", "Simphony checks", "Cases"],
         "formats": [T, D, I, I, I, I, I, I, N, N, I, I], "rows": day_rows},
        {"title": "Reservations", "header": ["Restaurant", "Day", "Time", "Reference", "Party", "Came", "Status", "Kind", "Walk-in", "Booked by",
                                             "Shift", "Area", "Tables", "Served by", "VIP", "Check numbers", "Found in Simphony",
                                             "Spend (Simphony)", "Spend (SevenRooms)", "Lead days", "Link", "Prepaid"],
         "formats": [T, D, T, T, I, I, T, T, T, T, T, T, T, T, T, T, T, N, N, I, T, N], "rows": res_rows},
        {"title": "Guests & value", "header": ["By", "Value", "Reservations, all", "Reservations", "Cancelled", "Cancel rate", "Seated",
                                               "No-shows", "No-show rate", "Covers seated", "No-show covers", "No-show cost",
                                               "Sales with a reservation", "Per cover", "Minutes at the table"],
         "formats": [T, T, I, I, I, P, I, I, P, I, I, N, N, N, "qty"], "rows": mix_rows},
        {"title": "Tables & hours", "header": ["Area", "Seated", "Tables used (day, shift)", "Turns", "Covers", "Minutes at the table"],
         "formats": [T, I, I, "qty", I, "qty"], "rows": turn_rows},
        {"title": "Hours", "header": ["Weekday", "Hour", "Reservations", "Covers booked", "Covers seated", "Simphony checks opened",
                                      "Simphony sales"],
         "formats": [T, I, I, I, I, I, N], "rows": hour_rows},
        {"title": "Staff", "header": ["Served by", "Reservations", "Seated", "Covers", "Linked to a check", "Linked rate", "Sales",
                                      "Per cover", "Minutes at the table", "Seated, no check", "Cases", "Feedback", "Overall rating",
                                      "Rated 1–2", "Simphony employees"],
         "formats": [T, I, I, I, I, P, N, N, "qty", I, I, I, "qty", I, T], "rows": staff_rows},
        {"title": "Next 14 days", "header": ["Restaurant", "Day", "Reservations", "Covers on the books", "VIP", f"Parties of {BIG_PARTY}+",
                                             "Weeks learnt from", "Covers on the books then", "Covers seated then", "Pickup",
                                             "Covers added after", "Forecast covers", "Simphony guests then", "Forecast guests", "Forecast net sales", "Method"],
         "formats": [T, D, I, I, I, I, I, "qty", "qty", "qty", "qty", "qty", "qty", "qty", N, T], "rows": fc_rows},
        {"title": "Feedback", "header": ["Restaurant", "Reservation day", "Received", "Overall", "Food", "Drinks", "Service", "Ambience",
                                         "Recommend", "Reference", "Tables", "Served by"],
         "formats": [T, D, T, "qty", "qty", "qty", "qty", "qty", T, T, T, T], "rows": fb_rows},
        {"title": "Venues", "header": ["Venue", "Internal name", "SevenRooms id", "Restaurant", "Simphony ref", "Placed by"],
         "formats": [T] * 6, "rows": ven_rows}]
    notes = [f"SevenRooms ↔ Simphony · {r['company']} · {r['range']['from']} to {r['range']['to']}; read only, no guest's name, phone or email."]
    if len(r["rows"]) > XLSX_ROWS:
        notes.append(f"Reservations: the first {XLSX_ROWS:,} of {len(r['rows']):,}; choose fewer days for every one.")
    notes += list(DEFINITIONS.values())
    return xlsxtable.render(sheets, notes)
