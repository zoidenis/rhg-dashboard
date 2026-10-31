"""Fixed assets → Location intelligence: the fixed assets of every location, from the FA ledger.

The FA ledger holds hundreds of thousands of entries across the years, so it is never held in memory:
it is read page by page into a summary per asset (its class, description, FA location and, per
depreciation book, posting type and month, the amounts), kept in data/fa_summary.json and topped up
with the entries numbered after the last one read. Everything on the screen comes from that summary:

  * per location (the asset's FA location on its latest entry, matched to the restaurant registry):
    assets in use, acquisition cost, accumulated depreciation and net book value at the end of the
    period chosen, how far depreciated, assets fully depreciated and still on the books;
  * the period and the year to date: additions, depreciation, disposals (book value taken off) and
    proceeds; additions and depreciation month by month for the year;
  * by FA class, and the largest assets.
Months are the unit: a week or a day in the header counts as its month. Read-only.
"""
import json
import threading
import time
import traceback
from datetime import date

import config
import locintel
import store

FILE = store.DATA_DIR / "fa_summary.json"
_file_lock, _lock = threading.Lock(), threading.Lock()
_jobs = {}
KEEP = 600
FIELDS = ["Entry_No", "FA_No", "FA_Description", "FA_Class_Code", "FA_Posting_Type", "FA_Posting_Category",
          "FA_Location_Code", "Depreciation_Book_Code", "Posting_Date", "Amount_LCY"]
BOOK_VALUE = {"Acquisition Cost", "Depreciation", "Write-Down", "Appreciation", "Custom 1", "Custom 2"}
# short keys kept in the file: a = acquisition cost, d = depreciation, w = write-down / appreciation /
# custom, p = proceeds on disposal, g = gain/loss; a trailing x = posted as part of a disposal
TYPE_KEY = {"Acquisition Cost": "a", "Depreciation": "d", "Write-Down": "w", "Appreciation": "w",
            "Custom 1": "w", "Custom 2": "w", "Proceeds on Disposal": "p", "Gain/Loss": "g"}


def _read():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(d):
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
    tmp.replace(FILE)


def refresh(client, company, progress=lambda t: None):
    """Tops up the summary of a company with the FA entries numbered after the last one read."""
    with _file_lock:
        allc = _read()
    S = allc.get(company) or {"last": 0, "assets": {}, "books": {}, "entries": 0}
    seen = {"n": 0}

    def page(rows):
        for e in rows:
            no = int(e.get("Entry_No") or 0)
            fa = str(e.get("FA_No") or "")
            if not fa:
                continue
            A = S["assets"].setdefault(fa, {"desc": "", "class": "", "loc": "", "le": 0, "b": {}})
            if no >= A["le"]:
                A["le"] = no
                A["loc"] = (e.get("FA_Location_Code") or "").strip()
                A["desc"] = (e.get("FA_Description") or A["desc"] or "")[:80]
                A["class"] = (e.get("FA_Class_Code") or A["class"] or "").strip()
            book = (e.get("Depreciation_Book_Code") or "").strip()
            S["books"][book] = S["books"].get(book, 0) + 1
            k = TYPE_KEY.get((e.get("FA_Posting_Type") or "").strip())
            if not k:
                continue
            if (e.get("FA_Posting_Category") or "").strip().lower() == "disposal" and k in ("a", "d", "w"):
                k += "x"
            m = str(e.get("Posting_Date") or "")[:7]
            cell = A["b"].setdefault(book, {}).setdefault(k, {})
            cell[m] = round(cell.get(m, 0.0) + float(e.get("Amount_LCY") or 0), 2)
            S["last"] = max(S["last"], no)
            S["entries"] += 1
        seen["n"] += len(rows)
        progress(f"FA ledger: {seen['n']:,} entries read")

    flt = f"Entry_No gt {S['last']}" if S["last"] else None
    params = {"$select": ",".join(FIELDS)}
    if flt:
        params["$filter"] = flt
    client._get_all(client._url(company, config.E_FA), params, on_page=page)
    S["at"] = time.time()
    with _file_lock:
        allc = _read()
        allc[company] = S
        _write(allc)
    return S


def _upto(cell, m):
    return sum(v for k, v in (cell or {}).items() if k <= m)


def _between(cell, a, b):
    return sum(v for k, v in (cell or {}).items() if a <= k <= b)


def build(S, company, period, currency, settings):
    c_from, c_to = period[0], period[1]
    m_from, m_to = f"{c_from:%Y-%m}", f"{c_to:%Y-%m}"
    y_from = f"{c_to.year}-01"
    books = S.get("books") or {}
    book = max(books, key=lambda b: books[b]) if books else ""
    reg = [r for r in locintel.registry(settings) if (r.get("company") or "").upper() == (company or "").upper()]
    match = {}
    for r in reg:
        for k in (r.get("bc"), r.get("dept"), r.get("ref")):
            if k:
                match.setdefault(k.strip().upper(), r)
    closed = c_to.replace(day=1) < date.today().replace(day=1)          # the month of the period has ended
    months = [f"{c_to.year}-{i:02d}" for i in range(1, c_to.month + 1)]
    out = {}
    for fa, A in (S.get("assets") or {}).items():
        b = (A.get("b") or {}).get(book)
        if not b:
            continue
        cost = _upto(b.get("a"), m_to) + _upto(b.get("ax"), m_to)
        dep = _upto(b.get("d"), m_to) + _upto(b.get("dx"), m_to)
        wd = _upto(b.get("w"), m_to) + _upto(b.get("wx"), m_to)
        nbv = cost + dep + wd
        acquired = _upto(b.get("a"), m_to)
        if not acquired and not nbv:
            continue                                                   # bought after the period
        disposed_in = [k for k in list((b.get("ax") or {}).keys()) + list((b.get("dx") or {}).keys()) if k <= m_to]
        disposed = bool(disposed_in) and abs(cost) < 1
        code = A.get("loc") or ""
        r = match.get(code.upper())
        L = out.get(code)
        if L is None:
            L = out[code] = {"name": (r or {}).get("name") or code or "No FA location",
                             "group": "restaurant" if r else ("unassigned" if not code else "support"),
                             "brand": (r or {}).get("brand") or "", "seasonal": bool((r or {}).get("seasonal")),
                             "assets": 0, "cost": 0.0, "dep": 0.0, "nbv": 0.0, "fully": 0, "fully_cost": 0.0,
                             "p_add": 0.0, "p_dep": 0.0, "p_disp": 0.0, "p_proc": 0.0, "y_add": 0.0, "y_dep": 0.0,
                             "y_disp": 0.0, "dep_month": 0.0, "disposed_n": 0,
                             "by_class": {}, "top": [], "added": [], "m_add": {m: 0.0 for m in months},
                             "m_dep": {m: 0.0 for m in months}}
        # movements of the period and the year (disposal postings apart)
        L["p_add"] += _between(b.get("a"), m_from, m_to)
        L["p_dep"] += -_between(b.get("d"), m_from, m_to)
        L["p_disp"] += -(_between(b.get("ax"), m_from, m_to) + _between(b.get("dx"), m_from, m_to) + _between(b.get("wx"), m_from, m_to))
        L["p_proc"] += -_between(b.get("p"), m_from, m_to)
        L["y_add"] += _between(b.get("a"), y_from, m_to)
        L["y_dep"] += -_between(b.get("d"), y_from, m_to)
        L["y_disp"] += -(_between(b.get("ax"), y_from, m_to) + _between(b.get("dx"), y_from, m_to) + _between(b.get("wx"), y_from, m_to))
        L["dep_month"] += -(b.get("d") or {}).get(m_to, 0.0)
        for m in months:
            L["m_add"][m] += (b.get("a") or {}).get(m, 0.0)
            L["m_dep"][m] += -(b.get("d") or {}).get(m, 0.0)
        y_added = _between(b.get("a"), y_from, m_to)
        if disposed:
            if any(k >= y_from for k in disposed_in):
                L["disposed_n"] += 1
            continue
        L["assets"] += 1
        L["cost"] += cost
        L["dep"] += -(dep + wd)
        L["nbv"] += nbv
        if cost > 0 and nbv <= max(1.0, cost * 0.001):
            L["fully"] += 1
            L["fully_cost"] += cost
        c = L["by_class"].setdefault(A.get("class") or "—", [0, 0.0, 0.0])
        c[0] += 1
        c[1] += cost
        c[2] += nbv
        L["top"].append([fa, A.get("desc") or "", A.get("class") or "", round(cost), round(nbv),
                         round(-_between(b.get("d"), y_from, m_to))])
        if y_added:
            first = min(k for k, v in (b.get("a") or {}).items() if k >= y_from and v)
            L["added"].append([fa, A.get("desc") or "", A.get("class") or "", first, round(y_added)])
    for code, L in out.items():
        L["top"] = sorted(L["top"], key=lambda x: -x[4])[:40]
        L["added"] = sorted(L["added"], key=lambda x: (x[3], -x[4]), reverse=True)[:40]
        L["by_class"] = sorted(([k, v[0], round(v[1]), round(v[2])] for k, v in L["by_class"].items()), key=lambda x: -x[2])
        for k in ("cost", "dep", "nbv", "fully_cost", "p_add", "p_dep", "p_disp", "p_proc", "y_add", "y_dep", "y_disp", "dep_month"):
            L[k] = round(L[k])
        L["m_add"] = [round(L["m_add"][m]) for m in months]
        L["m_dep"] = [round(L["m_dep"][m]) for m in months]
        L["dep_pct"] = round(L["dep"] / L["cost"] * 100, 1) if L["cost"] > 0 else None
        al = []
        if closed and L["nbv"] > 1000 and L["assets"] and not L["dep_month"] and L["group"] != "unassigned":
            al.append(["warn", f"No depreciation posted for {c_to:%B %Y}"])
        if not code and L["assets"]:
            al.append(["warn", f"{L['assets']} assets with no FA location"])
        if L["assets"] and L["fully"] / L["assets"] > 0.5:
            al.append(["info", f"{L['fully']} of {L['assets']} assets fully depreciated and still on the books"])
        if L["p_add"] > 0:
            al.append(["info", f"Additions in the period: {L['p_add']:,.0f}"])
        L["alerts"] = al
    out = {k: v for k, v in out.items() if v["assets"] or v["p_add"] or v["p_disp"] or v["y_add"] or v["y_dep"]}
    return {"company": company, "currency": currency, "book": book, "locations": out, "months": months,
            "period": {"from": c_from.isoformat(), "to": c_to.isoformat(), "months": [m_from, m_to], "label": period[7]},
            "closed": closed, "entries": S.get("entries", 0), "read_at": S.get("at"),
            "definitions": DEFINITIONS}


def request(client, company, period, currency, refresh_now=False):
    """The screen: from the summary when it was topped up in the last ten minutes, else a background
    read (the first one reads the whole FA ledger; later ones only the newer entries)."""
    with _file_lock:
        S = _read().get(company)
    fresh = S and time.time() - (S.get("at") or 0) < KEEP and not refresh_now
    if fresh:
        return build(S, company, period, currency, store.get_settings())
    key = company or ""
    with _lock:
        job = _jobs.get(key)
        if not job or job["state"] != "running":
            if job and job["state"] == "failed" and time.time() - job["finished"] < 60 and not refresh_now:
                return {"available": False, "error": job["error"]}
            job = _jobs[key] = {"state": "running", "started": time.time(), "stage": "Starting"}

            def progress(text):
                job["stage"] = text

            def run():
                try:
                    refresh(client, company, progress)
                    job.update(state="done", finished=time.time())
                except Exception as exc:                           # noqa: BLE001
                    traceback.print_exc()
                    job.update(state="failed", finished=time.time(), error=str(exc)[:300])
            threading.Thread(target=run, daemon=True, name="faloc").start()
    # while the top-up runs, the summary already held answers (marked as being refreshed)
    if S:
        r = build(S, company, period, currency, store.get_settings())
        r["refreshing"] = job.get("stage", "")
        return r
    return {"pending": True, "stage": job.get("stage", ""), "elapsed": int(time.time() - job["started"])}


DEFINITIONS = {
    "source": "FA ledger entries of the depreciation book used most (the book is named on top), summed per asset "
              "and month. The location is the FA location on the asset's latest entry, matched to the restaurant "
              "registry of Settings → Location intelligence; other FA locations (administration, warehouses) are "
              "shown apart.",
    "values": "Cost = acquisition cost; accumulated depreciation = depreciation, write-downs and appreciation; net "
              "book value = cost − accumulated depreciation, at the end of the month of the period chosen. Disposed "
              "assets (book value and cost taken to zero by a disposal) are left out of the counts.",
    "movements": "Additions = acquisition cost posted in the period (not disposals); depreciation = depreciation "
                 "posted; disposals = book value taken off by disposal postings; proceeds = proceeds on disposal. "
                 "A week or a day in the header counts as its month.",
}
