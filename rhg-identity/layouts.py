"""Layouts of the control towers: the order of their sections and the ones hidden.

A layout belongs to a role (what everyone in the role sees) or to one person (their own
arrangement, which wins over the role's until they reset it). Nothing is deleted from a
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


def view(user, app, role=None):
    """What the page needs: the role's layout, the person's own, and what they may change."""
    can = rights(user)
    own_role = user.get("role") or ""
    out = {"app": app, "role": own_role, "can": can,
           "role_layout": get(app, "role", own_role),
           "personal": get(app, "user", user["username"])}
    if can["roles"]:
        out["roles"] = [r["name"] for r in store.q("SELECT name FROM roles ORDER BY name")]
        if role:
            out["asked_role"] = role
            out["asked_layout"] = get(app, "role", role)
    return out


def save(user, body, ip=""):
    """POST body {app, scope: "me" | "role", role?, layout: {...} | null (null = reset)}.
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
    else:
        return 400, {"error": "Say whose layout this is (me or a role)."}
    layout = body.get("layout")
    if layout is None:
        drop(app, kind, owner)
        store.audit("layout reset", user["username"], f"{app} · {kind} {owner}", {}, ip)
        return 200, {"ok": True, "layout": None}
    try:
        data = put(app, kind, owner, layout, user["username"])
    except ValueError as exc:
        return 400, {"error": str(exc)}
    store.audit("layout saved", user["username"], f"{app} · {kind} {owner}",
                {"hidden": len(data["hidden"]), "nav_hidden": len(data["nav"]["hidden"])}, ip)
    return 200, {"ok": True, "layout": data}
