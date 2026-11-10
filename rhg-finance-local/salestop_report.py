"""The daily email of Sales → Blocked by stock: the items whose quantity stops the sales from being posted in
Business Central now, and the ones today's Simphony sales will run out of, with the whole report in Excel.

One email per company to Email recipients by company → Sales blocked by stock · daily (cost control and operations
by default), one per location to the location's own people with that location only; every day at the time set
in Finance → Settings (09:30 by default). It reads what the screen reads (salestop.build), fresh at the time of
sending; Business Central and Simphony are only read.
"""
import html
import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta

import arap_notify
import config
import recipients
import salestop
import store

REPORT = "stockstop_daily"
STATE_FILE = store.DATA_DIR / "stockstop_report.json"
DAYS = {"daily": (0, 1, 2, 3, 4, 5, 6), "mon_sat": (0, 1, 2, 3, 4, 5), "weekdays": (0, 1, 2, 3, 4)}
DEFAULT_TIME = "09:30"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_state_lock, _run_lock = threading.Lock(), threading.Lock()


def _m(v):
    v = v or 0
    return f"{v:,.0f}" if abs(v) < 1e6 else f"{v / 1e6:,.1f} M"


def _q(v):
    v = v or 0
    return f"{v:,.0f}" if abs(v - round(v)) < 1e-9 else f"{v:,.2f}"


def subject(r, test=False):
    t = r["totals"]
    d = datetime.fromisoformat(r["read_at"])
    n = t["components"]
    return (("[Test] " if test else "") + f"[RHG Finance] {r.get('label') or r['company']} · Sales blocked by stock "
            f"{d:%d %b %Y} · {n} {'component' if n == 1 else 'components'} short, {t['orders']:,} "
            f"{'order' if t['orders'] == 1 else 'orders'} blocked, {t['risk']} at risk today")


def render(r, public_url="", max_lines=50):
    e = html.escape
    C = r.get("currency") or ""
    t = r["totals"]
    navy, primary, ink, muted, line, sky = "#08303f", "#0c475e", "#0e2733", "#5c7482", "#d9dcdd", "#e4f3f9"
    red, amber, green = "#c0392b", "#b26a00", "#0e8a57"
    th = f"text-align:left;padding:6px 8px;background:{sky};color:{ink};font-size:11.5px;font-weight:600"
    thn = th.replace("text-align:left", "text-align:right")
    td = f"padding:5px 8px;border-bottom:1px solid #f0f1f2;font-size:12px;color:{ink};vertical-align:top"
    tdn = td + ";text-align:right;white-space:nowrap"
    read = datetime.fromisoformat(r["read_at"])

    def tile(label, value, sub, color=ink):
        return (f"<td style='padding:10px 12px;background:#fff;border:1px solid {line};border-radius:10px;vertical-align:top;"
                f"width:20%'><div style='font-size:11.5px;color:{muted}'>{e(label)}</div>"
                f"<div style='font-size:19px;font-weight:700;color:{color};margin:3px 0'>{value}</div>"
                f"<div style='font-size:11px;color:{muted}'>{e(sub)}</div></td>")

    def table(head, rows):
        return (f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;"
                f"border:1px solid {line};margin-top:6px'><tr>{head}</tr>{rows}</table>")

    def more(n, shown):
        return (f"<div style='font-size:11.5px;color:{muted};margin-top:4px'>{n - shown:,} more lines in the Excel file.</div>"
                if shown < n else "")

    def menu(x, k=3):
        return "; ".join(f"{m[1] or m[0]} ×{_q(m[2])}" for m in x["menu"][:k]) + (" …" if len(x["menu"]) > k else "")

    tiles = ("<table role='presentation' cellspacing='6' cellpadding='0' style='width:100%;margin:0 -6px'><tr>"
             + tile("Components short", f"{t['components']:,}", "blocking the posting of sales now", red if t["components"] else green)
             + tile("Sales orders blocked", f"{t['orders']:,}", f"{_q(t['portions'])} portions"
                    + (f" · since {min(t['days'])}" if t.get("days") else ""), red if t["orders"] else ink)
             + tile("Sales held", _m(t["sales"]), f"portions × menu price in Simphony, {C}", amber if t["sales"] else ink)
             + tile("At risk today", f"{t['risk']:,}", "components today's sales will run out of", amber if t["risk"] else green)
             + tile("Flagged, covered now", f"{t['covered']:,}", "BC still flags them; the stock is there: post the orders")
             + "</tr></table>")
    L = r["locations"]
    loc_table = (f"<h3 style='font-size:14px;color:{ink};margin:22px 0 6px'>By location</h3>" + table(
        f"<th style='{th}'>Location</th><th style='{thn}'>Orders blocked</th><th style='{thn}'>Portions</th>"
        f"<th style='{thn}'>Sales held</th><th style='{thn}'>Components short</th><th style='{thn}'>At risk today</th>"
        f"<th style='{th}'>Days</th>",
        "".join(f"<tr><td style='{td}'><b>{e(x['location'])}</b> <span style='color:{muted}'>{e(x['name'])}</span></td>"
                f"<td style='{tdn}'>{x['orders']:,}</td><td style='{tdn}'>{_q(x['portions'])}</td>"
                f"<td style='{tdn}'>{_m(x['sales'])}</td><td style='{tdn};color:{red if x['components'] else ink}'>{x['components']:,}</td>"
                f"<td style='{tdn};color:{amber if x['risk'] else ink}'>{x['risk']:,}</td>"
                f"<td style='{td};color:{muted}'>{e(', '.join(x['days']))}</td></tr>" for x in L))) if len(L) > 1 else ""
    short = [x for x in r["now"] if x["status"] == "short"]
    covered = [x for x in r["now"] if x["status"] == "covered"]
    shown = short if not max_lines else short[:max_lines]
    now_html = (f"<h3 style='font-size:14px;color:{ink};margin:24px 0 2px'>Blocking the sales now "
                f"<span style='color:{muted};font-weight:400'>({len(short):,})</span></h3>"
                f"<div style='font-size:12px;color:{muted}'>The open orders need more than the location holds: until the stock "
                f"is there (a receipt, a transfer, a count), these orders and their sales are not posted. Largest sales held first.</div>")
    if short:
        now_html += table(
            f"<th style='{th}'>Location</th><th style='{th}'>Item</th><th style='{thn}'>Short</th><th style='{thn}'>On hand</th>"
            f"<th style='{thn}'>Orders</th><th style='{thn}'>Sales held</th><th style='{th}'>Since</th><th style='{th}'>Menu items</th>",
            "".join(f"<tr><td style='{td}'>{e(x['location'])}</td><td style='{td}'>{e(x['desc'] or x['item'])} "
                    f"<span style='color:{muted}'>{e(x['item'])}</span></td>"
                    f"<td style='{tdn};color:{red}'><b>{_q(x['short'])}</b> {e(x['unit'])}</td><td style='{tdn}'>{_q(x['on_hand'])}</td>"
                    f"<td style='{tdn}'>{x['orders']:,}</td><td style='{tdn}'>{_m(x['sales'])}</td>"
                    f"<td style='{td};color:{muted}'>{e(x['oldest'] or '')}</td><td style='{td};color:{muted}'>{e(menu(x))}</td></tr>"
                    for x in shown)) + more(len(short), len(shown))
    else:
        now_html += f"<p style='font-size:12px;color:{green};margin:6px 0 0'>Nothing blocked: every open order has its components in stock.</p>"
    cov_html = ""
    if covered:
        cs = covered[:max_lines] if max_lines else covered
        cov_html = (f"<h3 style='font-size:14px;color:{ink};margin:24px 0 2px'>Flagged by Business Central, covered now "
                    f"<span style='color:{muted};font-weight:400'>({len(covered):,})</span></h3>"
                    f"<div style='font-size:12px;color:{muted}'>BC's Avail. Warning is still on, but the stock now covers what the "
                    f"open orders need: these orders can be posted.</div>" + table(
                        f"<th style='{th}'>Location</th><th style='{th}'>Item</th><th style='{thn}'>Open orders need</th>"
                        f"<th style='{thn}'>On hand</th><th style='{thn}'>Orders</th><th style='{thn}'>Sales held</th>",
                        "".join(f"<tr><td style='{td}'>{e(x['location'])}</td><td style='{td}'>{e(x['desc'] or x['item'])} "
                                f"<span style='color:{muted}'>{e(x['item'])}</span></td><td style='{tdn}'>{_q(x['need'])} {e(x['unit'])}</td>"
                                f"<td style='{tdn}'>{_q(x['on_hand'])}</td><td style='{tdn}'>{x['orders']:,}</td>"
                                f"<td style='{tdn}'>{_m(x['sales'])}</td></tr>" for x in cs)) + more(len(covered), len(cs)))
    risk = r["risk"]
    rs = risk if not max_lines else risk[:max_lines]
    risk_html = (f"<h3 style='font-size:14px;color:{ink};margin:24px 0 2px'>At risk today "
                 f"<span style='color:{muted};font-weight:400'>({len(risk):,})</span></h3>"
                 f"<div style='font-size:12px;color:{muted}'>What Simphony has sold today and Business Central has not imported "
                 f"yet, through the recipes: the components whose stock, after what the open orders already need, does not "
                 f"cover it. Fix them before the import, and the sales post.</div>")
    if risk:
        risk_html += table(
            f"<th style='{th}'>Location</th><th style='{th}'>Item</th><th style='{thn}'>On hand</th><th style='{thn}'>Open orders need</th>"
            f"<th style='{thn}'>Today's need</th><th style='{thn}'>After today</th><th style='{th}'>Sold today</th>",
            "".join(f"<tr><td style='{td}'>{e(x['location'])}</td><td style='{td}'>{e(x['desc'] or x['item'])} "
                    f"<span style='color:{muted}'>{e(x['item'])}</span>"
                    + (f" <span style='background:#fbe9e7;color:{red};font-size:10.5px;font-weight:700;padding:1px 5px;"
                       f"border-radius:4px'>BLOCKING NOW TOO</span>" if x["blocked_now"] else "")
                    + f"</td><td style='{tdn}'>{_q(x['on_hand'])}</td><td style='{tdn}'>{_q(x['open_need'])}</td>"
                    f"<td style='{tdn}'>{_q(x['today_need'])}</td><td style='{tdn};color:{red}'><b>{_q(x['after'])}</b> {e(x['unit'])}</td>"
                    f"<td style='{td};color:{muted}'>{e(menu(x))}</td></tr>" for x in rs)) + more(len(risk), len(rs))
    else:
        risk_html += (f"<p style='font-size:12px;color:{green if not r.get('simphony_errors') else amber};margin:6px 0 0'>"
                      + ("Simphony could not be read." if r.get("simphony_errors") else
                         "Nothing at risk: today's sales are covered by the stock.") + "</p>")
    errs = "".join(f"<div style='background:#fdf3e2;color:{amber};padding:7px 11px;border-radius:8px;font-size:12px;margin-bottom:8px'>"
                   f"{e(x)}</div>" for x in (r.get("simphony_errors") or [])[:5])
    link = (f"<p style='margin:20px 0 0'><a href='{e(public_url)}/finance/#stockstop' style='background:{primary};color:#fff;"
            f"text-decoration:none;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600;display:inline-block'>"
            f"Open Sales · Blocked by stock</a></p>") if public_url else ""
    body = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif;color:{ink}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ecebeb"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:960px">
<tr><td style="background:{navy};color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · RHG FINANCE</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">Sales blocked by stock</div>
<div style="font-size:12px;color:#cfe3ea;margin-top:4px">{e(r.get('label') or r['company'])} · read {read:%A %d %b %Y %H:%M} · {r['open_orders']:,} open assembly orders, {r['open_lines']:,} lines</div></td></tr>
<tr><td style="background:#fff;padding:20px 22px;border-radius:0 0 12px 12px">
{errs}
<h3 style="font-size:14px;color:{ink};margin:0 0 6px">Now</h3>{tiles}
{loc_table}{now_html}{risk_html}{cov_html}
<p style="color:{muted};font-size:12px;margin:20px 0 0"><b>How it is read.</b> The Simphony sales are imported into Business Central as assembly orders, one per menu item, location and day; an order is posted when its components can be taken out of the location's stock. Open orders need = the remaining quantity of every open order of the component at the location (base unit); short = need − on hand now. Sales held = portions of the blocked orders × the menu item's price in Simphony. At risk today: the menu items sold today in Simphony, less what is already imported, through their BC recipe.</p>
<p style="color:{muted};font-size:12px;margin:8px 0 0"><b>The attached Excel file holds every line</b>: blocked now, at risk today, by location, with the menu items behind each component.</p>
{link}
<p style="color:{muted};font-size:11px;margin:22px 0 0;border-top:1px solid {line};padding-top:12px">Sent by RHG Control Towers from Business Central and Simphony; nothing was changed there. Recipients are in Settings → Email recipients by company (Sales blocked by stock · daily); the time in Finance → Settings → Sales blocked by stock daily email.</p>
</td></tr></table></td></tr></table></body></html>"""
    text = (f"RHG Finance · Sales blocked by stock · {r.get('label') or r['company']} · read {read:%d %b %Y %H:%M}\n"
            f"{t['components']} components short, {t['orders']} orders blocked ({_q(t['portions'])} portions, {_m(t['sales'])} {C} "
            f"held), {t['risk']} at risk today, {t['covered']} flagged but covered now.\n\n"
            + "\n".join(f"- {x['location']} {x['item']} {x['desc']}: short {_q(x['short'])} {x['unit']}, {x['orders']} orders"
                        for x in short[:200])
            + ("\n\nAt risk today:\n" + "\n".join(f"- {x['location']} {x['item']} {x['desc']}: {_q(x['after'])} {x['unit']} after today"
                                                   for x in risk[:100]) if risk else "")
            + "\n\nThe attached Excel file holds every line.")
    return body, text


# ---------------------------------------------------------------- state, sending, schedule
def _read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(s):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=1, default=str), encoding="utf-8")
    tmp.replace(STATE_FILE)


def recent_runs(limit=15):
    with _state_lock:
        return list(reversed(_read_state().get("runs", [])))[:limit]


def _log(entry):
    with _state_lock:
        s = _read_state()
        s["runs"] = (s.get("runs", []) + [entry])[-100:]
        _write_state(s)


class Reporter:
    def __init__(self, client, currency_of, default_company):
        self.client, self.currency_of, self.default_company = client, currency_of, default_company
        self._jobs, self._jobs_lock = {}, threading.Lock()

    @staticmethod
    def settings():
        s = dict(store.get_settings().get("stockstop_report") or {})
        s.setdefault("time", DEFAULT_TIME)
        s.setdefault("days", "daily")
        return s

    def companies(self, s=None):
        s = s or self.settings()
        return [c.strip() for c in (s.get("companies") or "").split(",") if c.strip()] or [self.default_company]

    def recipients(self, s=None):
        s = s or self.settings()
        to, bad = arap_notify.parse_emails(s.get("to"))
        cc, bad2 = arap_notify.parse_emails(s.get("cc"))
        return {"to": to, "cc": [c for c in cc if c not in to], "invalid": bad + bad2}

    def _lines(self):
        try:
            return int(float(self.settings().get("email_lines") or 0))
        except (TypeError, ValueError):
            return 50

    def build(self, company):
        r = salestop.build(self.client, company)
        r.update(currency=self.currency_of(company), label=company)
        return r

    def send(self, company, r=None, only_to=None, actor="scheduler", test=False, location=None, to=None, cc=None):
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "company": f"{company} · {location}" if location else company,
                 "actor": actor, "test": test}
        try:
            if only_to:
                rec = {"to": only_to, "cc": []}
            elif location:
                rec = {"to": to or [], "cc": cc or []}
            else:
                g = self.recipients()
                rec = recipients.resolve(REPORT, [company], g["to"], g["cc"])
            if not rec["to"]:
                raise RuntimeError("No recipient is set: Settings → Email recipients by company (Sales blocked by stock · "
                                   "daily) or Finance → Settings → Sales blocked by stock daily email.")
            r = r or self.build(company)
            if location:
                r = dict(salestop.cut(r, {location.upper()}), label=f"{company} · {location}")
            body, text = render(r, (config.PUBLIC_URL or "").rstrip("/"), self._lines())
            name = (f"RHG_{(r.get('label') or company).replace(' · ', '_').replace(' ', '_')}_sales_blocked_by_stock_"
                    f"{r['read_at'][:10]}.xlsx")
            arap_notify.send_mail(rec["to"], rec["cc"], subject(r, test), body, text, [(name, XLSX, salestop.render_xlsx(r))],
                                  source="Finance · sales blocked by stock")
            t = r["totals"]
            entry.update(status="sent", to=rec["to"], cc=rec["cc"], read_at=r["read_at"],
                         detail=f"{t['components']} short · {t['orders']} orders blocked · {t['risk']} at risk today")
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            entry.update(status="failed", detail=str(exc)[:300])
        _log(entry)
        store.audit("email", f"sales blocked by stock daily · {entry['company']}", actor, {},
                    {k: entry.get(k) for k in ("status", "to", "cc", "test")}, entry.get("detail", ""))
        return entry

    # ---- preview and test in the background
    def start_job(self, company, actor, only_to=None):
        with self._jobs_lock:
            for j in self._jobs.values():
                if j["actor"] == actor and j["status"] == "running":
                    return {k: v for k, v in j.items() if k != "html"}
            self._jobs = {k: v for k, v in list(self._jobs.items())[-20:]}
            job = {"id": uuid.uuid4().hex, "actor": actor, "company": company, "to": only_to or [], "status": "running",
                   "detail": "", "html": None, "started": datetime.now().isoformat(timespec="seconds")}
            self._jobs[job["id"]] = job

        def go():
            try:
                r = self.build(company)
                job["html"] = render(r, (config.PUBLIC_URL or "").rstrip("/"), self._lines())[0]
                if only_to:
                    res = self.send(company, r, only_to, actor, test=True)
                    if res["status"] != "sent":
                        raise RuntimeError(res.get("detail") or "not sent")
                    job["detail"] = "Sent to " + ", ".join(only_to)
                else:
                    t = r["totals"]
                    job["detail"] = f"{t['components']} components short · {t['orders']} orders blocked · {t['risk']} at risk today"
                job["status"] = "done"
            except Exception as exc:                               # noqa: BLE001
                traceback.print_exc()
                job.update(status="failed", detail=str(exc)[:300])
        threading.Thread(target=go, daemon=True, name="stockstop-report-job").start()
        return {k: v for k, v in job.items() if k != "html"}

    def job(self, job_id, actor):
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            return j if j and j["actor"] == actor else None

    # ---- the daily schedule
    @staticmethod
    def _when(s):
        hh, _, mm = (s.get("time") or DEFAULT_TIME).partition(":")
        return DAYS.get(s.get("days") or "daily", DAYS["daily"]), int(hh or 9), int(mm or 0)

    def next_run(self, now=None):
        s = self.settings()
        if not s.get("enabled"):
            return None
        now = now or datetime.now()
        days, hh, mm = self._when(s)
        with _state_lock:
            done = _read_state().get("done")
        for k in range(8):
            d = now.date() + timedelta(days=k)
            if d.weekday() not in days:
                continue
            t = datetime(d.year, d.month, d.day, hh, mm)
            if t + timedelta(hours=6) >= now and done != d.isoformat():
                return t.isoformat(timespec="minutes")
        return None

    def due(self, now):
        """The day to send, when it is time: from the time set until six hours later (a server restarted at 10:00
        still sends), once a day, three tries at most half an hour apart."""
        s = self.settings()
        if not s.get("enabled"):
            return None
        days, hh, mm = self._when(s)
        if now.weekday() not in days:
            return None
        start = datetime(now.year, now.month, now.day, hh, mm)
        if not (start <= now <= start + timedelta(hours=6)):
            return None
        day = now.date().isoformat()
        with _state_lock:
            st = _read_state()
        if st.get("done") == day:
            return None
        tries = st.get("tries", {})
        if tries.get("day") == day and (tries.get("count", 0) >= 3 or time.time() - tries.get("at", 0) < 1800):
            return None
        return day

    def run(self, day):
        with _run_lock:
            results = []
            for c in self.companies():
                r = None
                try:
                    r = self.build(c)
                except Exception:                                  # noqa: BLE001
                    traceback.print_exc()
                results.append(self.send(c, r))
                for loc, to, cc in recipients.location_targets(REPORT, c):
                    if r is None:
                        break
                    results.append(self.send(c, r, location=loc, to=to, cc=cc))
            with _state_lock:
                st = _read_state()
                if all(x["status"] == "sent" for x in results):
                    st["done"] = day
                    st.pop("tries", None)
                else:
                    t = st.get("tries", {})
                    count = t.get("count", 0) + 1 if t.get("day") == day else 1
                    st["tries"] = {"day": day, "count": count, "at": time.time()}
                    if count >= 3:
                        st["done"] = day
                _write_state(st)
            return results

    def loop(self):
        while True:
            try:
                day = self.due(datetime.now())
                if day:
                    self.run(day)
            except Exception:                                      # noqa: BLE001
                traceback.print_exc()
            time.sleep(60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="stockstop-report").start()


def status(rep):
    return {"settings": rep.settings(), "companies": rep.companies(), "recipients": rep.recipients(),
            "next_run": rep.next_run(), "runs": recent_runs(), "mail": arap_notify.mail_status()}
