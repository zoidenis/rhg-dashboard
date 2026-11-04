"""Control → Takings POS ↔ BC: every restaurant and business day of the last weeks, what the till took by tender
(Simphony) against what Business Central received for it, tender by tender, on the restaurant's department.

  * Simphony: the payments of the day by tender media (getTenderMediaDailyTotals) and the day's totals (net sales
    with VAT, VAT, service charge, returns); paid out and paid in at the till apart;
  * the tender mapping (this screen, Finance → Settings permission): every tender is cash, card, online, on
    account, voucher or comp, and the money ones say which BC bank accounts or cash boxes receive them and in how
    many days (the lag); the proposal comes from the tender's category and name (BKT, Raiffeisen, Credins, POK,
    Paysera) and the bank accounts seen on the restaurants' departments;
  * Business Central: the bank account ledger entries on the restaurant's department (dimension set): receipts
    (debits; reversals of receipts taken off) on the mapped accounts; money going out of a card or online account
    on the restaurant is listed as a refund or chargeback to check.

Per restaurant and channel (the tenders that land on the same accounts), the day's takings are matched with the
receipts first in, first out: a receipt pays the oldest day still open. A day still open after its lag and the grace
days is a case; a day paid later than its lag is late; receipts with no takings left to pay are over. Cash expected =
cash tenders − paid out + paid in.

Tips, VAT and service charge: Simphony's net sales include VAT, the service charge is apart, and the tenders are what
was paid; tenders − (net sales + service charge) is what was paid on top (card tips keyed on the till, rounding).
How card tips are recorded is Content required until Finance confirms it.

Bank and terminal settlement extracts (the money that really reached the bank, fees, chargebacks notified by the
acquirer) are not read yet: this compares the till with what is posted in Business Central. Nothing is written to
Simphony or Business Central; the cases go to the Exceptions register (source "receipts_rec").
"""
import json
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import cashloc
import locintel
import simphony
import store

FILE = store.DATA_DIR / "receipts.json"
DEFAULTS = {"days": 21, "grace": 2, "tol_abs": 1000.0, "tol_pct": 0.5, "tips_pct": 2.0}
GROUPS = {"cash": "Cash", "card": "Card", "online": "Online and wallets", "account": "On account",
          "voucher": "Vouchers & gift cards", "comp": "Comps & staff meals", "paid_out": "Paid out at the till",
          "paid_in": "Paid in at the till", "other": "Other"}
MONEY = ("cash", "card", "online")
LAG = {"cash": 1, "card": 2, "online": 3}
ACQUIRERS = [("BKT", ("BKT", "KOMB")), ("RAIFFEISEN", ("RAIF", "RFB")), ("CREDINS", ("CREDINS",)),
             ("POK", ("POK",)), ("PAYSERA", ("PAYSERA",)), ("TIRANA BANK", ("TIRANA",)), ("INTESA", ("INTESA",))]
KINDS = {"not_received": "Not received in BC", "late": "Received late", "over": "Received with no takings",
         "refund": "Refund or chargeback", "tips": "Tips and other on top of sales", "unmapped": "Tender not mapped"}
_results, _jobs, _lock, _cfg_lock = {}, {}, threading.Lock(), threading.Lock()
on_built = None
KEEP = 900


# ---------------------------------------------------------------- configuration
def config(company):
    with _cfg_lock:
        try:
            d = json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
    c = d.get(str(company or "").strip().upper()) or {}
    p = dict(DEFAULTS, **{k: v for k, v in (c.get("params") or {}).items() if k in DEFAULTS})
    return {"params": p, "tenders": c.get("tenders") or {}, "saved_by": c.get("saved_by", ""),
            "saved_at": c.get("saved_at", "")}


def save_config(company, params, tenders, actor):
    p = {}
    for k, v in (params or {}).items():
        if k in DEFAULTS:
            try:
                p[k] = max(0.0, min(float(v), 1e9 if k == "tol_abs" else 120))
            except (TypeError, ValueError):
                continue
    t = {}
    for name, m in (tenders or {}).items():
        g = (m or {}).get("group")
        if g not in GROUPS:
            continue
        accts = [a.strip().upper() for a in str((m or {}).get("accounts") or "").replace(";", ",").split(",") if a.strip()]
        try:
            lag = int(float((m or {}).get("lag") if (m or {}).get("lag") not in (None, "") else LAG.get(g, 0)))
        except (TypeError, ValueError):
            lag = LAG.get(g, 0)
        t[str(name).strip().upper()] = {"group": g, "accounts": accts, "lag": max(0, min(lag, 30))}
    with _cfg_lock:
        try:
            d = json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        old = d.get(str(company).upper()) or {}
        d[str(company).upper()] = {"params": p or old.get("params") or {}, "tenders": t,
                                   "saved_by": actor, "saved_at": datetime.now().isoformat(timespec="seconds")}
        tmp = FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FILE)
    store.audit("receipts", f"tender mapping {company}", actor, {"tenders": len(old.get("tenders") or {})},
                {"tenders": len(t)}, "")
    with _lock:
        _results.pop(company, None)
    return config(company)


def propose(name, dims, accounts):
    """The proposed mapping of a tender: its group from Simphony's category and name; for money, the bank accounts
    seen on the restaurants whose name carries the acquirer named in the tender, else every account of that kind."""
    g = cashloc.tender_group(dims or {"name": name})
    n = str(name or "").upper()
    if g in ("paid_out", "paid_in"):
        return {"group": g, "accounts": [], "lag": 0, "proposal": True}
    if g == "card" and any(k in n for k in ("POK", "PAYSERA", "WOLT", "ONLINE", "PAYPAL", "STRIPE")):
        g = "online"
    if g not in GROUPS:
        g = "other"
    accts = []
    if g in MONEY:
        want = "cash" if g == "cash" else "online" if g == "online" else "bank"
        for acq, keys in ACQUIRERS:
            if any(k in n for k in keys):
                accts = [no for no, (nm, kind) in accounts.items() if any(k in f"{no} {nm}".upper() for k in keys)]
                break
        if not accts:
            accts = [no for no, (nm, kind) in accounts.items() if kind == want]
    return {"group": g, "accounts": sorted(accts), "lag": LAG.get(g, 0), "proposal": True}


# ---------------------------------------------------------------- the read
def _days(a, b):
    out, d = [], a
    while d <= b:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def build(client, company, latest_fn, parallel_fn, progress=None, today=None):
    progress = progress or (lambda *_: None)
    today = today or date.today()
    settings = store.get_settings()
    cfg = config(company)
    P = cfg["params"]
    b = today - timedelta(days=1)
    days = _days(b - timedelta(days=int(P["days"]) - 1), b)
    reg = [r for r in locintel.registry(settings) if (r.get("company") or "").upper() == (company or "").upper()
           and r.get("ref") and (r.get("dept") or r.get("bc"))]
    if not reg:
        raise ValueError(f"No restaurant of {company} has a Simphony reference and a department in Settings → "
                         "Location intelligence.")
    errors = []
    # ---- Simphony
    sim_on = bool(simphony.info().get("configured"))
    sim, dims = {}, {}
    if sim_on:
        progress("Simphony · tender media")
        latest = {ref: (v if not e else None) for ref, (v, e) in
                  parallel_fn({r["ref"]: (lambda x=r["ref"]: latest_fn(x)) for r in reg}).items()}
        for ref, (v, e) in parallel_fn({r["ref"]: (lambda x=r["ref"]: simphony.tender_dims(x)) for r in reg},
                                       workers=4).items():
            if e:
                errors.append(f"Simphony {ref}: tender media could not be read ({str(e)[:120]})")
            dims[ref] = v or {}
        jobs = {}
        for r in reg:
            lt = latest.get(r["ref"])
            for d in days:
                if lt and d > lt:
                    continue
                jobs[("t", r["ref"], d)] = (lambda x=r["ref"], dd=d, l=lt: simphony.tender_totals(x, dd, l))
                jobs[("o", r["ref"], d)] = (lambda x=r["ref"], dd=d, l=lt: simphony.daily_totals(x, dd, l))
        progress(f"Simphony · {len(jobs) // 2} restaurant-days")
        res = parallel_fn(jobs, workers=4)
        simphony.flush_days()
        failed = 0
        for (kind, ref, d), (v, e) in res.items():
            if e or v is None:
                failed += 1
                continue
            S = sim.setdefault(ref, {})
            day = S.setdefault(d, {"tenders": {}, "net": 0.0, "tax": 0.0, "service": 0.0, "returns": 0.0})
            if kind == "o":
                f = simphony.day_figures(v.get("rvcs") or [])
                for k in ("net", "tax", "service", "returns"):
                    day[k] += f[k]
            else:
                for num, (ttl, cnt) in v.items():
                    t = dims.get(ref, {}).get(int(num)) or {"name": f"Tender {num}"}
                    nm = str(t.get("name") or f"Tender {num}").strip()
                    x = day["tenders"].setdefault(nm, [0.0, 0, t])
                    x[0] += float(ttl or 0)
                    x[1] += int(cnt or 0)
        if failed:
            errors.append(f"Simphony: {failed} restaurant-days could not be read; they are left out.")
    else:
        errors.append("Simphony is not set up (Settings → Simphony sales): there is no till side.")
    # ---- Business Central: bank and cash-box entries on the departments, up to today (late receipts)
    progress("Business Central · bank and cash-box entries")
    rows = []
    try:
        sets = cashloc._dim_sets(client, company)
        rows = client.bank_entries_dim(company, days[0], today.isoformat())
    except Exception as exc:                                       # noqa: BLE001
        traceback.print_exc()
        errors.append(f"Business Central bank ledger: {str(exc)[:200]}")
        sets = {}
    depts = {(r.get("dept") or r.get("bc")).upper(): r for r in reg}
    accounts, recv, outs = {}, {}, []
    for e in rows:
        no = str(e.get("Bank_Account_No") or "").upper()
        nm = e.get("Bank_Account_Name") or no
        kind = cashloc.account_kind(no, nm)
        accounts[no] = (nm, kind)
        dept = (sets.get(int(e.get("Dimension_Set_ID") or 0), "") or "").upper()
        if dept not in depts:
            continue
        d = str(e.get("Posting_Date") or "")[:10]
        dr, cr = float(e.get("Debit_Amount_LCY") or 0), float(e.get("Credit_Amount_LCY") or 0)
        rev = str(e.get("Source_Code") or "").upper() == "REVERSAL"
        amt = (-cr if rev else dr) if (dr or cr) else 0.0
        if amt:
            x = recv.setdefault(dept, {}).setdefault(no, {})
            x[d] = x.get(d, 0.0) + amt
        if cr and not rev and kind in ("bank", "online"):
            outs.append({"dept": dept, "account": no, "name": nm, "date": d, "amount": round(cr, 2),
                         "document": e.get("Document_No") or "", "type": e.get("Document_Type") or "",
                         "entry": e.get("Entry_No")})
    # ---- the tender mapping (saved, else proposed) of every tender seen
    seen = {}
    for ref, S in sim.items():
        for d, day in S.items():
            for nm, (ttl, cnt, t) in day["tenders"].items():
                s = seen.setdefault(nm.upper(), {"name": nm, "dims": t, "total": 0.0, "count": 0, "locations": set()})
                s["total"] += ttl
                s["count"] += cnt
                s["locations"].add(ref)
    tmap = {}
    for key, s in seen.items():
        m = cfg["tenders"].get(key)
        tmap[key] = dict(m, proposal=False) if m else propose(s["name"], s["dims"], accounts)
    # ---- per restaurant: channels, matching, tips
    tol_abs, tol_pct = float(P["tol_abs"]), float(P["tol_pct"])
    grace = int(P["grace"])
    out = []
    for r in reg:
        dept = (r.get("dept") or r.get("bc")).upper()
        S = sim.get(r["ref"]) or {}
        R = recv.get(dept) or {}
        # channels: tenders whose accounts overlap are one channel
        chans = []
        for key, m in tmap.items():
            if m["group"] not in MONEY or not m["accounts"]:
                continue
            if not any(key in {k.upper() for k in day["tenders"]} for day in S.values()):
                continue
            acc = set(m["accounts"])
            hit = [c for c in chans if c["accounts"] & acc]
            if hit:
                c = hit[0]
                for o in hit[1:]:
                    c["accounts"] |= o["accounts"]
                    c["tenders"] |= o["tenders"]
                    c["groups"] |= o["groups"]
                    c["lag"] = max(c["lag"], o["lag"])
                    chans.remove(o)
                c["accounts"] |= acc
                c["tenders"].add(key)
                c["groups"].add(m["group"])
                c["lag"] = max(c["lag"], m["lag"])
            else:
                chans.append({"accounts": acc, "tenders": {key}, "groups": {m["group"]}, "lag": m["lag"]})
        L = {"ref": r["ref"], "name": r["name"], "dept": dept, "brand": r.get("brand") or "", "channels": [],
             "days": {}, "tips": None}
        for c in chans:
            exp = {}
            for d in days:
                day = S.get(d)
                if not day:
                    continue
                v = 0.0
                for nm, (ttl, cnt, t) in day["tenders"].items():
                    key = nm.upper()
                    m = tmap.get(key) or {}
                    if key in c["tenders"]:
                        v += ttl
                    elif "cash" in c["groups"] and cashloc.tender_group(t) in ("paid_out", "paid_in"):
                        v += -abs(ttl) if cashloc.tender_group(t) == "paid_out" else abs(ttl)
                exp[d] = v
            got = {}
            for no in c["accounts"]:
                for d, v in (R.get(no) or {}).items():
                    got[d] = got.get(d, 0.0) + v
            # a day's receipts pay that day first; then what is left pays the open days before it, within the lag
            # and the grace days, oldest first; then any open day before it (received late); the rest is over
            queue = [[d, exp[d], 0.0, None] for d in sorted(exp) if abs(exp[d]) >= 0.5]
            pool = [[d, v] for d, v in sorted(got.items()) if v > 0.005]

            def pay(q, x):
                take = min(x[1], q[1] - q[2])
                if take > 0.005:
                    q[2] += take
                    x[1] -= take
                    q[3] = max(q[3] or x[0], x[0])
            for q in queue:
                for x in pool:
                    if x[0] == q[0]:
                        pay(q, x)
            win = c["lag"] + grace
            for q in queue:
                for x in pool:
                    if q[0] < x[0] and (date.fromisoformat(x[0]) - date.fromisoformat(q[0])).days <= win:
                        pay(q, x)
            for q in queue:
                for x in pool:
                    if q[0] < x[0]:
                        pay(q, x)
            over = sum(x[1] for x in pool if days[0] <= x[0] <= days[-1])
            ch = {"key": "+".join(sorted(c["accounts"])), "accounts": sorted(c["accounts"]),
                  "account_names": [accounts.get(a, (a, ""))[0] for a in sorted(c["accounts"])],
                  "tenders": sorted(seen[k]["name"] for k in c["tenders"]), "groups": sorted(c["groups"]),
                  "lag": c["lag"], "taken": 0.0, "received": round(sum(v for d, v in got.items() if d >= days[0]), 2),
                  "matched": 0.0, "open": 0.0, "pending": 0.0, "late_days": 0, "open_days": 0, "ok_days": 0,
                  "over": round(over, 2), "days": []}
            for d, e_, paid, last in queue:
                age = (today - date.fromisoformat(d)).days
                rest = e_ - paid
                small = abs(rest) <= max(tol_abs, abs(e_) * tol_pct / 100)
                if small:
                    lag_used = (date.fromisoformat(last) - date.fromisoformat(d)).days if last else 0
                    st = "late" if lag_used > c["lag"] else "ok"
                elif age <= c["lag"] + grace:
                    st = "pending"
                else:
                    st = "open"
                ch["taken"] += e_
                ch["matched"] += paid
                if st == "open":
                    ch["open"] += rest
                    ch["open_days"] += 1
                elif st == "pending":
                    ch["pending"] += rest
                elif st == "late":
                    ch["late_days"] += 1
                else:
                    ch["ok_days"] += 1
                ch["days"].append({"date": d, "taken": round(e_, 2), "received": round(paid, 2),
                                   "outstanding": round(rest, 2), "paid_on": last, "status": st, "age": age})
                L["days"].setdefault(d, {})[ch["key"]] = st
            for k in ("taken", "matched", "open", "pending"):
                ch[k] = round(ch[k], 2)
            L["channels"].append(ch)
        # tips, VAT and service charge over the window
        net = sum(day["net"] for day in S.values())
        tax = sum(day["tax"] for day in S.values())
        svc = sum(day["service"] for day in S.values())
        ret = sum(day["returns"] for day in S.values())
        paid = sum(ttl for day in S.values() for nm, (ttl, cnt, t) in day["tenders"].items()
                   if cashloc.tender_group(t) not in ("paid_out", "paid_in"))
        by_group = {}
        for day in S.values():
            for nm, (ttl, cnt, t) in day["tenders"].items():
                g = (tmap.get(nm.upper()) or {}).get("group") or cashloc.tender_group(t)
                if g in ("paid_out", "paid_in"):
                    continue
                by_group[g] = by_group.get(g, 0.0) + ttl
        if S:
            L["tips"] = {"net": round(net, 2), "vat": round(tax, 2), "ex_vat": round(net - tax, 2),
                         "service": round(svc, 2), "returns": round(ret, 2), "tenders": round(paid, 2),
                         "on_top": round(paid - net - svc, 2),
                         "on_top_pct": round((paid - net - svc) / paid * 100, 2) if paid else None,
                         "by_group": {k: round(v, 2) for k, v in by_group.items()}}
        L["refunds"] = [o for o in outs if o["dept"] == dept and o["date"] >= days[0]
                        and any(o["account"] in c["accounts"] for c in chans)]
        L["unmapped"] = sorted({seen[k]["name"] for k, m in tmap.items() if (m["group"] == "other" or
                                (m["group"] in MONEY and not m["accounts"])) and r["ref"] in seen[k]["locations"]})
        out.append(L)
    tenders = sorted(({"key": k, "name": s["name"], "total": round(s["total"], 2), "count": s["count"],
                       "locations": sorted(s["locations"]), **tmap[k]} for k, s in seen.items()),
                     key=lambda x: -abs(x["total"]))
    kpis = {"taken": round(sum(c["taken"] for L in out for c in L["channels"]), 2),
            "matched": round(sum(c["matched"] for L in out for c in L["channels"]), 2),
            "open": round(sum(c["open"] for L in out for c in L["channels"]), 2),
            "pending": round(sum(c["pending"] for L in out for c in L["channels"]), 2),
            "over": round(sum(c["over"] for L in out for c in L["channels"]), 2),
            "open_days": sum(c["open_days"] for L in out for c in L["channels"]),
            "late_days": sum(c["late_days"] for L in out for c in L["channels"]),
            "ok_days": sum(c["ok_days"] for L in out for c in L["channels"]),
            "refunds": round(sum(o["amount"] for L in out for o in L["refunds"]), 2),
            "unmapped": len({t for L in out for t in L["unmapped"]})}
    return {"company": company, "from": days[0], "to": days[-1], "today": today.isoformat(), "days": days,
            "params": P, "locations": out, "tenders": tenders, "kpis": kpis, "groups": GROUPS, "kinds": KINDS,
            "accounts": [{"no": no, "name": nm, "kind": kind} for no, (nm, kind) in sorted(accounts.items())],
            "mapping": {"saved_by": cfg["saved_by"], "saved_at": cfg["saved_at"],
                        "saved": bool(cfg["tenders"])},
            "simphony": sim_on, "errors": errors, "complete": not errors,
            "checked_at": datetime.now().isoformat(timespec="seconds")}


# ---------------------------------------------------------------- the register
def findings(r):
    """The cases of a reconciliation for exreg.sync (source "receipts_rec")."""
    out, P = [], r["params"]
    tol = float(P["tol_abs"])
    for L in r["locations"]:
        for c in L["channels"]:
            what = " + ".join(c["account_names"]) or "the mapped accounts"
            for d in c["days"]:
                if d["status"] == "open":
                    out.append({"subject": f"{d['date']} {c['key']}", "kind": "not_received", "location": L["dept"],
                                "title": f"{L['name']} {d['date']}: {d['outstanding']:,.0f} of {', '.join(c['tenders'])} "
                                         f"not received in BC",
                                "detail": f"Taken {d['taken']:,.0f}, received {d['received']:,.0f} on {what} after "
                                          f"{d['age']} days (lag {c['lag']} + {int(P['grace'])} grace days).",
                                "severity": "critical" if d["outstanding"] > max(tol, 0.2 * abs(d["taken"])) else "warning",
                                "value": abs(d["outstanding"])})
            if c["over"] > max(tol, 0.005 * abs(c["taken"])):
                out.append({"subject": f"over {c['key']} {r['from']}", "kind": "over", "location": L["dept"],
                            "title": f"{L['name']}: {c['over']:,.0f} received on {what} with no takings to pay",
                            "detail": f"From {r['from']} to {r['to']} the receipts on the restaurant's department exceed "
                                      f"what {', '.join(c['tenders'])} took. Check deposits of earlier days, "
                                      f"transfers or receipts posted on the wrong department.",
                            "severity": "warning", "value": c["over"]})
        for o in L["refunds"]:
            out.append({"subject": f"out {o['entry']}", "kind": "refund", "location": L["dept"],
                        "title": f"{L['name']} {o['date']}: {o['amount']:,.0f} out of {o['name']}",
                        "detail": f"{o['type'] or 'Entry'} {o['document']} on the restaurant's department: a refund, "
                                  f"a chargeback or a fee. Check it against the bank or terminal statement.",
                        "severity": "warning", "value": o["amount"]})
        T = L.get("tips")
        if T and T["on_top_pct"] is not None and abs(T["on_top_pct"]) > float(P["tips_pct"]):
            out.append({"subject": f"tips {r['from']}", "kind": "tips", "location": L["dept"],
                        "title": f"{L['name']}: tenders {T['on_top_pct']:+.1f}% against sales and service charge",
                        "detail": f"Paid {T['tenders']:,.0f} against net sales {T['net']:,.0f} (VAT {T['vat']:,.0f}) and "
                                  f"service charge {T['service']:,.0f}: {T['on_top']:,.0f} on top, card tips or rounding. "
                                  f"How card tips are recorded is Content required from Finance.",
                        "severity": "info", "value": abs(T["on_top"])})
        for t in L["unmapped"]:
            out.append({"subject": f"tender {t}", "kind": "unmapped", "location": L["dept"],
                        "title": f"{L['name']}: tender {t} is not mapped to a BC account",
                        "detail": "Map it on Control → Takings POS ↔ BC so its takings can be matched.",
                        "severity": "warning", "value": 0})
    return out


def in_scope(r):
    days = set(r["days"])
    return lambda x: (x.get("subject") or "")[:10] in days or x.get("kind") in ("over", "refund", "tips", "unmapped")


# ---------------------------------------------------------------- held results and background reads
def held(company):
    with _lock:
        hit = _results.get(company)
    return hit[1] if hit else None


def remember(company, r):
    with _lock:
        _results[company] = (time.time(), r)


def companies(settings=None):
    return sorted({r["company"] for r in locintel.registry(settings) if r.get("company") and r.get("ref")})


def request(client, company, latest_fn, parallel_fn, refresh=False):
    with _lock:
        hit = _results.get(company)
        job = _jobs.get(company)
        if hit and not refresh and time.time() - hit[0] < KEEP and not (job and job["state"] == "running"):
            return hit[1]
        if job and job["state"] == "running":
            return {"pending": True, "stage": job["stage"]}
        if job and job["state"] == "failed" and not refresh and time.time() - job["at"] < 60:
            _jobs.pop(company, None)
            return {"available": False, "error": job["error"]}
        job = _jobs[company] = {"state": "running", "stage": "starting", "at": time.time(), "error": ""}

    def go():
        try:
            r = build(client, company, latest_fn, parallel_fn, progress=lambda st: job.update(stage=st))
            if on_built:
                try:
                    r["register"] = on_built(company, r)
                except Exception:                                  # noqa: BLE001
                    traceback.print_exc()
            remember(company, r)
            job["state"] = "done"
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            job.update(state="failed", error=str(exc)[:300], at=time.time())
    threading.Thread(target=go, daemon=True, name=f"receipts-{company}").start()
    return {"pending": True, "stage": "starting"}
