"""The weekly cost control report: inventory exceptions for the cost controller, short, with an action list.

One email per company, for the week before (Monday to Sunday), with an Excel file of every line:
  * highlights: stock at item cost against the G/L, slow and dead stock, stock received and never
    used, days of cover against target, items below zero, recipe coverage of last week's sales and
    item master issues, each against the previous report;
  * the action list: what to do this week, most important first, with the area, the suggested owner
    and the value at stake;
  * one short block per screen: Inventory (G/L), Movement & dead stock, Typology & turnover, Recipe
    integrity and Item master control, five lines each.
It reads what the Finance screens read (Business Central is never written) and is sent through the
Purchasing tower's mail server, like the receivables & payables email.
"""
import html
import io
import json
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta

import arap_notify
import config
import movement
import recipients
import recipes
import store
import typology

STATE_FILE = store.DATA_DIR / "cost_report.json"
_state_lock, _run_lock = threading.Lock(), threading.Lock()
WAIT = 45 * 60                      # the first read of a company's ledger can take a while

IM_LABEL = {"ato_without_bom": "Assemble-to-Order without an Assembly BOM",
            "blocked_sold": "Blocked item still sold", "blocked_in_recipes": "Blocked item used in a sold recipe",
            "unit_no_conversion": "Unit of measure without a conversion", "no_base_unit": "No base unit of measure",
            "zero_cost_in_use": "Zero unit cost on an item in use",
            "cost_far_from_last_direct": "Unit cost far from the last direct cost",
            "no_inventory_group": "No inventory posting group", "no_product_group": "No product posting group",
            "no_category": "No item category",
            "assembly_setup_but_bought": "Set up as assembled but bought"}
IM_SEV = {"ato_without_bom": "critical", "blocked_sold": "critical", "blocked_in_recipes": "critical",
          "unit_no_conversion": "critical", "no_base_unit": "critical", "zero_cost_in_use": "warning",
          "cost_far_from_last_direct": "warning", "no_inventory_group": "warning", "no_product_group": "warning",
          "no_category": "info", "assembly_setup_but_bought": "info"}


def _m(v):
    if v is None:
        return "–"
    a = abs(v)
    s = f"{a / 1e6:,.1f} M" if a >= 1e6 else f"{a / 1e3:,.0f} k" if a >= 1e4 else f"{a:,.0f}"
    return ("−" if v < 0 else "") + s


def last_week(today=None):
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    return monday - timedelta(days=7), monday - timedelta(days=1)


def _wait(fn, what):
    """Movement and Recipes read in the background; ask again until they are ready."""
    t0 = time.time()
    while True:
        r = fn()
        if not r.get("pending"):
            if r.get("available") is False:
                raise RuntimeError(r.get("error") or f"{what} could not be read.")
            return r
        if time.time() - t0 > WAIT:
            raise RuntimeError(f"{what} was still being read after {WAIT // 60} minutes.")
        time.sleep(10)


# ---------------------------------------------------------------- the figures
def build(company, client, stock_summary, currency, today=None):
    today = today or date.today()
    a, b = last_week(today)
    settings = store.get_settings()
    errors, parts = [], {}

    # ---- the stock position against the G/L, at the end of last week's month
    ss = None
    try:
        ss = stock_summary(company, b.isoformat(), "month")
        if not ss.get("available"):
            errors.append("Inventory against the G/L: " + (ss.get("error") or "not available"))
            ss = None
    except Exception as exc:                                       # noqa: BLE001
        traceback.print_exc()
        errors.append(f"Inventory against the G/L could not be read: {str(exc)[:200]}")

    # ---- movement & dead stock, as of last Sunday, 90 days of history; typology on the same read
    mv = ty = None
    try:
        mv = _wait(lambda: movement.request(client, company, b.isoformat(), 90), "Movement & dead stock")
    except Exception as exc:                                       # noqa: BLE001
        errors.append(str(exc)[:200])
    # ---- recipes of last week's sales; it also reads the item cards typology needs
    rc = None
    try:
        rc = _wait(lambda: recipes.request(client, company, a.isoformat(), b.isoformat()), "Recipe integrity")
    except Exception as exc:                                       # noqa: BLE001
        errors.append(str(exc)[:200])
    if mv:
        try:
            m = recipes.master_cached(company) or {}
            ty = typology.analyse(mv, m.get("items") or {}, settings)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            errors.append(f"Typology & turnover: {str(exc)[:200]}")

    H = {}                                       # the headline numbers, kept for next week's comparison
    acts = []

    def act(sev, area, owner, text, value=None, view=""):
        acts.append({"sev": sev, "area": area, "owner": owner, "text": text, "value": value, "view": view})

    # ---- inventory against the G/L
    inv = {}
    if ss:
        t = ss["totals"]
        H.update(gl=t["gl_closing"], item_value=t["item_value"], difference=t["difference"],
                 negative_items=sum(r.get("negative_items", 0) for r in ss["locations"]))
        th = float(settings["thresholds"].get("inventory_difference") or 0)
        accs = sorted([x for x in ss["accounts"] if x.get("found") and abs(x["difference"]) >= 1],
                      key=lambda x: -abs(x["difference"]))
        neg = sorted([r for r in ss["locations"] if r.get("negative_items")], key=lambda r: -r["negative_items"])
        inv = {"totals": t, "accounts": accs, "unmapped": ss.get("unmapped") or [], "negative": neg,
               "as_of": ss.get("as_of"), "dio": ss.get("dio") or {}}
        if abs(t["difference"]) >= th and accs:
            x = accs[0]
            act("crit", "Inventory · G/L", "Accounting",
                f"Item value differs from the G/L by {_m(t['difference'])}; the largest gap is on account {x['account']} "
                f"{x['name']} ({_m(x['difference'])}). Run the cost adjustment and reconcile.", abs(t["difference"]), "inventory")
        if inv["unmapped"]:
            act("warn", "Inventory · G/L", "Accounting",
                f"{_m(sum(u['value'] for u in inv['unmapped']))} of stock posts to groups outside the inventory accounts "
                f"in Settings ({', '.join(u['posting_group'] for u in inv['unmapped'][:3])}).",
                sum(u["value"] for u in inv["unmapped"]), "inventory")

    # ---- movement & dead stock
    mvx = {}
    if mv:
        tt = mv["totals"]
        c = tt["classes"]
        slow = c["SLOW MOVING"][1] + c["NON-MOVING"][1] + c["DEAD STOCK"][1]
        rows = mv.get("_rows") or []
        names = mv.get("_names") or {}
        H.update(stock=tt["value"], slow=slow, dead=c["DEAD STOCK"][1] + c["NON-MOVING"][1],
                 rnc=tt["received_not_used"][1], negative_lines=tt["negative"][0])
        locs = sorted(mv["locations"], key=lambda L: -(L["SLOW MOVING"][1] + L["NON-MOVING"][1] + L["DEAD STOCK"][1]))
        dead_items = sorted([r for r in rows if r[13] in ("NON-MOVING", "DEAD STOCK") and r[3] > 0], key=lambda r: -r[3])
        rnc_items = sorted([r for r in rows if r[14]], key=lambda r: ({"critical": 0, "warning": 1}.get(r[14], 2), -r[3]))
        neg_items = sorted([r for r in rows if r[2] < 0], key=lambda r: r[3])
        adj = sorted(mv.get("adjustments") or [], key=lambda x: x["neg_cost"])
        mvx = {"as_of": mv["as_of"], "totals": tt, "slow": slow, "locations": locs, "names": names,
               "dead_items": dead_items, "rnc_items": rnc_items, "neg_items": neg_items, "adjustments": adj,
               "limits": mv["rules"]["limits"]}
        if neg_items:
            by = {}
            for r in neg_items:
                by[r[0]] = by.get(r[0], 0) + 1
            top = ", ".join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:4])
            act("crit", "Movement", "Location managers",
                f"{len(neg_items)} item lines below zero ({top}): post the missing receipts or transfers, or count them.",
                abs(sum(r[3] for r in neg_items)), "movement")
        crit_rnc = [r for r in rnc_items if r[14] == "critical"]
        if crit_rnc:
            act("crit", "Movement", "Cost controller",
                f"{len(crit_rnc)} lines received long ago and never used ({_m(sum(r[3] for r in crit_rnc))}): "
                f"check they are on the shelf; use, transfer, return or write down.", sum(r[3] for r in crit_rnc), "movement")
        if dead_items:
            L = locs[0]
            act("warn", "Movement", "Cost controller · Chef",
                f"Non-moving and dead stock {_m(H['dead'])} ({tt['value'] and H['dead'] / tt['value'] * 100:.0f}% of stock); "
                f"most at {L['location']} ({_m(L['NON-MOVING'][1] + L['DEAD STOCK'][1])}). Largest: "
                f"{names.get(dead_items[0][1]) or dead_items[0][1]} {_m(dead_items[0][3])}. Decide use, transfer, return or provision.",
                H["dead"], "movement")
        big_adj = [x for x in adj if x["neg_cost"] < 0][:3]
        if big_adj:
            act("info", "Movement", "Location managers",
                "Negative adjustments in 90 days: " + ", ".join(f"{x['location']} {_m(x['neg_cost'])}" for x in big_adj)
                + ". Agree which are counts, corrections or waste.", abs(sum(x["neg_cost"] for x in big_adj)), "movement")

    # ---- typology & turnover
    tyx = {}
    if ty:
        days = ty["history_days"] or 90
        fam, fb, use, ex = {}, 0.0, 0.0, 0.0
        tw = dw = 0.0
        for loc, typ, f, nat, lines, value, neg, u, nouse, excess, t_w, c_w, d_w, cnt in ty["rows"]:
            if nat not in ("ingredients", "resale"):
                continue
            fb += value
            use += u
            ex += excess
            tw += t_w
            dw += d_w
            g = fam.setdefault((loc, f or typ), [0.0, 0.0, 0.0, 0.0, 0.0])
            g[0] += value
            g[1] += u
            g[2] += excess
            g[3] += t_w
            g[4] += d_w
        doh = round(fb / (use / days)) if use > 0 else None
        target = round(tw / dw) if dw else None
        fams = sorted(([k[0], k[1], round(v[0]), round(v[0] / (v[1] / days)) if v[1] > 0 else None,
                        round(v[3] / v[4]) if v[4] else None, round(v[2])] for k, v in fam.items() if v[2] > 0),
                      key=lambda x: -x[5])
        count = [x for x in ty["items"] if x[15]]
        H.update(doh=doh, target=target, above_target=ex, count_lines=len(count))
        tyx = {"doh": doh, "target": target, "fb": fb, "above": ex, "families": fams, "count": count,
               "review": [x for x in ty["items"] if not x[15]]}
        if fams:
            f = fams[0]
            act("warn", "Typology", "Purchasing · Chef",
                f"Food, beverage and resale stock covers {doh} days against a target of {target}; {_m(ex)} above target. "
                f"Furthest: {f[1]} at {f[0]}, {f[3]} days against {f[4]} ({_m(f[5])}). Order less until it comes down.",
                ex, "typology")
        if count:
            by = {}
            for x in count:
                by[x[0]] = by.get(x[0], 0) + 1
            act("warn", "Typology", "Location managers",
                f"Count {len(count)} fresh lines that cannot be on the shelf in that quantity ("
                + ", ".join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:4]) + ").",
                sum(x[8] for x in count), "typology")

    # ---- recipe integrity and item master, on last week's sales
    rcx, imx = {}, {}
    if rc:
        k = rc["kpis"]
        H.update(coverage=k.get("coverage"), missing=k.get("missing"), incomplete=(k.get("incomplete") or 0) + (k.get("inactive") or 0))
        missing = [i for i in rc["missing"] if i.get("sale_entries")]
        inc = [i for i in rc["incomplete"] if i.get("sale_entries")]
        rcx = {"kpis": k, "missing": missing, "incomplete": inc, "coverage": rc["coverage"], "period": rc["period"]}
        if missing:
            act("crit", "Recipes", "Chef · Cost controller",
                f"{len(missing)} items sold last week with no recipe ({_m(k.get('missing_sold_qty'))} portions): they sell "
                f"with no ingredient consumption. Largest: {', '.join((i['desc'] or i['item']) for i in missing[:3])}.",
                None, "recipes")
        if inc:
            act("warn", "Recipes", "Chef · Cost controller",
                f"{len(inc)} sold recipes incomplete or inactive (missing or blocked component, zero quantity, unit "
                f"without conversion).", None, "recipes")
        cnt = rc["check_counts"]
        H["im_critical"] = sum(v for kk, v in cnt.items() if IM_SEV.get(kk) == "critical")
        imx = {"counts": cnt, "checks": rc["checks"]}
        crit = [kk for kk in cnt if cnt[kk] and IM_SEV.get(kk) == "critical"]
        if crit:
            act("crit", "Item master", "Cost controller",
                "Fix the item cards: " + "; ".join(f"{IM_LABEL.get(kk, kk).lower()} {cnt[kk]}" for kk in crit) + ".",
                None, "itemmaster")
        warn = [kk for kk in cnt if cnt[kk] and IM_SEV.get(kk) == "warning"]
        if warn:
            act("warn", "Item master", "Cost controller",
                "Review costing and posting setup: " + "; ".join(f"{IM_LABEL.get(kk, kk).lower()} {cnt[kk]}" for kk in warn) + ".",
                None, "itemmaster")

    rank = {"crit": 0, "warn": 1, "info": 2}
    acts.sort(key=lambda x: (rank[x["sev"]], -(x["value"] or 0)))
    with _state_lock:
        prev = (_read_state().get("last") or {}).get(company)
    return {"company": company, "currency": currency, "week": {"from": a.isoformat(), "to": b.isoformat()},
            "built_at": datetime.now().isoformat(timespec="seconds"), "errors": errors,
            "headline": H, "previous": prev, "actions": acts,
            "inventory": inv, "movement": mvx, "typology": tyx, "recipes": rcx, "item_master": imx}


# ---------------------------------------------------------------- the email
def subject(r, test=False):
    crit = sum(1 for x in r["actions"] if x["sev"] == "crit")
    a, b = date.fromisoformat(r["week"]["from"]), date.fromisoformat(r["week"]["to"])
    return (("[Test] " if test else "") + f"[RHG Finance] {r['company']} · Cost control {a:%d %b} – {b:%d %b %Y} · "
            f"{len(r['actions'])} actions, {crit} critical")


def render(r, public_url=""):
    e = html.escape
    C = r["currency"]
    H, P = r["headline"], (r.get("previous") or {}).get("headline") or {}
    navy, primary, ink, muted, line, sky = "#08303f", "#0c475e", "#0e2733", "#5c7482", "#d9dcdd", "#e4f3f9"
    red, amber, green = "#c0392b", "#b26a00", "#0e8a57"
    th = f"text-align:left;padding:6px 8px;background:{sky};color:{ink};font-size:11.5px;font-weight:600"
    thn = th.replace("text-align:left", "text-align:right")
    td = f"padding:5px 8px;border-bottom:1px solid #f0f1f2;font-size:12px;color:{ink};vertical-align:top"
    tdn = td + ";text-align:right;white-space:nowrap"
    a, b = date.fromisoformat(r["week"]["from"]), date.fromisoformat(r["week"]["to"])

    def delta(key, bad_up=True, money=True):
        if key not in H or H[key] is None or P.get(key) is None:
            return ""
        d = H[key] - P[key]
        if abs(d) < (1 if not money else 1000):
            return f"<span style='color:{muted}'>= last week</span>"
        worse = (d > 0) == bad_up
        txt = (_m(d) if money else f"{d:+,.0f}")
        if money and d > 0:
            txt = "+" + txt
        return f"<span style='color:{red if worse else green}'>{'▲' if d > 0 else '▼'} {txt} vs last week</span>"

    def tile(label, value, sub, color=ink, dl=""):
        return (f"<td style='padding:10px 12px;background:#fff;border:1px solid {line};border-radius:10px;vertical-align:top;"
                f"width:25%'><div style='font-size:11.5px;color:{muted}'>{e(label)}</div>"
                f"<div style='font-size:19px;font-weight:700;color:{color};margin:3px 0'>{value}</div>"
                f"<div style='font-size:11px;color:{muted}'>{e(sub)}</div><div style='font-size:11px;margin-top:2px'>{dl}</div></td>")
    inv, mv, ty, rc, im = r["inventory"], r["movement"], r["typology"], r["recipes"], r["item_master"]
    t = inv.get("totals") or {}
    tiles1 = [
        tile("Stock at item cost", _m(t.get("item_value") if t else H.get("stock")),
             f"G/L {_m(t.get('gl_closing'))} · difference {_m(t.get('difference'))}" if t else "",
             red if t and abs(t.get("difference") or 0) >= 1e6 else ink, delta("difference")),
        tile("Slow, non-moving, dead", _m(H.get("slow")),
             f"{H['slow'] / H['stock'] * 100:.0f}% of stock · not moving {_m(H.get('dead'))}" if H.get("stock") else "",
             amber if H.get("slow") else ink, delta("slow")),
        tile("Received, never used", _m(H.get("rnc")), f"{len(mv.get('rnc_items') or [])} lines" if mv else "",
             amber if H.get("rnc") else ink, delta("rnc")),
        tile("Below zero", f"{H.get('negative_lines', H.get('negative_items', 0)):,}", "item lines", red if H.get("negative_lines") else ink,
             delta("negative_lines", money=False))]
    tiles2 = [
        tile("Days of cover, F&B", f"{H['doh']} days" if H.get("doh") is not None else "–",
             f"target {H.get('target') or '–'} · {_m(H.get('above_target'))} above target" if ty else "",
             red if H.get("doh") and H.get("target") and H["doh"] > 2 * H["target"] else ink, delta("above_target")),
        tile("Fresh lines to count", f"{H.get('count_lines', 0):,}", "cover beyond the count limit", amber if H.get("count_lines") else ink,
             delta("count_lines", money=False)),
        tile("Recipe coverage", f"{H['coverage']:.0f}%" if H.get("coverage") is not None else "–",
             f"{H.get('missing', 0)} missing · {H.get('incomplete', 0)} incomplete, items sold last week" if rc else "",
             red if (H.get("coverage") or 100) < 90 else ink, delta("coverage", bad_up=False, money=False)),
        tile("Item master, critical", f"{H.get('im_critical', 0):,}", "blocked in use, no BOM, units", red if H.get("im_critical") else ink,
             delta("im_critical", money=False))]
    tiles = ("<table role='presentation' cellspacing='6' cellpadding='0' style='width:100%;margin:0 -6px'><tr>" + "".join(tiles1)
             + "</tr><tr>" + "".join(tiles2) + "</tr></table>")
    dot = {"crit": red, "warn": amber, "info": primary}
    pr = {"crit": "Critical", "warn": "This week", "info": "Review"}
    act_rows = "".join(
        f"<tr><td style='{td};width:84px'><span style='display:inline-block;width:8px;height:8px;border-radius:50%;background:{dot[x['sev']]};"
        f"margin-right:5px'></span>{pr[x['sev']]}</td><td style='{td};width:92px;color:{muted}'>{e(x['area'])}</td>"
        f"<td style='{td}'>{e(x['text'])}</td><td style='{td};width:110px;color:{muted}'>{e(x['owner'])}</td>"
        f"<td style='{tdn}'>{_m(x['value']) if x['value'] else ''}</td></tr>" for x in r["actions"])
    actions = (f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;border:1px solid {line}'>"
               f"<tr><th style='{th}'>Priority</th><th style='{th}'>Area</th><th style='{th}'>Action</th><th style='{th}'>Owner</th>"
               f"<th style='{thn}'>{e(C)}</th></tr>{act_rows}</table>") if act_rows else \
        f"<p style='font-size:13px;color:{muted}'>Nothing crossed a limit last week.</p>"

    def block(title, view, summary, head, rows):
        link = (f" <a href='{e(public_url)}/finance/#{view}' style='color:{primary};font-size:12px;font-weight:400'>Open ›</a>"
                if public_url else "")
        t_ = ""
        if rows:
            hs = "".join(f"<th style='{thn if n else th}'>{e(h)}</th>" for h, n in head)
            t_ = (f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;"
                  f"border:1px solid {line};margin-top:6px'><tr>{hs}</tr>{''.join(rows)}</table>")
        return (f"<h3 style='font-size:14px;color:{ink};margin:22px 0 2px'>{e(title)}{link}</h3>"
                f"<div style='font-size:12px;color:{muted}'>{summary}</div>{t_}")
    blocks = []
    if inv:
        blocks.append(block("Inventory against the G/L", "inventory",
                            e(f"G/L {_m(t['gl_closing'])} · item value {_m(t['item_value'])} · difference {_m(t['difference'])} · "
                              f"{t['negative_locations']} locations with stock below zero ({_m(t['negative_value'])}) · at {inv['as_of']}"),
                            [("Account", 0), ("G/L", 1), ("Items", 1), ("Difference", 1)],
                            [f"<tr><td style='{td}'>{e(x['account'])} {e(x['name'])}</td><td style='{tdn}'>{_m(x['closing'])}</td>"
                             f"<td style='{tdn}'>{_m(x['item_value'])}</td><td style='{tdn};color:{red if x.get('flag') else ink}'>{_m(x['difference'])}</td></tr>"
                             for x in inv["accounts"][:5]]))
    if mv:
        tt = mv["totals"]
        c = tt["classes"]
        blocks.append(block("Movement & dead stock", "movement",
                            e(f"As of {mv['as_of']}, 90 days: stock {_m(tt['value'])} · slow {_m(c['SLOW MOVING'][1])} · non-moving "
                              f"{_m(c['NON-MOVING'][1])} · dead {_m(c['DEAD STOCK'][1])} · received never used {_m(tt['received_not_used'][1])}"),
                            [("Location", 0), ("Stock", 1), ("Slow", 1), ("Non-moving + dead", 1), ("Never used", 1)],
                            [f"<tr><td style='{td}'>{e(L['location'])}</td><td style='{tdn}'>{_m(L['value'])}</td>"
                             f"<td style='{tdn}'>{_m(L['SLOW MOVING'][1])}</td><td style='{tdn};color:{red}'>{_m(L['NON-MOVING'][1] + L['DEAD STOCK'][1])}</td>"
                             f"<td style='{tdn}'>{_m(L['rnc'][1])}</td></tr>" for L in mv["locations"][:5]]))
    if ty:
        blocks.append(block("Typology & turnover", "typology",
                            e(f"Food, beverage and resale stock {_m(ty['fb'])} covers {ty['doh']} days against a target of "
                              f"{ty['target']}; {_m(ty['above'])} above target · {len(ty['count'])} fresh lines to count"),
                            [("Family", 0), ("Location", 0), ("Stock", 1), ("Days", 1), ("Target", 1), ("Above", 1)],
                            [f"<tr><td style='{td}'>{e(f[1])}</td><td style='{td}'>{e(f[0])}</td><td style='{tdn}'>{_m(f[2])}</td>"
                             f"<td style='{tdn};color:{red if f[3] and f[4] and f[3] > 2 * f[4] else amber}'>{f[3] if f[3] is not None else '–'}</td>"
                             f"<td style='{tdn}'>{f[4] if f[4] is not None else '–'}</td><td style='{tdn}'>{_m(f[5])}</td></tr>"
                             for f in ty["families"][:5]]))
    if rc:
        k = rc["kpis"]
        blocks.append(block("Recipe integrity", "recipes",
                            e(f"Items sold {rc['period']['from']} to {rc['period']['to']}: coverage "
                              f"{k['coverage'] if k.get('coverage') is not None else '–'}% · {len(rc['missing'])} sold with no recipe "
                              f"({_m(k.get('missing_sold_qty'))} portions) · {len(rc['incomplete'])} incomplete or inactive"),
                            [("Sold with no recipe", 0), ("Category", 0), ("Sold", 1)],
                            [f"<tr><td style='{td}'>{e(i['desc'] or i['item'])} <span style='color:{muted}'>{e(i['item'])}</span></td>"
                             f"<td style='{td}'>{e(i.get('category') or '')}</td><td style='{tdn}'>{i['qty_sold']:,.0f}</td></tr>"
                             for i in rc["missing"][:5]]))
    if im:
        n = im["counts"]
        rows = [f"<tr><td style='{td}'><span style='color:{dot['crit' if IM_SEV.get(kk) == 'critical' else 'warn' if IM_SEV.get(kk) == 'warning' else 'info']}'>●</span> "
                f"{e(IM_LABEL.get(kk, kk))}</td><td style='{tdn}'>{n[kk]:,}</td></tr>"
                for kk in sorted(n, key=lambda x: ({"critical": 0, "warning": 1}.get(IM_SEV.get(x), 2), -n[x])) if n[kk]]
        blocks.append(block("Item master control", "itemmaster", e("Configuration checks on the item cards in use last week."),
                            [("Check", 0), ("Items", 1)], rows[:8]))
    errs = "".join(f"<div style='background:#fdf3e2;color:{amber};padding:7px 11px;border-radius:8px;font-size:12px;margin-bottom:8px'>"
                   f"{e(x)}</div>" for x in r["errors"])
    body = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif;color:{ink}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ecebeb"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:900px">
<tr><td style="background:{navy};color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · RHG FINANCE</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">Weekly cost control report</div>
<div style="font-size:12px;color:#cfe3ea;margin-top:4px">{e(r['company'])} · week {a:%d %b} – {b:%d %b %Y} · stock as of {e(mv.get('as_of') or b.isoformat())}{' · against the report of ' + e(r['previous']['week']['to']) if r.get('previous') else ''}</div></td></tr>
<tr><td style="background:#fff;padding:20px 22px;border-radius:0 0 12px 12px">
{errs}
<h3 style="font-size:14px;color:{ink};margin:0 0 6px">Highlights</h3>{tiles}
<h3 style="font-size:14px;color:{ink};margin:22px 0 8px">Action list</h3>{actions}
{''.join(blocks)}
<p style="color:{muted};font-size:12px;margin:18px 0 0"><b>The attached Excel file holds every line behind these figures.</b> Values at item unit cost in {e(C)} (estimated); quantities from Business Central.</p>
{f"<p style='margin:20px 0 0'><a href='{e(public_url)}/finance/#inventory' style='background:{primary};color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600;display:inline-block'>Open Inventory in the Finance tower</a></p>" if public_url else ''}
<p style="color:{muted};font-size:11px;margin:22px 0 0;border-top:1px solid {line};padding-top:12px">Sent every week by RHG Control Towers from Business Central; nothing was changed there. Recipients and the day are in Finance → Settings → Cost control weekly email.</p>
</td></tr></table></td></tr></table></body></html>"""
    text = (f"RHG Finance · Weekly cost control report · {r['company']} · {a:%d %b} – {b:%d %b %Y}\n\nAction list:\n"
            + "\n".join(f"- [{pr[x['sev']]}] {x['area']}: {x['text']} ({x['owner']})" for x in r["actions"])
            + "\n\nThe attached Excel file holds every line.")
    return body, text


def render_xlsx(r):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    fill, font = PatternFill("solid", fgColor="E4F3F9"), Font(bold=True, color="0E2733")
    money, qty = '#,##0;[Red]-#,##0', '#,##0.00'

    def sheet(title, header, rows, formats, widths, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title[:31]
        ws.append(header)
        for c in ws[1]:
            c.fill, c.font, c.alignment = fill, font, Alignment(wrap_text=True, vertical="top")
        for row in rows:
            ws.append(row)
        for i, f in enumerate(formats, start=1):
            if f:
                for cell in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                    cell[0].number_format = f
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
    pr = {"crit": "Critical", "warn": "This week", "info": "Review"}
    sheet("Action list", ["Priority", "Area", "Action", "Owner", "Value", "Screen"],
          [[pr[x["sev"]], x["area"], x["text"], x["owner"], round(x["value"]) if x["value"] else None, x["view"]]
           for x in r["actions"]], [None, None, None, None, money, None], [11, 14, 100, 22, 14, 12], first=True)
    mv = r["movement"]
    names = mv.get("names") or {}

    def item_rows(lst):
        return [[x[0], x[1], names.get(x[1], ""), x[2], round(x[3]), x[10], x[11], x[12], x[13], x[14], x[15]] for x in lst]
    ihead = ["Location", "Item", "Description", "Quantity", "Value", "Last receipt", "Last use", "Days without use",
             "Class", "Never used", "Category"]
    ifmt, iw = [None, None, None, qty, money, None, None, None, None, None, None], [14, 11, 34, 11, 13, 11, 11, 10, 13, 10, 16]
    if mv:
        sheet("Below zero", ihead, item_rows(mv["neg_items"]), ifmt, iw)
        sheet("Non-moving and dead", ihead, item_rows(mv["dead_items"]), ifmt, iw)
        sheet("Received never used", ihead, item_rows(mv["rnc_items"]), ifmt, iw)
        sheet("Adjustments", ["Location", "Positive qty", "Positive value", "Negative qty", "Negative value"],
              [[x["location"], round(x["pos_qty"], 2), round(x["pos_cost"]), round(x["neg_qty"], 2), round(x["neg_cost"])]
               for x in mv["adjustments"]], [None, qty, money, qty, money], [16, 12, 14, 12, 14])
        sheet("Locations", ["Location", "Stock", "Slow", "Non-moving", "Dead", "Never used", "Below zero (lines)"],
              [[L["location"], L["value"], round(L["SLOW MOVING"][1]), round(L["NON-MOVING"][1]), round(L["DEAD STOCK"][1]),
                round(L["rnc"][1]), L["NEGATIVE"][0]] for L in mv["locations"]], [None, money, money, money, money, money, None],
              [16, 14, 13, 13, 13, 13, 12])
    ty = r["typology"]
    if ty:
        sheet("Families above target", ["Location", "Family", "Stock", "Days of cover", "Target", "Above target"],
              [list(f) for f in ty["families"]], [None, None, money, None, None, money], [16, 26, 14, 12, 10, 14])
        sheet("Count it", ["Location", "Item", "Description", "Type", "Family", "Nature", "Quantity", "Unit", "Value",
                           "Used", "Days of cover"],
              [x[:11] for x in ty["count"]], [None] * 6 + [qty, None, money, qty, None], [14, 11, 32, 14, 18, 12, 11, 7, 13, 11, 11])
    rc = r["recipes"]
    if rc:
        sheet("Sold with no recipe", ["Item", "Description", "Category", "Sold", "Sale entries", "Last sale", "Locations"],
              [[i["item"], i["desc"], i.get("category"), i["qty_sold"], i["sale_entries"], i["last_sale"], ", ".join(i["locations"])]
               for i in rc["missing"]], [None, None, None, qty, None, None, None], [11, 34, 18, 10, 10, 11, 30])
        sheet("Recipes incomplete", ["Item", "Description", "Status", "Issues", "Sold"],
              [[i["item"], i["desc"], i["status"], "; ".join(i.get("issues") or [])[:300] if isinstance(i.get("issues"), list) else str(i.get("issues") or ""), i["qty_sold"]]
               for i in rc["incomplete"]], [None, None, None, None, qty], [11, 34, 12, 70, 10])
    im = r["item_master"]
    if im:
        sheet("Item master", ["Check", "Severity", "Item", "Description", "Note"],
              [[IM_LABEL.get(k, k), IM_SEV.get(k, ""), it, desc, note] for k, lst in im["checks"].items() for it, desc, note in lst],
              [None] * 5, [40, 10, 11, 34, 60])
    inv = r["inventory"]
    if inv:
        sheet("G-L accounts", ["Account", "Name", "G/L opening", "G/L closing", "Item value", "Difference"],
              [[x["account"], x["name"], round(x["opening"]), round(x["closing"]), round(x["item_value"]), round(x["difference"])]
               for x in inv["accounts"]], [None, None, money, money, money, money], [10, 34, 14, 14, 14, 14])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- state, sending, schedule
def _read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(s):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=1, default=str), encoding="utf-8")
    tmp.replace(STATE_FILE)


def recent_runs(limit=15):
    with _state_lock:
        return list(reversed(_read_state().get("runs", [])))[:limit]


def _log(entry):
    with _state_lock:
        s = _read_state()
        s["runs"] = (s.get("runs", []) + [entry])[-100:]
        _write_state(s)


class Reporter:
    def __init__(self, client, stock_summary, currency_of, default_company):
        self.client, self.stock_summary, self.currency_of = client, stock_summary, currency_of
        self.default_company = default_company
        self._jobs, self._jobs_lock = {}, threading.Lock()

    @staticmethod
    def settings():
        return store.get_settings().get("cost_report", {})

    def companies(self, s=None):
        s = s or self.settings()
        return [c.strip() for c in (s.get("companies") or "").split(",") if c.strip()] or [self.default_company]

    def recipients(self, s=None):
        s = s or self.settings()
        to, bad = arap_notify.parse_emails(s.get("to"))
        cc, bad2 = arap_notify.parse_emails(s.get("cc"))
        return {"to": to, "cc": [c for c in cc if c not in to], "invalid": bad + bad2}

    def build(self, company):
        return build(company, self.client, self.stock_summary, self.currency_of(company))

    def send(self, company, r=None, only_to=None, actor="scheduler", test=False):
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "company": company, "actor": actor, "test": test}
        try:
            rec = {"to": only_to, "cc": []} if only_to else self.recipients()
            if not only_to:                  # the company's people (Settings → Email recipients by company)
                rec = recipients.resolve("cost_weekly", [company], rec["to"], rec["cc"])
            if not rec["to"]:
                raise RuntimeError("No recipient is set in Settings → Cost control weekly email.")
            r = r or self.build(company)
            public = (config.PUBLIC_URL or "").rstrip("/")
            body, text = render(r, public)
            name = f"RHG_{company.replace(' ', '_')}_cost_control_week_{r['week']['from']}.xlsx"
            arap_notify.send_mail(rec["to"], rec["cc"], subject(r, test), body, text,
                                  [(name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", render_xlsx(r))])
            entry.update(status="sent", to=rec["to"], cc=rec["cc"], week=r["week"],
                         detail=f"{len(r['actions'])} actions · {sum(1 for x in r['actions'] if x['sev'] == 'crit')} critical")
            if not test:
                with _state_lock:
                    s = _read_state()
                    s.setdefault("last", {})[company] = {"week": r["week"], "headline": r["headline"]}
                    _write_state(s)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            entry.update(status="failed", detail=str(exc)[:300])
        _log(entry)
        store.audit("email", f"cost control weekly · {company}", actor, {},
                    {k: entry.get(k) for k in ("status", "to", "cc", "test")}, entry.get("detail", ""))
        return entry

    # ---- preview and test in the background
    def start_job(self, company, actor, only_to=None):
        with self._jobs_lock:
            for j in self._jobs.values():
                if j["actor"] == actor and j["status"] == "running":
                    return {k: v for k, v in j.items() if k != "html"}
            self._jobs = {k: v for k, v in list(self._jobs.items())[-20:]}
            job = {"id": uuid.uuid4().hex, "actor": actor, "company": company, "to": only_to or [], "status": "running",
                   "detail": "", "html": None, "started": datetime.now().isoformat(timespec="seconds")}
            self._jobs[job["id"]] = job

        def go():
            try:
                r = self.build(company)
                public = (config.PUBLIC_URL or "").rstrip("/")
                job["html"] = render(r, public)[0]
                if only_to:
                    res = self.send(company, r, only_to, actor, test=True)
                    if res["status"] != "sent":
                        raise RuntimeError(res.get("detail") or "not sent")
                    job["detail"] = "Sent to " + ", ".join(only_to)
                job["status"] = "done"
            except Exception as exc:                               # noqa: BLE001
                traceback.print_exc()
                job.update(status="failed", detail=str(exc)[:300])
        threading.Thread(target=go, daemon=True, name="cost-report-job").start()
        return {k: v for k, v in job.items() if k != "html"}

    def job(self, job_id, actor):
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            return j if j and j["actor"] == actor else None

    # ---- the weekly schedule
    @staticmethod
    def _when(s):
        try:
            day = max(0, min(6, int(float(s.get("day") or 0))))
        except (TypeError, ValueError):
            day = 0
        hh, _, mm = (s.get("time") or "08:30").partition(":")
        return day, int(hh or 8), int(mm or 0)

    def next_run(self, now=None):
        s = self.settings()
        if not s.get("enabled"):
            return None
        now = now or datetime.now()
        day, hh, mm = self._when(s)
        with _state_lock:
            done = _read_state().get("done")
        for k in range(8):
            d = now.date() + timedelta(days=k)
            if d.weekday() != day:
                continue
            t = datetime(d.year, d.month, d.day, hh, mm)
            if t + timedelta(hours=10) >= now and done != last_week(d)[0].isoformat():
                return t.isoformat(timespec="minutes")
        return None

    def due(self, now):
        s = self.settings()
        if not s.get("enabled"):
            return None
        day, hh, mm = self._when(s)
        if now.weekday() != day:
            return None
        start = datetime(now.year, now.month, now.day, hh, mm)
        if not (start <= now <= start + timedelta(hours=10)):
            return None
        week = last_week(now.date())[0].isoformat()
        with _state_lock:
            st = _read_state()
        if st.get("done") == week:
            return None
        tries = st.get("tries", {})
        if tries.get("week") == week and (tries.get("count", 0) >= 3 or time.time() - tries.get("at", 0) < 1800):
            return None
        return week

    def run(self, week):
        with _run_lock:
            results = [self.send(c) for c in self.companies()]
            with _state_lock:
                st = _read_state()
                if all(x["status"] == "sent" for x in results):
                    st["done"] = week
                    st.pop("tries", None)
                else:
                    t = st.get("tries", {})
                    count = t.get("count", 0) + 1 if t.get("week") == week else 1
                    st["tries"] = {"week": week, "count": count, "at": time.time()}
                    if count >= 3:
                        st["done"] = week
                _write_state(st)
            return results

    def loop(self):
        while True:
            try:
                week = self.due(datetime.now())
                if week:
                    self.run(week)
            except Exception:                                      # noqa: BLE001
                traceback.print_exc()
            time.sleep(60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="cost-report").start()
