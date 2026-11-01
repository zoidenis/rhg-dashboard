"""The access scope of the signed-in person: which companies, and which locations of a company, they may see.

Set per account in Admin → Accounts → access (identity), and sent with the session: {"companies": "*" | [names],
"locations": {company: [BC location codes]}} (a company missing from "locations" = all its locations).
The towers refuse a company outside the scope, refuse the company-wide screens to a person limited to some
locations of that company, and cut the location screens' answers to those locations.
Same file in Finance and Purchasing.
"""

ROW_KEYS = ("ref", "bc", "dept", "code", "location", "locRef", "key", "location_code")
COMPANY_WIDE_KEYS = ("totals", "days", "company_totals", "by_day", "group_totals")


def _n(x):
    return str(x or "").strip().upper()


def _scope(user):
    return (user or {}).get("scope") or {}


def companies(user):
    """None = every company, else the set of company names (upper case)."""
    c = _scope(user).get("companies", "*")
    return None if c == "*" or c is None else {_n(x) for x in c}


def company_ok(user, company):
    allowed = companies(user)
    return allowed is None or _n(company) in allowed


def locations(user, company):
    """None = every location of the company, else the set of BC location codes allowed (upper case)."""
    for c, v in (_scope(user).get("locations") or {}).items():
        if _n(c) == _n(company):
            return None if v in ("*", None) else {_n(x) for x in v}
    return None


def limited(user):
    """True when anything in the scope is narrower than everything."""
    sc = _scope(user)
    return companies(user) is not None or any(v not in ("*", None) for v in (sc.get("locations") or {}).values())


def describe(user, company):
    L = locations(user, company)
    return ", ".join(sorted(L)) if L else ""


def refuse_company(company):
    return {"error": f"Your access does not include the company {company}.", "access": "company"}


def refuse_company_wide(user, company):
    return {"error": f"Your access covers {describe(user, company)} of {company}; this screen shows the whole "
                     f"company.", "access": "location", "scope": describe(user, company)}


def filter_companies(names, user):
    allowed = companies(user)
    return list(names or []) if allowed is None else [c for c in names or [] if _n(c) in allowed]


def _keys(k, row):
    keys = {_n(k)} if k is not None else set()
    if isinstance(row, dict):
        keys |= {_n(row.get(f)) for f in ROW_KEYS if row.get(f)}
    elif isinstance(row, str):
        keys.add(_n(row))
    keys.discard("")
    return keys


def filter_payload(body, user, company, expand=None, fields=("locations", "rows", "all_locations"), wide=True):
    """A copy of the answer with only the companies and locations in scope; never changes the held original.
    expand(company, codes) widens the BC location codes to the other keys a tower uses for the same place
    (Finance: the registry's department and Simphony reference). For an answer already read for one location of
    the scope, fields=("all_locations",) and wide=False: only the list of locations to choose from is cut."""
    if not isinstance(body, dict) or not limited(user):
        return body
    comps = companies(user)
    cache = {}

    def allowed(co):
        if co not in cache:
            L = locations(user, co)
            cache[co] = None if L is None else ({_n(x) for x in (expand(co, L) if expand else L)} | L)
        return cache[co]

    def keep(k, row):
        co = row.get("company") if isinstance(row, dict) and row.get("company") else company
        if comps is not None and _n(co) not in comps:
            return False
        A = allowed(co)
        return A is None or bool(_keys(k, row) & A)

    out = dict(body)
    cut = False
    for field in fields:
        val = body.get(field)
        if isinstance(val, dict):
            new = {k: v for k, v in val.items() if keep(k, v)}
        elif isinstance(val, list) and (field != "rows" or (val and isinstance(val[0], dict))):
            new = [r for r in val if keep(None, r)]
        else:
            continue
        cut = cut or len(new) != len(val)
        out[field] = new
    if comps is not None and isinstance(body.get("companies"), list):
        out["companies"] = [c for c in body["companies"]
                            if _n(c.get("company") if isinstance(c, dict) else c) in comps]
    if wide and (cut or allowed(company) is not None):
        for k in COMPANY_WIDE_KEYS:
            out.pop(k, None)
    return out
