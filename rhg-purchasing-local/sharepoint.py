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
from datetime import date, datetime, timedelta
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


def _cached(cache, read, force):
    """read() at most every TTL seconds; when SharePoint does not answer, the last good reading, marked stale."""
    if not (config.GRAPH_TENANT_ID and config.GRAPH_CLIENT_ID):
        return {"available": False, "error": "Microsoft 365 is not connected (GRAPH_TENANT_ID, GRAPH_CLIENT_ID)."}
    with _lock:
        if not force and cache["value"] and cache["value"].get("available") and time.time() - cache["at"] < TTL:
            return cache["value"]
        try:
            value = read()
        except Exception as exc:                                   # noqa: BLE001
            stale = cache["value"] if cache["value"] and cache["value"].get("available") else None
            if stale:
                return dict(stale, stale=True, error=f"Last reading kept: {str(exc)[:200]}")
            return {"available": False, "error": str(exc)[:300]}
        cache.update(at=time.time(), value=value)
        return value


def navigation(force=False):
    """The navigation register of RHG Home, read at most every TTL seconds."""
    return _cached(_cache, _read, force)


# ---------------------------------------------------------------- contracts and policies (RHG-KONTRATA, RHG-Politika)
# What is read is the description of each file (its columns), never its content: the file itself opens in SharePoint,
# which checks the person's own access. The libraries are named "Kontrata - <entity> - Në punë | Të nënshkruara",
# "Politika dhe SOP - <brand> - Në punë | Të publikuara" and "Arkiv publikimi - Politika dhe SOP - <brand>".
_docs_cache = {"at": 0.0, "value": None}

CONTRACT_COLUMNS = {
    "Contract Title": "title", "Contract ID": "contract_id", "Contract Type": "type", "Contract Status": "status",
    "Kundërpala / Furnitori": "counterparty", "Brand / Location Scope": "scope", "Starting Date": "start",
    "Ending Date": "end", "Signature Date": "signed", "Notice Deadline": "notice_deadline",
    "Notice Period": "notice_period", "Notice Period Unit": "notice_unit", "Automatic Renewal": "auto_renewal",
    "Open-ended Contract": "open_ended", "Contract Owner": "owner", "Kategoria DMS": "category",
    "Approval Reference": "approval_ref", "Amendment / Parent Contract ID": "parent_id"}
POLICY_COLUMNS = {
    "Kodi i dokumentit": "code", "Kategoria e dokumentit": "category", "Kategoria DMS": "category",
    "Statusi i dokumentit": "status", "Brandi": "brand_codes", "Brand": "brand_codes", "Njësia": "units",
    "Lokacioni": "units", "Rruga e aprovimit": "approval_route", "Përgjegjësi i dokumentit": "owner",
    "Departamenti pronar": "department", "Data e hyrjes në fuqi": "effective", "Data e rishikimit": "review",
    "Data e skadimit": "expiry", "Konfidencialiteti": "confidentiality", "Entiteti ligjor": "entity",
    "Referenca e aprovimit": "approval_ref", "Data e dokumentit": "date", "Fusha e zbatimit": "scope_level"}
DATES = {"start", "end", "signed", "notice_deadline", "effective", "review", "expiry", "date"}
STAGES = ("Në punë", "Të nënshkruara", "Të publikuara")
MODERATION = {0: "Approved", 1: "Rejected", 2: "Pending", 3: "Draft", 4: "Scheduled"}
CONTRACT_NEEDS = {"contract_id": "Contract ID", "counterparty": "Counterparty", "type": "Type", "scope": "Brand / location",
                  "start": "Start date", "end": "End date", "notice_deadline": "Notice deadline", "owner": "Owner"}
POLICY_NEEDS = {"code": "Document code", "category": "Category", "status": "Status", "owner": "Owner",
                "department": "Owning department", "review": "Review date"}


def _library(name):
    """(entity or brand, stage) from a library's name."""
    parts = [p.strip() for p in name.split(" - ")]
    if name.startswith("Arkiv publikimi"):
        return parts[-1], "Arkiv"
    if len(parts) >= 3 and parts[-1] in STAGES:
        return " - ".join(parts[1:-1]), parts[-1]
    return "", name


def _day(value):
    """A date-only column is the site's local midnight stored in UTC (22:00 or 23:00 the day before in Tirana):
    half a day later is always the right calendar day."""
    if not value:
        return ""
    try:
        return (datetime.fromisoformat(str(value).replace("Z", "+00:00")) + timedelta(hours=12)).date().isoformat()
    except ValueError:
        return ""


def _people(site_id):
    """{lookup id: name} from the site's user information list, for the person columns (owner)."""
    g = graph_mail.GRAPH
    try:
        lists = _all(f"{g}/sites/{site_id}/lists?$select=id,displayName,system")
        uil = [x for x in lists if x.get("displayName") == "User Information List"]
        if not uil:
            return {}
        rows = _all(f"{g}/sites/{site_id}/lists/{uil[0]['id']}/items?expand=fields($select=Title,EMail)&$top=500")
        return {str(r.get("id")): (r.get("fields") or {}).get("Title") or "" for r in rows}
    except Exception:                                              # noqa: BLE001 - names are a convenience
        return {}


def _value(fields, name, kind, people):
    if kind == "personOrGroup":
        ids = fields.get(name + "LookupId")
        if ids in (None, "", []):
            return ""
        ids = ids if isinstance(ids, list) else [ids]
        return ", ".join(people.get(str(i), "set") for i in ids)
    v = fields.get(name)
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    return v


def _read_site(path, columns):
    """Every document library of one site: [(library, [file records])] with the columns named in `columns`."""
    g = graph_mail.GRAPH
    site = _get(f"{g}/sites/{path}?$select=id,displayName,webUrl")
    people = _people(site["id"])
    out = []
    for lst in _all(f"{g}/sites/{site['id']}/lists?$select=id,displayName,webUrl,list"):
        meta = lst.get("list") or {}
        if meta.get("template") != "documentLibrary" or meta.get("hidden"):
            continue
        base = f"{g}/sites/{site['id']}/lists/{quote(lst['id'], safe='')}"
        cols = {}
        for c in _all(base + "/columns"):
            key = columns.get(c.get("displayName"))
            if key and key not in cols.values():
                kind = next((k for k in ("personOrGroup", "dateTime", "boolean", "number", "choice", "text") if k in c), "")
                cols[c["name"]] = (key, kind)
        files = []
        for row in _all(base + "/items?expand=fields&$top=500"):
            f = row.get("fields") or {}
            if str(f.get("ContentType", "")).startswith("Folder") or "FileLeafRef" not in f:
                continue
            x = {key: _value(f, name, kind, people) for name, (key, kind) in cols.items()}
            for k in list(x):
                if k in DATES:
                    x[k] = _day(x[k])
                elif x[k] is None:
                    x[k] = ""
            mod = f.get("_ModerationStatus")
            x.update(name=f.get("FileLeafRef") or "", url=_safe_url(row.get("webUrl")),
                     version=str(f.get("_UIVersionString") or ""),
                     approval=MODERATION.get(mod, "") if isinstance(mod, int) else "",
                     modified=str(row.get("lastModifiedDateTime") or "")[:10],
                     modified_by=((row.get("lastModifiedBy") or {}).get("user") or {}).get("displayName", ""))
            files.append(x)
        out.append(({"site": site.get("displayName") or "", "library": lst.get("displayName") or "",
                     "url": _safe_url(lst.get("webUrl")), "files": len(files)}, files))
    return site, out


def _blank(v):
    return v in (None, "", [], {})


def _contract_checks(x, stage, today):
    status = str(x.get("status") or "")
    open_ended = x.get("open_ended") is True
    need = dict(CONTRACT_NEEDS)
    if open_ended:
        need.pop("end", None)
        need.pop("notice_deadline", None)
    if stage == "Të nënshkruara":
        need["signed"] = "Signature date"
    x["missing"] = [label for key, label in need.items() if _blank(x.get(key))]
    flags = []
    if stage == "Të nënshkruara" and status in ("", "Draft", "Në aprovim"):
        flags.append(("warning", f"In a signed library but its status is {status or 'empty'}"))
    if stage == "Në punë" and status in ("Nënshkruar", "Aktiv"):
        flags.append(("warning", f"Status {status} but still in Në punë"))
    live = status not in ("Skaduar", "Ndërprerë", "Pezulluar")
    x["days_to_notice"] = x["days_to_end"] = None
    if x.get("notice_deadline"):
        d = (date.fromisoformat(x["notice_deadline"]) - today).days
        x["days_to_notice"] = d
        ended = bool(x.get("end")) and x["end"] < today.isoformat()   # then the end date speaks, not the notice
        if live and d < 0 and not ended:
            flags.append(("critical", f"Notice deadline passed {-d} days ago"))
        elif live and 0 <= d <= 30:
            flags.append(("critical", f"Notice deadline in {d} days"))
        elif live and 30 < d <= 60:
            flags.append(("warning", f"Notice deadline in {d} days"))
    if x.get("end"):
        d = (date.fromisoformat(x["end"]) - today).days
        x["days_to_end"] = d
        if d < 0 and status in ("Aktiv", "Nënshkruar"):
            flags.append(("critical", f"Ended {-d} days ago but still {status}"))
        elif live and 0 <= d <= 60:
            flags.append(("warning", f"Ends in {d} days"))
    x["flags"] = [{"level": lv, "text": t} for lv, t in flags]
    return x


def _policy_checks(x, stage, today):
    x["missing"] = [label for key, label in POLICY_NEEDS.items() if _blank(x.get(key))]
    flags = []
    if x.get("version", "").startswith("0."):
        flags.append(("info", f"Draft version {x['version']}"))
    if stage == "Të publikuara" and x.get("approval") not in ("", "Approved"):
        flags.append(("warning", f"In a published library, approval {x['approval']}"))
    if x.get("review") and x["review"] < today.isoformat():
        flags.append(("critical", f"Review date passed ({x['review']})"))
    if x.get("expiry") and x["expiry"] < today.isoformat():
        flags.append(("critical", f"Expired ({x['expiry']})"))
    x["flags"] = [{"level": lv, "text": t} for lv, t in flags]
    return x


def _read_documents():
    today = date.today()
    out = {"available": True, "today": today.isoformat(), "libraries": [], "contracts": [], "policies": []}
    for kind, path, columns in (("contracts", config.SP_CONTRACTS_SITE, CONTRACT_COLUMNS),
                                ("policies", config.SP_POLICIES_SITE, POLICY_COLUMNS)):
        site, libs = _read_site(path, columns)
        out[kind + "_site"] = {"name": site.get("displayName") or "", "url": _safe_url(site.get("webUrl"))}
        for lib, files in libs:
            owner, stage = _library(lib["library"])
            out["libraries"].append(dict(lib, kind=kind, owner=owner, stage=stage))
            for x in files:
                x.update(library=lib["library"], stage=stage,
                         **({"entity": owner} if kind == "contracts" else {"brand": owner}))
                if kind == "contracts":
                    x["title"] = x.get("title") or x["name"]
                    out[kind].append(_contract_checks(x, stage, today))
                else:
                    out[kind].append(_policy_checks(x, stage, today))
    c, p = out["contracts"], out["policies"]
    out["summary"] = {
        "contracts": len(c), "contracts_complete": sum(1 for x in c if not x["missing"]),
        "contracts_with_end": sum(1 for x in c if x.get("end") or x.get("open_ended") is True),
        "contracts_with_counterparty": sum(1 for x in c if not _blank(x.get("counterparty"))),
        "contracts_flagged": sum(1 for x in c if x["flags"]),
        "notices_60": sum(1 for x in c if x["days_to_notice"] is not None and 0 <= x["days_to_notice"] <= 60),
        "policies": len(p), "policies_approved": sum(1 for x in p if x.get("approval") == "Approved"),
        "policies_draft": sum(1 for x in p if x.get("version", "").startswith("0.") or x.get("status") == "Draft"),
        "policies_overdue": sum(1 for x in p if x.get("review") and x["review"] < today.isoformat()),
        "policies_complete": sum(1 for x in p if not x["missing"]),
    }
    out["read_at"] = datetime.now().isoformat(timespec="seconds")
    return out


def documents(force=False):
    """Contracts (RHG-KONTRATA) and policies and SOPs (RHG-Politika): each file's columns, what is missing, and
    what needs attention; read at most every TTL seconds."""
    return _cached(_docs_cache, _read_documents, force)
