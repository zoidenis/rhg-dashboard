"""Marketing: the RHG Performance dashboard that nl360 (RHG's campaign manager) builds, read into pulse: ads (Meta and
Google), the Google Business Profile listings (interactions and reviews) and organic social media (Instagram and
Facebook), by brand and month, and the OPA units one by one. Only read.

Where it comes from: https://rhg.nl360.co/dashboard/ draws itself from three files nl360 regenerates every morning,
data.js (window.RHG: every brand by month), loc.js (window.RHG_LOC: the OPA units, with their Simphony reference "till")
and checks.js (window.RHG_CHECKS: nl360's own reconciliation of the figures). The site is behind Cloudflare Access (a code
by email), so the server reads it with a Cloudflare Access service token nl360 issues for pulse (the headers
CF-Access-Client-Id and CF-Access-Client-Secret); until then, the files can be uploaded on the screen. They are kept in
data/marketing/ as they were read. Amounts are in euros, as nl360 shows them.

The figures are nl360's, with their definitions (shown on the screen): the estimated ads revenue is an estimate from ad
results, not tracked sales (Meta: reservations × 90 % show-up, clicks × 4.5 % for OPA, impressions ÷ 10,000 for Le
District; Google: directions × 20 %; each × the venue's average check); social media are organic only.
"""
import json
import threading
import time
import traceback
from datetime import datetime

import requests

import store

DIR = store.DATA_DIR / "marketing"
CONFIG_FILE = store.DATA_DIR / "marketing.json"
FILES = {"data": ("data.js", "RHG"), "loc": ("loc.js", "RHG_LOC"), "checks": ("checks.js", "RHG_CHECKS")}
DEFAULTS = {"url": "https://rhg.nl360.co/dashboard/", "client_id": "", "client_secret": ""}
TIMEOUT = 60
_lock = threading.Lock()
_mem = {"at": 0.0, "value": None}
_state = {"last_fetch": None, "error": "", "running": False}

DEFINITIONS = {
    "spend": "Ad spend: Meta and Google Ads, as their APIs report it (nl360), in euros.",
    "est_rev": "Estimated ads revenue (nl360): an estimate from ad results, not tracked sales. Meta: reservations × 90% "
               "show up (SevenRooms venues), clicks × 4.5% become customers (OPA), impressions ÷ 10,000 (Le District); "
               "Google: direction requests × 20% visit the venue; each × the venue's average check at the tills.",
    "cpc": "Cost per click = ad spend ÷ ad clicks.",
    "gbp": "Google Business Profile: the free listing on Maps and Search. Interactions = every action on the listing "
           "(directions, calls, website clicks, menu views, bookings, orders, messages).",
    "reviews": "Google rating = the average of all reviews at the month end; new reviews = those written in the month, "
               "and their average.",
    "social": "Social media: organic only, no ads (nl360): Instagram posts that went out in the month, their numbers to date.",
}


# ---------------------------------------------------------------- settings
def _cfg():
    c = dict(DEFAULTS)
    c.update(store._read(CONFIG_FILE, {}) or {})
    return c


def settings():
    c = _cfg()
    return {"url": c["url"], "client_id": c["client_id"], "client_secret_set": bool(c["client_secret"]),
            "configured": bool(c["client_id"] and c["client_secret"])}


def save_settings(patch, actor):
    with _lock:
        c = _cfg()
        before = dict(c, client_secret="•••" if c["client_secret"] else "")
        for k in ("url", "client_id", "client_secret"):
            if k in (patch or {}):
                v = str(patch[k] or "").strip()
                if k == "client_secret" and not v:
                    continue
                if k == "url" and v and not v.startswith("https://"):
                    raise ValueError("The address must start with https://.")
                c[k] = v[:500]
        if c["url"] and not c["url"].endswith("/"):
            c["url"] += "/"
        store._write(CONFIG_FILE, c)
    store.audit("marketing source saved", "nl360", actor, before, dict(c, client_secret="•••" if c["client_secret"] else ""))
    return settings()


# ---------------------------------------------------------------- the files
def parse_js(text, var):
    """'window.<var> = {...};' → the object."""
    t = (text or "").strip()
    if not t.startswith("window." + var) and not t.startswith("{"):
        raise ValueError(f"This is not nl360's {var} file (it should start with window.{var} =).")
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < a:
        raise ValueError("The file has no data.")
    return json.loads(t[a:b + 1])


def _keep(kind, obj, how, actor):
    DIR.mkdir(parents=True, exist_ok=True)
    store._write(DIR / f"{kind}.json", obj)
    meta = store._read(DIR / "meta.json", {}) or {}
    meta[kind] = {"at": datetime.now().isoformat(timespec="seconds"), "how": how, "by": actor,
                  "generated": obj.get("generated") if isinstance(obj, dict) else None}
    store._write(DIR / "meta.json", meta)
    _mem["at"] = 0.0


def upload(name, text, actor):
    """A file saved from the dashboard (data.js, loc.js or checks.js), kept as if read."""
    kind = next((k for k, (fn, _v) in FILES.items() if str(name or "").lower().endswith(fn)), None)
    if not kind:
        raise ValueError("Upload data.js, loc.js or checks.js from nl360's dashboard.")
    obj = parse_js(text, FILES[kind][1])
    if kind == "data" and not isinstance(obj.get("brands"), list):
        raise ValueError("data.js has no brands.")
    _keep(kind, obj, "uploaded", actor)
    store.audit("marketing file uploaded", FILES[kind][0], actor, {}, {"generated": obj.get("generated")})
    return kind


def fetch(actor="scheduler"):
    """The three files from the dashboard, through Cloudflare Access with the service token."""
    c = _cfg()
    if not (c["client_id"] and c["client_secret"]):
        raise ValueError("No Cloudflare Access service token yet: ask nl360 for one, or upload the files.")
    hdr = {"CF-Access-Client-Id": c["client_id"], "CF-Access-Client-Secret": c["client_secret"],
           "User-Agent": "pulse-rhg/1.0"}
    got = []
    for kind, (fn, var) in FILES.items():
        r = requests.get(c["url"] + fn, headers=hdr, timeout=TIMEOUT, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 401, 403) or "cloudflareaccess.com" in (r.headers.get("Location") or ""):
            raise RuntimeError(f"Cloudflare Access refused the service token for {fn} (HTTP {r.status_code}): nl360 must "
                               "allow the token in the application's policy (Service Auth).")
        if r.status_code != 200:
            raise RuntimeError(f"{fn}: HTTP {r.status_code}")
        try:
            obj = parse_js(r.text, var)
        except ValueError as exc:
            if kind == "checks":
                continue                       # nl360's own checks are a nice-to-have
            raise RuntimeError(f"{fn}: {exc}")
        _keep(kind, obj, "service token", actor)
        got.append(fn)
    _state.update(last_fetch=datetime.now().isoformat(timespec="seconds"), error="")
    return got


def load():
    """{data, loc, checks, meta} as kept (held a minute in memory)."""
    if _mem["value"] is not None and time.time() - _mem["at"] < 60:
        return _mem["value"]
    v = {k: store._read(DIR / f"{k}.json", None) for k in FILES}
    v["meta"] = store._read(DIR / "meta.json", {}) or {}
    _mem.update(at=time.time(), value=v)
    return v


# ---------------------------------------------------------------- what the screen gets
def public():
    v = load()
    d, loc = v["data"], v["loc"]
    if not isinstance(d, dict):
        return {"available": False, "settings": settings(), "state": dict(_state), "meta": v["meta"],
                "error": "No marketing data yet: enter nl360's service token, or upload data.js and loc.js below."}
    till = {x.get("slug"): x.get("till") for x in (loc or {}).get("locs") or []} if isinstance(loc, dict) else {}
    brands = []
    for b in d.get("brands") or []:
        brands.append({
            "id": b.get("id"), "name": b.get("name"), "group": b.get("group"), "sub": b.get("sub") or "", "type": b.get("type"),
            "sales": [(m or {}).get("sales") for m in b.get("months") or []],
            "checks": [(m or {}).get("checks") for m in b.get("months") or []],
            "ads": b.get("ads") or [], "gbp": {k: v_ for k, v_ in (b.get("gbp") or {}).items() if k != "locs"} if b.get("gbp") else None,
            "social": b.get("social"),
            "locs": [{"name": l.get("name"), "slug": l.get("slug"), "till": till.get(l.get("slug")),
                      "sales": [(m or {}).get("sales") for m in l.get("months") or []],
                      "ads": l.get("ads") or [], "gbp": {k: v_ for k, v_ in (l.get("gbp") or {}).items() if k != "name"} if l.get("gbp") else None}
                     for l in b.get("locs") or []]})
    ch = v["checks"] if isinstance(v["checks"], dict) else {}
    return {"available": True, "generated": d.get("generated"), "synced": d.get("syncedAt"), "latest_bus_dt": d.get("latestBusDt"),
            "months": d.get("months") or [], "fx_rate": d.get("fxRate"), "gbp_first": d.get("gbpFirst"), "social_from": d.get("socialFrom"),
            "ads_source": d.get("adsSource"), "brands": brands, "symbiosis": d.get("symbiosis"),
            "checks": {"total": ch.get("total"), "mismatches": len(ch.get("mismatches") or []),
                       "workbook_diffs": len(ch.get("workbookDiffs") or [])} if ch else None,
            "meta": v["meta"], "settings": settings(), "state": dict(_state), "definitions": DEFINITIONS}


def opa_reviews(ref, ym):
    """The Google reviews written in month ym at the unit whose Simphony reference is ref (nl360 reads the Business
    Profile of each OPA unit): {value, count, approximate, detail} or None. A month whose units all show none while the
    brand has some is not finished on nl360's side: None."""
    v = load()
    d, loc = v["data"], v["loc"]
    if not isinstance(d, dict) or not isinstance(loc, dict):
        return None
    slug = next((x.get("slug") for x in loc.get("locs") or [] if x.get("till") == ref), None)
    if not slug:
        return None
    months = d.get("months") or []
    if ym not in months:
        return None
    k = months.index(ym)

    def at(arr, i):
        try:
            x = arr[i] if isinstance(arr, list) else (arr or {}).get(str(i))
        except IndexError:
            return None
        return x
    for b in d.get("brands") or []:
        locs = b.get("locs") or []
        mine = next((l for l in locs if l.get("slug") == slug), None)
        if not mine or not mine.get("gbp"):
            continue
        rv = mine["gbp"].get("reviews") or {}
        n, s = at(rv.get("n"), k), at(rv.get("s"), k)
        if n is None:
            return None
        all_units = sum((at(((l.get("gbp") or {}).get("reviews") or {}).get("n"), k) or 0) for l in locs)
        brand_n = at(((b.get("gbp") or {}).get("reviews") or {}).get("n"), k) or 0
        if all_units == 0 and brand_n > 0:
            return None                       # the units are not filled in yet for this month
        return {"value": round(s / n, 2) if n else None, "count": int(n), "approximate": False,
                "detail": f"Google: {int(n)} review(s) written in the month (Business Profile, nl360's dashboard)"}
    return None


# ---------------------------------------------------------------- the loop
def start():
    """Every six hours, when the service token is set (nl360 regenerates the files every morning)."""
    def loop():
        time.sleep(90)
        while True:
            if settings()["configured"] and not _state["running"]:
                _state["running"] = True
                try:
                    fetch()
                except Exception as exc:                            # noqa: BLE001
                    _state["error"] = str(exc)[:300]
                finally:
                    _state["running"] = False
            time.sleep(6 * 3600)
    threading.Thread(target=loop, daemon=True, name="marketing").start()


def fetch_now(actor):
    if _state["running"]:
        return
    _state["running"] = True
    try:
        fetch(actor)
    except Exception as exc:                                        # noqa: BLE001
        traceback.print_exc()
        _state["error"] = str(exc)[:300]
    finally:
        _state["running"] = False
