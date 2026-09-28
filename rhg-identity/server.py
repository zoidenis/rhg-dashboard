"""RHG identity service - central sign-in, accounts and permissions.

The control towers do not authenticate anyone themselves any more: they pass the
session cookie here through /internal/validate and are told who the user is and
what they may do. That call is protected by a shared service token, so only the
group's own services can ask.
"""
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
import permissions
import store

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
}
PUBLIC_PATHS = {"/", "/login", "/login.html", "/api/login", "/api/session",
                "/brand.css", "/app.css", "/icon.svg", "/manifest.webmanifest", "/sw.js",
                "/select.js", "/select.css"}

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
            if url.path in ("/", "/login", "/login.html"):
                self._send(200, (STATIC / "login.html").read_bytes(), "text/html; charset=utf-8")
                return
            if url.path in ("/admin", "/admin.html"):
                self._send(200, (STATIC / "admin.html").read_bytes(), "text/html; charset=utf-8")
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
                                 "roles": [dict(r) for r in store.q("SELECT * FROM roles ORDER BY name")],
                                 "permissions": permissions.PERMISSIONS})
            elif url.path == "/api/roles":
                if not auth.can(user, "admin.roles.manage"):
                    self._send(403, {"error": "Your role cannot manage roles."}); return
                rows = [{"name": r["name"], "description": r["description"],
                         "permissions": json.loads(r["permissions"]), "system": bool(r["system"])}
                        for r in store.q("SELECT * FROM roles ORDER BY name")]
                self._send(200, {"roles": rows, "permissions": permissions.PERMISSIONS})
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

            if url.path == "/api/login":
                if rate_limited(ip):
                    self._send(429, {"error": "Too many attempts. Wait a few minutes."}); return
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
            elif url.path == "/api/roles":
                if not auth.can(user, "admin.roles.manage"):
                    self._send(403, {"error": "Your role cannot manage roles."}); return
                name = (body.get("name") or "").strip()
                perms = [p for p in (body.get("permissions") or []) if permissions.valid(p)]
                if not name:
                    self._send(400, {"error": "A role name is required."}); return
                existing = store.one("SELECT * FROM roles WHERE name=?", (name,))
                if existing and existing["system"] and set(perms) != set(json.loads(existing["permissions"])):
                    self._send(400, {"error": "The Administrator role cannot be narrowed."}); return
                store.run("""INSERT INTO roles(name,description,permissions,system) VALUES(?,?,?,0)
                             ON CONFLICT(name) DO UPDATE SET description=excluded.description,
                                                             permissions=excluded.permissions""",
                          (name, (body.get("description") or "")[:200], json.dumps(perms)))
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

    srv = ThreadingHTTPServer((config.APP_HOST, config.APP_PORT), Handler)
    print(f"RHG identity service on http://{config.APP_HOST}:{config.APP_PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
