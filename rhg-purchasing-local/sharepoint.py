"""RHG Home on SharePoint, read through Microsoft Graph with the same Entra ID app as email (graph_mail.py).

The app holds only Sites.Selected, with "read" granted on RHG-Home alone: it sees no other site, and writes
nothing. What is read here is the navigation register of RHG Home ("Regjistri Navigimit RHG"): the destinations
of the portal (sites, brand pages, units), each with its owner, audience and status. A link gives no access: the
person who opens it sees only what SharePoint already lets their own account see.

The register's own rules are kept: a destination switched off (Statusi "Çaktivizuar") or not verified
(Verifikimi "Jo", the ones it marks "Nuk shfaqet ne navigimin e miratuar") is not shown, and neither is one
without a SharePoint address.
"""
import threading
import time
from datetime import datetime
from urllib.parse import quote, urlsplit

import config
import graph_mail

TTL = 600                 # seconds between two readings of the register
_lock = threading.Lock()
_cache = {"at": 0.0, "value": None}

# the register's columns by their display name (the internal names are field_1, field_2 ...)
COLUMNS = {"Title": "title", "KodiDestinacionit": "code", "Lloji": "type", "Prindi": "parent", "URL": "url",
           "Pronari": "owner", "Audienca": "audience", "Statusi": "status", "Verifikimi": "verified",
           "Miratuar": "approved", "Renditja": "order", "KodBrandi": "brand", "KodLokacioni": "location",
           "Shenime": "note"}


def _get(url):
    r = graph_mail._call("GET", url)
    if r.status_code != 200:
        try:
            e = r.json().get("error") or {}
            text = f"{e.get('code') or r.status_code}: {e.get('message') or ''}".strip(": ")
        except (ValueError, AttributeError):
            text = f"HTTP {r.status_code}"
        hint = " (the app has no read access to this site: Sites.Selected grant)" if r.status_code == 403 else ""
        raise RuntimeError(f"SharePoint refused the reading: {text[:240]}{hint}")
    return r.json()


def _all(url):
    out = []
    while url:
        j = _get(url)
        out += j.get("value", [])
        url = j.get("@odata.nextLink")
    return out


def _safe_url(value):
    """Only https addresses on SharePoint: anything else in the register is never turned into a link."""
    url = (value or {}).get("Url") if isinstance(value, dict) else value
    url = str(url or "").strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "sharepoint.com" or host.endswith(".sharepoint.com")):
        return ""
    return url


def _read():
    g = graph_mail.GRAPH
    site = _get(f"{g}/sites/{config.SP_HOME_SITE}?$select=id,displayName,webUrl")
    lists = _all(f"{g}/sites/{site['id']}/lists?$select=id,displayName,webUrl")
    match = [x for x in lists if x.get("displayName") == config.SP_NAV_LIST]
    if not match:
        raise RuntimeError(f"The list \"{config.SP_NAV_LIST}\" was not found on {site.get('webUrl')}.")
    lst = match[0]
    base = f"{g}/sites/{site['id']}/lists/{quote(lst['id'], safe='')}"
    names = {c.get("displayName"): c.get("name") for c in _all(base + "/columns?$select=name,displayName")}
    fields = {names[d]: key for d, key in COLUMNS.items() if names.get(d)}
    items, hidden = [], []
    for row in _all(base + "/items?expand=fields&$top=200"):
        f = row.get("fields") or {}
        x = {key: f.get(name) for name, key in fields.items()}
        x = {k: ("" if v is None else v) for k, v in x.items()}
        x["url"] = _safe_url(x.get("url"))
        try:
            x["order"] = float(x.get("order") or 0)
        except (TypeError, ValueError):
            x["order"] = 0.0
        for k in ("title", "code", "type", "parent", "owner", "audience", "status", "verified", "approved", "brand",
                  "location", "note"):
            x[k] = str(x.get(k) or "").strip()
        reason = ("switched off" if x["status"].casefold() == "çaktivizuar".casefold()
                  else "not verified" if x["verified"].casefold() == "jo"
                  else "no SharePoint address" if not x["url"] else "")
        if reason:
            hidden.append({"title": x["title"], "code": x["code"], "reason": reason})
        else:
            items.append(x)
    items.sort(key=lambda x: (x["order"], x["title"]))
    return {"available": True, "site": site.get("displayName") or "", "site_url": site.get("webUrl") or "",
            "list": lst.get("displayName"), "list_url": lst.get("webUrl") or "", "items": items, "hidden": hidden,
            "read_at": datetime.now().isoformat(timespec="seconds")}


def navigation(force=False):
    """The navigation register of RHG Home, read at most every TTL seconds."""
    if not (config.GRAPH_TENANT_ID and config.GRAPH_CLIENT_ID):
        return {"available": False, "error": "Microsoft 365 is not connected (GRAPH_TENANT_ID, GRAPH_CLIENT_ID)."}
    with _lock:
        if not force and _cache["value"] and _cache["value"].get("available") and time.time() - _cache["at"] < TTL:
            return _cache["value"]
        try:
            value = _read()
        except Exception as exc:                                   # noqa: BLE001
            stale = _cache["value"] if _cache["value"] and _cache["value"].get("available") else None
            if stale:   # SharePoint did not answer: keep showing the last good reading, and say so
                return dict(stale, stale=True, error=f"Last reading kept: {str(exc)[:200]}")
            return {"available": False, "error": str(exc)[:300]}
        _cache.update(at=time.time(), value=value)
        return value
