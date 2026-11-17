"""Layouts of the control towers: the order of their sections and the ones hidden.

A layout belongs to everyone (the layout of the tower for all), to a role (what everyone in the role
sees, over everyone's) or to one person (their own arrangement, which wins over both until they reset
it). Nothing is deleted from a
tower: a hidden section is only left out of view and comes back with Show or a reset.

The page builds the layout; this module only checks its shape and size and stores it.
"""
import json

import auth
import store

APPS = ("finance", "purchasing")
MAX_BYTES = 200_000
MAX_ITEMS = 5_000


def _clean_ids(values):
    out = []
    for v in values or []:
        if isinstance(v, str) and 0 < len(v) <= 200:
            out.append(v)
        if len(out) >= MAX_ITEMS:
            break
    return out


def clean(layout):
    """The stored shape: {v, order: {container: [ids]}, hidden: [ids], nav: {order, hidden}}."""
    if not isinstance(layout, dict):
        raise ValueError("The layout must be an object.")
    order = {}
    for k, ids in (layout.get("order") or {}).items():
        if isinstance(k, str) and 0 < len(k) <= 200 and isinstance(ids, list):
            order[k] = _clean_ids(ids)
        if len(order) >= MAX_ITEMS:
            break
    nav = layout.get("nav") or {}
    out = {"v": 1, "order": order, "hidden": _clean_ids(layout.get("hidden")),
           "nav": {"order": _clean_ids(nav.get("order")), "hidden": _clean_ids(nav.get("hidden"))}}
    if len(json.dumps(out)) > MAX_BYTES:
        raise ValueError("The layout is too large.")
    return out


def get(app, scope, owner):
    r = store.one("SELECT data, updated_at, updated_by FROM layouts WHERE app=? AND scope=? AND owner=?",
                  (app, scope, owner))
    if not r:
        return None
    try:
        data = json.loads(r["data"])
    except ValueError:
        return None
    data["updated_at"], data["updated_by"] = r["updated_at"], r["updated_by"]
    return data


def put(app, scope, owner, layout, actor):
    data = clean(layout)
    store.run("""INSERT INTO layouts(app, scope, owner, data, updated_at, updated_by) VALUES(?,?,?,?,?,?)
                 ON CONFLICT(app, scope, owner) DO UPDATE SET data=excluded.data,
                 updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
              (app, scope, owner, json.dumps(data), store.now(), actor))
    return data


def drop(app, scope, owner):
    store.run("DELETE FROM layouts WHERE app=? AND scope=? AND owner=?", (app, scope, owner))


def rights(user):
    return {"personal": auth.can(user, "layout.personal"), "roles": auth.can(user, "layout.roles.manage")}


EVERYONE = "*"


def view(user, app, role=None):
    """What the page needs: everyone's layout, the role's, the person's own, and what they may change."""
    can = rights(user)
    own_role = user.get("role") or ""
    out = {"app": app, "role": own_role, "can": can,
           "all_layout": get(app, "all", EVERYONE),
           "role_layout": get(app, "role", own_role),
           "personal": get(app, "user", user["username"])}
    if can["roles"]:
        out["roles"] = [r["name"] for r in store.q("SELECT name FROM roles ORDER BY name")]
        # who keeps a layout of their own (it wins over everyone's)
        out["own_roles"] = [r["owner"] for r in store.q("SELECT owner FROM layouts WHERE app=? AND scope='role' ORDER BY owner", (app,))]
        out["own_people"] = store.one("SELECT COUNT(*) AS n FROM layouts WHERE app=? AND scope='user'", (app,))["n"]
        if role:
            out["asked_role"] = role
            out["asked_layout"] = get(app, "role", role)
    return out


def save(user, body, ip=""):
    """POST body {app, scope: "me" | "role" | "all", role?, layout: {...} | null (null = reset), override?}.
    scope "all" with override: the roles' and the people's own layouts of the tower go, so everyone sees it.
    Returns (status, answer)."""
    app = body.get("app")
    if app not in APPS:
        return 400, {"error": "Unknown tower."}
    scope = body.get("scope")
    can = rights(user)
    if scope == "me":
        if not can["personal"]:
            return 403, {"error": "Your role cannot change its own layout."}
        owner, kind = user["username"], "user"
    elif scope == "role":
        if not can["roles"]:
            return 403, {"error": "Your role cannot set the layout of roles."}
        owner, kind = (body.get("role") or "").strip(), "role"
        if not owner or not store.one("SELECT name FROM roles WHERE name=?", (owner,)):
            return 400, {"error": "Unknown role."}
    elif scope == "all":
        if not can["roles"]:
            return 403, {"error": "Your role cannot set the layout of roles."}
        owner, kind = EVERYONE, "all"
    else:
        return 400, {"error": "Say whose layout this is (me, a role or everyone)."}
    layout = body.get("layout")
    if layout is None:
        drop(app, kind, owner)
        store.audit("layout reset", user["username"], f"{app} · {kind} {owner}", {}, ip)
        return 200, {"ok": True, "layout": None}
    try:
        data = put(app, kind, owner, layout, user["username"])
    except ValueError as exc:
        return 400, {"error": str(exc)}
    if kind == "all" and body.get("override"):
        gone = store.q("SELECT scope, owner FROM layouts WHERE app=? AND scope IN ('role','user')", (app,))
        store.run("DELETE FROM layouts WHERE app=? AND scope IN ('role','user')", (app,))
        store.audit("layout for everyone", user["username"], f"{app} · own layouts removed",
                    {"roles": [r["owner"] for r in gone if r["scope"] == "role"],
                     "people": sum(1 for r in gone if r["scope"] == "user")}, ip)
    store.audit("layout saved", user["username"], f"{app} · {kind} {owner}",
                {"hidden": len(data["hidden"]), "nav_hidden": len(data["nav"]["hidden"])}, ip)
    return 200, {"ok": True, "layout": data}
