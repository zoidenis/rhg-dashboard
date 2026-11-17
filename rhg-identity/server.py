"""RHG identity service - central sign-in, accounts and permissions.

The control towers do not authenticate anyone themselves any more: they pass the
session cookie here through /internal/validate and are told who the user is and
what they may do. That call is protected by a shared service token, so only the
group's own services can ask.
"""
import gzip
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
import reportcenter
import screens
import store
import actioncenter
import unicorn
import unicorn_registers as ureg

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
    "pulse.js": "application/javascript; charset=utf-8",
    "reports.js": "application/javascript; charset=utf-8",
    "gear.js": "application/javascript; charset=utf-8",
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


_opts = {"at": 0, "value": None, "reading": False}
_opts_lock = threading.Lock()


def _read_options():
    try:
        status, body = scope_options()
        if status == 200:
            with _opts_lock:
                _opts.update(at=time.time(), value=body)
        return status, body
    finally:
        _opts["reading"] = False


def scope_options_cached():
    """scope_options for the report tool: the last list read is answered at once (and read again in the background
    after 10 minutes); only the very first read waits for Finance."""
    with _opts_lock:
        value, age = _opts["value"], time.time() - _opts["at"]
        stale = value is not None and age > 600 and not _opts["reading"]
        if stale:
            _opts["reading"] = True
    if value is not None:
        if stale:
            threading.Thread(target=_read_options, daemon=True, name="scope-options").start()
        return 200, value
    return _read_options()


def scope_options():
    """(status, body): the companies and their locations, from Finance (/internal/scope-options)."""
    import urllib.error
    import urllib.request
    base = config.get("FINANCE_URL", "http://finance:8766").rstrip("/")
    req = urllib.request.Request(base + "/internal/scope-options", headers={"X-Service-Token": config.SERVICE_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=170) as r:
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
        zipped = (len(payload) > 16 * 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "")
                  and ctype.startswith(("application/json", "text/html", "application/javascript", "text/javascript", "text/css", "image/svg+xml")))
        if zipped:                                       # the pages, the i18n bundle and large answers travel compressed
            payload = gzip.compress(payload, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if zipped:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
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
            if url.path in ("/actions", "/actions.html"):
                u = self._user()
                page = "actions.html" if u and not u.get("must_change") else "login.html"
                self._send(200, (STATIC / page).read_bytes(), "text/html; charset=utf-8")
                return
            if url.path == "/internal/actioncenter/policy":
                # the towers' exceptions registers: the SLA matrix and whether the combined email replaces theirs
                if not self._service_ok():
                    self._send(403, {"error": "Bad service token."}); return
                self._send(200, actioncenter.policy())
                return
            if url.path in ("/settings", "/settings.html"):
                # Settings: every setting of pulse in one place (the cards open in frames from where they live)
                u = self._user()
                page = "settings.html" if u and not u.get("must_change") else "login.html"
                self._send(200, (STATIC / page).read_bytes(), "text/html; charset=utf-8")
                return
            if url.path in ("/reports-center", "/reports-center.html"):
                u = self._user()
                page = "reports-center.html" if u and not u.get("must_change") else "login.html"
                self._send(200, (STATIC / page).read_bytes(), "text/html; charset=utf-8")
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
            elif url.path == "/api/reports-center":
                # Reports center: every report pulse sends (both towers' own emails, the scheduled screens, the
                # Action Center's email, Unicorn's brief), on or paused, failing or late, next and last sends
                if not reportcenter.can_view(user):
                    self._send(403, {"error": "Your role cannot see the reports."}); return
                self._send(200, reportcenter.collect(user))
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
            elif url.path == "/api/actions":
                # Executive Action Center: every item of the registers and of Unicorn, its owner, deadline, progress
                tabs = [t for t in ("executive", "departments", "mine", "unicorn", "matrix") if auth.can(user, f"platform.s.actions.{t}")]
                if not tabs:
                    self._send(403, {"error": "Your role does not include the Executive Action Center."}); return
                d = actioncenter.collect(user.get("scope"))
                me = (user.get("email") or "").strip().lower()
                items = d["items"]
                if not ({"executive", "departments"} & set(tabs)):
                    items = [x for x in items if me and me in x["owners"]]
                self._send(200, {"items": actioncenter.sort_items(items), "stats": actioncenter.stats(items, d["today"]),
                                 "sources": d["sources"], "errors": d["errors"], "runs": d["runs"], "tabs": tabs,
                                 "settings": d["settings"], "departments": actioncenter.DEPARTMENTS,
                                 "severities": actioncenter.SEVERITIES, "me": me, "today": d["today"],
                                 "can_manage": auth.can(user, "actioncenter.manage"),
                                 "accounts": ([{"email": e, "name": u["name"], "role": u["role"], "scope": u["scope"]} for e, u in sorted(actioncenter.users().items(), key=lambda kv: kv[1]["name"])]
                                              if any(auth.can(user, p) for p in ("actioncenter.manage", "finance.actions.manage",
                                                                                   "purchasing.actions.manage", "unicorn.decisions.approve")) else []),
                                 "can_tower": {"finance": auth.can(user, "finance.actions.manage"),
                                               "purchasing": auth.can(user, "purchasing.actions.manage"),
                                               "unicorn": auth.can(user, "unicorn.decisions.approve")}})
            elif url.path == "/api/actions/matrix":
                if not (auth.can(user, "platform.s.actions.matrix") or auth.can(user, "actioncenter.manage")):
                    self._send(403, {"error": "Your role does not include the responsibility matrix."}); return
                m = actioncenter.matrix()
                m.update(sla=actioncenter.settings()["sla"], escalation=actioncenter.settings()["escalation"],
                         departments=actioncenter.DEPARTMENTS, severities=actioncenter.SEVERITIES,
                         can_manage=auth.can(user, "actioncenter.manage"))
                self._send(200, m)
            elif url.path == "/api/actions/unicorn":
                if not auth.can(user, "platform.s.actions.unicorn"):
                    self._send(403, {"error": "Your role does not include Unicorn's tasks."}); return
                st = unicorn.state()
                self._send(200, {"state": st, "counts": ureg.counts(),
                                 "new_findings": [f for f in ureg.findings("New", limit=50)],
                                 "investigating": sorted(unicorn._investigating),
                                 "proposals": ureg.decisions("Proposed")[:20],
                                 "measured": [a for a in ureg.actions("open") if a["latest_at"]][:30],
                                 "runs": [dict(r) for r in store.q("SELECT * FROM unicorn_runs ORDER BY day DESC LIMIT 14")]})
            elif url.path == "/api/unicorn/registers":
                # Unicorn 2.0: findings, decisions, actions and the rules library (pulse's own registers)
                if not auth.can(user, "unicorn.use"):
                    self._send(403, {"error": "Your role cannot talk to Unicorn."}); return
                qs = parse_qs(url.query)
                fst = (qs.get("status") or ["open"])[0]
                self._send(200, {"counts": ureg.counts(),
                                 "findings": ureg.findings(None if fst == "all" else fst, (qs.get("capability") or [None])[0] or None,
                                                           ((qs.get("q") or [""])[0] or "").strip()[:80] or None),
                                 "decisions": ureg.decisions(), "actions": ureg.actions(),
                                 "rules": ureg.rules(), "capabilities": ureg.CAPABILITIES,
                                 "can_approve": auth.can(user, "unicorn.decisions.approve"),
                                 # the pulse accounts an approved decision can be given to (Administration → Accounts)
                                 "accounts": ([{"email": e, "name": u["name"], "role": u["role"], "scope": u["scope"]}
                                               for e, u in sorted(actioncenter.users().items(), key=lambda kv: kv[1]["name"].lower())]
                                              if auth.can(user, "unicorn.decisions.approve") else []),
                                 "can_rules": auth.can(user, "unicorn.rules.manage"),
                                 "investigating": sorted(unicorn._investigating), "enabled": unicorn.enabled()})
            elif url.path == "/api/unicorn/finding":
                if not auth.can(user, "unicorn.use"):
                    self._send(403, {"error": "Your role cannot talk to Unicorn."}); return
                fid = (parse_qs(url.query).get("id") or ["0"])[0]
                f = ureg.finding(int(fid)) if fid.isdigit() else None
                if not f:
                    self._send(404, {"error": "No such finding."}); return
                d = ureg.decision(f["decision_id"]) if f["decision_id"] else None
                self._send(200, {"finding": f, "decision": d, "trail": ureg.tool_calls(f"finding:{f['id']}"),
                                 "history": ureg.similar_history(f),
                                 "actions": [a for a in ureg.actions() if d and a["decision_id"] == d["id"]]})
            elif url.path == "/api/unicorn/rules":
                if not auth.can(user, "unicorn.use"):
                    self._send(403, {"error": "Your role cannot talk to Unicorn."}); return
                self._send(200, {"versions": ureg.rules(with_history=True)})
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
                         "users": users_by.get(r["name"], 0), "scope": auth.role_scope(r["name"])}
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

            if url.path in ("/api/reports-center/toggle", "/api/reports-center/send"):
                if not reportcenter.can_view(user):
                    self._send(403, {"error": "Your role cannot see the reports."}); return
                try:
                    if url.path.endswith("/toggle"):
                        reportcenter.toggle(user, str(body.get("id") or ""), bool(body.get("enabled")))
                        self._send(200, {"ok": True})
                    else:
                        self._send(200, reportcenter.send(user, str(body.get("id") or ""), bool(body.get("test"))))
                except PermissionError as exc:
                    self._send(403, {"error": str(exc)})
                except (ValueError, RuntimeError) as exc:
                    self._send(400, {"error": str(exc)})
                except Exception as exc:                            # noqa: BLE001
                    self._send(502, {"error": "The tower could not be reached: " + str(exc)[:160]})
                return

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
            elif url.path in ("/api/actions/export", "/api/actions/send"):
                # one item out of the Action Center: an Excel or a PDF to download, or sent by email with it attached
                if not any(auth.can(user, f"platform.s.actions.{t}") for t in ("executive", "departments", "mine")):
                    self._send(403, {"error": "Your role does not include the Executive Action Center."}); return
                fmt = "pdf" if body.get("format") == "pdf" else "xlsx"
                try:
                    if url.path.endswith("/export"):
                        fname, ctype, data = actioncenter.export_item(str(body.get("uid") or ""), fmt, user)
                        self._send(200, data, ctype or "application/octet-stream",
                                   [("Content-Disposition", f'attachment; filename="{fname}"')])
                    else:
                        self._send(200, {"sent": actioncenter.send_item(str(body.get("uid") or ""), body.get("to"), fmt,
                                                                       body.get("note"), bool(body.get("copy_me")), user)})
                except (ValueError, RuntimeError) as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path == "/api/actions/item":
                if not any(auth.can(user, f"platform.s.actions.{t}") for t in ("executive", "departments", "mine")):
                    self._send(403, {"error": "Your role does not include the Executive Action Center."}); return
                try:
                    x = actioncenter.update(str(body.get("uid") or ""), {k: body.get(k) for k in ("status", "comment", "owner", "due")},
                                            user, auth.can)
                    self._send(200, {"item": x})
                except PermissionError as exc:
                    self._send(403, {"error": str(exc)})
                except (ValueError, RuntimeError) as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path == "/api/actions/assign":
                if not any(auth.can(user, f"platform.s.actions.{t}") for t in ("executive", "departments")):
                    self._send(403, {"error": "Your role does not include the Executive Action Center."}); return
                try:
                    uids = body.get("uids") if isinstance(body.get("uids"), list) else []
                    self._send(200, actioncenter.assign(uids, str(body.get("owner") or ""), str(body.get("due") or ""),
                                                        str(body.get("comment") or ""), user, auth.can,
                                                        notify=bool(body.get("notify"))))
                except PermissionError as exc:
                    self._send(403, {"error": str(exc)})
                except (ValueError, RuntimeError) as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path in ("/api/actions/settings", "/api/actions/apply-sla", "/api/actions/matrix", "/api/actions/email-test",
                              "/api/actions/owner-rule", "/api/actions/role-department"):
                if not auth.can(user, "actioncenter.manage"):
                    self._send(403, {"error": "Your role cannot change the Action Center's matrices or emails."}); return
                try:
                    if url.path.endswith("/settings"):
                        self._send(200, {"settings": actioncenter.save_settings(body, user["username"])})
                    elif url.path.endswith("/apply-sla"):
                        self._send(200, {"changed": actioncenter.apply_sla(user.get("email") or user["username"])})
                    elif url.path.endswith("/role-department"):
                        self._send(200, {"departments": actioncenter.save_role_department(
                            str(body.get("role") or ""), str(body.get("department") or ""), user["username"])})
                    elif url.path.endswith("/owner-rule"):
                        self._send(200, {"owner_rules": actioncenter.save_owner_rule(
                            str(body.get("tower") or ""), str(body.get("source") or ""), str(body.get("kind") or "*"),
                            str(body.get("owner") or ""), user["username"])})
                    elif url.path.endswith("/matrix"):
                        emails = body.get("emails") if isinstance(body.get("emails"), list) else []
                        self._send(200, actioncenter.save_people(str(body.get("company") or "*")[:80], str(body.get("location") or "")[:40],
                                                                 str(body.get("role") or ""), emails, user["username"]))
                    else:
                        if not user.get("email"):
                            raise ValueError("Your account has no email to send the test to.")
                        self._send(200, {"sent": actioncenter.send_digests(user["username"], [user["email"]])})
                except (ValueError, RuntimeError) as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path in ("/api/unicorn/finding/export", "/api/unicorn/finding/send"):
                # a finding out of Unicorn: Excel or PDF to download, or sent by email with it attached (Purchasing builds and sends)
                if not auth.can(user, "unicorn.use"):
                    self._send(403, {"error": "Your role cannot talk to Unicorn."}); return
                fmt = "pdf" if body.get("format") == "pdf" else "xlsx"
                try:
                    f, table = ureg.finding_table(int(body.get("id") or 0), user)
                    if url.path.endswith("/export"):
                        fname, ctype, data = actioncenter.build_file(table, fmt, user, "unicorn finding")
                        self._send(200, data, ctype or "application/octet-stream", [("Content-Disposition", f'attachment; filename="{fname}"')])
                    else:
                        kind, when, _of, vs = ureg.period_of(f)
                        rows = [("Place", f"{f['location_name'] or f['location']} · {f['company']}"), ("Period", f"{when} ({kind.lower()})"),
                                ("Compared with", vs), ("Measured", f"{ureg._v(f['value'], f['unit'])} against the limit {ureg._v(f['limit'], f['unit'])}"),
                                ("Estimated impact", f"{ureg._v(f['impact'])} {f['currency']}".strip()), ("Status", f["status"])]
                        self._send(200, {"sent": actioncenter.send_file(table, fmt, body.get("to"), body.get("note"), bool(body.get("copy_me")), user,
                                                                        source="Unicorn", title=f"F{f['id']} · {f['title']}", detail=f["detail"],
                                                                        rows=rows, where=f"Unicorn → Findings → F{f['id']}", what="unicorn finding")})
                except (ValueError, RuntimeError) as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path == "/api/unicorn/finding":
                if not auth.can(user, "unicorn.use"):
                    self._send(403, {"error": "Your role cannot talk to Unicorn."}); return
                try:
                    act, fid = body.get("action"), int(body.get("id") or 0)
                    if act == "investigate":
                        self._send(200, unicorn.investigate_now(fid, user["username"])); return
                    if act == "delegate":
                        # a person hands the finding to an owner at once: a decision of theirs and its action
                        if not auth.can(user, "unicorn.decisions.approve"):
                            self._send(403, {"error": "Your role cannot delegate findings."}); return
                        did, ids = ureg.delegate(fid, user["username"], str(body.get("owner") or ""), body.get("due"),
                                                 str(body.get("task") or ""), body.get("target"), str(body.get("note") or ""))
                        mailed = None
                        if body.get("notify"):
                            mailed = actioncenter.notify_action(ids[0], user)
                        self._send(200, {"decision": ureg.decision(did), "actions": ids, "emailed": mailed}); return
                    if act not in ("dismiss", "reopen", "close"):
                        raise ValueError("Action: investigate, dismiss, reopen or close.")
                    if act != "reopen" and not auth.can(user, "unicorn.decisions.approve"):
                        self._send(403, {"error": "Your role cannot close or dismiss findings."}); return
                    ureg.set_finding(fid, {"dismiss": "Dismissed", "reopen": "New", "close": "Closed"}[act],
                                     user["username"], str(body.get("note") or ""))
                    self._send(200, {"finding": ureg.finding(fid)})
                except (ValueError, RuntimeError) as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path == "/api/unicorn/decision":
                if not auth.can(user, "unicorn.decisions.approve"):
                    self._send(403, {"error": "Your role cannot approve or refuse Unicorn's proposals."}); return
                try:
                    did = int(body.get("id") or 0)
                    if body.get("action") == "approve":
                        ids = ureg.approve(did, user["username"], body.get("owner"), body.get("due"), body.get("target"),
                                           str(body.get("note") or ""), body.get("actions") or None)
                        self._send(200, {"decision": ureg.decision(did), "actions": ids})
                    elif body.get("action") == "refuse":
                        ureg.refuse(did, user["username"], str(body.get("reason") or ""))
                        self._send(200, {"decision": ureg.decision(did)})
                    else:
                        raise ValueError("Action: approve or refuse.")
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path == "/api/unicorn/action":
                if not auth.can(user, "unicorn.use"):
                    self._send(403, {"error": "Your role cannot talk to Unicorn."}); return
                try:
                    if (body.get("owner") or body.get("due") or body.get("status") in ("Cancelled",)) and \
                            not auth.can(user, "unicorn.decisions.approve"):
                        self._send(403, {"error": "Only who approves decisions changes the owner, the deadline or cancels."}); return
                    ureg.update_action(int(body.get("id") or 0), user["username"], body.get("status") or None,
                                       body.get("note"), body.get("owner") or None, body.get("due") or None)
                    self._send(200, {"actions": ureg.actions()})
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path == "/api/unicorn/rule":
                if not auth.can(user, "unicorn.rules.manage"):
                    self._send(403, {"error": "Your role cannot change Unicorn's rules."}); return
                try:
                    act = body.get("action")
                    if act == "save":
                        self._send(200, ureg.save_rule(body, user["username"], approve=True))
                    elif act in ("approve", "set_aside"):
                        ureg.decide_rule(int(body.get("id") or 0), act == "approve", user["username"], str(body.get("reason") or ""))
                        self._send(200, {"ok": True})
                    elif act == "retire":
                        self._send(200, {"retired": ureg.retire_rule(str(body.get("key") or ""), user["username"], str(body.get("reason") or ""))})
                    else:
                        raise ValueError("Action: save, approve, set_aside or retire.")
                except ValueError as exc:
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
                sc = None
                if "scope" in body:                    # the companies and locations of the role
                    try:
                        sc = auth.clean_scope(body.get("scope"))
                    except ValueError as exc:
                        self._send(400, {"error": str(exc)}); return
                    if existing and existing["system"] and sc != auth.ALL_SCOPE:
                        self._send(400, {"error": "The Administrator role sees every company and location."}); return
                dep = (body.get("department") or (existing["department"] if existing else "") or "Other")[:40]
                store.run("""INSERT INTO roles(name,description,permissions,system,department) VALUES(?,?,?,0,?)
                             ON CONFLICT(name) DO UPDATE SET description=excluded.description,
                                                             permissions=excluded.permissions,
                                                             department=excluded.department""",
                          (name, (body.get("description") or "")[:200], json.dumps(perms), dep))
                if sc is not None:
                    store.run("UPDATE roles SET scope=? WHERE name=?",
                              (None if sc == auth.ALL_SCOPE else json.dumps(sc), name))
                store.audit("role saved", user["username"], name,
                            {"count": len(perms)} | ({"scope": sc} if sc is not None else {}), ip)
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
    actioncenter.start()
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
