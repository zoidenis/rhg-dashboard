"""Purchasing → Prices & contracts → Negotiations: a proposed supplier price against the volume bought.

A scenario tool. Nothing is written to Business Central and nothing here is a realized saving.

Purchases of the period, per company (claude/negotiations-step2-method.md):
  * posted purchase invoices (Cancelled / Corrective left out) and posted purchase credit memos, their item
    lines; quantity in the item's base unit (Item Units of Measure; an empty unit is the base unit, a unit
    without a conversion leaves the line out, counted); amount = Amount, excluding VAT, after line and invoice
    discounts, in the document's currency (empty = the company currency);
  * a row = item + variant + vendor + currency: quantity = invoices − credit memos, net amount the same, the
    weighted average = net amount ÷ quantity when both are above 0.
Contract price (baseline A): the price list lines valid today (bc_client.price_lines: active, purchase, started,
not ended; a price list that includes VAT already taken back to excluding VAT), of the row's vendor (or every
vendor), item, variant and currency; per base unit after the line discount. A minimum quantity counts per invoice
line. One price = the contract; several different prices = "ambiguous"; else the weighted average (baseline B).
Savings = (baseline − proposed) × quantity, computed by the page as the price is typed and again for the export.

Kept in data/negotiations.db: the lines (a month read once, refreshed while it can change), the scenarios, their
prices and switches, and the audit trail of every change.
"""
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from calendar import monthrange
from datetime import date, timedelta

import auth
import classify
import config
import fx
import savings
import store

DB_PATH = config.BASE_DIR / "data" / "negotiations.db"
OPEN_DAYS = 45
STATUSES = ("Draft", "Under negotiation", "Agreed", "Rejected")
NO_SUB = "(no category group 4)"
_lock = threading.Lock()
_conn = None
_load_lock = threading.Lock()
_jobs, _jobs_lock = {}, threading.Lock()
_registry = {"at": 0, "rows": None, "error": ""}


def _db():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS lines (
                company TEXT, kind TEXT, doc TEXT, day TEXT, vendor TEXT, currency TEXT, location TEXT, dept TEXT,
                item TEXT, variant TEXT, uom TEXT, qty REAL, base_qty REAL, amount REAL, unit_ok INTEGER);
            CREATE INDEX IF NOT EXISTS lines_day ON lines (company, day);
            CREATE INDEX IF NOT EXISTS lines_item ON lines (company, item, vendor);
            CREATE TABLE IF NOT EXISTS loaded (company TEXT, month TEXT, date_to TEXT, at REAL,
                                               PRIMARY KEY (company, month));
            CREATE TABLE IF NOT EXISTS names (company TEXT, kind TEXT, code TEXT, name TEXT,
                                              PRIMARY KEY (company, kind, code));
            CREATE TABLE IF NOT EXISTS scenarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT, name TEXT, owner TEXT, target TEXT,
                status TEXT, notes TEXT, created_by TEXT, created_at TEXT, edited_by TEXT, edited_at TEXT);
            CREATE TABLE IF NOT EXISTS prices (
                scenario INTEGER, item TEXT, variant TEXT, vendor TEXT, currency TEXT, price REAL, note TEXT,
                PRIMARY KEY (scenario, item, variant, vendor, currency));
            CREATE TABLE IF NOT EXISTS switches (
                scenario INTEGER, item TEXT, variant TEXT, currency TEXT, to_vendor TEXT, to_name TEXT,
                price REAL, from_vendors TEXT, note TEXT, PRIMARY KEY (scenario, item, variant, currency));
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scenario INTEGER, at TEXT, who TEXT, target TEXT,
                field TEXT, old TEXT, new TEXT);
        """)
    return _conn


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- reading BC, a month at a time
def _month_bounds(month, today):
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, 1), min(date(y, m, monthrange(y, m)[1]), today)


def _fresh(company, month, b, today, force):
    with _lock:
        row = _db().execute("SELECT date_to, at FROM loaded WHERE company=? AND month=?", (company, month)).fetchone()
    if not row:
        return False
    date_to, at = row
    if date_to < b.isoformat():
        return False
    age = time.time() - at
    open_month = (today - b).days <= OPEN_DAYS
    if force and open_month and age > 120:
        return False
    ttl = 3600 if b >= today - timedelta(days=1) else (6 * 3600 if open_month else 30 * 86400)
    return age < ttl


def _read_lines(client, company, entity, docs):
    if not docs:
        return []
    first, last = min(docs), max(docs)
    return client._get_all(client._url(company, entity),
                           {"$filter": f"Type eq 'Item' and Document_No ge '{first}' and Document_No le '{last}'",
                            "$select": "Document_No,No,Variant_Code,Description,Quantity,Unit_of_Measure_Code,"
                                       "Amount,Shortcut_Dimension_1_Code"})


def load_month(client, company, month, today=None, force=False):
    today = today or date.today()
    a, b = _month_bounds(month, today)
    if a > today or _fresh(company, month, b, today, force):
        return False
    uom = savings._uoms(client, company)
    rows, items, vendors = [], {}, {}
    for kind, heads, entity in (("inv", client.posted_invoice_headers(company, a.isoformat(), b.isoformat()),
                                 config.BC_PINV_LINE_ENTITY),
                                ("cr", client.posted_crmemo_headers(company, a.isoformat(), b.isoformat()),
                                 config.BC_PCR_LINE_ENTITY)):
        live = {str(h["No"]): h for h in heads if h.get("No") and not (h.get("Cancelled") or h.get("Corrective"))}
        for ln in _read_lines(client, company, entity, list(live)):
            h = live.get(str(ln.get("Document_No")))
            item, qty = str(ln.get("No") or ""), float(ln.get("Quantity") or 0)
            if not h or not item or not qty:
                continue
            code = str(ln.get("Unit_of_Measure_Code") or "").upper()
            per = 1.0 if not code else uom.get((item, code))
            vendor = str(h.get("Buy_from_Vendor_No") or "")
            rows.append((company, kind, str(h["No"]), str(h.get("Posting_Date"))[:10], vendor,
                         str(h.get("Currency_Code") or ""), str(h.get("Location_Code") or ""),
                         str(ln.get("Shortcut_Dimension_1_Code") or h.get("Shortcut_Dimension_1_Code") or ""),
                         item, str(ln.get("Variant_Code") or ""), code, qty, qty * per if per else None,
                         float(ln.get("Amount") or 0), 1 if per else 0))
            if ln.get("Description"):
                items[item] = ln["Description"].strip()
            vendors[vendor] = h.get("Buy_from_Vendor_Name") or vendor
    with _lock:
        db = _db()
        db.execute("DELETE FROM lines WHERE company=? AND day>=? AND day<=?",
                   (company, a.isoformat(), date(a.year, a.month, monthrange(a.year, a.month)[1]).isoformat()))
        db.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.executemany("INSERT OR REPLACE INTO names VALUES (?,?,?,?)",
                       [(company, "item", k, v) for k, v in items.items()]
                       + [(company, "vendor", k, v) for k, v in vendors.items()])
        db.execute("INSERT OR REPLACE INTO loaded VALUES (?,?,?,?)", (company, month, b.isoformat(), time.time()))
        db.commit()
    return True


def _start_load(client, company, months, force):
    with _jobs_lock:
        job = _jobs.get(company)
        if job and job["state"] == "running":
            return job
        job = _jobs[company] = {"state": "running", "done": 0, "total": len(months), "errors": []}

    def run():
        errors = []
        with _load_lock:
            for m in months:
                try:
                    load_month(client, company, m, force=force)
                except Exception as exc:                               # noqa: BLE001
                    errors.append(f"{m}: {str(exc)[:200]}")
                with _jobs_lock:
                    job["done"] += 1
        with _jobs_lock:
            job.update(state="done", errors=errors)
    threading.Thread(target=run, daemon=True, name="negotiations-load").start()
    return job


def ensure(client, company, a, b, refresh=False):
    """None when the months are held; else the progress of the read that is running."""
    today = date.today()
    months = savings.months_between(a, b)
    missing = [m for m in months if not _fresh(company, m, _month_bounds(m, today)[1], today, False)]
    if refresh:
        missing = sorted(set(missing) | {m for m in months if m >= (today - timedelta(days=OPEN_DAYS)).strftime("%Y-%m")})
    with _jobs_lock:
        job = dict(_jobs.get(company) or {})
    if job.get("state") == "running" or missing:
        if job.get("state") != "running":
            job = dict(_start_load(client, company, sorted(missing, reverse=True), refresh))
        return {"done": job.get("done", 0), "total": job.get("total", 0)}
    return None


def last_errors(company):
    with _jobs_lock:
        return list((_jobs.get(company) or {}).get("errors") or [])


# ---------------------------------------------------------------- brand (Finance's location registry)
def registry():
    """(rows, error): the restaurants of Finance → Settings → Location intelligence, asked of Finance."""
    if time.time() - _registry["at"] < 600 and _registry["rows"] is not None:
        return _registry["rows"], _registry["error"]
    req = urllib.request.Request(config.FINANCE_URL.rstrip("/") + "/internal/registry",
                                 headers={"X-Service-Token": auth.SERVICE_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rows, err = json.loads(r.read() or b"{}").get("registry") or [], ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        rows, err = [], f"The brands could not be read from Finance: {str(exc)[:120]}"
    _registry.update(at=time.time(), rows=rows, error=err)
    return rows, err


# ---------------------------------------------------------------- the view
def _f(v, d=4):
    return None if v is None else round(v, d)


def build(client, company, a, b, f, settings=None, today=None):
    """Every row of the period (item + variant + vendor + currency) with its baseline, after the filters."""
    settings = settings or store.get_settings()
    today = today or date.today()
    tol = float(settings["thresholds"].get("price_above_contract_pct") or 2)
    expiry_days = int(settings["thresholds"].get("contract_expiry_days") or 30)
    local_cur = f.get("_company_currency") or "ALL"
    reg, reg_err = registry()
    brands = sorted({(r.get("brand") or "").strip() for r in reg if (r.get("company") or "") == company} - {""})
    brand_locs = {(r.get("bc") or "").upper() for r in reg if (r.get("brand") or "") == f.get("brand") and r.get("company") == company} if f.get("brand") else None
    brand_depts = {(r.get("dept") or "").upper() for r in reg if (r.get("brand") or "") == f.get("brand") and r.get("company") == company} if f.get("brand") else None

    try:
        vcards = {str(v.get("No") or ""): v for v in client.vendor_cards(company)}
    except Exception:                                                  # noqa: BLE001
        vcards = {}
    with _lock:
        lines = _db().execute("SELECT kind, doc, day, vendor, currency, location, dept, item, variant, uom, qty, "
                              "base_qty, amount, unit_ok FROM lines WHERE company=? AND day>=? AND day<=?",
                              (company, a.isoformat(), b.isoformat())).fetchall()
        names = dict(_db().execute("SELECT kind || '|' || code, name FROM names WHERE company=?", (company,)).fetchall())
    options = {"locations": set(), "depts": set(), "vendors": set(), "currencies": set(), "purchasers": set()}
    rows = {}
    for kind, doc, day, vendor, cur, loc, dept, item, variant, uom, qty, bq, amt, ok in lines:
        cur = cur or local_cur
        pur = str((vcards.get(vendor) or {}).get("Purchaser_Code") or "")
        options["locations"].add(loc)
        options["depts"].add(dept)
        options["vendors"].add(vendor)
        options["currencies"].add(cur)
        options["purchasers"].add(pur)
        if f.get("location") and loc != f["location"]:
            continue
        if f.get("dept") and dept != f["dept"]:
            continue
        if brand_locs is not None and loc.upper() not in brand_locs and dept.upper() not in brand_depts:
            continue
        if f.get("vendor") and vendor != f["vendor"]:
            continue
        if f.get("purchaser") and pur != f["purchaser"]:
            continue
        if f.get("currency") and cur != f["currency"]:
            continue
        k = (item, variant, vendor, cur)
        r = rows.setdefault(k, {"inv_qty": 0.0, "inv_amt": 0.0, "inv_n": 0, "cr_qty": 0.0, "cr_amt": 0.0, "cr_n": 0,
                                "no_unit": 0, "blank_unit": 0, "max_line": 0.0, "locs": {}})
        if not ok:
            r["no_unit"] += 1
            continue
        if not uom:
            r["blank_unit"] += 1
        if kind == "inv":
            r["inv_qty"] += bq
            r["inv_amt"] += amt
            r["inv_n"] += 1
            r["max_line"] = max(r["max_line"], bq)
            r["locs"][loc or dept] = r["locs"].get(loc or dept, 0.0) + amt
        else:
            r["cr_qty"] += bq
            r["cr_amt"] += amt
            r["cr_n"] += 1

    # contracts: per (item, variant, currency) the lines of each vendor ("" = every vendor)
    uom = savings._uoms(client, company)
    plines, zero, pstatus = client.price_lines(company)
    contracts = {}
    for p in plines:
        it, cur = str(p["item"]), (p.get("currency") or "") or local_cur
        code = str(p.get("uom") or "").upper()
        per = 1.0 if not code else uom.get((it, code))
        base = None if not per else p["price"] * (1 - (p.get("line_disc") or 0) / 100) / per
        contracts.setdefault((it, p.get("variant") or "", cur), []).append(dict(
            vendor=str(p.get("vendor") or ""), list=p.get("list") or "", price=_f(base), uom=code or "(base)",
            per=per, as_listed=p.get("price_as_listed"), vat_pct=p.get("vat_pct"), line_disc=p.get("line_disc") or 0,
            min_qty=p.get("min_qty") or 0, starting=p.get("starting") or "", ending=p.get("ending") or "",
            unit_missing=not per))
    zero_keys = {(z["item"], z["vendor"]) for z in zero}

    catalog = {}
    try:
        catalog = client.item_catalog(company)
    except Exception:                                                  # noqa: BLE001
        pass
    maps = classify.parse_maps(settings.get("classification") or {})
    soon = (today + timedelta(days=expiry_days)).isoformat()
    out = []
    for (item, variant, vendor, cur), r in rows.items():
        card = catalog.get(item) or {}
        ptype = classify.classify_item(card, maps)[0] if card else "Unclassified"
        sub = (card.get("g4") or "").strip() or NO_SUB
        parent = (card.get("g3") or "").strip()
        qty = r["inv_qty"] - r["cr_qty"]
        net = r["inv_amt"] - r["cr_amt"]
        avg = net / qty if qty > 0 and net > 0 else None
        cands = contracts.get((item, variant, cur), [])
        own = [c for c in cands if c["vendor"] in (vendor, "")]
        usable = []
        for c in own:
            if c["unit_missing"] or c["price"] is None:
                continue
            if c["min_qty"] and (c["per"] and r["max_line"] / c["per"] < c["min_qty"]):
                continue                                   # no invoice line reached the minimum quantity
            usable.append(c)
        distinct = sorted({round(c["price"], 4) for c in usable})
        flags, reason = [], ""
        if len(distinct) == 1:
            contract = distinct[0]
            source = "contract"
        elif len(distinct) > 1:
            contract, source = None, "ambiguous"
            flags.append("ambiguous")
        else:
            contract, source = None, "average"
        if source == "contract":
            baseline = contract
        elif source == "average":
            baseline = avg
        else:
            baseline = None
        if baseline is None:
            if source == "ambiguous":
                reason = "Ambiguous contract: several different prices apply"
            elif any(c["unit_missing"] for c in own):
                reason = "The contract's unit has no conversion to the base unit"
            elif (item, vendor) in zero_keys and not own:
                reason = "Contract price 0"
            elif r["no_unit"] and not (r["inv_n"] or r["cr_n"]):
                reason = "Unit conversion missing on every line"
            else:
                reason = "No net purchase in the period"
        if r["no_unit"]:
            flags.append("unit_missing")
        if contract is not None and avg is not None and avg > contract * (1 + tol / 100):
            flags.append("above_contract")
        if not own and any(c["vendor"] not in ("", vendor) for c in cands):
            flags.append("other_vendor")
        if any(c["min_qty"] for c in own) and source != "contract":
            flags.append("below_min")
        if any(c["ending"][:4] > "0001" and c["ending"] <= soon for c in usable):
            flags.append("ends_soon")
        if f.get("contract") == "with" and source != "contract":
            continue
        if f.get("contract") == "without" and source == "contract":
            continue
        if f.get("ptype") and ptype != f["ptype"]:
            continue
        if f.get("sub") and sub != f["sub"]:
            continue
        desc = card.get("desc") or names.get("item|" + item, "")
        ql = (f.get("q") or "").strip().lower()
        if ql and ql not in item.lower() and ql not in desc.lower() and ql not in (card.get("barcode") or "").lower():
            continue
        out.append({
            "item": item, "variant": variant, "vendor": vendor, "vendor_name": names.get("vendor|" + vendor, vendor),
            "currency": cur, "desc": desc, "barcode": card.get("barcode") or "", "unit": card.get("uom") or "",
            "ptype": ptype, "sub": sub, "parent": parent,
            "purchaser": str((vcards.get(vendor) or {}).get("Purchaser_Code") or ""),
            "qty": _f(qty, 3), "net": _f(net, 2), "avg": _f(avg),
            "inv": [_f(r["inv_qty"], 3), _f(r["inv_amt"], 2), r["inv_n"]],
            "cr": [_f(r["cr_qty"], 3), _f(r["cr_amt"], 2), r["cr_n"]],
            "blank_unit": r["blank_unit"], "no_unit": r["no_unit"],
            "baseline": _f(baseline), "source": source, "contract": _f(contract), "reason": reason,
            "contracts": own, "flags": flags,
            "top_loc": max(r["locs"].items(), key=lambda kv: kv[1])[0] if r["locs"] else "",
        })
    out.sort(key=lambda x: -(x["net"] or 0))
    subs = sorted({x["sub"] for x in out})
    return {
        "rows": out, "currency": local_cur,
        "options": {"locations": sorted(options["locations"] - {""}), "depts": sorted(options["depts"] - {""}),
                    "vendors": sorted(({"no": v, "name": names.get("vendor|" + v, v)} for v in options["vendors"] - {""}),
                                      key=lambda x: x["name"].lower()),
                    "currencies": sorted(options["currencies"]), "purchasers": sorted(options["purchasers"] - {""}),
                    "brands": brands, "ptypes": sorted({x["ptype"] for x in out}), "subs": subs},
        "brand_error": reg_err, "price_status": {k: pstatus.get(k) for k in ("available", "entity", "vat_note", "reason")},
        "tolerance": tol, "expiry_days": expiry_days,
    }


def fx_rates(client, company, local_cur, currencies, a, b):
    """{currency: rate in the company currency} at the period's average of BC's Currency Exchange Rates (page 483),
    the method of the Executive group view; and what could not be converted."""
    need = sorted(c for c in currencies if c and c != local_cur)
    if not need:
        return {}, [], ""
    try:
        series, _skipped, errs = fx.parse(client.exchange_rates(company))
    except Exception as exc:                                           # noqa: BLE001
        return {}, need, f"Currency Exchange Rates could not be read: {str(exc)[:120]}"
    rates, missing = {}, []
    for c in need:
        r = fx.average(series.get(c), a, b)
        if r is None:
            missing.append(c)
        else:
            rates[c] = round(r, 6)
    return rates, missing, "; ".join(errs)


# ---------------------------------------------------------------- scenarios
def scenarios(company):
    with _lock:
        db = _db()
        heads = db.execute("SELECT id, name, owner, target, status, notes, created_by, created_at, edited_by, "
                           "edited_at FROM scenarios WHERE company=? ORDER BY id", (company,)).fetchall()
        out = []
        for h in heads:
            prices = db.execute("SELECT item, variant, vendor, currency, price, note FROM prices WHERE scenario=?",
                                (h[0],)).fetchall()
            sw = db.execute("SELECT item, variant, currency, to_vendor, to_name, price, from_vendors, note "
                            "FROM switches WHERE scenario=?", (h[0],)).fetchall()
            out.append({"id": h[0], "name": h[1], "owner": h[2], "target": h[3], "status": h[4], "notes": h[5],
                        "created_by": h[6], "created_at": h[7], "edited_by": h[8], "edited_at": h[9],
                        "prices": [{"item": p[0], "variant": p[1], "vendor": p[2], "currency": p[3], "price": p[4],
                                    "note": p[5]} for p in prices],
                        "switches": [{"item": s[0], "variant": s[1], "currency": s[2], "to_vendor": s[3],
                                      "to_name": s[4], "price": s[5], "from": json.loads(s[6] or "[]"),
                                      "note": s[7]} for s in sw]})
    return out


def _audit(db, scenario, who, target, field, old, new):
    if (old if old is not None else "") == (new if new is not None else ""):
        return
    db.execute("INSERT INTO audit (scenario, at, who, target, field, old, new) VALUES (?,?,?,?,?,?,?)",
               (scenario, _now(), who, target, field, None if old is None else str(old),
                None if new is None else str(new)))


def save_scenario(company, body, who):
    name = str(body.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("Give the scenario a name.")
    status = body.get("status") or "Draft"
    if status not in STATUSES:
        raise ValueError("Unknown status.")
    vals = {"name": name, "owner": str(body.get("owner") or "").strip()[:80],
            "target": str(body.get("target") or "")[:10], "status": status,
            "notes": str(body.get("notes") or "")[:2000]}
    with _lock:
        db = _db()
        sid = body.get("id")
        if sid:
            row = db.execute("SELECT name, owner, target, status, notes FROM scenarios WHERE id=? AND company=?",
                             (int(sid), company)).fetchone()
            if not row:
                raise ValueError("Scenario not found.")
            for (k, v), old in zip(vals.items(), row):
                _audit(db, int(sid), who, "scenario", k, old, v)
            db.execute("UPDATE scenarios SET name=?, owner=?, target=?, status=?, notes=?, edited_by=?, edited_at=? "
                       "WHERE id=?", (*vals.values(), who, _now(), int(sid)))
        else:
            cur = db.execute("INSERT INTO scenarios (company, name, owner, target, status, notes, created_by, "
                             "created_at, edited_by, edited_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (company, *vals.values(), who, _now(), who, _now()))
            sid = cur.lastrowid
            _audit(db, sid, who, "scenario", "created", None, name)
        db.commit()
    return int(sid)


def _check(db, company, sid):
    row = db.execute("SELECT status FROM scenarios WHERE id=? AND company=?", (int(sid), company)).fetchone()
    if not row:
        raise ValueError("Scenario not found.")


def set_price(company, body, who):
    sid = int(body.get("scenario") or 0)
    key = tuple(str(body.get(k) or "") for k in ("item", "variant", "vendor", "currency"))
    if not key[0] or not key[2]:
        raise ValueError("Item and vendor are required.")
    price = body.get("price")
    if price in ("", None):
        price = None
    else:
        price = float(price)
        if price < 0:
            raise ValueError("A price cannot be negative.")
    note = str(body.get("note") or "")[:500]
    target = f"{key[0]}{'/' + key[1] if key[1] else ''} · {key[2]} · {key[3]}"
    with _lock:
        db = _db()
        _check(db, company, sid)
        old = db.execute("SELECT price, note FROM prices WHERE scenario=? AND item=? AND variant=? AND vendor=? "
                         "AND currency=?", (sid, *key)).fetchone()
        _audit(db, sid, who, target, "proposed price", old[0] if old else None, price)
        _audit(db, sid, who, target, "note", old[1] if old else None, note or None)
        if price is None and not note:
            db.execute("DELETE FROM prices WHERE scenario=? AND item=? AND variant=? AND vendor=? AND currency=?",
                       (sid, *key))
        else:
            db.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?)", (sid, *key, price, note))
        db.execute("UPDATE scenarios SET edited_by=?, edited_at=? WHERE id=?", (who, _now(), sid))
        db.commit()
    return True


def set_switch(company, body, who):
    sid = int(body.get("scenario") or 0)
    key = tuple(str(body.get(k) or "") for k in ("item", "variant", "currency"))
    if not key[0]:
        raise ValueError("Item is required.")
    target = f"{key[0]} · switch · {key[2]}"
    with _lock:
        db = _db()
        _check(db, company, sid)
        old = db.execute("SELECT to_vendor, to_name, price, from_vendors FROM switches WHERE scenario=? AND item=? "
                         "AND variant=? AND currency=?", (sid, *key)).fetchone()
        if body.get("remove"):
            db.execute("DELETE FROM switches WHERE scenario=? AND item=? AND variant=? AND currency=?", (sid, *key))
            _audit(db, sid, who, target, "switch", f"{old[0] or old[1]} at {old[2]}" if old else None, None)
        else:
            price = float(body.get("price"))
            if price < 0:
                raise ValueError("A price cannot be negative.")
            to_vendor = str(body.get("to_vendor") or "")
            to_name = str(body.get("to_name") or "").strip()[:80]
            if not to_vendor and not to_name:
                raise ValueError("Choose the proposed vendor, or name the new supplier.")
            frm = [str(v) for v in (body.get("from") or [])][:50]
            if not frm:
                raise ValueError("Choose the current vendors whose volume moves.")
            _audit(db, sid, who, target, "switch", f"{old[0] or old[1]} at {old[2]}" if old else None,
                   f"{to_vendor or to_name} at {price} from {', '.join(frm)}")
            db.execute("INSERT OR REPLACE INTO switches VALUES (?,?,?,?,?,?,?,?,?)",
                       (sid, *key, to_vendor, to_name, price, json.dumps(frm), str(body.get("note") or "")[:500]))
        db.execute("UPDATE scenarios SET edited_by=?, edited_at=? WHERE id=?", (who, _now(), sid))
        db.commit()
    return True


def audit_of(company, sid, target_prefix=""):
    with _lock:
        db = _db()
        _check(db, company, sid)
        rows = db.execute("SELECT at, who, target, field, old, new FROM audit WHERE scenario=? AND target LIKE ? "
                          "ORDER BY id DESC LIMIT 200", (int(sid), target_prefix + "%")).fetchall()
    return [dict(zip(("at", "who", "target", "field", "old", "new"), r)) for r in rows]


# ---------------------------------------------------------------- one row in detail
def detail(company, a, b, item, variant, vendor, currency, local_cur, weekly=None):
    cur_clause = "(currency=? OR (currency='' AND ?=?))"
    with _lock:
        rows = _db().execute(
            "SELECT kind, doc, day, location, dept, uom, qty, base_qty, amount, unit_ok FROM lines WHERE company=? "
            "AND item=? AND variant=? AND vendor=? AND day>=? AND day<=? AND " + cur_clause + " ORDER BY day DESC, doc",
            (company, item, variant, vendor, a.isoformat(), b.isoformat(), currency, currency, local_cur)).fetchall()
    weekly = weekly if weekly is not None else (b - a).days <= 62
    buckets = {}
    for kind, doc, day, loc, dept, uom, qty, bq, amt, ok in rows:
        if not ok:
            continue
        d = date.fromisoformat(day)
        key = (d - timedelta(days=d.weekday())).isoformat() if weekly else day[:7]
        x = buckets.setdefault(key, [0.0, 0.0])
        sign = 1 if kind == "inv" else -1
        x[0] += sign * bq
        x[1] += sign * amt
    return {"by": "week" if weekly else "month",
            "series": [{"key": k, "qty": round(v[0], 3), "amount": round(v[1], 2),
                        "avg": round(v[1] / v[0], 4) if v[0] > 0 and v[1] > 0 else None} for k, v in sorted(buckets.items())],
            "lines": [{"kind": r[0], "doc": r[1], "day": r[2], "location": r[3] or r[4], "uom": r[5] or "(base)",
                       "qty": r[6], "base_qty": r[7], "amount": r[8], "unit_ok": bool(r[9])} for r in rows[:400]],
            "count": len(rows)}


# ---------------------------------------------------------------- export
def render_xlsx(view, scenario, meta):
    """The filtered table with the scenario's prices and the assumptions."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Negotiations"
    head_fill, bold = PatternFill("solid", fgColor="EFEDE6"), Font(bold=True)
    prices = {(p["item"], p["variant"], p["vendor"], p["currency"]): p for p in (scenario or {}).get("prices", [])}
    cols = ["Company", "Item No.", "Description", "Purchase type", "Sub-category", "Vendor No.", "Vendor", "Currency",
            "Quantity (base unit)", "Base unit", "Baseline / unit", "Baseline source", "Contract / unit",
            "Paid, weighted average", "Proposed / unit", "Difference / unit", "Potential Savings at Historical Volume",
            "% change", "Flags", "Calculation unavailable", "Note"]
    ws.append(cols)
    for c in ws[1]:
        c.font, c.fill = bold, head_fill
    src = {"contract": "Active contracted price", "average": "Weighted average purchase price",
           "ambiguous": "Ambiguous contract"}
    totals = {}
    for r in view["rows"]:
        p = prices.get((r["item"], r["variant"], r["vendor"], r["currency"]))
        prop = p["price"] if p else None
        diff = sav = pct = None
        if prop is not None and r["baseline"] is not None and r["qty"]:
            diff = r["baseline"] - prop
            sav = diff * r["qty"]
            pct = (prop - r["baseline"]) / r["baseline"] if r["baseline"] else None
            totals[r["currency"]] = totals.get(r["currency"], 0.0) + sav
        ws.append([meta["company"], r["item"] + (f"/{r['variant']}" if r["variant"] else ""), r["desc"], r["ptype"],
                   r["sub"], r["vendor"], r["vendor_name"], r["currency"], r["qty"], r["unit"], r["baseline"],
                   src.get(r["source"], r["source"]), r["contract"], r["avg"], prop, diff, sav, pct,
                   ", ".join(r["flags"]) or "", r["reason"] or "", (p or {}).get("note") or ""])
    for col, fmtx in (("I", "#,##0.000"), ("K", "#,##0.0000"), ("M", "#,##0.0000"), ("N", "#,##0.0000"),
                      ("O", "#,##0.0000"), ("P", "#,##0.0000"), ("Q", "#,##0;[Red]-#,##0"), ("R", "0.0%")):
        for cell in ws[col][1:]:
            cell.number_format = fmtx
    for i, w in enumerate([10, 12, 34, 18, 20, 10, 28, 9, 14, 9, 13, 26, 13, 13, 13, 13, 18, 9, 22, 30, 30], 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws2 = wb.create_sheet("Switch supplier")
    ws2.append(["Item No.", "Currency", "Proposed vendor", "Proposed / unit", "Current vendor", "Quantity",
                "Current baseline", "Potential savings"])
    for c in ws2[1]:
        c.font, c.fill = bold, head_fill
    by_key = {(r["item"], r["variant"], r["vendor"], r["currency"]): r for r in view["rows"]}
    for s in (scenario or {}).get("switches", []):
        for v in s["from"]:
            r = by_key.get((s["item"], s["variant"], v, s["currency"]))
            if not r or r["baseline"] is None:
                continue
            sav = (r["baseline"] - s["price"]) * r["qty"]
            totals[s["currency"]] = totals.get(s["currency"], 0.0) + sav
            ws2.append([s["item"], s["currency"], s["to_vendor"] or s["to_name"], s["price"], v, r["qty"],
                        r["baseline"], sav])
    ws3 = wb.create_sheet("Assumptions")
    for k, v in meta["assumptions"]:
        ws3.append([k, v])
    ws3.append([])
    ws3.append(["Potential Savings at Historical Volume (rows and switches), by currency"])
    for c, v in sorted(totals.items()):
        ws3.append([c, round(v, 2)])
    ws3.column_dimensions["A"].width = 46
    ws3.column_dimensions["B"].width = 90
    import io
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
