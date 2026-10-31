"""Who receives each report, company by company: the directory kept in Purchasing
(Settings → Email recipients by company), read here through Purchasing's /internal/recipients.

For every company, the people in each role (finance, chief accountant, cost controller, operations ...),
plus a row for the whole group added to every company; each report goes to the roles set for it. When a
company has nobody in the roles of a report, the report keeps the addresses of its own Settings card.
If Purchasing cannot be reached, the last directory read is used, else the general addresses.
People set for a location get, for the reports cut by location, an email of their location only.
"""
import re
import threading
import time

import requests

import auth
import config

GROUP = "*"
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_cache = {"at": 0.0, "value": None, "error": ""}
_lock = threading.Lock()


def parse(text):
    good, bad = [], []
    for part in re.split(r"[,;\s]+", text or ""):
        part = part.strip()
        if part:
            (good if EMAIL_RE.match(part) else bad).append(part)
    return good, bad


def directory(max_age=60):
    """The directory from Purchasing, cached a minute; None when it was never reached."""
    with _lock:
        if _cache["value"] is not None and time.time() - _cache["at"] < max_age:
            return _cache["value"]
    try:
        r = requests.get(f"{config.PURCHASING_URL}/internal/recipients", timeout=10,
                         headers={"X-Service-Token": auth.SERVICE_TOKEN})
        if not r.ok:
            raise RuntimeError(f"Purchasing answered {r.status_code}.")
        value = r.json()
        with _lock:
            _cache.update(at=time.time(), value=value, error="")
        return value
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        with _lock:
            _cache["error"] = str(exc)[:200]
            return _cache["value"]


def save(data, actor):
    """Saves the directory in Purchasing for a Finance user; returns the new directory."""
    r = requests.post(f"{config.PURCHASING_URL}/internal/recipients", timeout=20,
                      headers={"X-Service-Token": auth.SERVICE_TOKEN, "Content-Type": "application/json"},
                      json={"data": data, "actor": actor})
    try:
        j = r.json()
    except ValueError:
        j = {}
    if not r.ok:
        raise ValueError(j.get("error") or f"Purchasing answered {r.status_code}.")
    with _lock:
        _cache.update(at=time.time(), value=j, error="")
    return j


def last_error():
    return _cache["error"]


def resolve_in(d, report, companies, general_to=(), general_cc=(), promote_cc=True):
    """{"to", "cc", "source"} of one report for the companies it covers (one, or the companies of a brand).
    The same function sits in Finance's recipients.py; keep the two alike."""
    people = {str(k).strip().upper(): v for k, v in (d.get("people") or {}).items()}
    route = (d.get("routing") or {}).get(report) or {"to": [], "cc": []}

    def emails(roles):
        out = []
        for c in [*companies, GROUP]:
            row = people.get(str(c or "").strip().upper()) or {}
            for role in roles:
                out += parse(row.get(role, ""))[0]
        return list(dict.fromkeys(out))
    to, cc = emails(route.get("to") or []), emails(route.get("cc") or [])
    general_to, general_cc = list(general_to or []), list(general_cc or [])
    if not to and not cc:
        return {"to": general_to, "cc": [x for x in general_cc if x not in general_to], "source": "general"}
    if not to:
        to = general_to or (cc[:1] if promote_cc else [])
    return {"to": to, "cc": [x for x in cc if x not in to], "source": "company"}


def resolve(report, companies, general_to=(), general_cc=(), promote_cc=True):
    d = directory()
    if not d:
        general_to, general_cc = list(general_to or []), list(general_cc or [])
        return {"to": general_to, "cc": [x for x in general_cc if x not in general_to], "source": "general"}
    return resolve_in(d, report, companies, general_to, general_cc, promote_cc)


def location_targets_in(d, report, company):
    """[(location, to, cc)]: the locations of a company that have people in the roles of a report, for
    the reports cut by location. Nobody in To: the first Cc moves up. Same function in Finance."""
    if report not in {"purchasing_daily", "purchasing_weekly", "savings_monthly", "cost_weekly"}:
        return []
    route = (d.get("routing") or {}).get(report) or {"to": [], "cc": []}
    locs = next((v for k, v in (d.get("locations") or {}).items() if str(k).strip().upper() == str(company or "").strip().upper()), {})
    out = []
    for loc, row in sorted(locs.items()):
        to = list(dict.fromkeys(e for r in route.get("to") or [] for e in parse(row.get(r, ""))[0]))
        cc = list(dict.fromkeys(e for r in route.get("cc") or [] for e in parse(row.get(r, ""))[0]))
        if not to:
            to, cc = cc[:1], cc[1:]
        if to:
            out.append((loc, to, [x for x in cc if x not in to]))
    return out


def location_targets(report, company):
    d = directory()
    return location_targets_in(d, report, company) if d else []
