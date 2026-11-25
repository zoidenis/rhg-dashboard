"""Sales → Price simulator (Geri, 24 Nov 2026): what a new price does to the sales and the margin, on one sales
channel or on several (eat in only, or DE-Wolt only), and the price Pulse suggests for each item and channel to the
operations and unit managers.

The base: the period of the filters, read as Menu engineering reads it (menueng.py: Simphony items sold and sales
less VAT by menu item and order type, the portion cost from the BC recipes, the item's class: Star, Plowhorse,
Puzzle, Dog). By channel (Simphony order type, as Channel profitability names them):
  * items sold at a price and the average price received without VAT (net sales / items sold at a price); with the
    VAT % of the BC item, the price on the menu (VAT included);
  * the channel's commission (Channel profitability's parameters: % on the sales with VAT or without).
Competitors: the price files people upload (Excel or CSV: competitor, our brand, channel, item, price with VAT,
our item, date), matched to our items by the item number or name given, else by the words of the name.

The simulation (the page works it out as prices are typed; the same formulas here, for the tests and the Excel):
  new items sold = items sold × (new price / price) ^ elasticity, the elasticity by class (Parameters);
  net price = price / (1 + VAT); commission = k × price (on the sales with VAT) or k × net price;
  margin per item = net price − commission − portion cost; the change = new margin − margin, over the period and
  for a year at the same pace. Break-even: how many fewer items the new price can sell before the margin falls.
The suggested price, per item and channel (the rules of menu engineering and pricing practice):
  * Star: a small rise (Parameters, 4%), the customers hold;
  * Plowhorse and Dog: towards the price that gives the portion cost target (Menu engineering's, 32%), no more than
    the largest step (8%) at once; a Dog may also go;
  * Puzzle: no rise; lower it towards the competitors when it is dearer than them by more than the band;
  * No cost: only against the competitors;
  * a delivery channel (commission): at least the price that leaves the margin of eating in after the commission,
    within the largest step;
  * the competitors' median on the channel (plus the band, 5%) caps a rise; never below the portion cost;
  * rounded to the currency's step (10 lek, 0.10 EUR): up for a rise, down for a cut.
Nothing is written to Simphony: a new price is set in EMC by whoever sets the menus.
"""
import csv
import io
import json
import math
import re
import statistics
import threading
import time
import unicodedata
import uuid

import chanprof
import elastic
import locintel
import menueng
import simphony
import store

COMP_FILE = store.DATA_DIR / "competitor_prices.json"
SCEN_FILE = store.DATA_DIR / "price_scenarios.json"
PARAMS_FILE = store.DATA_DIR / "price_params.json"
_lock = threading.Lock()

# el_plowhorse −0.8 (was −0.9; Geri, 25 Nov 2026): on delivery −0.96, a rise no longer loses sales by itself.
# el_measured: the elasticity measured from RHG's own price changes where there are enough (elastic.py)
DEFAULTS = {"el_star": -0.6, "el_plowhorse": -0.8, "el_puzzle": -1.2, "el_dog": -1.4, "el_nocost": -1.0,
            "el_delivery": 1.2,                      # delivery customers compare more: the elasticity × this
            "star_step_pct": 4.0, "max_step_pct": 8.0, "comp_band_pct": 5.0, "comp_low_pct": 10.0,
            "round_all": 10.0, "round_eur": 0.1, "parity": True, "el_measured": True}
CLASSES = ("star", "plowhorse", "puzzle", "dog", "nocost")
DELIVERY_HINT = re.compile(r"WOLT|GLOVO|BOLT|DELIVER|DORËZIM|DOREZIM|\bDE\b|DE-|UBER|FOODPANDA|BAJA|TAKEAWAY ONLINE", re.I)
EAT_IN_HINT = re.compile(r"EAT|DINE|SALL|IN ?HOUSE|LOKAL|TABLE|TAVOL", re.I)


# ---------------------------------------------------------------- parameters
def _num(v, d, lo=-1e9, hi=1e9):
    try:
        x = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return d
    return max(lo, min(hi, x)) if math.isfinite(x) else d


def load_params():
    try:
        raw = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return {k: (bool(raw[k]) if isinstance(v, bool) else _num(raw[k], v)) if k in raw else v for k, v in DEFAULTS.items()}


def save_params(patch, actor):
    p = load_params()
    for k, v in DEFAULTS.items():
        if k not in (patch or {}):
            continue
        if isinstance(v, bool):
            p[k] = bool(patch[k])
        elif k.startswith("el_delivery"):
            p[k] = _num(patch[k], v, 1.0, 3.0)
        elif k.startswith("el_"):
            p[k] = -abs(_num(patch[k], v, -5.0, 5.0))
        elif k.startswith("round_"):
            p[k] = _num(patch[k], v, 0.0, 1000.0)
        else:
            p[k] = _num(patch[k], v, 0.0, 100.0)
    with _lock:
        _atomic(PARAMS_FILE, dict(p, updated_at=time.strftime("%Y-%m-%dT%H:%M:%S"), updated_by=actor))
    store.audit("settings", "price simulator parameters", actor, {}, {}, "")
    return load_params()


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- competitors' prices
def norm(text):
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", t)).strip()


def words(text):
    stop = {"me", "dhe", "e", "i", "te", "the", "with", "and", "of", "per", "nga", "ne", "a"}
    return {w for w in norm(text).split() if w not in stop and not w.isdigit()}


HEAD = {"competitor": ("competitor", "konkurrent", "konkurenti", "konkurrenti", "restaurant", "restoranti"),
        "brand": ("our brand", "brand", "brandi", "brendi"),
        "channel": ("channel", "kanal", "kanali"),
        "item": ("item", "product", "produkt", "produkti", "artikull", "artikulli", "competitor item", "emri"),
        "price": ("price", "cmim", "cmimi", "price vat", "price with vat", "price vat incl"),
        "our_item": ("our item", "plu", "artikulli yne", "our plu", "menu item"),
        "date": ("date", "data")}


def _col(header):
    h = norm(header)
    for k, names in HEAD.items():
        if h in names or any(h.startswith(n) for n in names if len(n) > 4):
            return k
    return None


def parse(filename, data):
    """The rows of an uploaded price file (.xlsx or .csv): [{competitor, brand, channel, item, price, our_item, date}]."""
    rows = []
    if data[:2] == b"PK":
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for ws in wb.worksheets:
            rows += [list(r) for r in ws.iter_rows(values_only=True)]
            if rows:
                break
    else:
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t") if text.strip() else csv.excel
        rows = [r for r in csv.reader(io.StringIO(text), dialect)]
    hi = next((i for i, r in enumerate(rows[:15]) if sum(1 for c in r if _col(c or "")) >= 2), None)
    if hi is None:
        raise ValueError("No header found: the file needs at least the columns Item and Price (and Competitor).")
    cols = {_col(c or ""): i for i, c in enumerate(rows[hi]) if _col(c or "")}
    if "item" not in cols or "price" not in cols:
        raise ValueError("The columns Item and Price are needed.")
    out = []
    for r in rows[hi + 1:]:
        get = lambda k: (r[cols[k]] if k in cols and cols[k] < len(r) else None)    # noqa: E731
        item, price = str(get("item") or "").strip(), _num(get("price"), None, 0, 1e9)
        if not item or not price:
            continue
        d = get("date")
        out.append({"competitor": str(get("competitor") or filename or "").strip()[:80], "brand": str(get("brand") or "").strip()[:60],
                    "channel": chanprof.channel_key(get("channel") or "")[:60], "item": item[:120], "price": round(price, 2),
                    "our_item": str(get("our_item") or "").strip()[:120],
                    "date": d.date().isoformat() if hasattr(d, "date") else str(d or "")[:10]})
    if not out:
        raise ValueError("No row with an item and a price.")
    return out


def load_competitors():
    try:
        return json.loads(COMP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sources": []}


def import_competitors(filename, data, actor):
    rows = parse(filename, data)
    with _lock:
        d = load_competitors()
        src = {"id": uuid.uuid4().hex[:10], "file": str(filename or "file")[:120], "by": actor, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "rows": rows[:5000]}
        d["sources"] = [s for s in d["sources"] if s.get("file") != src["file"]] + [src]   # the same file replaces itself
        _atomic(COMP_FILE, d)
    store.audit("import", "competitor prices", actor, {}, {"rows": len(rows)}, src["file"])
    return {"id": src["id"], "rows": len(src["rows"])}


def delete_competitors(source_id, actor):
    with _lock:
        d = load_competitors()
        n = len(d["sources"])
        d["sources"] = [s for s in d["sources"] if s.get("id") != source_id]
        _atomic(COMP_FILE, d)
    store.audit("delete", "competitor prices", actor, {}, {}, source_id)
    return n != len(d["sources"])


def match(comp_rows, items, brands):
    """{mi: [competitor rows]} for our items, and the rows matched to none. A row names our item by number or name
    ("our item"), else its own name is compared word by word (half the words in common, at least)."""
    by_num = {str(it["mi"]): it for it in items}
    by_name = {norm(it["name"]): it for it in items}
    toks = [(it, words(it["name"])) for it in items]
    B = {norm(b) for b in brands if b}
    out, unmatched = {}, []
    for r in comp_rows:
        if r.get("brand") and B and norm(r["brand"]) not in B:
            continue
        it = by_num.get(r.get("our_item") or "") or by_name.get(norm(r.get("our_item") or "")) if r.get("our_item") else None
        how = "given"
        if it is None:
            w = words(r["item"])
            best, score = None, 0.0
            for cand, cw in toks:
                if not w or not cw:
                    continue
                s = len(w & cw) / len(w | cw)
                if s > score:
                    best, score = cand, s
            it, how = (best, f"by name ({score:.0%})") if score >= 0.5 else (None, "")
        if it is None:
            unmatched.append(r)
            continue
        out.setdefault(it["mi"], []).append(dict(r, matched=how))
    return out, unmatched


# ---------------------------------------------------------------- the suggested price
def rounded(p, step, up):
    if not step:
        return round(p, 2)
    return round((math.ceil(p / step - 1e-9) if up else math.floor(p / step + 1e-9)) * step, 2)


def unit_factor(vat, k, base):
    """d(net price after commission) / d(menu price with VAT)."""
    v = (vat or 0) / 100
    return (1 - k) / (1 + v) if base == "net" else 1 / (1 + v) - k


def suggest(cls, price, cost, vat, k=0.0, base="vat", target=32.0, comps=(), eat_ex=None, P=None, step=10.0, el=None):
    """(suggested price with VAT, [reasons]) for one item on one channel. price: today's price with VAT on it.
    el: the elasticity of the item on the channel; a price that would earn less than today's at it (fewer sold) is
    not suggested: the best price between today's and the rule's is, or none."""
    P = P or DEFAULTS
    if not price or price <= 0:
        return None, ["No price on this channel."]
    a = unit_factor(vat, k, base)
    reasons, cand = [], price
    mx, st = P["max_step_pct"] / 100, P["star_step_pct"] / 100
    p_target = cost / (target / 100) / a if cost is not None and a > 0 and target else None
    med = statistics.median(comps) if comps else None
    if cls == "star":
        cand = price * (1 + st)
        reasons.append(f"Star: popular and high margin, a small rise ({P['star_step_pct']:g}%) usually holds.")
    elif cls in ("plowhorse", "dog"):
        name = "Plowhorse: sold a lot at a low margin" if cls == "plowhorse" else "Dog: low margin and sold little"
        if p_target and p_target > price * 1.005:
            cand = min(p_target, price * (1 + mx))
            reasons.append(f"{name}: towards the price at the {target:g}% portion cost target ({p_target:,.0f}), "
                           f"no more than {P['max_step_pct']:g}% at once.")
        elif cls == "plowhorse":
            cand = price * (1 + st)
            reasons.append(f"{name}, already within the cost target: a small rise.")
        else:
            reasons.append(f"{name}, within the cost target: hold the price; consider taking it off the menu.")
    elif cls == "puzzle":
        reasons.append("Puzzle: high margin, sold little: no rise; move it on the menu and recommend it.")
        if med and price > med * (1 + P["comp_band_pct"] / 100):
            cand = max(med * (1 + P["comp_band_pct"] / 100), price * (1 - mx))
            reasons.append(f"Dearer than the competitors' median ({med:,.0f}): lower it to sell more.")
    else:
        reasons.append("No portion cost in BC: priced against the competitors only.")
        if med and price < med * (1 - P["comp_low_pct"] / 100):
            cand = min(med * (1 - P["comp_low_pct"] / 200), price * (1 + mx))
            reasons.append(f"Cheaper than the competitors' median ({med:,.0f}) by more than {P['comp_low_pct']:g}%.")
    if k > 0 and P.get("parity") and eat_ex and a > 0:
        par = eat_ex / a                                     # the margin of eating in, left after the commission
        if par > cand * 1.005:
            cand = max(cand, min(par, price * (1 + mx)))
            reasons.append(f"Delivery: {k * 100:g}% commission; {par:,.0f} keeps the margin of eating in"
                           + (f", no more than {P['max_step_pct']:g}% at once." if par > price * (1 + mx) else "."))
    if med and cand > price:
        cap = med * (1 + P["comp_band_pct"] / 100)
        if cand > cap:
            cand = max(price, cap)
            reasons.append(f"Capped at the competitors' median + {P['comp_band_pct']:g}% ({cap:,.0f}).")
    if cost is not None and a > 0 and cand < cost / a:
        cand = cost / a
        reasons.append("Not below the portion cost.")
    if abs(cand - price) < max(step / 2, price * 0.005):
        return price, reasons + ["Hold the price."]
    new = rounded(cand, step, cand > price)
    if el is not None:
        def earn(p):
            e = effect(1.0, price, p, cost, vat, k, base, el)
            return e["new_cm"] if cost is not None else e["new_net"]
        what = "margin" if cost is not None else "sales"
        if earn(new) < earn(price) - 1e-9:
            lo, hi = sorted((price, new))
            grid, p = [], rounded(lo, step, True)
            while p <= hi + 1e-9 and len(grid) < 200:
                grid.append(p)
                p = round(p + (step or max(0.01, price * 0.01)), 2)
            best = max(grid + [price], key=earn)
            if best == price or abs(best - price) < max(step / 2, price * 0.005):
                return price, reasons + [f"At the elasticity of {el:.2f}, {new:,.0f} would sell fewer and earn less {what} "
                                         f"than today: hold the price."]
            return best, reasons + [f"At the elasticity of {el:.2f}, {new:,.0f} would earn less {what}; {best:,.0f} earns "
                                    f"the most between the two."]
    return new, reasons


def effect(q, price, new, cost, vat, k=0.0, base="vat", el=-1.0):
    """The change of one item on one channel: {qty, net, cm, cm_unit, new_qty, new_net, new_cm, new_cm_unit, be_pct}."""
    a = unit_factor(vat, k, base)
    v = 1 + (vat or 0) / 100
    nq = q * (new / price) ** el if price and new else q
    net, nnet = price / v * q, new / v * nq
    cmu = price * a - (cost or 0)
    ncmu = new * a - (cost or 0)
    be = (cmu * q / ncmu - q) / q * 100 if ncmu > 0 and q else None
    return {"qty": q, "net": net, "cm": cmu * q, "cm_unit": cmu, "new_qty": nq, "new_net": nnet, "new_cm": ncmu * nq,
            "new_cm_unit": ncmu, "be_pct": be}


# ---------------------------------------------------------------- the base of the simulation
GENERIC = {"ORDER", "TYPE", "NO", "THE", "AND", "TAKE", "AWAY", "EAT", "IN", "DINE", "OUT", "DE", "TO", "GO", "ME", "NE"}


DEL_RE = re.compile(r"[\s\-_(]+DEL\)?\s*$", re.I)


def is_del(name):
    """A delivery item: its name ends in DEL (Chicken Gyros Big DEL), with a price of its own for delivery."""
    return bool(DEL_RE.search(str(name or "")))


def base_name(name):
    return DEL_RE.sub("", str(name or "")).strip()


def channel_words(ch):
    return {w for w in re.split(r"[^A-Z0-9]+", str(ch).upper()) if len(w) >= 3 and w not in GENERIC}


def loc_channel_prices(locs, channels, items, rates=None, errors=None):
    """{(location, menu item, channel): (price with VAT, "EMC level <name>")}: the price of the EMC price levels or
    revenue centres of each location whose name carries a word of the channel's (WOLT of DE - WOLT), the most common."""
    want = {ch: channel_words(ch) for ch in channels}
    want = {ch: w for ch, w in want.items() if w}
    if not want:
        return {}
    found = {}
    for L in locs:
        f = rates.get(L["currency"]) if rates is not None else 1.0
        if f is None:
            continue
        try:
            rows = simphony.menu_item_prices(L["ref"])
            rvcs = simphony.rvc_dims(L["ref"])
        except Exception as exc:                                     # noqa: BLE001
            if errors is not None:
                errors.append(f"{L['ref']}: menu prices by level: {str(exc)[:120]}")
            continue
        for r in rows:
            p = r.get("price")
            if r["num"] not in items or p is None or float(p) <= 1:
                continue
            names = f"{r.get('level_name') or ''} {rvcs.get(r.get('rvc'), '') if isinstance(rvcs, dict) else ''}".upper()
            words = set(re.split(r"[^A-Z0-9]+", names))
            for ch, w in want.items():
                if w & words:
                    found.setdefault((L["ref"], r["num"], ch), []).append((round(float(p) * f, 2), r.get("level_name") or f"level {r.get('level')}"))
    out = {}
    for k, lst in found.items():
        price = statistics.mode([p for p, _n in lst])
        out[k] = (price, f"EMC level {next(n for p, n in lst if p == price)}")
    return out


def channel_prices(locs, channels, items, rates=None, errors=None):
    """{(menu item, channel): (price with VAT, "EMC level <name>")}: the most common across the locations."""
    by = {}
    for (_ref, mi, ch), v in loc_channel_prices(locs, channels, items, rates, errors).items():
        by.setdefault((mi, ch), []).append(v)
    out = {}
    for k, lst in by.items():
        price = statistics.mode([p for p, _n in lst])
        out[k] = next(v for v in lst if v[0] == price)
    return out


def is_delivery(ch, cfg):
    c = (cfg.get("channels") or {}).get(ch) or {}
    return float(c.get("commission_pct") or 0) > 0 or bool(DELIVERY_HINT.search(ch))


def build(ds, level="all", key="", section="family", settings=None):
    """The items of the selection with their price, items sold and net sales on every channel, the class, the cost,
    the competitors and the suggested price per channel."""
    settings = settings or store.get_settings()
    # a brand is its price group (locintel.price_group): OPA without OPA · TIA Airport, which has prices of its own
    # (Geri, 25 Nov 2026); the simulator asks Menu engineering for those locations
    reg = {r["ref"]: r for r in locintel.registry(settings)}
    # franchises are not RHG's to price (Geri, 25 Nov 2026: OPA Shkodër): left out of every level and the location box
    fr = [L for L in ds["locations"] if locintel.is_franchise(reg.get(L["ref"]) or {})]
    if fr:
        frefs = {L["ref"] for L in fr}
        ds = dict(ds, locations=[L for L in ds["locations"] if L["ref"] not in frefs])
    group = {L["ref"]: (reg.get(L["ref"]) or {}).get("price_group") or locintel.price_group(dict(L, **(reg.get(L["ref"]) or {})))
             for L in ds["locations"]}
    groups = sorted({g for g in group.values() if g})
    title = None
    if level == "brand":
        title = f"{key} (brand)"
        level, key = "locations", ",".join(r for r, g in group.items() if g == (key or "").upper())
    an = menueng.analyse(ds, level, key, section, settings)
    if title:
        an = dict(an, title=title, level="brand", key=title[:-8])
    S, P, cfg = an["settings"], load_params(), chanprof.load_config()
    reporting, rates = menueng._rates(settings)
    sel_refs = {x["ref"] for x in an["selection"]}
    locs = [L for L in ds["locations"] if L["ref"] in sel_refs]
    convert = an["converted"]
    cur = an["currency"]
    step = P["round_eur"] if cur == "EUR" else P["round_all"]
    items = {it["mi"]: it for it in an["items"] if it["cls"] != "noprice"}
    # the VAT % of each item (its BC item), from the rows of the read
    vat = {}
    for L in locs:
        for r in ds["rows"].get(L["ref"], []):
            if r.get("vat") is not None and r["mi"] not in vat:
                vat[r["mi"]] = r["vat"]
    share = {mi: it["qty"] / (it["qty"] + it["combo_qty"]) if it["qty"] + it["combo_qty"] > 0 else 1.0
             for mi, it in items.items()}
    per, errors = {}, list(ds.get("errors") or [])
    for L in locs:
        f = rates.get(L["currency"]) if convert else 1.0
        if f is None:
            continue
        try:
            ots = simphony.order_type_dims(L["ref"])
        except Exception as exc:                                     # noqa: BLE001
            ots = {}
            errors.append(f"{L['ref']}: order types: {str(exc)[:120]}")
        for _d, ot, mi, net, qty, _prep in menueng.items_by_order_type(L["ref"], ds["period"]["from"], ds["period"]["to"]):
            if mi not in items:
                continue
            n = ots.get(int(ot)) if str(ot).lstrip("-").isdigit() and int(ot) else None
            ch = chanprof.channel_key(n or ("No order type" if not ot or str(ot) == "0" else f"Order type {ot}"))
            x = per.setdefault((mi, ch, L["ref"]), [0.0, 0.0])
            x[0] += qty or 0
            x[1] += (net or 0) * f

    def vat_of(mi):
        v = vat.get(mi)
        return float(cfg.get("vat_pct") or 20) if v is None else v

    # the items sold AT A PRICE on each channel (Geri, 25 Nov 2026: Chicken Gyros on DE - WOLT showed 15 lek). Units
    # rung at no price (inside combo meals, or the platform's own lines) do not move with the item's price: the units at
    # a price are the channel's sales with VAT / the item's menu price in EMC (as Menu engineering counts them), at
    # most the units rung; without an EMC price, the item's share of units sold at a price
    # the menu price of the item on a channel: the EMC price of a price level or revenue centre named like the channel
    # (a level "Wolt" for DE - WOLT), else the item's menu price (Menu engineering's)
    # each location at its own price (Geri, 25 Nov 2026: Chicken Gyros Pita Combo is 530 at four OPA and 590 at Blloku
    # and Arena; Wolt sold it at 590 and showed 541, the price of the six together): the units at a price are counted
    # location by location, at the location's EMC price for the channel, and the channel's price is what they sold at
    ch_names = {ch for (_mi, ch, _r) in per}
    lchref = loc_channel_prices(locs, ch_names, set(items), rates if convert else None, errors)
    lemc, lname = {}, {L["ref"]: L.get("name") or L["ref"] for L in locs}
    for L in locs:
        f = rates.get(L["currency"]) if convert else 1.0
        try:
            lemc[L["ref"]] = {num: p * f for num, (p, _low) in menueng.emc_prices(L["ref"]).items()} if f else {}
        except Exception:                                            # noqa: BLE001 - no prices: the item's own
            lemc[L["ref"]] = {}

    def ref_price(mi, ch, ref):
        lv = lchref.get((ref, mi, ch))
        if lv:
            return lv
        p = lemc.get(ref, {}).get(mi)
        return (p, "EMC") if p and p > 1 else (items[mi].get("emc_incl"), "EMC")

    def priced(mi, ch, q, n, ref):
        rp = ref_price(mi, ch, ref)[0]
        if rp and rp > 0:
            return min(q, n * (1 + vat_of(mi) / 100) / rp)
        return q * share[mi]
    per_loc, agg = {}, {}
    for (mi, ch, ref), (q, n) in per.items():
        qp = priced(mi, ch, q, n, ref)
        rp, src = ref_price(mi, ch, ref)
        per_loc.setdefault((mi, ch), []).append({"ref": ref, "name": lname.get(ref, ref), "qty": round(qp, 2), "rung": round(q, 2),
                                                 "price": round(n * (1 + vat_of(mi) / 100) / qp, 2) if qp > 0 else None,
                                                 "menu_price": round(rp, 2) if rp else None, "menu_source": src})
        x = agg.setdefault((mi, ch), [0.0, 0.0, 0.0, 0.0, set()])
        x[0] += qp
        x[1] += n
        x[2] += q
        x[3] += (rp or 0) * qp
        if src != "EMC":
            x[4].add(src)
    per = {k: (q, n, raw) for k, (q, n, raw, _w, _s) in agg.items()}
    chref = {k: (round(w / q, 2), " / ".join(sorted(srcs)) if srcs else "EMC") for k, (q, _n, _r, w, srcs) in agg.items() if q > 0 and w > 0}
    chans = {}
    for (mi, ch), (q, n, _raw) in per.items():
        if q <= 0.5:
            continue
        c = chans.setdefault(ch, {"channel": ch, "qty": 0.0, "net": 0.0, "items": 0})
        c["qty"] += q
        c["net"] += n
        c["items"] += 1
    for ch, c in chans.items():
        cc = (cfg.get("channels") or {}).get(ch) or {}
        c.update(qty=round(c["qty"], 1), net=round(c["net"], 0), commission_pct=float(cc.get("commission_pct") or 0),
                 commission_base=cc.get("commission_base") or "vat", delivery=is_delivery(ch, cfg))
    eat = max((c for c in chans.values() if not c["delivery"]), key=lambda c: (bool(EAT_IN_HINT.search(c["channel"])), c["qty"]),
              default=None)
    # delivery items: the same product with " DEL" at the end of its name and a price of its own (Chicken Gyros Big DEL);
    # each is paired with its eat-in item by the name
    by_name = {norm(it["name"]): mi for mi, it in items.items() if not is_del(it["name"])}
    twin = {}
    for mi, it in items.items():
        if is_del(it["name"]):
            b = by_name.get(norm(base_name(it["name"])))
            if b is not None:
                twin[mi], twin[b] = b, mi
    # the elasticity measured from RHG's own price changes (elastic.py), else the class's (Parameters)
    meas = {"items": {}, "status": None}
    if P.get("el_measured", True):
        try:
            meas = elastic.measure([L["ref"] for L in locs], list(items), {mi: it.get("family") for mi, it in items.items()})
        except Exception as exc:                                     # noqa: BLE001
            errors.append(f"elasticity from history: {str(exc)[:160]}")
    comp_rows = [dict(r, source=s["file"]) for s in load_competitors()["sources"] for r in s["rows"]]
    brands = sorted({L["brand"] for L in locs if L["brand"]})
    cmap, unmatched = match(comp_rows, list(items.values()), brands)
    out = []
    for mi, it in items.items():
        v = vat_of(mi)
        comps = cmap.get(mi, [])
        chs = {}
        for ch, c in chans.items():
            q, n, raw = per.get((mi, ch), (0.0, 0.0, 0.0))
            if q <= 0.5:
                continue
            p_ex = n / q
            price = round(p_ex * (1 + v / 100), 2)
            k = c["commission_pct"] / 100
            ccomp = [r["price"] for r in comps if r["channel"] == ch] or \
                    ([] if c["delivery"] else [r["price"] for r in comps if not r["channel"] or not DELIVERY_HINT.search(r["channel"])])
            eat_ex = None
            if eat and ch != eat["channel"]:
                # a delivery item ("… DEL") keeps the margin of its eat-in twin (Geri, 25 Nov 2026)
                src = twin.get(mi) if twin.get(mi) and is_del(items[mi]["name"]) else mi
                eq, en, _r = per.get((src, eat["channel"]), (0.0, 0.0, 0.0))
                eat_ex = en / eq if eq > 0 else None
            em = meas["items"].get(mi)
            el = em["value"] if em else P.get(f"el_{it['cls']}", -1.0) * (P["el_delivery"] if c["delivery"] else 1.0)
            sug, why = suggest(it["cls"], price, it["cost"], v, k, c["commission_base"], S["target"], ccomp, eat_ex, P, step, el)
            unpriced = max(0.0, raw - q)
            if unpriced >= 1 and unpriced >= 0.1 * raw:
                why = [f"{unpriced:,.0f} of the {raw:,.0f} rung on this channel were at no price (inside combo meals or the "
                       f"platform's own lines): left out; the price and the change are of the {q:,.0f} sold at a price."] + why
            ref = chref.get((mi, ch))
            ls = sorted((x for x in per_loc.get((mi, ch), []) if x["qty"] > 0.5), key=lambda x: -x["qty"])
            chs[ch] = {"qty": round(q, 2), "net": round(n, 0), "price": price, "price_ex": round(p_ex, 4),
                       "rung": round(raw, 2), "unpriced": round(unpriced, 2), "locs": ls,
                       "menu_price": ref[0] if ref else it.get("emc_incl"), "menu_source": ref[1] if ref else "EMC",
                       "comp_median": round(statistics.median(ccomp), 2) if ccomp else None, "comp_n": len(ccomp),
                       "suggested": sug, "why": why}
        if not chs:
            continue
        out.append({"mi": mi, "bc_item": it.get("bc_item") or "", "name": it["name"], "section": it["section"], "family": it["family"], "major": it["major"],
                    "cls": it["cls"], "cost": it["cost"], "vat": v, "emc_incl": it.get("emc_incl"), "qty": it["qty"],
                    "net": it["net"], "channels": chs,
                    "delivery": is_del(it["name"]), "twin": twin.get(mi), "el_m": meas["items"].get(mi),
                    "competitors": [{k: r.get(k) for k in ("competitor", "item", "price", "channel", "date", "source", "matched")}
                                    for r in comps][:30]})
    out.sort(key=lambda x: -x["net"])
    return {"title": an["title"], "level": an["level"], "key": an["key"], "currency": cur, "converted": convert,
            "period": an["period"], "read_at": an["read_at"], "errors": errors[:12], "settings": S, "params": P,
            "round_step": step, "eat_in": eat["channel"] if eat else None,
            "channels": sorted(chans.values(), key=lambda c: -c["net"]), "items": out,
            "brands": groups, "locations": an["locations"], "selection": an["selection"],
            "franchises": sorted(L["name"] for L in fr), "elasticity": meas["status"],
            "labels": an["labels"],
            "competitors": {"sources": [{k: s.get(k) for k in ("id", "file", "by", "at")} | {"rows": len(s["rows"])}
                                        for s in load_competitors()["sources"]],
                            "matched": sum(len(v) for v in cmap.values()), "unmatched": unmatched[:200],
                            "unmatched_n": len(unmatched)}}


# ---------------------------------------------------------------- scenarios
def load_scenarios():
    try:
        return json.loads(SCEN_FILE.read_text(encoding="utf-8")).get("items") or []
    except (OSError, ValueError):
        return []


def save_scenario(body, actor):
    name = str(body.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("Give the scenario a name.")
    changes = {str(k)[:140]: round(_num(v, 0, 0, 1e9), 2) for k, v in (body.get("changes") or {}).items()
               if _num(v, 0, 0, 1e9) > 0}
    if not changes:
        raise ValueError("No price was changed.")
    with _lock:
        items = load_scenarios()
        sid = str(body.get("id") or "") or uuid.uuid4().hex[:10]
        old = next((s for s in items if s["id"] == sid), None)
        if old and old.get("by") != actor and not body.get("admin"):
            raise ValueError("Only who saved a scenario changes it; save it under another name.")
        s = {"id": sid, "name": name, "note": str(body.get("note") or "")[:500], "by": actor,
             "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "level": str(body.get("level") or "all")[:20],
             "key": str(body.get("key") or "")[:60], "company": str(body.get("company") or "")[:60],
             "changes": changes, "state": _state(body.get("state")), "summary": {k: body.get("summary", {}).get(k) for k in ("net", "cm", "items")}
             if isinstance(body.get("summary"), dict) else {}}
        items = [x for x in items if x["id"] != sid] + [s]
        _atomic(SCEN_FILE, {"items": items[-300:]})
    store.audit("save", "price scenario", actor, {}, {"changes": len(changes)}, name)
    return s


def _state(st):
    """The moves of the playground, to open the scenario as it was made: a % per category and per item, Pulse's
    prices, the prices typed, the channels."""
    st = st if isinstance(st, dict) else {}
    pct = lambda d: {str(k)[:80]: round(_num(v, 0, -90, 300), 1) for k, v in list((d or {}).items())[:500]    # noqa: E731
                     if isinstance(d, dict) and _num(v, 0, -90, 300)}
    price = lambda d: {str(k)[:140]: round(_num(v, 0, 0, 1e9), 2) for k, v in list((d or {}).items())[:3000]    # noqa: E731
                       if isinstance(d, dict) and _num(v, 0, 0, 1e9) > 0}
    return {"cat": pct(st.get("cat")), "item": pct(st.get("item")), "sug": price(st.get("sug")),
            "manual": price(st.get("manual")), "channels": [str(c)[:60] for c in (st.get("channels") or [])[:20]]}


def delete_scenario(sid, actor, admin=False):
    with _lock:
        items = load_scenarios()
        s = next((x for x in items if x["id"] == sid), None)
        if not s:
            return False
        if s.get("by") != actor and not admin:
            raise ValueError("Only who saved a scenario deletes it.")
        _atomic(SCEN_FILE, {"items": [x for x in items if x["id"] != sid]})
    store.audit("delete", "price scenario", actor, {}, {}, s["name"])
    return True


TEMPLATE_HEAD = ["Competitor", "Our brand", "Channel", "Item", "Price (VAT incl.)", "Our item (number or name)", "Date"]


def template_xlsx():
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Competitor prices"
    ws.append(TEMPLATE_HEAD)
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.append(["Restaurant X", "OPA", "DE-WOLT", "Pita gyros pule", 450, "", time.strftime("%Y-%m-%d")])
    ws.append(["Restaurant X", "OPA", "", "Pita gyros pule", 400, "", time.strftime("%Y-%m-%d")])
    for col, w in zip("ABCDEFG", (22, 14, 14, 34, 16, 26, 12)):
        ws.column_dimensions[col].width = w
    r = wb.create_sheet("Read me")
    for line in ("One row per competitor item and price (VAT included).",
                 "Our brand: the brand it is compared with (empty: every brand of the selection).",
                 "Channel: as the order type in Simphony (e.g. DE-WOLT); empty = eating in / the menu price.",
                 "Our item: our menu item number or its name when the names differ; empty = matched by the words of the name.",
                 "Uploading a file with the same name replaces it."):
        r.append([line])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
