"""The daily email of Sales → SevenRooms ↔ Simphony: yesterday's reservations of each restaurant against what
Simphony sold, the cases of the last 7 days, the low ratings, and today's and the next days' book with the forecast.

One email per company to Email recipients by company → Reservations against Simphony · daily (unit managers and
operations by default), one per location to the location's own people with that location only; every day at the
time set in Finance → Settings (10:00 by default: SevenRooms sets the night's statuses after closing). It reads what
the screen reads (srsim.build over the last 7 days), fresh at the time of sending; SevenRooms and Simphony are only
read, and no guest's name, phone or email is in it.
"""
import html
import json
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta

import arap_notify
import config
import recipients
import srsim
import store

REPORT = "sevenrooms_daily"
STATE_FILE = store.DATA_DIR / "sevenrooms_report.json"
DAYS = {"daily": (0, 1, 2, 3, 4, 5, 6), "mon_sat": (0, 1, 2, 3, 4, 5), "weekdays": (0, 1, 2, 3, 4)}
DEFAULT_TIME = "10:00"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
WD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CASE_LABEL = {"not_found": "Check numbers Simphony does not have", "no_check": "Seated, no check",
              "shared": "Its check is another reservation's too", "check_on_noshow": "No-show or cancelled, with a check",
              "open": "Left booked on a closed day"}
_state_lock, _run_lock = threading.Lock(), threading.Lock()


def _m(v):
    if v is None:
        return "–"
    return f"{v:,.0f}" if abs(v) < 1e6 else f"{v / 1e6:,.1f} M"


def _pct(v):
    return "–" if v is None else f"{v * 100:.0f}%"


def _n(v, d=0):
    return "–" if v is None else f"{v:,.{d}f}"


def yesterday_of(p):
    return p["range"]["to"]


def day_totals(p, day):
    """The restaurants' cells of one day, added up."""
    t = {}
    for L in p["locations"]:
        for k, v in (L["cells"].get(day) or {}).items():
            t[k] = t.get(k, 0) + (v or 0)
    return t


def subject(p, test=False):
    y = yesterday_of(p)
    t = day_totals(p, y)
    today = [x for x in p.get("forecast") or [] if x["lead"] == 0]
    booked = sum(x["covers"] for x in today)
    cases = sum(1 for x in p["rows"] if x["day"] == y and (x["link"] in srsim.CASES or x["cat"] == "open"))
    return (("[Test] " if test else "") + f"[RHG Finance] {p.get('label') or p['company']} · Reservations vs Simphony "
            f"{date.fromisoformat(y):%d %b %Y} · {t.get('covers', 0):,} covers seated, {t.get('no_show', 0):,} "
            f"{'no-show' if t.get('no_show', 0) == 1 else 'no-shows'}, {cases} {'case' if cases == 1 else 'cases'}"
            + (f" · today {booked:,} covers booked" if today else ""))


def render(p, public_url="", max_lines=50):
    e = html.escape
    C = p.get("currency") or ""
    y = yesterday_of(p)
    navy, primary, ink, muted, line, sky = "#08303f", "#0c475e", "#0e2733", "#5c7482", "#d9dcdd", "#e4f3f9"
    red, amber, green = "#c0392b", "#b26a00", "#0e8a57"
    th = f"text-align:left;padding:6px 8px;background:{sky};color:{ink};font-size:11.5px;font-weight:600"
    thn = th.replace("text-align:left", "text-align:right")
    td = f"padding:5px 8px;border-bottom:1px solid #f0f1f2;font-size:12px;color:{ink};vertical-align:top"
    tdn = td + ";text-align:right;white-space:nowrap"
    read = datetime.fromisoformat(p["checked_at"])
    lim = (lambda xs: xs[:max_lines]) if max_lines else (lambda xs: xs)

    def tile(label, value, sub, color=ink):
        return (f"<td style='padding:10px 12px;background:#fff;border:1px solid {line};border-radius:10px;vertical-align:top;"
                f"width:16%'><div style='font-size:11.5px;color:{muted}'>{e(label)}</div>"
                f"<div style='font-size:19px;font-weight:700;color:{color};margin:3px 0'>{value}</div>"
                f"<div style='font-size:11px;color:{muted}'>{e(sub)}</div></td>")

    def table(head, rows):
        return (f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;"
                f"border:1px solid {line};margin-top:6px'><tr>{head}</tr>{rows}</table>")

    def h3(text, n=None, note=""):
        return (f"<h3 style='font-size:14px;color:{ink};margin:24px 0 2px'>{e(text)}"
                + (f" <span style='color:{muted};font-weight:400'>({n:,})</span>" if n is not None else "") + "</h3>"
                + (f"<div style='font-size:12px;color:{muted}'>{e(note)}</div>" if note else ""))

    def more(n, shown):
        return (f"<div style='font-size:11.5px;color:{muted};margin-top:4px'>{n - shown:,} more lines in the Excel file.</div>"
                if shown < n else "")

    t = day_totals(p, y)
    share = t.get("covers", 0) / t["sim_guests"] if t.get("sim_guests") else None
    ns_rate = t.get("no_show", 0) / (t.get("seated", 0) + t.get("no_show", 0)) if t.get("seated", 0) + t.get("no_show", 0) else None
    y_cases = sum(1 for x in p["rows"] if x["day"] == y and (x["link"] in srsim.CASES or x["cat"] == "open"))
    K = p["kpis"]
    tiles = ("<table role='presentation' cellspacing='6' cellpadding='0' style='width:100%;margin:0 -6px'><tr>"
             + tile("Reservations", f"{t.get('res', 0):,}", f"{t.get('seated', 0):,} seated · {t.get('cancel', 0):,} cancelled")
             + tile("Covers seated", f"{t.get('covers', 0):,}", f"Simphony guests {t.get('sim_guests', 0):,}")
             + tile("No-shows", f"{t.get('no_show', 0):,}", f"rate {_pct(ns_rate)}", red if ns_rate and ns_rate >= 0.1 else ink)
             + tile("Share of guests booked", _pct(share), "covers seated over Simphony's guests")
             + tile("Sales with a reservation", _m(t.get("linked_sales")), f"of {_m(t.get('sim_sales'))} {C} on the checks")
             + tile("Cases", f"{y_cases:,}", "to look at, below", amber if y_cases else green)
             + "</tr></table>")
    L = p["locations"]
    loc_html = h3("By restaurant", None, f"Yesterday, {date.fromisoformat(y):%A %d %b}; the rates and the spend over the last "
                                         f"7 days ({p['range']['from']} to {y}).") + table(
        f"<th style='{th}'>Restaurant</th><th style='{thn}'>Reservations</th><th style='{thn}'>Covers seated</th>"
        f"<th style='{thn}'>No-shows</th><th style='{thn}'>Simphony guests</th><th style='{thn}'>Share booked</th>"
        f"<th style='{thn}'>Cases</th><th style='{thn}'>7 days: no-show rate</th><th style='{thn}'>Linked to a check</th>"
        f"<th style='{thn}'>Per cover, reservations</th><th style='{thn}'>Per cover, the rest</th><th style='{thn}'>Rating</th>",
        "".join((lambda c, k: f"<tr><td style='{td}'><b>{e(x['name'])}</b> <span style='color:{muted}'>{e(x['ref'])}</span></td>"
                 f"<td style='{tdn}'>{c.get('res', 0):,}</td><td style='{tdn}'>{c.get('covers', 0):,}</td>"
                 f"<td style='{tdn};color:{red if c.get('no_show') else ink}'>{c.get('no_show', 0):,}</td>"
                 f"<td style='{tdn}'>{c.get('sim_guests', 0):,}</td>"
                 f"<td style='{tdn}'>{_pct(c.get('covers', 0) / c['sim_guests'] if c.get('sim_guests') else None)}</td>"
                 f"<td style='{tdn};color:{amber if c.get('cases') else ink}'>{c.get('cases', 0):,}</td>"
                 f"<td style='{tdn}'>{_pct(k['no_show_rate'])}</td><td style='{tdn}'>{_pct(k['link_rate'])}</td>"
                 f"<td style='{tdn}'>{_m(k['per_cover_res'])}</td><td style='{tdn}'>{_m(k['per_cover_other'])}</td>"
                 f"<td style='{tdn}'>{_n(k['fb_overall'], 1)}</td></tr>")(x["cells"].get(y) or {}, x["k"]) for x in L))
    # today and the days ahead
    F = p.get("forecast") or []
    names = {x["ref"]: x["name"] for x in L}
    today = [x for x in F if x["lead"] == 0]
    ahead_html = ""
    if F:
        ahead_html = h3("Today and the next 6 days", None,
                        "On the books now, and the forecast from the same weekday of the 4 weeks before (covers seated against "
                        "what was on the books then at the same number of days ahead).") + table(
            f"<th style='{th}'>Restaurant</th><th style='{th}'>Day</th><th style='{thn}'>Reservations</th>"
            f"<th style='{thn}'>Covers booked</th><th style='{thn}'>VIP</th><th style='{thn}'>Parties of {srsim.BIG_PARTY}+</th>"
            f"<th style='{thn}'>Forecast covers</th><th style='{thn}'>Forecast guests</th><th style='{thn}'>Forecast net sales</th>",
            "".join(f"<tr><td style='{td}'>{e(names.get(x['ref'], x['ref']))}</td>"
                    f"<td style='{td}'>{'<b>Today</b>' if x['lead'] == 0 else WD[date.fromisoformat(x['day']).weekday()]} "
                    f"<span style='color:{muted}'>{date.fromisoformat(x['day']):%d %b}</span></td>"
                    f"<td style='{tdn}'>{x['res']:,}</td><td style='{tdn}'><b>{x['covers']:,}</b></td>"
                    f"<td style='{tdn}'>{x['vip'] or ''}</td><td style='{tdn}'>{x['big'] or ''}</td>"
                    f"<td style='{tdn}'>{_n(x['fc_covers'])}</td><td style='{tdn}'>{_n(x['fc_guests'])}</td>"
                    f"<td style='{tdn}'>{_m(x['fc_net'])}</td></tr>"
                    for x in sorted((x for x in F if x["lead"] < 7), key=lambda x: (names.get(x["ref"], x["ref"]), x["day"]))))
    # the cases of the 7 days
    cases = [x for x in p["rows"] if x["link"] in srsim.CASES or x["cat"] == "open"]
    cases.sort(key=lambda x: (x["day"], x["time"]), reverse=True)
    cs = lim(cases)
    case_html = h3("Cases of the last 7 days", len(cases),
                   "A seated reservation with no check, check numbers Simphony does not have, a no-show or cancelled "
                   "reservation with a check, one check on two reservations, a closed day left booked.")
    if cases:
        case_html += table(
            f"<th style='{th}'>Restaurant</th><th style='{th}'>Day</th><th style='{th}'>Time</th><th style='{th}'>Reference</th>"
            f"<th style='{thn}'>Party</th><th style='{th}'>Case</th><th style='{th}'>Check numbers</th><th style='{th}'>Served by</th>",
            "".join(f"<tr><td style='{td}'>{e(names.get(x['ref'], x['ref']))}</td><td style='{td}'>{e(x['day'])}</td>"
                    f"<td style='{td}'>{e(x['time'])}</td><td style='{td}'>{e(x['res'])}</td><td style='{tdn}'>{x['guests']}</td>"
                    f"<td style='{td}'>{e(CASE_LABEL.get(x['link'] if x['link'] in srsim.CASES else 'open', ''))}</td>"
                    f"<td style='{td};color:{muted}'>{e(', '.join(x['checks']))}</td><td style='{td};color:{muted}'>{e(x['server'])}</td></tr>"
                    for x in cs)) + more(len(cases), len(cs))
    else:
        case_html += f"<p style='font-size:12px;color:{green};margin:6px 0 0'>No case: every seated table has its check.</p>"
    low = [f for f in p.get("feedback") or [] if f.get("overall") is not None and f["overall"] <= 2]
    low_html = ""
    if low:
        ls = lim(low)
        low_html = h3("Rated 1 or 2", len(low)) + table(
            f"<th style='{th}'>Restaurant</th><th style='{th}'>Reservation day</th><th style='{thn}'>Overall</th>"
            f"<th style='{thn}'>Food</th><th style='{thn}'>Service</th><th style='{th}'>Reference</th><th style='{th}'>Served by</th>",
            "".join(f"<tr><td style='{td}'>{e(names.get(f['ref'], f['ref']))}</td><td style='{td}'>{e(f['day'])}</td>"
                    f"<td style='{tdn};color:{red}'><b>{_n(f['overall'])}</b></td><td style='{tdn}'>{_n(f['food'])}</td>"
                    f"<td style='{tdn}'>{_n(f['service'])}</td><td style='{td}'>{e(f['res'])}</td>"
                    f"<td style='{td};color:{muted}'>{e(f['server'])}</td></tr>" for f in ls)) + more(len(low), len(ls))
    errs = "".join(f"<div style='background:#fdf3e2;color:{amber};padding:7px 11px;border-radius:8px;font-size:12px;margin-bottom:8px'>"
                   f"{e(x)}</div>" for x in (p.get("errors") or [])[:5])
    link = (f"<p style='margin:20px 0 0'><a href='{e(public_url)}/finance/#sevenrooms' style='background:{primary};color:#fff;"
            f"text-decoration:none;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600;display:inline-block'>"
            f"Open Sales · SevenRooms ↔ Simphony</a></p>") if public_url else ""
    body = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif;color:{ink}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ecebeb"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:960px">
<tr><td style="background:{navy};color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · RHG FINANCE</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">Reservations against Simphony</div>
<div style="font-size:12px;color:#cfe3ea;margin-top:4px">{e(p.get('label') or p['company'])} · {date.fromisoformat(y):%A %d %b %Y} · read {read:%d %b %H:%M}</div></td></tr>
<tr><td style="background:#fff;padding:20px 22px;border-radius:0 0 12px 12px">
{errs}
<h3 style="font-size:14px;color:{ink};margin:0 0 6px">Yesterday</h3>{tiles}
{loc_html}{ahead_html}{case_html}{low_html}
<p style="color:{muted};font-size:12px;margin:20px 0 0"><b>How it is read.</b> SevenRooms' reservations by reservation date, against the Simphony checks whose numbers SevenRooms holds for them (the same business day). Covers: the guests who came (the party when SevenRooms has no count). No-show rate: no-shows over seated and no-shows. Per cover: what Simphony closed on the reservations' checks over their covers, and the rest of the day's sales over the rest of the guests. 7 days: {e(p['range']['from'])} to {e(y)}; over the 7 days {K['seated_covers']:,} covers seated, {K['no_show']:,} no-shows.</p>
<p style="color:{muted};font-size:12px;margin:8px 0 0"><b>The attached Excel file holds every line</b>: restaurants, days, reservations, guests and value, tables and hours, staff, the next 14 days, the feedback.</p>
{link}
<p style="color:{muted};font-size:11px;margin:22px 0 0;border-top:1px solid {line};padding-top:12px">Sent by RHG Control Towers from SevenRooms and Simphony; nothing was changed there, and no guest's name, phone or email is read. Recipients are in Settings → Email recipients by company (Reservations against Simphony · daily); the time in Finance → Settings → SevenRooms daily email.</p>
</td></tr></table></td></tr></table></body></html>"""
    text = (f"RHG Finance · Reservations against Simphony · {p.get('label') or p['company']} · {y}\n"
            f"{t.get('res', 0)} reservations, {t.get('covers', 0)} covers seated, {t.get('no_show', 0)} no-shows, "
            f"Simphony guests {t.get('sim_guests', 0)}, {y_cases} cases.\n"
            + ("Today: " + ", ".join(f"{names.get(x['ref'], x['ref'])} {x['covers']} covers booked" for x in today) + "\n" if today else "")
            + "\n".join(f"- {names.get(x['ref'], x['ref'])} {x['day']} {x['time']} {x['res']}: "
                        f"{CASE_LABEL.get(x['link'] if x['link'] in srsim.CASES else 'open', '')}" for x in cases[:200])
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
    def __init__(self, currency_of, latest_fn):
        self.currency_of, self.latest_fn = currency_of, latest_fn
        self._jobs, self._jobs_lock = {}, threading.Lock()

    @staticmethod
    def settings():
        s = dict(store.get_settings().get("sevenrooms_report") or {})
        s.setdefault("time", DEFAULT_TIME)
        s.setdefault("days", "daily")
        return s

    def companies(self, s=None):
        s = s or self.settings()
        named = [c.strip() for c in (s.get("companies") or "").split(",") if c.strip()]
        if named:
            return named
        try:
            return srsim.companies()
        except Exception:                                          # noqa: BLE001
            traceback.print_exc()
            return []

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
        frm, to = srsim.default_range()
        r = srsim.build(company, frm, to, self.latest_fn)
        srsim.remember(company, r)                       # the screen shows what the email read
        return r

    def view(self, r, company, location=None):
        raw = srsim.cut(r, {location.upper()}) if location else r
        p = srsim.public(raw)
        p.update(currency=self.currency_of(company), label=f"{company} · {location}" if location else company)
        return raw, p

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
                raise RuntimeError("No recipient is set: Settings → Email recipients by company (Reservations against "
                                   "Simphony · daily) or Finance → Settings → SevenRooms daily email.")
            r = r or self.build(company)
            raw, p = self.view(r, company, location)
            if not p["locations"]:
                entry.update(status="skipped", detail="No SevenRooms venue for it.")
                _log(entry)
                return entry
            body, text = render(p, (config.PUBLIC_URL or "").rstrip("/"), self._lines())
            name = f"RHG_{p['label'].replace(' · ', '_').replace(' ', '_')}_sevenrooms_simphony_{yesterday_of(p)}.xlsx"
            arap_notify.send_mail(rec["to"], rec["cc"], subject(p, test), body, text, [(name, XLSX, srsim.render_xlsx(raw))],
                                  source="Finance · SevenRooms ↔ Simphony")
            t = day_totals(p, yesterday_of(p))
            entry.update(status="sent", to=rec["to"], cc=rec["cc"], read_at=p["checked_at"],
                         detail=f"{t.get('res', 0)} reservations · {t.get('covers', 0)} covers · {t.get('no_show', 0)} no-shows")
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            entry.update(status="failed", detail=str(exc)[:300])
        _log(entry)
        store.audit("email", f"sevenrooms daily · {entry['company']}", actor, {},
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
                _raw, p = self.view(r, company)
                job["html"] = render(p, (config.PUBLIC_URL or "").rstrip("/"), self._lines())[0]
                if only_to:
                    res = self.send(company, r, only_to, actor, test=True)
                    if res["status"] != "sent":
                        raise RuntimeError(res.get("detail") or "not sent")
                    job["detail"] = "Sent to " + ", ".join(only_to)
                else:
                    t = day_totals(p, yesterday_of(p))
                    job["detail"] = f"{t.get('res', 0)} reservations · {t.get('covers', 0)} covers seated yesterday"
                job["status"] = "done"
            except Exception as exc:                               # noqa: BLE001
                traceback.print_exc()
                job.update(status="failed", detail=str(exc)[:300])
        threading.Thread(target=go, daemon=True, name="sevenrooms-report-job").start()
        return {k: v for k, v in job.items() if k != "html"}

    def job(self, job_id, actor):
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            return j if j and j["actor"] == actor else None

    # ---- the daily schedule
    @staticmethod
    def _when(s):
        hh, _, mm = (s.get("time") or DEFAULT_TIME).partition(":")
        return DAYS.get(s.get("days") or "daily", DAYS["daily"]), int(hh or 10), int(mm or 0)

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
        """The day to send, when it is time: from the time set until six hours later, once a day, three tries at
        most half an hour apart."""
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
                if all(x["status"] in ("sent", "skipped") for x in results):
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
        threading.Thread(target=self.loop, daemon=True, name="sevenrooms-report").start()


def status(rep):
    return {"settings": rep.settings(), "companies": rep.companies(), "recipients": rep.recipients(),
            "next_run": rep.next_run(), "runs": recent_runs(), "mail": arap_notify.mail_status(),
            "connected": srsim.sevenrooms.info()["configured"]}
