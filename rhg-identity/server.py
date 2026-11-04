"""RHG identity service - central sign-in, accounts and permissions.

The control towers do not authenticate anyone themselves any more: they pass the
session cookie here through /internal/validate and are told who the user is and
what they may do. That call is protected by a shared service token, so only the
group's own services can ask.
"""
import hashlib
import json
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import auth
import config
import layouts
import permissions
import reports
import screens
import store
import unicorn

STATIC = config.BASE_DIR / "static"
STATIC_FILES = {
    "login.html": "text/html; charset=utf-8",
    "admin.html": "text/html; charset=utf-8",
    "brand.css": "text/css; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "application/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
    "select.js": "application/javascript; charset=utf-8",
    "select.css": "text/css; charset=utf-8",
    "locintel.js": "application/javascript; charset=utf-8",
    "locintel.css": "text/css; charset=utf-8",
    "tables.js": "application/javascript; charset=utf-8",
    "reports.js": "application/javascript; charset=utf-8",
}
PUBLIC_PATHS = {"/", "/login", "/login.html", "/api/login", "/api/session",
                "/brand.css", "/app.css", "/icon.svg", "/manifest.webmanifest", "/sw.js",
                "/select.js", "/select.css", "/i18n.js"}

# Simple in-process rate limit for sign-in attempts, keyed by client address.
_hits, _hits_lock = {}, threading.Lock()
RATE_MAX, RATE_WINDOW = 20, 300


def rate_limited(ip):
    now = time.time()
    with _hits_lock:
        hits = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
        hits.append(now)
        _hits[ip] = hits
        if len(_hits) > 4000:                      # keep the table bounded
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > RATE_WINDOW]:
                _hits.pop(k, None)
        return len(hits) > RATE_MAX


_i18n = {"key": None, "body": b""}


def i18n_bundle():
    """/i18n.js: the Albanian dictionary (i18n/sq.json, with data/i18n-sq.json on top for corrections made on the
    server) followed by the script that translates the pages (static/i18n-runtime.js); rebuilt when a file changes."""
    files = [config.BASE_DIR / "i18n" / "sq.json", config.BASE_DIR / "data" / "i18n-sq.json",
             STATIC / "i18n-runtime.js"]
    key = tuple(f.stat().st_mtime if f.exists() else 0 for f in files)
    if _i18n["key"] != key:
        words = {}
        for f in files[:2]:
            if f.exists():
                try:
                    words.update(json.loads(f.read_text(encoding="utf-8")))
                except ValueError:
                    pass
        runtime = files[2].read_text(encoding="utf-8") if files[2].exists() else ""
        body = "window.PULSE_SQ=" + json.dumps(words, ensure_ascii=False, separators=(",", ":")) + ";\n" + runtime
        _i18n.update(key=key, body=body.encode("utf-8"))
    return _i18n["body"]


_opts = {"at": 0, "value": None}


def scope_options_cached():
    """scope_options, kept 10 minutes (the report tool reads it each time it opens)."""
    if _opts["value"] and time.time() - _opts["at"] < 600:
        return 200, _opts["value"]
    status, body = scope_options()
    if status == 200:
        _opts.update(at=time.time(), value=body)
    return status, body


def scope_options():
    """(status, body): the companies and their locations, from Finance (/internal/scope-options)."""
    import urllib.error
    import urllib.request
    base = config.get("FINANCE_URL", "http://finance:8766").rstrip("/")
    req = urllib.request.Request(base + "/internal/scope-options", headers={"X-Service-Token": config.SERVICE_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return 200, json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 502, {"error": f"Finance could not list the companies and locations: {str(exc)[:160]}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "RHG-Identity"

    def log_message(self, *_a):
        pass

    # ------------------------------------------------------------ helpers
    def _ip(self):
        return self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For", "").split(",")[0].strip() \
            or self.client_address[0]

    def _cookie(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == config.COOKIE_NAME:
                return v
        return None

    def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        payload = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _user(self):
        return auth.session(self._cookie())

    def _service_ok(self):
        given = self.headers.get("X-Service-Token", "")
        return bool(config.SERVICE_TOKEN) and secrets.compare_digest(given, config.SERVICE_TOKEN)

    def _csrf_ok(self, user):
        return bool(user) and secrets.compare_digest(
            self.headers.get("X-CSRF-Token", ""), user.get("csrf", ""))

    # ------------------------------------------------------------ GET
    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/":
                # Signed in: the control center. Otherwise, or while a new password is
                # still owed, the sign-in page.
                u = self._user()
                page = "control.html" if u and not u.get("must_change") else "login.html"
                self._send(200, (STATIC / page).read_bytes(), "text/html; charset=utf-8")
                return
            if url.path in ("/login", "/login.html"):
                self._send(200, (STATIC / "login.html").read_bytes(), "text/html; charset=utf-8")
                return
            if url.path in ("/executive", "/executive.html"):
                u = self._user()
                if not u or u.get("must_change"):
                    self._send(200, (STATIC / "login.html").read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(200, (STATIC / "executive.html").read_bytes(), "text/html; charset=utf-8")
                return
            if url.path in ("/unicorn", "/unicorn.html"):
                u = self._user()
                page = "unicorn.html" if u and not u.get("must_change") else "login.html"
                self._send(200, (STATIC / page).read_bytes(), "text/html; charset=utf-8")
                return
            if url.path in ("/admin", "/admin.html"):
                self._send(200, (STATIC / "admin.html").read_bytes(), "text/html; charset=utf-8")
                return
            if url.path == "/i18n.js":
                # 240 kB of dictionary: asked again on every page, sent again only when it changed (ETag)
                body = i18n_bundle()
                tag = '"%s"' % hashlib.sha1(body).hexdigest()[:16]
                if self.headers.get("If-None-Match") == tag:
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", tag)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            name = url.path.lstrip("/")
            if name in STATIC_FILES and (STATIC / name).exists():
                self._send(200, (STATIC / name).read_bytes(), STATIC_FILES[name])
                return

            if url.path == "/api/session":
                u = self._user()
                self._send(200, {"user": u, "permissions": permissions.PERMISSIONS})
                return

            user = self._user()
            if not user:
                self._send(401, {"error": "Not signed in."})
                return

            if url.path == "/api/users":
                if not auth.can(user, "admin.users.manage"):
                    self._send(403, {"error": "Your role cannot manage accounts."}); return
                self._send(200, {"users": auth.list_users(),
                                 "roles": [{"name": r["name"], "department": r["department"] or "Other",
                                            "description": r["description"], "system": bool(r["system"])}
                                           for r in store.q("SELECT * FROM roles ORDER BY name")],
                                 "permissions": permissions.PERMISSIONS})
            elif url.path == "/api/screens":
                # The screens of pulse (menus and tabs) for the towers' menus: what the signed-in person may open.
                self._send(200, {"catalogue": screens.catalogue(), "held": [p for p in user["permissions"] if ".s." in p],
                                 "scope": user.get("scope")})
            elif url.path == "/api/admin/scope-options":
                # Companies and their locations to choose an account's access from (read by Finance from BC).
                if not auth.can(user, "admin.users.manage"):
                    self._send(403, {"error": "Your role cannot manage accounts."}); return
                self._send(*scope_options())
            elif url.path == "/api/reports":
                # Scheduled reports: the list, the screens that can be sent, the people who can receive them
                if not auth.can(user, "reports.manage"):
                    self._send(403, {"error": "Your role cannot schedule reports."}); return
                self._send(200, reports.view(user))
            elif url.path == "/api/reports/locations":
                if not auth.can(user, "reports.manage"):
                    self._send(403, {"error": "Your role cannot schedule reports."}); return
                self._send(*scope_options_cached())
            elif url.path == "/api/unicorn/status":
                # For the menus: may this person talk to Unicorn, and how many briefs are unread.
                ok = auth.can(user, "unicorn.use")
                self._send(200, {"allowed": ok, "unread": unicorn.unread(user["username"]) if ok else 0,
                                 "enabled": unicorn.enabled()})
            elif url.path == "/api/unicorn":
                if not auth.can(user, "unicorn.use"):
                    self._send(403, {"error": "Your role cannot talk to Unicorn."}); return
                qs = parse_qs(url.query)
                before = (qs.get("before") or [""])[0]
                rows = unicorn.messages(user["username"], int(before) if before.isdigit() else None)
                unread = unicorn.unread(user["username"])
                if rows and not before:
                    unicorn.mark_seen(user["username"], rows[-1]["id"])
                self._send(200, {"state": unicorn.state(), "messages": rows, "unread": unread,
                                 "more": len(rows) >= 60})
            elif url.path == "/api/layout":
                # The towers' layout (order and hidden sections) for the signed-in person.
                qs = parse_qs(url.query)
                app = (qs.get("app") or [""])[0]
                if app not in layouts.APPS:
                    self._send(400, {"error": "Unknown tower."}); return
                self._send(200, layouts.view(user, app, (qs.get("role") or [""])[0] or None))
            elif url.path == "/api/roles":
                if not auth.can(user, "admin.roles.manage"):
                    self._send(403, {"error": "Your role cannot manage roles."}); return
                users_by = {}
                for u in store.q("SELECT role FROM users"):
                    users_by[u["role"]] = users_by.get(u["role"], 0) + 1
                rows = [{"name": r["name"], "description": r["description"], "department": r["department"] or "Other",
                         "permissions": json.loads(r["permissions"]), "system": bool(r["system"]),
                         "users": users_by.get(r["name"], 0)}
                        for r in store.q("SELECT * FROM roles ORDER BY name")]
                self._send(200, {"roles": rows, "permissions": permissions.PERMISSIONS,
                                 "catalogue": screens.catalogue(), "actions": permissions.ACTIONS,
                                 "departments": permissions.DEPARTMENTS})
            elif url.path == "/api/audit":
                if not auth.can(user, "admin.audit.view"):
                    self._send(403, {"error": "Your role cannot read the audit trail."}); return
                limit = min(int((parse_qs(url.query).get("limit") or ["200"])[0]), 1000)
                rows = [dict(r) for r in store.q(
                    "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]
                self._send(200, {"entries": rows})
            elif url.path == "/api/sessions":
                if not auth.can(user, "admin.sessions.manage"):
                    self._send(403, {"error": "Your role cannot see sessions."}); return
                store.purge_sessions()
                rows = [{"username": r["username"], "created": r["created"],
                         "last_seen": r["last_seen"], "ip": r["ip"],
                         "user_agent": r["user_agent"],
                         "current": r["token"] == self._cookie()}
                        for r in store.q("SELECT * FROM sessions ORDER BY last_seen DESC")]
                self._send(200, {"sessions": rows})
            else:
                self._send(404, {"error": "Not found"})
        except Exception as exc:                                        # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": f"Unexpected error: {exc}"})

    # ------------------------------------------------------------ POST
    def do_POST(self):
        url = urlparse(self.path)
        ip = self._ip()
        try:
            # --- the call the control towers make
            if url.path == "/internal/validate":
                if not self._service_ok():
                    self._send(403, {"error": "Bad service token."}); return
                body = self._body()
                u = auth.session(body.get("token"))
                self._send(200, {"user": u})
                return

            body = self._body()

            # --- the report sender (rhg-reporter): the runs to do now, and how they went
            if url.path in ("/internal/reports/claim", "/internal/reports/done"):
                if not self._service_ok():
                    self._send(403, {"error": "Bad service token."}); return
                if url.path.endswith("/claim"):
                    self._send(200, reports.claim(body.get("reporter") or {}))
                else:
                    self._send(200, {"ok": reports.done(body.get("run"), body.get("results"), body.get("detail"))})
                return

            if url.path == "/api/login":
                if rate_limited(ip):
                    self._send(429, {"error": "Too many failed attempts. Try again in a few minutes."}); return
                token, csrf, err = auth.login(body.get("username"), body.get("password"),
                                              ip, self.headers.get("User-Agent", ""))
                if err:
                    self._send(401, {"error": err}); return
                u = auth.session(token)
                cookie = (f"{config.COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; "
                          f"Max-Age={config.SESSION_HOURS * 3600}"
                          + ("; Secure" if config.COOKIE_SECURE else ""))
                self._send(200, {"user": u, "csrf": csrf}, extra=[("Set-Cookie", cookie)])
                return

            user = self._user()
            if not user:
                self._send(401, {"error": "Not signed in."}); return

            if url.path == "/api/logout":
                auth.logout(self._cookie(), user["username"], ip)
                cookie = f"{config.COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                self._send(200, {"ok": True}, extra=[("Set-Cookie", cookie)])
                return

            # Everything below changes state, so it must carry the CSRF token.
            if not self._csrf_ok(user):
                self._send(403, {"error": "Missing or wrong CSRF token."}); return
            if user.get("report"):                  # a report session only reads
                self._send(403, {"error": "A report session cannot change anything."}); return

            if url.path in ("/api/reports", "/api/reports/delete", "/api/reports/send"):
                if not auth.can(user, "reports.manage"):
                    self._send(403, {"error": "Your role cannot schedule reports."}); return
                try:
                    if url.path == "/api/reports":
                        self._send(200, {"report": reports.save(body, user["username"])})
                    elif url.path.endswith("/delete"):
                        self._send(200, {"ok": reports.delete(str(body.get("id") or ""), user["username"])})
                    else:
                        self._send(200, reports.send_now(str(body.get("id") or ""), user["username"],
                                                         bool(body.get("test"))))
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                return

            if url.path == "/api/me/lang":
                # the language of pulse for the signed-in person (sq, en); every page reads it from the session
                if not auth.set_lang(user["username"], body.get("lang")):
                    self._send(400, {"error": "Unknown language."}); return
                self._send(200, {"ok": True, "lang": body.get("lang")})
                return
            if url.path == "/api/password":
                ok, err = auth.change_password(user["username"], body.get("current"),
                                               body.get("new"), ip)
                self._send(200 if ok else 400, {"ok": True} if ok else {"error": err})
            elif url.path == "/api/users":
                if not auth.can(user, "admin.users.manage"):
                    self._send(403, {"error": "Your role cannot manage accounts."}); return
                created, err = auth.create_user(body.get("username"), body.get("name"),
                                                body.get("email"), body.get("role"),
                                                body.get("password"), user["username"], ip)
                if created and body.get("scope"):
                    created, err = auth.update_user(created["username"], {"scope": body["scope"]},
                                                    user["username"], ip)
                self._send(200 if created else 400, created or {"error": err})
            elif url.path.startswith("/api/users/"):
                if not auth.can(user, "admin.users.manage"):
                    self._send(403, {"error": "Your role cannot manage accounts."}); return
                target = url.path.rsplit("/", 1)[-1]
                if body.get("_delete"):
                    ok, err = auth.delete_user(target, user["username"], ip)
                    self._send(200 if ok else 400, {"ok": True} if ok else {"error": err})
                else:
                    updated, err = auth.update_user(target, body, user["username"], ip)
                    self._send(200 if updated else 400, updated or {"error": err})
            elif url.path == "/api/unicorn/chat":
                if not auth.can(user, "unicorn.use"):
                    self._send(403, {"error": "Your role cannot talk to Unicorn."}); return
                try:
                    self._send(200, {"messages": unicorn.ask(user, body.get("text"))})
                except (ValueError, RuntimeError) as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path == "/api/unicorn/brief":
                if not auth.can(user, "unicorn.use"):
                    self._send(403, {"error": "Your role cannot talk to Unicorn."}); return
                try:
                    self._send(200, unicorn.run_now(user["username"]))
                except RuntimeError as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path == "/api/layout":
                status, answer = layouts.save(user, body, ip)
                self._send(status, answer)
            elif url.path == "/api/roles":
                if not auth.can(user, "admin.roles.manage"):
                    self._send(403, {"error": "Your role cannot manage roles."}); return
                name = (body.get("name") or "").strip()[:60]
                perms = [p for p in (body.get("permissions") or []) if permissions.valid(p)]
                if not name:
                    self._send(400, {"error": "A role name is required."}); return
                existing = store.one("SELECT * FROM roles WHERE name=?", (name,))
                if body.get("_delete"):
                    if not existing or existing["system"]:
                        self._send(400, {"error": "This role cannot be deleted."}); return
                    n = store.one("SELECT COUNT(*) n FROM users WHERE role=?", (name,))["n"]
                    if n:
                        self._send(400, {"error": f"{n} account(s) still have this role; move them first."}); return
                    store.run("DELETE FROM roles WHERE name=?", (name,))
                    store.audit("role deleted", user["username"], name, {}, ip)
                    self._send(200, {"ok": True}); return
                if existing and existing["system"] and set(perms) != set(json.loads(existing["permissions"])):
                    self._send(400, {"error": "The Administrator role cannot be narrowed."}); return
                dep = (body.get("department") or (existing["department"] if existing else "") or "Other")[:40]
                store.run("""INSERT INTO roles(name,description,permissions,system,department) VALUES(?,?,?,0,?)
                             ON CONFLICT(name) DO UPDATE SET description=excluded.description,
                                                             permissions=excluded.permissions,
                                                             department=excluded.department""",
                          (name, (body.get("description") or "")[:200], json.dumps(perms), dep))
                store.audit("role saved", user["username"], name, {"count": len(perms)}, ip)
                self._send(200, {"ok": True})
            elif url.path == "/api/sessions/revoke":
                if not auth.can(user, "admin.sessions.manage"):
                    self._send(403, {"error": "Your role cannot revoke sessions."}); return
                target = body.get("username")
                store.run("DELETE FROM sessions WHERE username=?", (target,))
                store.audit("sessions revoked", user["username"], target, {}, ip)
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "Not found"})
        except Exception as exc:                                        # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": f"Unexpected error: {exc}"})


def main():
    store.connect()
    first = auth.bootstrap()
    if first:
        print("=" * 62)
        print("First run: an administrator account was created.")
        print("   username: admin")
        print(f"   password: {first}")
        print("Sign in and change it when asked. It is shown only now.")
        print("=" * 62)
    if not config.SERVICE_TOKEN:
        print("WARNING: SERVICE_TOKEN is empty - the control towers cannot validate sessions.")

    def sweeper():
        while True:
            time.sleep(300)
            try:
                store.purge_sessions()
            except Exception:                                           # noqa: BLE001
                pass
    threading.Thread(target=sweeper, daemon=True).start()
    unicorn.start()
    if not unicorn.enabled():
        print("Unicorn: no ANTHROPIC_API_KEY in .env - the business analyst stays off until one is set.")

    srv = ThreadingHTTPServer((config.APP_HOST, config.APP_PORT), Handler)
    print(f"RHG identity service on http://{config.APP_HOST}:{config.APP_PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
