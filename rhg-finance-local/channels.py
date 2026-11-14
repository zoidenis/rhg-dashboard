"""Sales → Location intelligence, channels: each restaurant's sales by order type (the channel: dine in, take
away, delivery…), by revenue centre and by day part and hour, against the period before and the same dates last
year, and what the delivery partners (Wolt, Glovo… as tender media) brought in.

Source: Simphony Reporting and Analytics, one call per restaurant and business day:
getOrderTypeQuarterHourTotals gives the sales of every quarter hour by revenue centre and order type, with the
day part it belongs to, so the three views come from the same figures and add up to the same total. Delivery
partners come from getTenderMediaDailyTotals (what was paid through the partner's tender, VAT included).
Days already closed are kept in the local file and never read again (simphony.py).

Which order types are delivery, and which tender media are delivery partners, is set in Settings → Location
intelligence (words found in the names).
"""
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import locintel
import simphony
import store

KEEP_SECONDS = 600
_jobs, _lock = {}, threading.Lock()
PERIODS = ("cur", "prev", "ly")


def _words(text):
    return [w.strip().lower() for w in str(text or "").replace(";", ",").split(",") if w.strip()]


def _matches(name, words):
    n = (name or "").lower()
    return any(w in n for w in words)


def _dates(a, b):
    out, d = [], a
    while d <= b:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _blank():
    return {p: [0.0, 0.0, 0, 0] for p in PERIODS}


def _add(x, v):
    for i in range(4):
        x[i] += v[i]


def _rows(table, extra=None):
    """{name: {period: [net, disc, checks, guests]}} -> sorted list for the screen."""
    out = []
    for name, v in table.items():
        r = {"name": name}
        for p in PERIODS:
            n, d, c, g = v[p]
            r[p] = {"net": round(n), "disc": round(d), "checks": int(c), "guests": int(g),
                    "avg": round(n / c, 2) if c else None}
        if extra:
            r.update(extra(name))
        out.append(r)
    out.sort(key=lambda r: -r["cur"]["net"])
    return out


def summarise(rows, deliv_ot, dp_order):
    """The totals by currency of a set of restaurants, from their rows. A revenue centre is a restaurant's own: the
    same name at two restaurants ("Restaurant") is two lines, "restaurant · revenue centre" (QA 14 Nov 2026)."""
    totals = {}
    for row in rows:
        t = totals.setdefault(row["currency"], {"locations": 0, "total": {p: 0.0 for p in PERIODS},
                                                "delivery": {p: 0.0 for p in PERIODS}, "channels": {}, "partners": {},
                                                "dayparts": {}, "rvcs": {}})
        t["locations"] += 1
        for p in PERIODS:
            t["total"][p] += row["total"][p]
            t["delivery"][p] += row["delivery"][p]
        place = row.get("reg_name") or row["name"]
        for src, dst in (("channels", "channels"), ("dayparts", "dayparts"), ("rvcs", "rvcs")):
            for x in row.get(src) or []:
                n = f"{place} · {x['name']}" if dst == "rvcs" else x["name"]
                acc = t[dst].setdefault(n, _blank())
                for p in PERIODS:
                    v = x[p]
                    _add(acc[p], (v["net"], v["disc"], v["checks"], v["guests"]))
                if dst == "rvcs":
                    t.setdefault("rvc_of", {})[n] = (place, x["name"], row["locRef"])
        for x in row.get("partners") or []:
            acc = t["partners"].setdefault(x["name"], {p: [0.0, 0] for p in PERIODS})
            for p in PERIODS:
                acc[p][0] += x[p]["value"]
                acc[p][1] += x[p]["count"]
    out = {}
    for c, t in totals.items():
        rv_of = t.get("rvc_of") or {}
        out[c] = {"locations": t["locations"], "total": {p: round(v) for p, v in t["total"].items()},
                  "delivery": {p: round(v) for p, v in t["delivery"].items()},
                  "channels": _rows(t["channels"], lambda n: {"delivery": _matches(n, deliv_ot)}),
                  "dayparts": sorted(_rows(t["dayparts"]), key=lambda r: (dp_order.get(r["name"], 99), r["name"])),
                  "rvcs": _rows(t["rvcs"], lambda n: {"place": rv_of[n][0], "rvc": rv_of[n][1], "locRef": rv_of[n][2]}),
                  "partners": sorted(({"name": n, **{p: {"value": round(v[p][0]), "count": v[p][1]} for p in PERIODS}}
                                      for n, v in t["partners"].items()), key=lambda x: -x["cur"]["value"])}
    return out


def cut(r, refs):
    """Location intelligence of some restaurants only (refs: upper-case Simphony references; None = all): their rows
    and the totals summed again from them."""
    if refs is None or not isinstance(r, dict) or not isinstance(r.get("locations"), list):
        return r
    rows = [x for x in r["locations"] if (x.get("locRef") or "").upper() in refs]
    deliv = _words((r.get("settings") or {}).get("delivery_order_types", ""))
    tot = summarise(rows, deliv, r.get("dp_order") or {})
    main = max(tot, key=lambda k: tot[k]["total"]["cur"]) if tot else ""
    return dict(r, locations=rows, totals=tot, currency=main, cut=True)


def build(periods, latest_fn, progress=None):
    c_from, c_to, p_from, p_to, ly_from, ly_to, running, label = periods[:8]
    settings = store.get_settings()
    L = settings.get("locations") or {}
    deliv_ot = _words(L.get("delivery_order_types", "deliver, wolt, glovo"))
    deliv_tm = _words(L.get("delivery_tenders", "wolt, glovo"))
    locs = [l for l in simphony.locations() if locintel.in_use(l)]
    fallback = (settings.get("group") or {}).get("reporting_currency") or "ALL"
    reg = {r["ref"]: r for r in locintel.registry()}
    mapping = simphony.loc_map()
    for ref, r in reg.items():
        if r.get("bc") and ref not in mapping:
            mapping[ref] = r["bc"]
    latest = {}
    for l in locs:
        try:
            latest[l["locRef"]] = latest_fn(l["locRef"])
        except Exception:                                          # noqa: BLE001
            latest[l["locRef"]] = None
    # like for like: the current period stops at the last closed business day; the others get as many days
    closed = [v for v in latest.values() if v]
    if closed:
        last_closed = date.fromisoformat(max(closed))
        if last_closed < c_to:
            c_to = max(c_from, last_closed)
    span = c_to - c_from
    p_to, ly_to = min(p_to, p_from + span), min(ly_to, ly_from + span)
    days = {"cur": _dates(c_from, c_to), "prev": _dates(p_from, p_to), "ly": _dates(ly_from, ly_to)}

    jobs = {}
    for l in locs:
        ref, last = l["locRef"], latest.get(l["locRef"])
        for p in PERIODS:
            for d in days[p]:
                if last and d > last:
                    continue
                jobs[("Q", ref, d)] = (lambda r=ref, dd=d, lt=last: simphony.order_type_hours(r, dd, lt))
                jobs[("T", ref, d)] = (lambda r=ref, dd=d, lt=last: simphony.tender_totals(r, dd, lt))
    total_jobs, done = len(jobs), 0
    results = {}
    if progress:
        progress(0, total_jobs)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(fn): k for k, fn in jobs.items()}
        for f in as_completed(futs):
            k = futs[f]
            try:
                results[k] = (f.result(), None)
            except Exception as exc:                               # noqa: BLE001
                results[k] = (None, exc)
            done += 1
            if progress and done % 20 == 0:
                progress(done, total_jobs)
    simphony.flush_days()
    errors = sorted({str(e)[:200] for v, e in results.values() if e})

    rows, totals, dp_order = [], {}, {}
    for l in locs:
        ref = l["locRef"]
        rg = reg.get(ref) or {}
        cur_code = rg.get("currency") or locintel.norm_currency(l.get("currency")) or fallback
        try:
            ots, rvcs = simphony.order_type_dims(ref), simphony.rvc_dims(ref)
        except Exception as exc:                                   # noqa: BLE001
            ots, rvcs = {}, {}
            errors.append(f"{ref}: dimensions: {str(exc)[:120]}")
        try:
            tms = simphony.tender_dims(ref)
        except Exception:                                          # noqa: BLE001
            tms = {}
        ch, rv, dp, hr, partners = {}, {}, {}, {}, {}
        dp_hour, read, missing = {}, 0, 0
        delivery = {p: 0.0 for p in PERIODS}
        tot = {p: 0.0 for p in PERIODS}
        for p in PERIODS:
            for d in days[p]:
                q, err = results.get(("Q", ref, d), (None, None))
                if q is None:
                    if ("Q", ref, d) in jobs:
                        missing += 1
                    continue
                if p == "cur":
                    read += 1
                for k, v in q.items():
                    rvc_n, ot_n, dpn, h = k.split("|")
                    otname = ots.get(int(ot_n)) if ot_n.lstrip("-").isdigit() else None
                    otname = otname or (f"Order type {ot_n}" if ot_n else "No order type")
                    rvname = (rvcs.get(int(rvc_n)) if rvc_n.lstrip("-").isdigit() else None) or f"Revenue centre {rvc_n}"
                    dpname = dpn or "No day part"
                    _add(ch.setdefault(otname, _blank())[p], v)
                    _add(rv.setdefault(rvname, _blank())[p], v)
                    _add(dp.setdefault(dpname, _blank())[p], v)
                    if h != "":
                        hh = int(h) % 24
                        hr.setdefault(hh, {x: 0.0 for x in PERIODS})[p] += v[0]
                        dp_hour[dpname] = min(dp_hour.get(dpname, 99), hh)
                    tot[p] += v[0]
                    if _matches(otname, deliv_ot):
                        delivery[p] += v[0]
                t, err = results.get(("T", ref, d), (None, None))
                for num, (amt, cnt) in (t or {}).items():
                    name = ((tms.get(int(num)) or {}).get("name") or "").strip() if str(num).isdigit() else ""
                    if name and _matches(name, deliv_tm):
                        x = partners.setdefault(name, {pp: [0.0, 0] for pp in PERIODS})[p]
                        x[0] += float(amt or 0)
                        x[1] += int(cnt or 0)
        if not any(tot.values()):
            continue
        dps = _rows(dp)
        dps.sort(key=lambda r: (dp_hour.get(r["name"], 99), r["name"]))
        for n, h in dp_hour.items():
            dp_order[n] = min(dp_order.get(n, 99), h)
        row = {
            "locRef": ref, "name": l.get("name") or ref, "reg_name": rg.get("name") or "", "brand": rg.get("brand") or "",
            "company": rg.get("company") or "", "in_registry": bool(rg), "currency": cur_code, "bc": mapping.get(ref, ""),
            "total": {p: round(tot[p]) for p in PERIODS},
            "delivery": {p: round(delivery[p]) for p in PERIODS},
            "channels": _rows(ch, lambda n: {"delivery": _matches(n, deliv_ot)}),
            "rvcs": _rows(rv), "dayparts": dps,
            "hours": [{"h": h, **{p: round(v[p]) for p in PERIODS}} for h, v in sorted(hr.items())],
            "partners": sorted(({"name": n, **{p: {"value": round(v[p][0]), "count": v[p][1]} for p in PERIODS}}
                                for n, v in partners.items()), key=lambda x: -x["cur"]["value"]),
            "days_read": read, "days_missing": missing, "latest": latest.get(ref),
        }
        rows.append(row)
    rows.sort(key=lambda r: -r["total"]["cur"])
    out_tot = summarise(rows, deliv_ot, dp_order)
    main = max(out_tot, key=lambda k: out_tot[k]["total"]["cur"]) if out_tot else ""
    return {"dp_order": dp_order,
        "available": True,
        "period": {"from": c_from.isoformat(), "to": c_to.isoformat(), "label": label, "running": running},
        "prev": {"from": p_from.isoformat(), "to": p_to.isoformat()},
        "ly": {"from": ly_from.isoformat(), "to": ly_to.isoformat()},
        "locations": rows, "totals": out_tot, "currency": main, "errors": errors[:20], "calls": total_jobs,
        "settings": {"delivery_order_types": ", ".join(deliv_ot), "delivery_tenders": ", ".join(deliv_tm)},
        "definitions": {
            "channels": "Channels are the order types of Simphony (getOrderTypeQuarterHourTotals): net sales as "
                        "Simphony totals them, after discounts, VAT included, by order type, revenue centre, day "
                        "part and hour. The three views add up to the same total.",
            "delivery": "Delivery is the order types whose name holds one of the words in Settings → Location "
                        "intelligence.",
            "partners": "Delivery partners are the tender media whose name holds one of the words in Settings: "
                        "what was paid through them, VAT and tips included, so it does not equal the delivery "
                        "order types' net sales.",
            "period": "The current period stops at the last business day Simphony has closed; the period before "
                      "and the same dates last year get as many days, so all three compare like for like.",
        },
    }


def request(periods, latest_fn, key, refresh=False):
    now = time.time()
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return {"pending": True, "elapsed": int(now - job["started"]), "done": job.get("done", 0),
                    "total": job.get("total", 0)}
        if job and job["state"] == "done" and not refresh and now - job["finished"] < KEEP_SECONDS:
            return job["result"]
        if job and job["state"] == "failed" and not refresh:
            _jobs.pop(key, None)
            return {"available": False, "error": job["error"]}
        job = _jobs[key] = {"state": "running", "started": now, "done": 0, "total": 0}

    def progress(done, total):
        with _lock:
            job.update(done=done, total=total)

    def run():
        try:
            result = build(periods, latest_fn, progress)
            state = {"state": "done", "result": result}
        except simphony.SimphonyError as exc:
            state = {"state": "failed", "error": str(exc)}
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            state = {"state": "failed", "error": f"The channels could not be read: {str(exc)[:300]}"}
        with _lock:
            _jobs[key] = dict(state, started=now, finished=time.time())
    threading.Thread(target=run, daemon=True, name="channels").start()
    return {"pending": True, "elapsed": 0, "done": 0, "total": 0}
