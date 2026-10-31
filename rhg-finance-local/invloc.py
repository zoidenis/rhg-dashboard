"""Inventory → Location intelligence: every stock location of a company on one screen.

Four reads that already exist, put side by side per location:
  * the stock position (Inventory): value at the start and the end of the period at today's item
    cost, so the change is quantity, not revaluation; negative and zero-cost items;
  * Movement & dead stock: value on hand by movement class, received but never used, the age of
    the stock since its last use, the positive and negative adjustments, and the items behind them;
  * Typology & turnover: days of cover of the food, beverage and resale stock against the target
    of each line (delivery cycle + safety days, or the family target in Settings), the stock above
    target, the families furthest above it, and the fresh lines that need a count;
  * Recipe integrity: the items sold at the location in the last 30 days and how many of those that
    need a recipe have a valid one.
Each part loads on its own (the first reads of a company take minutes); the screen shows what is
ready and asks again for the rest. Business Central stays read-only.
"""
import threading
import time
import traceback

import locintel
import movement
import recipes
import store
import typology

_stock_jobs, _lock = {}, threading.Lock()
KEEP = 900


def thresholds(settings=None):
    s = (settings or store.get_settings()).get("locations") or {}

    def num(k, d):
        try:
            return float(s.get(k, d))
        except (TypeError, ValueError):
            return d
    return {"slow_pct": num("slow_stock_pct", 15), "recipe_coverage": num("recipe_coverage_pct", 90)}


def _stock(company, period, mode, summary_fn, refresh):
    """The stock position (finance summary) in the background: its first read of a period takes a
    minute or two, longer than a request should wait."""
    key = (company, period or "", mode)
    now = time.time()
    with _lock:
        job = _stock_jobs.get(key)
        if job and job["state"] == "running":
            return "pending", "Stock by location is being read", None
        if job and job["state"] == "done" and not refresh and now - job["finished"] < KEEP:
            r = job["result"]
            return ("ready", "", r) if r.get("available") else ("failed", r.get("error") or "", None)
        job = _stock_jobs[key] = {"state": "running", "started": now}

    def run():
        try:
            r = summary_fn(company, period, mode)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            r = {"available": False, "error": f"The stock position could not be read: {str(exc)[:300]}"}
        with _lock:
            job.update(state="done", finished=time.time(), result=r)
    threading.Thread(target=run, daemon=True, name="invloc-stock").start()
    return "pending", "Stock by location is being read", None


def _group(code, reg_by_bc, distribution):
    """restaurant brand from the registry; distribution locations (Settings) and anything not a
    restaurant go to warehouses, central and production."""
    r = reg_by_bc.get(code.upper())
    if r:
        return "restaurant", r.get("brand") or "", r.get("name") or code
    if code.upper() in distribution:
        return "warehouse", "", code
    return "other", "", code


def build(client, company, period, mode, summary_fn, refresh=False):
    settings = store.get_settings()
    th = thresholds(settings)
    inv = settings.get("inventory") or {}
    distribution = {p.strip().upper() for p in (inv.get("distribution_locations") or "MAGAZINE, CENTRAL").split(",")
                    if p.strip()}
    reg = [r for r in locintel.registry(settings) if (r.get("company") or "").upper() == (company or "").upper()]
    reg_by_bc = {(r.get("bc") or "").upper(): r for r in reg if r.get("bc")}
    out, parts = {}, {}

    def loc(code):
        L = out.get(code)
        if L is None:
            grp, brand, name = _group(code, reg_by_bc, distribution)
            L = out[code] = {"name": name, "group": grp, "brand": brand}
        return L

    # ---- stock position at the start and the end of the period
    state, note, ss = _stock(company, period, mode, summary_fn, refresh)
    parts["stock"] = {"state": state, "note": note}
    if ss:
        parts["stock"].update(opening_date=ss.get("opening_date"), as_of=(ss.get("period") or [None, None])[1],
                              missing=ss.get("missing_locations") or [])
        for r in ss.get("locations") or []:
            L = loc(r["location"])
            L.update(opening=round(r.get("opening") or 0), closing=round(r.get("closing") or 0),
                     change=round((r.get("closing") or 0) - (r.get("opening") or 0)),
                     change_pct=r.get("change_pct"), share=r.get("share_pct"),
                     negative_items=r.get("negative_items") or 0, no_cost_items=r.get("no_cost_items") or 0)

    # ---- movement & dead stock, as of yesterday with 90 days of history
    mv = movement.request(client, company, None, 90)
    if mv.get("pending"):
        g = mv.get("progress") or {}
        months = f" · {g.get('done')} of {g.get('total')} months" if g.get("total") else ""
        parts["movement"] = {"state": "pending", "note": f"Stock movements: {g.get('stage') or 'starting'}{months}"}
        mv = None
    elif mv.get("available") is False:
        parts["movement"] = {"state": "failed", "note": mv.get("error") or "Movement could not be read."}
        mv = None
    else:
        parts["movement"] = {"state": "ready", "as_of": mv["as_of"], "history_days": mv["history_days"]}
    if mv:
        names = mv.get("_names") or {}
        for Lm in mv.get("locations") or []:
            L = loc(Lm["location"])
            cls = {k: Lm.get(k, [0, 0]) for k in ("ACTIVE", "MONITOR", "SLOW MOVING", "NON-MOVING", "DEAD STOCK",
                                                     "NEW ITEM", "SEASONAL", "NEGATIVE")}
            on_hand = sum(v[1] for k, v in cls.items() if k != "NEGATIVE")
            slow = sum(cls[k][1] for k in ("SLOW MOVING", "NON-MOVING", "DEAD STOCK"))
            L.update(on_hand=round(on_hand), items=Lm.get("items") or 0,
                     classes={k: [v[0], round(v[1])] for k, v in cls.items()},
                     slow_value=round(slow), slow_pct=round(slow / on_hand * 100, 1) if on_hand > 0 else None,
                     dead_value=round(cls["DEAD STOCK"][1] + cls["NON-MOVING"][1]),
                     rnc=[Lm["rnc"][0], round(Lm["rnc"][1])], negative=[cls["NEGATIVE"][0], round(cls["NEGATIVE"][1])],
                     aging={k: round(v) for k, v in (mv.get("aging") or {}).get(Lm["location"], {}).items()})
        for a in mv.get("adjustments") or []:
            L = loc(a["location"])
            L["adjustments"] = {"positive": round(a["pos_cost"]), "negative": round(a["neg_cost"]),
                                "net": round(a["pos_cost"] + a["neg_cost"])}
        # the items behind the figures: largest on hand, and the stock that does not move
        by = {}
        for r in mv.get("_rows") or []:
            by.setdefault(r[0], []).append(r)
        for code, rows in by.items():
            L = loc(code)

            def item(r):
                return [names.get(r[1]) or r[1], r[1], r[2], round(r[3]), r[13], r[12], r[14]]
            pos = [r for r in rows if r[2] > 0]
            L["top_items"] = [item(r) for r in sorted(pos, key=lambda r: -r[3])[:8]]
            idle = [r for r in pos if r[13] in ("SLOW MOVING", "NON-MOVING", "DEAD STOCK") or r[14]]
            L["idle_items"] = [item(r) for r in sorted(idle, key=lambda r: -r[3])[:8]]
            L["negative_list"] = [item(r) for r in sorted((r for r in rows if r[2] < 0), key=lambda r: r[3])[:6]]
        parts["movement"]["buckets"] = mv.get("buckets") or []

        # ---- typology & turnover on the same read
        try:
            m = recipes.master_cached(company) or {}
            t = typology.analyse(mv, m.get("items") or {}, settings)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            t = None
            parts["turnover"] = {"state": "failed", "note": f"Typology could not be built: {str(exc)[:200]}"}
        if t:
            parts["turnover"] = {"state": "ready" if m else "partial",
                                 "note": "" if m else "Item types come with the item cards of Recipe integrity; "
                                                      "they are still being read."}
            days = t["history_days"] or 90
            acc = {}
            for code, typ, fam, nat, lines, value, neg, use, nouse, excess, tw, cw, dw, count in t["rows"]:
                a = acc.setdefault(code, {"fb": 0.0, "use": 0.0, "tw": 0.0, "dw": 0.0, "excess": 0.0, "nouse": 0.0,
                                          "count": 0, "nat": {}, "fam": {}})
                a["nat"][nat] = a["nat"].get(nat, 0.0) + value
                a["count"] += count
                if nat not in ("ingredients", "resale"):
                    continue
                a["fb"] += value
                a["use"] += use
                a["tw"] += tw
                a["dw"] += dw
                a["excess"] += excess
                a["nouse"] += nouse
                f = a["fam"].setdefault(fam or typ or "?", [0.0, 0.0, 0.0, 0.0, 0.0])
                f[0] += value
                f[1] += use
                f[2] += excess
                f[3] += tw
                f[4] += dw
            for code, a in acc.items():
                L = loc(code)
                daily = a["use"] / days
                fams = []
                for fam, (v, u, ex, tw, dw) in a["fam"].items():
                    d = u / days
                    fams.append([fam, round(v), round(v / d) if d > 0 else None, round(tw / dw) if dw else None,
                                 round(ex)])
                fams.sort(key=lambda f: (-f[4], -f[1]))
                L.update(fb_stock=round(a["fb"]), fb_use=round(a["use"]),
                         doh=round(a["fb"] / daily) if daily > 0 else None,
                         target=round(a["tw"] / a["dw"]) if a["dw"] else None,
                         above_target=round(a["excess"]), no_use=round(a["nouse"]), count_flags=a["count"],
                         natures={k: round(v) for k, v in a["nat"].items() if v},
                         families=fams[:8])
            cnt = {}
            for x in t["items"]:
                if x[15]:
                    cnt.setdefault(x[0], [])
                    if len(cnt[x[0]]) < 6:
                        cnt[x[0]].append([x[2] or x[1], x[1], x[6], x[7], x[8], x[10]])
            for code, rows in cnt.items():
                loc(code)["count_items"] = rows
            parts["turnover"]["natures"] = t.get("natures") or {}
    else:
        parts["turnover"] = {"state": parts["movement"]["state"], "note": "Built on the stock movements."}

    # ---- recipe coverage of the items sold in the last 30 days
    try:
        rc = recipes.request(client, company, None, None)
    except Exception as exc:                                       # noqa: BLE001
        rc = {"available": False, "error": str(exc)[:300]}
    if rc.get("pending"):
        g = rc.get("progress") or {}
        months = f" · {g.get('done')} of {g.get('total')} months" if g.get("total") else ""
        parts["recipes"] = {"state": "pending", "note": f"Recipes: {g.get('stage') or 'starting'}{months}"}
    elif rc.get("available") is False:
        parts["recipes"] = {"state": "failed", "note": rc.get("error") or "Recipes could not be read."}
    else:
        parts["recipes"] = {"state": "ready", "period": rc.get("period")}
        desc = {}
        for lst in ("missing", "incomplete", "review"):
            for i in rc.get(lst) or []:
                desc[i["item"]] = i.get("desc") or ""
        for c in rc.get("coverage") or []:
            if c["location"] == "(none)":
                continue
            L = loc(c["location"])
            bad = sorted((r for r in c.get("rows") or [] if r[1] in ("MISSING", "INCOMPLETE", "INACTIVE")),
                         key=lambda r: (-r[3], -r[2]))
            L["recipes"] = {"sold": c["sold_items"], "required": c["required"], "valid": c.get("VALID", 0),
                            "missing": c.get("MISSING", 0), "incomplete": c.get("INCOMPLETE", 0),
                            "inactive": c.get("INACTIVE", 0), "review": c.get("PENDING REVIEW", 0),
                            "coverage": c.get("coverage"),
                            "gaps": [[desc.get(r[0]) or r[0], r[0], r[1], r[2], r[3]] for r in bad[:8]]}

    # ---- alerts: what to look at first, per location
    for code, L in out.items():
        al = []
        if (L.get("negative") or [0])[0] or L.get("negative_items"):
            n = (L.get("negative") or [0])[0] or L.get("negative_items")
            al.append(["crit", f"{n} item{'s' if n != 1 else ''} below zero"])
        rc_ = L.get("recipes") or {}
        if rc_.get("coverage") is not None and rc_["coverage"] < th["recipe_coverage"]:
            al.append(["crit" if rc_["coverage"] < th["recipe_coverage"] - 20 else "warn",
                       f"Recipes cover {rc_['coverage']:.0f}% of the items sold"])
        if L.get("slow_pct") is not None and L["slow_pct"] >= th["slow_pct"]:
            al.append(["warn", f"{L['slow_pct']:.0f}% of the stock is slow or not moving"])
        if L.get("doh") is not None and L.get("target") and L["doh"] > 2 * L["target"]:
            al.append(["warn", f"{L['doh']} days of cover against {L['target']}"])
        if (L.get("rnc") or [0])[0]:
            al.append(["info", f"{L['rnc'][0]} item{'s' if L['rnc'][0] != 1 else ''} received, never used"])
        if L.get("count_flags"):
            al.append(["info", f"{L['count_flags']} fresh line{'s' if L['count_flags'] != 1 else ''} to count"])
        L["alerts"] = al
    # locations with nothing to show (no stock, no movement, nothing sold) are left out
    out = {k: v for k, v in out.items() if any(v.get(x) for x in ("closing", "opening", "on_hand", "fb_stock",
                                                                   "recipes", "adjustments"))}
    return {"company": company, "parts": parts, "thresholds": th, "locations": out,
            "pending": any(p["state"] == "pending" for p in parts.values()),
            "definitions": DEFINITIONS}


DEFINITIONS = {
    "stock": "Stock at the start and the end of the period, both at today's item unit cost, so the change is "
             "quantity, not revaluation (the Inventory overview).",
    "classes": "Movement classes by days since the last use (Movement & dead stock), as of yesterday on 90 days "
               "of history. Slow or not moving = slow moving + non-moving + dead stock.",
    "cover": "Days of cover of the food, beverage and resale stock = value / average daily use at cost. The target "
             "is each line's delivery cycle plus the safety days, or the family target in Settings, weighted by "
             "use (Typology & turnover).",
    "recipes": "Items sold at the location in the last 30 days that need a recipe, and how many of them have a "
               "valid one (Recipe integrity).",
    "adjustments": "Positive and negative adjustments at item cost in the 90 days of history; they are kept apart "
                   "until it is agreed which are counts, corrections or waste.",
    "values": "Quantities on hand are from Business Central; values at item unit cost are estimated.",
}
