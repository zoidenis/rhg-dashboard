"""Weekly sales email: every restaurant's sales of the week before against last year and the budget.

Every Monday (the day and time are in Finance → Settings → Sales weekly email):
  * one email with all the restaurants of the registry, by brand: sales excluding VAT (Simphony), the
    budget of those days (G/L budget of the restaurant's department, pro-rated by day), the same weekdays
    of last year and the week before, checks and the average check, and the month to date against budget
    and last year; to the people set for "Sales against last year and budget" in Settings → Email
    recipients (operations manager, executive chef, COO, CFO, CEO, others ...) of the companies;
  * one email per restaurant that has people of its own in Email recipients (unit manager, head chef ...),
    with that restaurant only, day by day.
The figures are those of the brand performance email (brand_report.build), restaurant by restaurant.
Read-only: Simphony and Business Central are only read; the mail goes through Purchasing.
"""
import html
import json
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta

import arap_notify
import brand_report
import config
import locintel
import recipients
import store

STATE_FILE = store.DATA_DIR / "sales_report.json"
_state_lock, _run_lock = threading.Lock(), threading.Lock()
_m, _pct, _p = brand_report._m, brand_report._pct, brand_report._p


def settings():
    return store.get_settings().get("sales_report", {})


def _read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(s):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=1, default=str), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _log(entry):
    with _state_lock:
        s = _read_state()
        s["runs"] = (s.get("runs", []) + [entry])[-200:]
        _write_state(s)


def recent_runs(limit=20):
    with _state_lock:
        return list(reversed(_read_state().get("runs", [])))[:limit]


def registry():
    return [r for r in locintel.registry() if r.get("ref")]


# ---------------------------------------------------------------- the figures
def build_all(client, latest_fn, parallel_fn, currency_of, today=None):
    out, errors, period = [], [], None
    for brand in brand_report.brands():
        try:
            r = brand_report.build("weekly", brand, client, latest_fn, parallel_fn, currency_of, today, sales_only=True)
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            errors.append(f"{brand}: {str(exc)[:200]}")
            continue
        errors += [f"{brand}: {e}" for e in r.get("errors") or []]
        period = period or r
        if r["locations"]:
            out.append(r)
    totals = {}
    for r in out:
        for cur, t in r["totals"].items():
            T = totals.setdefault(cur, {"sales": 0.0, "ly": 0.0, "prev": 0.0, "budget": 0.0, "b_sales": 0.0, "locations": 0,
                                        "checks": 0, "net": 0.0, "ly_checks": 0, "ly_net": 0.0, "mtd": {}})
            for k in ("sales", "ly", "prev", "budget", "b_sales", "locations", "checks", "net", "ly_checks", "ly_net"):
                T[k] += t.get(k) or 0
            for k in ("sales", "ly", "budget", "b_sales"):
                T["mtd"][k] = T["mtd"].get(k, 0.0) + ((t.get("mtd") or {}).get(k) or 0)
    main = max(totals, key=lambda c: totals[c]["sales"]) if totals else None
    locs = [dict(L, brand=r["brand"]) for r in out for L in r["locations"]]
    hl = []
    T = totals.get(main) or {}
    if T:
        vb, vl = _pct(T["b_sales"], T["budget"]), _pct(T["sales"], T["ly"])
        hl.append(("good" if (vb or 0) >= 0 else "bad",
                   f"Sales {_m(T['sales'])} {main} excluding VAT: {_p(vb)} against budget ({_m(T['budget'])}), "
                   f"{_p(vl)} against the same weekdays last year, {_p(_pct(T['sales'], T['prev']))} against the week before."))
        mt = T["mtd"]
        if mt.get("sales"):
            hl.append(("good" if (_pct(mt.get("b_sales"), mt.get("budget")) or 0) >= 0 else "bad",
                       f"Month to date {_m(mt['sales'])}: {_p(_pct(mt.get('b_sales'), mt.get('budget')))} against budget, "
                       f"{_p(_pct(mt['sales'], mt.get('ly')))} against last year."))
        bud = [L for L in locs if L.get("budget") and L["budget"]["sales"] and L["currency"] == main]
        if len(bud) > 1:
            g = sorted(bud, key=lambda L: _pct(L["sales"]["ex"], L["budget"]["sales"]))
            under = [L for L in g if _pct(L["sales"]["ex"], L["budget"]["sales"]) < 0]
            msg = ((f"{len(under)} of {len(bud)} restaurants under budget; furthest: "
                    + ", ".join(f"{L['name']} {_p(_pct(L['sales']['ex'], L['budget']['sales']))}" for L in under[:3]) + ". ")
                   if under else f"All {len(bud)} budgeted restaurants at or over budget. ")
            msg += f"Best: {g[-1]['name']} {_p(_pct(g[-1]['sales']['ex'], g[-1]['budget']['sales']))}."
            hl.append(("bad" if under else "good", msg))
        ly = [L for L in locs if L["ly"]["ex"] and L["currency"] == main]
        if ly:
            g = sorted(ly, key=lambda L: _pct(L["sales"]["ex"], L["ly"]["ex"]))
            hl.append(("info", f"Against last year, lowest: {g[0]['name']} {_p(_pct(g[0]['sales']['ex'], g[0]['ly']['ex']))}; "
                               f"highest: {g[-1]['name']} {_p(_pct(g[-1]['sales']['ex'], g[-1]['ly']['ex']))}."))
    if not period:
        raise RuntimeError("No brand in the restaurant registry (Settings → Location intelligence) could be read.")
    return {"brands": out, "totals": totals, "main": main, "other": [c for c in totals if c != main],
            "period": period["period"], "prev": period["prev"], "ly": period["ly"], "mtd_from": period["mtd_from"],
            "highlights": hl, "errors": errors, "locations": len(locs), "built_at": datetime.now().isoformat(timespec="seconds")}


def subject_all(r, test=False):
    a, b = date.fromisoformat(r["period"]["from"]), date.fromisoformat(r["period"]["to"])
    T = r["totals"].get(r["main"]) or {}
    vb, vl = _pct(T.get("b_sales"), T.get("budget")), _pct(T.get("sales"), T.get("ly"))
    return (("[Test] " if test else "") + f"[RHG] Sales week {a:%d %b} – {b:%d %b %Y} · {_p(vb)} vs budget · {_p(vl)} vs last year")


def render_all(r):
    e = html.escape
    navy, primary, ink, muted, line, sky = "#08303f", "#0c475e", "#0e2733", "#5c7482", "#d9dcdd", "#e4f3f9"
    red, green, amber = "#c0392b", "#0e8a57", "#b26a00"
    th = f"text-align:left;padding:6px 8px;background:{sky};color:{ink};font-size:11.5px;font-weight:600;white-space:nowrap"
    thn = th.replace("text-align:left", "text-align:right")
    td = f"padding:5px 8px;border-bottom:1px solid #f0f1f2;font-size:12px;color:{ink};vertical-align:top"
    tdn = td + ";text-align:right;white-space:nowrap"
    a, b = date.fromisoformat(r["period"]["from"]), date.fromisoformat(r["period"]["to"])
    main = r["main"]
    T = r["totals"].get(main) or {}

    def var(v):
        if v is None:
            return f"<span style='color:{muted}'>–</span>"
        return f"<span style='color:{green if v >= 0 else red}'>{_p(v)}</span>"

    def tile(label, value, sub):
        return (f"<td style='padding:10px 12px;background:#fff;border:1px solid {line};border-radius:10px;vertical-align:top;width:25%'>"
                f"<div style='font-size:11.5px;color:{muted}'>{e(label)}</div><div style='font-size:19px;font-weight:700;color:{ink};margin:3px 0'>{value}</div>"
                f"<div style='font-size:11px;color:{muted}'>{sub}</div></td>")
    tiles = ""
    if T:
        mt = T.get("mtd") or {}
        ac = T["net"] / T["checks"] if T["checks"] else None
        acl = T["ly_net"] / T["ly_checks"] if T["ly_checks"] else None
        tiles = ("<table role='presentation' cellspacing='6' cellpadding='0' style='width:100%;margin:0 -6px'><tr>"
                 + tile("Sales excl. VAT", _m(T["sales"]), f"{e(main)} · {T['locations']} restaurants")
                 + tile("Against budget", var(_pct(T["b_sales"], T["budget"])), f"budget {_m(T['budget'])}")
                 + tile("Against last year", var(_pct(T["sales"], T["ly"])), f"last year {_m(T['ly'])}")
                 + tile("Month to date", _m(mt.get("sales")), f"{var(_pct(mt.get('b_sales'), mt.get('budget')))} budget · {var(_pct(mt.get('sales'), mt.get('ly')))} LY")
                 + "</tr></table>"
                 + f"<div style='font-size:11px;color:{muted};margin-top:4px'>Checks {T['checks']:,} · average check {_m(ac)} incl. VAT ({_p(_pct(ac, acl))} vs last year)</div>")
    dot = {"good": green, "bad": red, "info": primary}
    hl = "".join(f"<tr><td style='padding:4px 0;width:14px;vertical-align:top'><span style='display:inline-block;width:8px;height:8px;border-radius:50%;"
                 f"background:{dot.get(k, primary)};margin-top:5px'></span></td><td style='padding:3px 0;font-size:13px;color:{ink}'>{e(t)}</td></tr>"
                 for k, t in r["highlights"])
    head = [("Restaurant", 0), ("Sales", 1), ("Budget", 1), ("vs budget", 1), ("Last year", 1), ("vs LY", 1), ("vs week before", 1),
            ("Checks", 1), ("Avg check", 1), ("MTD vs budget", 1), ("MTD vs LY", 1)]
    rows = []
    for br in r["brands"]:
        bt = br["totals"].get(br["main"]) or {}
        mt = bt.get("mtd") or {}
        rows.append(f"<tr><td style='{td};background:#f6f8f9;font-weight:700'>{e(br['brand'])}</td>"
                    f"<td style='{tdn};background:#f6f8f9;font-weight:700'>{_m(bt.get('sales'))}</td><td style='{tdn};background:#f6f8f9;color:{muted}'>{_m(bt.get('budget')) if bt.get('has_budget') else '–'}</td>"
                    f"<td style='{tdn};background:#f6f8f9'>{var(_pct(bt.get('b_sales'), bt.get('budget')) if bt.get('has_budget') else None)}</td>"
                    f"<td style='{tdn};background:#f6f8f9;color:{muted}'>{_m(bt.get('ly')) if bt.get('ly') else '–'}</td><td style='{tdn};background:#f6f8f9'>{var(_pct(bt.get('sales'), bt.get('ly')))}</td>"
                    f"<td style='{tdn};background:#f6f8f9'>{var(_pct(bt.get('sales'), bt.get('prev')))}</td><td style='{tdn};background:#f6f8f9'>{(bt.get('checks') or 0):,}</td>"
                    f"<td style='{tdn};background:#f6f8f9'>{_m(bt['net'] / bt['checks']) if bt.get('checks') else '–'}</td>"
                    f"<td style='{tdn};background:#f6f8f9'>{var(_pct(mt.get('b_sales'), mt.get('budget')))}</td><td style='{tdn};background:#f6f8f9'>{var(_pct(mt.get('sales'), mt.get('ly')))}</td></tr>")
        for L in sorted(br["locations"], key=lambda L: -L["sales"]["ex"]):
            s, bu = L["sales"], (L.get("budget") or {}).get("sales")
            m = L.get("mtd") or {}
            mb = (m.get("budget") or {}).get("sales")
            rows.append(f"<tr><td style='{td};padding-left:18px'>{e(L['name'])}<div style='color:{muted};font-size:11px'>{e(L['currency'])}{' · ' + e(L['dept']) if L.get('dept') else ''}</div></td>"
                        f"<td style='{tdn}'>{_m(s['ex'])}</td><td style='{tdn};color:{muted}'>{_m(bu) if bu else '–'}</td><td style='{tdn}'>{var(_pct(s['ex'], bu) if bu else None)}</td>"
                        f"<td style='{tdn};color:{muted}'>{_m(L['ly']['ex']) if L['ly']['ex'] else '–'}</td><td style='{tdn}'>{var(_pct(s['ex'], L['ly']['ex']))}</td>"
                        f"<td style='{tdn}'>{var(_pct(s['ex'], (L.get('prev') or {}).get('ex')))}</td><td style='{tdn}'>{s['checks']:,}</td>"
                        f"<td style='{tdn}'>{_m(s['net'] / s['checks']) if s['checks'] else '–'}</td>"
                        f"<td style='{tdn}'>{var(_pct((m.get('sales') or {}).get('ex'), mb) if mb else None)}</td><td style='{tdn}'>{var(_pct((m.get('sales') or {}).get('ex'), (m.get('ly') or {}).get('ex')))}</td></tr>")
    hs = "".join(f"<th style='{thn if n else th}'>{e(h)}</th>" for h, n in head)
    table = f"<div style='overflow-x:auto'><table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;border:1px solid {line}'><tr>{hs}</tr>{''.join(rows)}</table></div>"
    other = (f"<div style='font-size:11px;color:{muted};margin-top:4px'>Totals above are in {e(main)}; "
             + ", ".join(f"{e(c)}: sales {_m(r['totals'][c]['sales'])} ({_p(_pct(r['totals'][c]['sales'], r['totals'][c]['ly']))} vs LY)" for c in r["other"]) + ".</div>") if r["other"] else ""
    errs = "".join(f"<div style='background:#fdf3e2;color:{amber};padding:7px 11px;border-radius:8px;font-size:12px;margin-bottom:8px'>{e(x)}</div>" for x in r["errors"][:6])
    public = (config.PUBLIC_URL or "").rstrip("/")
    body = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif;color:{ink}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ecebeb"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:980px">
<tr><td style="background:{navy};color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · SALES</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">Sales of the week · every restaurant</div>
<div style="font-size:12px;color:#cfe3ea;margin-top:4px">Week {a:%d %b} – {b:%d %b %Y} · against the budget, the same weekdays last year ({e(r['ly']['from'])} → {e(r['ly']['to'])}) and the week before</div></td></tr>
<tr><td style="background:#fff;padding:20px 22px;border-radius:0 0 12px 12px">
{errs}{tiles}
<h3 style="font-size:14px;color:{ink};margin:18px 0 6px">Highlights</h3><table role="presentation" cellspacing="0" cellpadding="0" style="width:100%">{hl}</table>
<h3 style="font-size:14px;color:{ink};margin:22px 0 4px">By brand and restaurant</h3>
<div style="font-size:12px;color:{muted};margin-bottom:6px">Sales excluding VAT from Simphony; budget = the G/L budget of the restaurant's department for these days; month to date from {e(r['mtd_from'])}.</div>
{table}{other}
{f"<p style='margin:20px 0 0'><a href='{e(public)}/finance/#sales' style='background:{primary};color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600;display:inline-block'>Open Sales in the Finance tower</a></p>" if public else ''}
<p style="color:{muted};font-size:11px;margin:22px 0 0;border-top:1px solid {line};padding-top:12px">Sent every week by RHG Control Towers from Simphony and Business Central; nothing was changed there. Recipients are in Finance → Settings → Email recipients by company ("Sales against last year and budget"); the day and time in Sales weekly email.</p>
</td></tr></table></td></tr></table></body></html>"""
    text = f"RHG · Sales of the week {a:%d %b} – {b:%d %b %Y}\n\n" + "\n".join(f"- {t}" for _, t in r["highlights"])
    return body, text


# ---------------------------------------------------------------- sending, schedule, jobs
class Reporter:
    def __init__(self, client, latest_fn, parallel_fn, currency_of):
        self.client, self.latest_fn, self.parallel_fn, self.currency_of = client, latest_fn, parallel_fn, currency_of
        self._jobs, self._jobs_lock = {}, threading.Lock()

    def general(self, s=None):
        s = s or settings()
        to = arap_notify.parse_emails(s.get("to"))[0]
        return to, [c for c in arap_notify.parse_emails(s.get("cc"))[0] if c not in to]

    def group_recipients(self):
        to, cc = self.general()
        comps = sorted({r.get("company") for r in registry() if r.get("company")})
        rec = recipients.resolve("sales_weekly", comps, to, cc)
        return rec["to"], rec["cc"], rec.get("source", "general")

    def location_recipients(self):
        """[(registry row, to, cc)] of the restaurants with people of their own."""
        reg = registry()
        out = []
        for company in sorted({r.get("company") for r in reg if r.get("company")}):
            for loc, to, cc in recipients.location_targets("sales_weekly", company):
                k = loc.strip().upper()
                row = next((r for r in reg if (r.get("company") or "").upper() == company.upper()
                            and k in {(r.get(x) or "").strip().upper() for x in ("bc", "dept", "ref")}), None)
                if row:
                    out.append((row, to, cc))
        return out

    @staticmethod
    def render_location(r):
        body, text = brand_report.render(r)
        return body.replace("Recipients and schedule are in Finance → Settings → Brand performance email.",
                            "Sent every week to the people of this restaurant in Finance → Settings → Email recipients "
                            "(\"Sales against last year and budget\"); the day and time in Sales weekly email."), text

    def build_location(self, row):
        brand = (row.get("brand") or row.get("name") or row["ref"]).strip().upper()
        return brand_report.build("weekly", brand, self.client, self.latest_fn, self.parallel_fn, self.currency_of,
                                  only_ref=row["ref"], label=row.get("name") or row["ref"], sales_only=True)

    def _send(self, kind, label, to, cc, body, text, subj, actor, test, period):
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, "target": label, "actor": actor,
                 "test": test, "to": to, "cc": cc, "period": period}
        try:
            arap_notify.send_mail(to, cc, subj, body, text)
            entry.update(status="sent", detail=subj)
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            entry.update(status="failed", detail=str(exc)[:300])
        _log(entry)
        store.audit("email", f"sales weekly · {label}", actor, {}, {k: entry.get(k) for k in ("status", "to", "cc", "test")},
                    entry.get("detail", ""))
        return entry

    def run(self, key):
        """The scheduled run: the email of all restaurants, then one per restaurant with people of its own."""
        with _run_lock:
            results = []
            to, cc, _ = self.group_recipients()
            if to:
                try:
                    r = build_all(self.client, self.latest_fn, self.parallel_fn, self.currency_of)
                    body, text = render_all(r)
                    results.append(self._send("all", "All restaurants", to, cc, body, text, subject_all(r), "scheduler", False, r["period"]))
                except Exception as exc:                            # noqa: BLE001
                    traceback.print_exc()
                    results.append({"status": "failed"})
                    _log({"ts": datetime.now().isoformat(timespec="seconds"), "kind": "all", "target": "All restaurants",
                          "actor": "scheduler", "status": "failed", "detail": str(exc)[:300]})
            else:
                _log({"ts": datetime.now().isoformat(timespec="seconds"), "kind": "all", "target": "All restaurants",
                      "actor": "scheduler", "status": "skipped", "detail": "Nobody to send the email of all restaurants to."})
            for row, lto, lcc in self.location_recipients():
                try:
                    r = self.build_location(row)
                    if not r["locations"]:
                        continue
                    body, text = self.render_location(r)
                    results.append(self._send("location", row.get("name") or row["ref"], lto, lcc, body, text,
                                              brand_report.subject(r), "scheduler", False, r["period"]))
                except Exception as exc:                            # noqa: BLE001
                    traceback.print_exc()
                    results.append({"status": "failed"})
                    _log({"ts": datetime.now().isoformat(timespec="seconds"), "kind": "location", "target": row.get("name"),
                          "actor": "scheduler", "status": "failed", "detail": str(exc)[:300]})
            with _state_lock:
                st = _read_state()
                if all(x.get("status") != "failed" for x in results):
                    st.setdefault("done", {})[key] = datetime.now().isoformat(timespec="seconds")
                    st.get("tries", {}).pop(key, None)
                else:
                    t = st.setdefault("tries", {}).setdefault(key, {"count": 0})
                    t.update(count=t["count"] + 1, at=time.time())
                    if t["count"] >= 3:
                        st.setdefault("done", {})[key] = "gave up"
                st["done"] = dict(list(st.get("done", {}).items())[-60:])
                _write_state(st)
            return results

    # ---- preview and test: target "all" or a restaurant's ref
    def start_job(self, target, actor, only_to=None):
        with self._jobs_lock:
            for j in self._jobs.values():
                if j["actor"] == actor and j["status"] == "running":
                    return {k: v for k, v in j.items() if k != "html"}
            self._jobs = {k: v for k, v in list(self._jobs.items())[-20:]}
            job = {"id": uuid.uuid4().hex, "actor": actor, "target": target, "to": only_to or [], "status": "running",
                   "detail": "", "html": None}
            self._jobs[job["id"]] = job

        def go():
            try:
                if target == "all":
                    r = build_all(self.client, self.latest_fn, self.parallel_fn, self.currency_of)
                    body, text = render_all(r)
                    subj, label = subject_all(r, test=True), "All restaurants"
                else:
                    row = next((x for x in registry() if x["ref"] == target), None)
                    if not row:
                        raise RuntimeError("That restaurant is not in the registry.")
                    r = self.build_location(row)
                    body, text = self.render_location(r)
                    subj, label = brand_report.subject(r, test=True), row.get("name") or row["ref"]
                job["html"] = body
                if only_to:
                    res = self._send("test", label, only_to, [], body, text, subj, actor, True, r["period"])
                    if res["status"] != "sent":
                        raise RuntimeError(res.get("detail") or res["status"])
                    job["detail"] = "Sent to " + ", ".join(only_to)
                job["status"] = "done"
            except Exception as exc:                                # noqa: BLE001
                traceback.print_exc()
                job.update(status="failed", detail=str(exc)[:300])
        threading.Thread(target=go, daemon=True, name="sales-report-job").start()
        return {k: v for k, v in job.items() if k != "html"}

    def job(self, job_id, actor):
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            return j if j and j["actor"] == actor else None

    @staticmethod
    def _when(s):
        hh, _, mm = (s.get("time") or "08:00").partition(":")
        try:
            wd = max(0, min(6, int(float(s.get("day") or 0))))
        except (TypeError, ValueError):
            wd = 0
        return wd, int(hh or 8), int(mm or 0)

    def due(self, now):
        s = settings()
        if not s.get("enabled"):
            return None
        wd, hh, mm = self._when(s)
        start = datetime(now.year, now.month, now.day, hh, mm)
        if now.weekday() != wd or not (start <= now <= start + timedelta(hours=10)):
            return None
        key = "W" + brand_report.last_week(now.date())[0].isoformat()
        with _state_lock:
            st = _read_state()
        t = st.get("tries", {}).get(key, {})
        if st.get("done", {}).get(key) or t.get("count", 0) >= 3 or time.time() - t.get("at", 0) < 1800:
            return None
        return key

    def next_run(self, now=None):
        s = settings()
        if not s.get("enabled"):
            return None
        now = now or datetime.now()
        wd, hh, mm = self._when(s)
        d = now.date() + timedelta(days=(wd - now.weekday()) % 7)
        t = datetime(d.year, d.month, d.day, hh, mm)
        if t + timedelta(hours=10) < now:
            t += timedelta(days=7)
        return t.isoformat(timespec="minutes")

    def loop(self):
        while True:
            try:
                key = self.due(datetime.now())
                if key:
                    self.run(key)
            except Exception:                                       # noqa: BLE001
                traceback.print_exc()
            time.sleep(60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="sales-report").start()
