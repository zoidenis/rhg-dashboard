"""Control → Simphony ↔ BC: the daily sales reconciliation, every restaurant and business day of the last weeks.

Three figures for each restaurant (Settings → Locations registry: the Simphony reference, the BC customer the POS
sales are invoiced to) and day:
  * Simphony: what the till sold, by menu item (getMenuItemDailyTotals, VAT included, before discounts, less the
    item discounts), kept in data/menu_items.db with the menu engineering days;
  * Imported: the POS import of Business Central (MID Simphony Archive): per menu item the line total, the line
    discount, the BC item it was posted as, and whether it went into a sales document (Postuar);
  * Invoiced: the sales invoices (less credit memos) of the restaurant's customer on that posting date in the
    customer ledger, and what is still open on them (the takings not yet applied).
The checks, each one an exception in the register with its owner, closed by the data when it is gone:
  * Not imported: Simphony sold, nothing imported after the import lag (default 2 days);
  * Import ≠ Simphony: the imported total differs beyond the tolerance (the menu items that differ are listed);
  * Not posted: imported lines not in a sales document, or no invoice, after the posting lag (default 4 days);
  * Invoice ≠ import: the posted invoice differs from the import (or from Simphony when nothing was imported);
  * Not settled: the day's invoice still open (cash, cards, online not applied) after the settle days (default 7);
  * Item mapping, per menu item over the period: imported with no BC item, an item BC does not know, a blocked
    item, an item that needs a recipe and has none, or a menu item Simphony sold that the import never carries.
A day still within its lag is "Pending", not an exception. Nothing is written to Simphony or Business Central.
"""
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import config
import locintel
import menueng
import recipes
import simphony
import heldjob
import store

E_ARCHIVE = config.E_POS_ARCHIVE
E_SINV, E_SCM = config.get("E_SALES_INV", "PBSI1"), config.get("E_SALES_CM", "PBSC1")
DEFAULTS = {"days": "14", "import_lag": "2", "post_lag": "4", "settle_days": "7", "tol_abs": "1000", "tol_pct": "0.5"}
MAX_RANGE = 93
# "Open" = the day's sales are not posted in BC: RHG does not post a day whose value is wrong (Geri, 13 Nov 2026).
# What is still open on a posted invoice (the takings not applied to it) is shown, but checked in Takings POS ↔ BC.
STATUS = {"not_imported": "Not imported", "import_diff": "Import ≠ Simphony", "invoice_diff": "Invoice ≠ import",
          "not_posted": "Open: not posted in BC", "pending": "Not imported yet (within the lag)",
          "open_wait": "Open: not posted yet (within the lag)", "ok": "Reconciled", "no_sales": "No sales"}
RANK = ("not_imported", "import_diff", "invoice_diff", "not_posted", "pending", "open_wait", "ok", "no_sales")
MAPPING = {"no_item": "Imported with no BC item", "unknown_item": "BC item does not exist",
           "blocked_item": "BC item is blocked", "no_recipe": "Needs a recipe, has none",
           "not_in_import": "Sold in Simphony, never imported"}
# each case explained on the screen: what it means, the usual causes, what to do, who owns it
HELP = {
    "not_imported": {"means": "Simphony closed the business day with sales, and nothing of that day is in the POS import "
                              "of Business Central (MID Simphony Archive) after the import lag.",
                     "causes": "The daily import job did not run or failed for the restaurant; the file was imported "
                               "under another customer or location code; the day was imported on another transaction "
                               "date.",
                     "fix": "Run the MID Simphony import for the day, or check the import log for the restaurant's "
                            "file (GL_<restaurant>_<date>).", "owner": "Chief accountant"},
    "import_diff": {"means": "The import of the day in Business Central does not add up to what Simphony sold, beyond the "
                             "tolerance; the menu items that differ are listed, quantity and value.",
                    "causes": "Checks closed or corrected after the import ran; a menu item imported twice or at "
                              "another price; discounts imported differently; items not included in sales.",
                    "fix": "Compare the listed menu items with Simphony's report of the day; re-import the day or "
                           "correct the lines before posting.", "owner": "Chief accountant"},
    "invoice_diff": {"means": "The sales invoice posted for the day (less credit memos) does not equal the import "
                              "(or Simphony when nothing was imported).",
                     "causes": "Lines changed or deleted in the sales document before posting; part of the day posted "
                               "on another date or customer; a credit memo that belongs to another day.",
                     "fix": "Open the invoice and compare its lines with the import of the day.",
                     "owner": "Chief accountant"},
    "not_posted": {"means": "The day is imported but not posted in BC: imported lines not in a sales document, or no "
                            "sales invoice of the restaurant's customer on that date, after the posting lag. RHG does "
                            "not post a day whose value is wrong, so an open day is a day to correct.",
                   "causes": "The value of the day is under correction (see Import ≠ Simphony); the sales document "
                             "was created but not posted; posting stopped on missing stock or a missing recipe "
                             "(Sales → Blocked by stock).",
                   "fix": "Correct the day, then post it; when stock blocks it, post the receipts or fix the "
                          "recipes first.", "owner": "Chief accountant"},
    "pending": {"means": "Simphony has the day, the import does not yet, and the import lag has not passed: not an "
                         "exception yet.", "causes": "The import runs the next days.", "fix": "Nothing yet.",
                "owner": ""},
    "open_wait": {"means": "Imported and not posted yet, within the posting lag: not an exception yet.",
                  "causes": "Posting follows the import by a few days.", "fix": "Nothing yet.", "owner": ""},
}
on_built = None          # (company, result) → the exceptions register (server.register_after_sales)
KEEP = 900
_held = heldjob.Held("salesrec", KEEP)


def settings_of(settings):
    s = dict(DEFAULTS, **{k: v for k, v in ((settings or {}).get("sales_rec") or {}).items() if v not in (None, "")})

    def num(k, lo, hi):
        try:
            return max(lo, min(hi, float(s[k])))
        except (TypeError, ValueError):
            return float(DEFAULTS[k])
    return {"days": int(num("days", 3, 62)), "import_lag": int(num("import_lag", 0, 30)),
            "post_lag": int(num("post_lag", 0, 30)), "settle_days": int(num("settle_days", 0, 90)),
            "tol_abs": num("tol_abs", 0, 1e9), "tol_pct": num("tol_pct", 0, 100)}


def _dates(a, b):
    out, d = [], a
    while d <= b:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def restaurants(company, settings=None):
    """The registry rows of a company whose POS sales are invoiced to a customer."""
    return [r for r in locintel.registry(settings) if (r.get("company") or "").upper() == (company or "").upper()
            and r.get("customer")]


def companies(settings=None):
    return sorted({r["company"] for r in locintel.registry(settings) if r.get("company") and r.get("customer")})


# ---------------------------------------------------------------- reads
def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _or(field, values):
    return "(" + " or ".join(f"{field} eq '" + str(v).replace("'", "''") + "'" for v in values) + ")"


def read_bc(client, company, a, b, customers):
    """imp: {(customer, day): {"mi": {menu item: [total, count, discount, BC item, name]}, "unposted", "rows",
    "imported_at"}}; inv: {(customer, day): {"amount", "remaining", "docs"}}; posted: {(customer, day): {"ex", "incl",
    "disc", "lines"}} from the posted invoice and credit memo lines; vat: {BC item: VAT % as posted}."""
    cust = {c.upper(): c for c in customers}
    imp, inv, posted, vat = {}, {}, {}, {}
    rows = client._get_all(client._url(company, E_ARCHIVE), {
        "$filter": f"TransactionDateArch ge {a} and TransactionDateArch le {b}",
        "$select": "CustomerNo,LocationCode,TransactionDateArch,Menu_Item_Number,Menu_Item_Name1,ItemNo,Postuar,"
                   "RLineTotal,RLineCount,LineDiscount,SystemCreatedAt88448"}, page_size=5000)
    for r in rows:
        c = cust.get(str(r.get("CustomerNo") or "").upper()) or cust.get(str(r.get("LocationCode") or "").upper())
        if not c:
            continue
        d = str(r.get("TransactionDateArch") or "")[:10]
        x = imp.setdefault((c, d), {"mi": {}, "unposted": 0, "rows": 0, "imported_at": ""})
        mi = str(r.get("Menu_Item_Number") or "").strip()
        m = x["mi"].setdefault(mi, [0.0, 0.0, 0.0, "", ""])
        m[0] += _f(r.get("RLineTotal"))
        m[1] += _f(r.get("RLineCount"))
        m[2] += _f(r.get("LineDiscount"))
        m[3] = m[3] or str(r.get("ItemNo") or "").strip()
        m[4] = m[4] or str(r.get("Menu_Item_Name1") or "").strip()
        x["rows"] += 1
        x["unposted"] += 0 if r.get("Postuar") else 1
        x["imported_at"] = max(x["imported_at"], str(r.get("SystemCreatedAt88448") or "")[:16])
    led = client._get_all(client._url(company, config.E_CUSTOMER), {
        "$filter": f"Posting_Date ge {a} and Posting_Date le {b} and "
                   f"(Document_Type eq 'Invoice' or Document_Type eq 'Credit Memo')",
        "$select": "Customer_No,Posting_Date,Document_Type,Document_No,Amount_LCY,Remaining_Amt_LCY,Open"})
    for r in led:
        c = cust.get(str(r.get("Customer_No") or "").upper())
        if not c:
            continue
        d = str(r.get("Posting_Date") or "")[:10]
        x = inv.setdefault((c, d), {"amount": 0.0, "remaining": 0.0, "docs": []})
        x["amount"] += _f(r.get("Amount_LCY"))
        x["remaining"] += _f(r.get("Remaining_Amt_LCY"))
        x["docs"].append(f"{'CM ' if r.get('Document_Type') == 'Credit Memo' else ''}{r.get('Document_No') or ''}")
    # the posted lines of the restaurants' customers: sales without VAT, VAT and with VAT as BC posted them
    if customers:
        for ent, sign in ((E_SINV, 1), (E_SCM, -1)):
            for r in client._get_all(client._url(company, ent), {
                    "$filter": f"Posting_Date ge {a} and Posting_Date le {b} and "
                               + _or("Sell_to_Customer_No_", customers),
                    "$select": "Sell_to_Customer_No_,Posting_Date,No_,Amount,Amount_Including_VAT,Line_Discount_Amount,"
                               "VAT__"}, page_size=5000):
                c = cust.get(str(r.get("Sell_to_Customer_No_") or "").upper())
                if not c:
                    continue
                d = str(r.get("Posting_Date") or "")[:10]
                x = posted.setdefault((c, d), {"ex": 0.0, "incl": 0.0, "disc": 0.0, "lines": 0})
                x["ex"] += sign * _f(r.get("Amount"))
                x["incl"] += sign * _f(r.get("Amount_Including_VAT"))
                x["disc"] += sign * _f(r.get("Line_Discount_Amount"))
                x["lines"] += 1
                no = str(r.get("No_") or "").strip()
                if no and sign > 0 and r.get("VAT__") is not None:
                    vat[no] = _f(r.get("VAT__"))
    return imp, inv, posted, vat


def read_simphony(refs, days, latest_fn, progress=None, force_from=None):
    """{ref: {day: {menu item: (sales, count, discounts)}}} for the days each location has closed, {ref: {day: {menu
    item: VAT}}}; errors. force_from: the days from it are read from Simphony again ("Read again")."""
    errors, latest = [], {}
    for ref in refs:
        try:
            latest[ref] = latest_fn(ref)
        except Exception:                                           # noqa: BLE001
            latest[ref] = None
    jobs = [(r, d) for r in refs for d in days if latest.get(r) and d <= latest[r]]
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(menueng.read_day, r, d, latest.get(r), True, bool(force_from and d >= force_from)): (r, d)
                for r, d in jobs}
        for f in as_completed(futs):
            done += 1
            try:
                f.result()
            except Exception as exc:                                # noqa: BLE001
                if len(errors) < 6:
                    errors.append(f"Simphony {futs[f][0]} {futs[f][1]}: {str(exc)[:140]}")
            if progress and (done % 10 == 0 or done == len(jobs)):
                progress(f"Simphony · {done} of {len(jobs)} days")
    out = {r: menueng.items_vat(r, days[0], days[-1]) for r in refs}
    vat = {r: menueng.vat_by_item(r, days[0], days[-1]) for r in refs}
    return out, latest, errors, vat


# ---------------------------------------------------------------- the reconciliation
def _r(v):
    return None if v is None else round(v, 2) + 0.0                # + 0.0: never −0


def _part(gross, disc, vat, unknown=0.0):
    """A day's sales in four: before discounts, discounts, VAT, with and without VAT (VAT included prices)."""
    incl = gross - disc
    return {"gross": _r(gross), "disc": _r(disc), "incl": _r(incl), "vat": _r(vat), "ex": _r(incl - vat),
            "vat_unknown": _r(unknown)}


def explain(L, d, c, k, S, ccy):
    """The case of one restaurant's day, in words: what was found, with the figures."""
    def money(v):
        return "–" if v is None else f"{v + 0.0:,.2f}"
    base = c["imp"] if c["imp"] is not None else c["sim"]
    if k == "not_imported":
        return (f"Simphony sold {money(c['sim'])} {ccy} on {d}; nothing of that day is in the POS import of BC "
                f"{c['age']} days later (import lag {S['import_lag']} days).")
    if k == "pending":
        return f"Simphony sold {money(c['sim'])} {ccy}; the import is not in yet, within the {S['import_lag']}-day lag."
    if k == "import_diff":
        top = "; ".join(f"{x['mi']} {x['name'] or ''} {x['diff']:+,.2f}" for x in c["items"][:5])
        return (f"Imported {money(c['imp'])} against Simphony {money(c['sim'])} {ccy} ({c['imp'] - c['sim']:+,.2f}"
                + (f", of which {c['unmapped']:,.2f} on menu items never imported" if c["unmapped"] else "")
                + f"); {c['items_n']} menu item{'s' if c['items_n'] != 1 else ''} differ{'' if c['items_n'] != 1 else 's'}" + (f": {top}." if top else "."))
    if k in ("not_posted", "open_wait"):
        what = (f"{c['unposted']} of {c['rows']} imported lines are not in a sales document" if c["unposted"]
                else f"no sales invoice of the customer {L['customer']} on {d}")
        return (f"Imported {money(c['imp'])} {ccy}; {what}, {c['age']} days later "
                + (f"(posting lag {S['post_lag']} days): the day is open." if k == "not_posted"
                   else f"(within the {S['post_lag']}-day posting lag)."))
    if k == "invoice_diff":
        return (f"Invoiced {money(c['inv'])} ({', '.join(c['docs'])}) against "
                f"{'the import' if c['imp'] is not None else 'Simphony'} {money(base)} {ccy} "
                f"({(c['inv'] or 0) - (base or 0):+,.2f}).")
    return ""


def totals(locs, maps):
    """The KPIs of a set of restaurants: the sums of their days (to the cent), the breakdowns and the counts."""
    tot = {"sim": 0.0, "imp": 0.0, "inv": 0.0, "remaining": 0.0, "imp_gap": 0.0, "inv_gap": 0.0}
    br = {s: {"gross": 0.0, "disc": 0.0, "incl": 0.0, "vat": 0.0, "ex": 0.0, "vat_unknown": 0.0} for s in ("sim", "imp", "inv")}
    counts = {k: 0 for k in RANK}
    for L in locs:
        for c in L["cells"].values():
            counts[c["status"]] = counts.get(c["status"], 0) + 1
            for k in ("sim", "imp", "inv"):
                tot[k] += c.get(k) or 0
                for f, v in ((c.get("b") or {}).get(k) or {}).items():
                    if f in br[k]:
                        br[k][f] += v or 0
            tot["remaining"] += max(c.get("remaining") or 0, 0)
            if c.get("sim") is not None and c.get("imp") is not None:
                tot["imp_gap"] += c["imp"] - c["sim"]
            if c.get("inv") is not None and c.get("imp") is not None:
                tot["inv_gap"] += c["inv"] - c["imp"]
    checked = [c for L in locs for c in L["cells"].values() if c["status"] not in ("pending", "open_wait", "no_sales")]
    cases = sum(1 for L in locs for c in L["cells"].values() for i in c["issues"] if i not in ("pending", "open_wait"))
    return {**{k: round(v, 2) for k, v in tot.items()}, "days": len(checked),
            "ok": sum(1 for c in checked if c["status"] == "ok"),
            "issues": cases + len(maps), "cases": cases, "mapping": len(maps), "counts": counts,
            "split": {k: {f: round(v, 2) for f, v in x.items()} for k, x in br.items()}}


def analyse(company, regs, days, sim, latest, imp, inv, master, S, today=None, currency="ALL", sim_on=True,
            dims=None, posted=None, vat_item=None, sim_vat=None):
    """dims: {ref: simphony.menu_items(ref)}: names, and the items not included in sales (left out)."""
    today = today or date.today()
    dims, posted, vat_item, sim_vat = dims or {}, posted or {}, vat_item or {}, sim_vat or {}
    items = (master or {}).get("items") or {}
    has_bom = {p for p, *_ in (master or {}).get("bom") or []}

    def gap(a, b):
        return abs(a - b) > max(S["tol_abs"], S["tol_pct"] / 100 * max(abs(a), abs(b)))
    locs, mapping = [], {}
    # menu items the import never carries in the period: one exception each (item mapping), so their sales are
    # left out of the daily comparison instead of making every day differ
    ever = {mi for im in imp.values() for mi in im["mi"]}
    for r in regs:
        ref, cu = r["ref"], r["customer"]
        L = {"ref": ref, "name": r["name"], "brand": r.get("brand") or "", "bc": r.get("bc") or cu, "customer": cu,
             "dept": r.get("dept") or "", "latest": latest.get(ref), "cells": {}, "issues": 0, "gap": 0.0}
        excl = {k for k, v in (dims.get(ref) or {}).items() if v.get("excluded")}
        sim_days = {d: {k: v for k, v in x.items() if k not in excl} for d, x in (sim.get(ref) or {}).items()}
        for d in days:
            age = (today - date.fromisoformat(d)).days
            sd = sim_days.get(d) if sim_on else None
            s_t = round(sum(v[0] - v[2] for v in sd.values()), 2) if sd is not None else None
            unmapped = round(sum(v[0] - v[2] for k, v in sd.items() if str(k) not in ever), 2) if sd and imp else 0.0
            im = imp.get((cu, d))
            i_t = round(sum(v[0] + v[2] for v in im["mi"].values()), 2) if im else None
            iv = inv.get((cu, d))
            v_t = round(iv["amount"], 2) if iv else None
            issues = []
            if s_t and i_t is None:
                issues.append("not_imported" if age > S["import_lag"] else "pending")
            if s_t is not None and i_t is not None and gap(i_t, s_t - unmapped):
                issues.append("import_diff")
            if i_t is not None and (im["unposted"] or v_t is None):
                issues.append("not_posted" if age > S["post_lag"] else "open_wait")
            if v_t is not None:
                base = i_t if i_t is not None else s_t
                if base is not None and gap(v_t, base):
                    issues.append("invoice_diff")
            if not issues:
                issues = ["no_sales"] if not (s_t or i_t or v_t) else ["ok"]
            status = min(issues, key=RANK.index)
            diffs = []
            if sd is not None and im:
                for mi in set(str(k) for k in sd) | set(im["mi"]):
                    a = sd.get(int(mi)) if mi.isdigit() else None
                    b = im["mi"].get(mi)
                    sv, sc = (a[0] - a[2], a[1]) if a else (0.0, 0.0)
                    iv_, ic = (b[0] + b[2], b[1]) if b else (0.0, 0.0)
                    if abs(sv - iv_) >= 0.01 or abs(sc - ic) >= 0.01:
                        nm = (b or [""] * 5)[4] or ((dims.get(ref) or {}).get(int(mi) if mi.isdigit() else mi) or {}).get("name", "")
                        diffs.append({"mi": mi, "name": nm, "item": (b or [""] * 5)[3],
                                      "sim": round(sv, 2), "sim_qty": sc, "imp": round(iv_, 2), "imp_qty": ic,
                                      "diff": round(iv_ - sv, 2)})
                diffs.sort(key=lambda x: -abs(x["diff"]))
            # each figure in four: before discounts, discounts, VAT, with and without VAT
            split = {}
            if sd is not None:
                sv = (sim_vat.get(ref) or {}).get(d) or {}
                split["sim"] = _part(sum(v[0] for v in sd.values()), sum(v[2] for v in sd.values()),
                                     sum(sv.get(k, 0.0) for k in sd))
            if im:
                g = sum(v[0] for v in im["mi"].values())
                ds = -sum(v[2] for v in im["mi"].values())
                vt, unk = 0.0, 0.0
                for v in im["mi"].values():
                    rate = vat_item.get(v[3]) if v[3] else None
                    net = v[0] + v[2]
                    if rate is None:
                        unk += net
                    else:
                        vt += net * rate / (100 + rate)
                split["imp"] = _part(g, ds, vt, unk)
            pb = posted.get((cu, d))
            if pb:
                split["inv"] = {"gross": _r(pb["incl"] + pb["disc"]), "disc": _r(pb["disc"]), "incl": _r(pb["incl"]),
                                "vat": _r(pb["incl"] - pb["ex"]), "ex": _r(pb["ex"]), "vat_unknown": 0.0,
                                "lines": pb["lines"]}
            cell = {"status": status, "issues": [i for i in dict.fromkeys(issues) if i not in ("ok", "no_sales")],
                    "sim": s_t, "imp": i_t, "inv": v_t, "age": age,
                    "remaining": round(iv["remaining"], 2) if iv else None, "docs": iv["docs"] if iv else [],
                    "unposted": im["unposted"] if im else 0, "rows": im["rows"] if im else 0,
                    "imported_at": im["imported_at"] if im else "", "items": diffs[:25], "items_n": len(diffs),
                    "unmapped": unmapped, "b": split}
            cell["why"] = {k: explain(L, d, cell, k, S, currency) for k in cell["issues"]}
            L["cells"][d] = cell
            L["issues"] += sum(1 for i in cell["issues"] if i not in ("pending", "open_wait"))
            if s_t is not None and i_t is not None:
                L["gap"] += i_t - s_t
            # item mapping, over the period
            if im:
                for mi, b in im["mi"].items():
                    k = None
                    card = items.get(b[3]) if b[3] else None
                    if not b[3]:
                        k = "no_item"
                    elif items and card is None:
                        k = "unknown_item"
                    elif card and card.get("Blocked"):
                        k = "blocked_item"
                    elif card and ((card.get("Assembly_Policy") or "") == "Assemble-to-Order"
                                   or (card.get("Replenishment_System") or "") == "Assembly") \
                            and b[3] not in has_bom and not card.get("Assembly_BOM"):
                        k = "no_recipe"
                    if k:
                        M = mapping.setdefault((k, mi), {"kind": k, "mi": mi, "name": b[4], "item": b[3],
                                                         "value": 0.0, "qty": 0.0, "locations": set(), "days": set()})
                        M["value"] += b[0] + b[2]
                        M["qty"] += b[1]
                        M["locations"].add(L["bc"])
                        M["days"].add(d)
            if sd and im:
                imported = set(im["mi"])
                for mi, a in sd.items():
                    if str(mi) not in imported and abs(a[0] - a[2]) >= 0.01:
                        M = mapping.setdefault(("not_in_import", str(mi)), {
                            "kind": "not_in_import", "mi": str(mi), "name": ((dims.get(ref) or {}).get(mi) or {}).get("name", ""),
                            "item": "", "value": 0.0, "qty": 0.0, "locations": set(), "days": set()})
                        M["value"] += a[0] - a[2]
                        M["qty"] += a[1]
                        M["locations"].add(L["bc"])
                        M["days"].add(d)
        L["gap"] = round(L["gap"], 2)
        locs.append(L)
    # a menu item missing from the import only counts when it was never imported in the period
    names = {mi: b[4] for (c, d), im in imp.items() for mi, b in im["mi"].items() if b[4]}
    maps = []
    for (k, mi), M in mapping.items():
        if k == "not_in_import" and mi in ever:
            continue
        M.update(value=round(M["value"], 2), locations=sorted(M["locations"]), days=len(M["days"]),
                 last=max(M["days"]), name=M["name"] or names.get(mi, ""), label=MAPPING[k])
        maps.append(M)
    maps.sort(key=lambda M: (list(MAPPING).index(M["kind"]), -abs(M["value"])))
    return {"company": company, "currency": currency, "from": days[0], "to": days[-1], "days": days,
            "today": today.isoformat(), "settings": S, "locations": locs, "mapping": maps, "kpis": totals(locs, maps),
            "statuses": STATUS, "mapping_labels": MAPPING, "help": HELP, "simphony": sim_on}


def cut(r, allowed):
    """The reconciliation for some restaurants only (allowed: upper-case codes of the scope or of the departments
    chosen; None = all): their rows, the totals recomputed from their days; a menu item problem is kept when it is
    at one of them, with only their codes, and its value only when it is theirs alone."""
    if allowed is None or not isinstance(r, dict) or not isinstance(r.get("locations"), list):
        return r
    A = {str(x).strip().upper() for x in allowed}
    keys = lambda L: {str(L.get(k) or "").strip().upper() for k in ("bc", "ref", "customer", "dept")} - {""}  # noqa: E731
    locs = [L for L in r["locations"] if keys(L) & A]
    codes = {str(L.get(k) or "").strip().upper() for L in locs for k in ("bc", "customer")}
    maps = []
    for M in r.get("mapping") or []:
        mine = [x for x in M.get("locations") or [] if str(x).strip().upper() in codes]
        if mine:
            alone = len(mine) == len(M.get("locations") or [])
            maps.append(dict(M, locations=mine, **({} if alone else {"value": None, "qty": None})))
    return dict(r, locations=locs, mapping=maps, kpis=totals(locs, maps), cut=True)


def check_range(frm, to, today=None):
    """A range asked on the screen → (first day, last day): yesterday at the latest, MAX_RANGE days at most."""
    today = today or date.today()
    try:
        a, b = date.fromisoformat(str(frm)[:10]), date.fromisoformat(str(to)[:10])
    except ValueError:
        raise ValueError("Choose the first and the last day of the range.") from None
    b = min(b, today - timedelta(days=1))
    if a > b:
        raise ValueError("The first day is after the last one (the last day is yesterday at the latest).")
    if (b - a).days + 1 > MAX_RANGE:
        raise ValueError(f"At most {MAX_RANGE} days at a time.")
    return a, b


def key_of(company, frm=None, to=None):
    return f"{company}|{frm}|{to}" if frm and to else company


def build(client, company, latest_fn, progress=None, today=None, frm=None, to=None, refresh=False):
    """The last days of Settings, or the range frm–to. refresh ("Read again"): Simphony's last seven days are read
    again too; Business Central is read anew every time."""
    today = today or date.today()
    settings = store.get_settings()
    S = settings_of(settings)
    regs = restaurants(company, settings)
    if not regs:
        raise ValueError(f"No restaurant of {company} has a BC customer in Settings → Locations registry.")
    if frm and to:
        a, b = check_range(frm, to, today)
    else:
        b = today - timedelta(days=1)
        a = b - timedelta(days=S["days"] - 1)
    days = _dates(a, b)
    errors = []
    sim_on = bool(simphony.info().get("configured"))
    sim, latest, dims, sim_vat = {}, {}, {}, {}
    if sim_on:
        (progress or (lambda *_: None))("Simphony · menu items by day")
        force = (today - timedelta(days=7)).isoformat() if refresh else None
        sim, latest, errs, sim_vat = read_simphony([r["ref"] for r in regs], days, latest_fn, progress, force)
        errors += errs
        for ref in sim:
            try:
                dims[ref] = simphony.menu_items(ref)
            except Exception:                                       # noqa: BLE001
                dims[ref] = {}
    (progress or (lambda *_: None))("Business Central · POS import, invoices and their lines")
    imp, inv, posted, vat_item = read_bc(client, company, days[0], days[-1], [r["customer"] for r in regs])
    (progress or (lambda *_: None))("Business Central · item cards and recipes")
    try:
        m = recipes.master_cached(company) or recipes.master(client, company)
    except Exception as exc:                                        # noqa: BLE001
        m, errors = {}, errors + [f"Item cards: {str(exc)[:160]}"]
    ccy = ((settings.get("group") or {}).get("reporting_currency") or "ALL")
    r = analyse(company, regs, days, sim, latest, imp, inv, m, S, today, ccy, sim_on, dims, posted, vat_item, sim_vat)
    r.update(errors=errors, checked_at=datetime.now().isoformat(timespec="seconds"),
             complete=not errors, custom=bool(frm and to))
    return r


# ---------------------------------------------------------------- held results and background reads
def held(company, key=None):
    return _held.held(key or company)


def remember(company, r, key=None):
    _held.remember(key or company, r)


def request(client, company, latest_fn, refresh=False, frm=None, to=None):
    """The last reconciliation at once (reading: True while a new one runs behind it), else a background read. A range
    of its own is held apart and does not touch the exceptions register."""
    if frm and to:
        a, b = check_range(frm, to)
        frm, to = a.isoformat(), b.isoformat()
    custom = bool(frm and to)
    return _held.request(key_of(company, frm, to),
                         lambda progress: build(client, company, latest_fn, progress=progress, frm=frm, to=to,
                                                refresh=refresh), refresh,
                         (lambda r: on_built(company, r)) if on_built and not custom else None,
                         "Reading Simphony and Business Central…")


def findings(r):
    """The exceptions of a reconciliation for exreg.sync (source "sales_rec") and the days it covered."""
    out, ccy = [], r.get("currency") or ""
    sev = {"not_imported": "critical", "import_diff": "warning", "invoice_diff": "critical", "not_posted": "warning"}
    titles = {"not_imported": "sales not imported in BC", "import_diff": "import differs from Simphony",
              "not_posted": "open: sales not posted in BC", "invoice_diff": "invoice differs from the import"}
    for L in r["locations"]:
        for d, c in L["cells"].items():
            for k in c["issues"]:
                if k not in sev:
                    continue
                base = c["imp"] if c["imp"] is not None else c["sim"]
                value = {"not_imported": c["sim"], "import_diff": (c["imp"] or 0) - (c["sim"] or 0) + c["unmapped"],
                         "not_posted": base, "invoice_diff": (c["inv"] or 0) - (base or 0)}[k]
                out.append({"subject": f"{d} {k}", "kind": k, "location": L["bc"], "title": f"{L['name']} {d}: {titles[k]}",
                            "detail": (c.get("why") or {}).get(k) or explain(L, d, c, k, r["settings"], ccy),
                            "severity": "critical" if k == "import_diff" and base and abs(value) > 0.05 * abs(base)
                            else sev[k], "value": abs(value or 0)})
    for M in r["mapping"]:
        out.append({"subject": f"item {M['mi']}", "kind": M["kind"], "location": "",
                    "title": f"{M['mi']} {M['name']}: {M['label']}".strip(),
                    "detail": (f"BC item {M['item'] or '–'}; {M['qty']:g} sold for {M['value']:,.2f} {ccy} on "
                               f"{M['days']} days at {', '.join(M['locations'])}, last {M['last']}."),
                    "severity": "critical" if M["kind"] in ("no_item", "not_in_import") else "warning",
                    "value": abs(M["value"])})
    return out


def in_scope(r):
    """Which active register items a reconciliation may close: its days, and the item mapping (and the takings
    cases this screen no longer raises: Takings POS ↔ BC checks them)."""
    days = set(r["days"])
    return lambda x: x["kind"] in MAPPING or x["kind"] == "unpaid" or (x["subject"] or "")[:10] in days
