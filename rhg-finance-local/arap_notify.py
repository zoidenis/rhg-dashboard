"""Daily email: exceptions in the receivables and payables control sections.

What it holds, per company:
- Receivables and payables at a glance: open, overdue, beyond the critical limit,
  and the ageing buckets, as the Finance tower shows them.
- The exceptions of those two sections (overdue beyond the critical limit, supplier
  accounts in debit, concentration on one party), grouped by customer or supplier,
  with what is new since the last email marked.

The tower does not hold a mail server of its own. The email is handed to the
Purchasing tower over the internal network (shared service token), which sends it
through the mail server set up in Purchasing → Settings. Recipients, time and
companies are set in Finance → Settings → Receivables & payables email.
Business Central is only read.
"""
import html
import json
import re
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta

import requests

import auth
import config
import finance
import store

STATE_FILE = store.DATA_DIR / "arap_notify.json"
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
MAX_GROUPS = 40
MAX_DOCS = 60
_state_lock = threading.Lock()
_run_lock = threading.Lock()


def parse_emails(text):
    good, bad = [], []
    for part in re.split(r"[,;\s]+", text or ""):
        part = part.strip()
        if part:
            (good if EMAIL_RE.match(part) else bad).append(part)
    return good, bad


# ---------------------------------------------------------------- state
def _read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(state):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    tmp.replace(STATE_FILE)


def recent_runs(limit=20):
    with _state_lock:
        return list(reversed(_read_state().get("runs", [])))[:limit]


def _log_run(entry):
    with _state_lock:
        state = _read_state()
        state["runs"] = (state.get("runs", []) + [entry])[-200:]
        _write_state(state)


# ---------------------------------------------------------------- mail, through Purchasing
_mail_status = {"at": 0, "value": None}


def mail_status():
    """Is the Purchasing mail server ready, and which address sends."""
    if time.time() - _mail_status["at"] < 60 and _mail_status["value"]:
        return _mail_status["value"]
    try:
        r = requests.get(f"{config.PURCHASING_URL}/internal/mail/status", timeout=10,
                         headers={"X-Service-Token": auth.SERVICE_TOKEN})
        value = r.json() if r.ok else {"configured": False, "error": f"Purchasing answered {r.status_code}."}
    except (requests.RequestException, ValueError) as exc:
        value = {"configured": False, "error": f"Purchasing could not be reached: {str(exc)[:160]}"}
    _mail_status.update(at=time.time(), value=value)
    return value


def send_mail(to, cc, subject, html_body, text_body):
    try:
        r = requests.post(f"{config.PURCHASING_URL}/internal/mail", timeout=90,
                          headers={"X-Service-Token": auth.SERVICE_TOKEN, "Content-Type": "application/json"},
                          data=json.dumps({"to": to, "cc": cc, "subject": subject, "html": html_body,
                                           "text": text_body, "source": "Finance · receivables & payables"}))
    except requests.RequestException as exc:
        raise RuntimeError(f"The Purchasing tower, which sends the email, could not be reached: {exc}") from exc
    try:
        j = r.json()
    except ValueError:
        j = {}
    if not r.ok:
        raise RuntimeError(j.get("error") or f"The Purchasing tower answered {r.status_code}.")
    return j


# ---------------------------------------------------------------- the digest
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


def build_digest(company, vendor_rows, customer_rows, settings, currency, seen, today):
    ap = finance.payables(vendor_rows, today, settings)
    ar = finance.receivables(customer_rows, today, settings)
    exceptions = ar["exceptions"] + ap["exceptions"]
    groups = {}
    for e in exceptions:
        key = (e.get("area"), e.get("rule"), e.get("party") or e.get("title"))
        g = groups.setdefault(key, {"area": e.get("area"), "rule": e.get("rule"), "party": key[2],
                                    "severity": e.get("severity"), "count": 0, "impact": 0.0,
                                    "oldest": None, "new": 0, "documents": [], "next_action": e.get("next_action"),
                                    "title": e.get("title"), "detail": e.get("detail")})
        g["count"] += 1
        g["impact"] += abs(e.get("impact") or 0)
        if e.get("days") is not None:
            g["oldest"] = max(g["oldest"] or 0, e["days"])
        is_new = e["code"] not in seen
        g["new"] += 1 if is_new else 0
        if e.get("document"):
            g["documents"].append({"document": e["document"], "due": e.get("due"), "days": e.get("days"),
                                   "amount": abs(e.get("impact") or 0), "new": is_new})
        if SEVERITY_RANK.get(e.get("severity"), 3) < SEVERITY_RANK.get(g["severity"], 3):
            g["severity"] = e.get("severity")
    rows = sorted(groups.values(), key=lambda g: (SEVERITY_RANK.get(g["severity"], 3), g["area"] != "AR",
                                                  -g["impact"]))
    for g in rows:
        g["documents"].sort(key=lambda d: -(d["days"] or 0))
    th = settings["thresholds"]

    def beyond(section, limit):
        return sum(x["remaining"] for x in section["detail"] if x["days"] > limit and x["remaining"] > 0)
    new_codes = [e["code"] for e in exceptions if e["code"] not in seen]
    return {
        "company": company, "today": today.isoformat(), "currency": currency,
        "ar": {"total": ar["total"], "overdue": ar["overdue"], "buckets": ar["buckets"],
               "beyond": beyond(ar, th["ar_critical_days"]), "limit": th["ar_critical_days"],
               "concentration": ar["concentration"], "top": ar["parties"][:5]},
        "ap": {"total": ap["total"], "overdue": ap["overdue"], "buckets": ap["buckets"],
               "beyond": beyond(ap, th["ap_critical_days"]), "limit": th["ap_critical_days"],
               "concentration": ap["concentration"], "top": ap["parties"][:5],
               "debit": sum(-x["remaining"] for x in ap["detail"] if x["remaining"] < 0)},
        "groups": rows, "exceptions": len(exceptions), "new": len(new_codes),
        "codes": [e["code"] for e in exceptions],
        "materiality": th["materiality"],
    }


def _m(v):
    return f"{v:,.0f}"


def subject(d, test=False):
    crit = sum(g["count"] for g in d["groups"] if g["severity"] == "critical")
    parts = [f"[RHG Finance] {d['company']} · Receivables & payables",
             f"{d['exceptions']} exception{'s' if d['exceptions'] != 1 else ''}"
             + (f" ({d['new']} new)" if d["new"] else ""),
             f"overdue AR {_m(d['ar']['overdue'])} / AP {_m(d['ap']['overdue'])} {d['currency']}"]
    if crit:
        parts.insert(2, f"{crit} critical")
    return ("[Test] " if test else "") + " · ".join(parts)


def render(d, test=False):
    e = html.escape
    navy, primary, ink, muted, line, sky = "#08303f", "#0c475e", "#0e2733", "#5c7482", "#d9dcdd", "#e4f3f9"
    red, redbg, amber, amberbg = "#c0392b", "#fbecea", "#b26a00", "#fdf3e2"
    cur = d["currency"]
    link = f"{config.PUBLIC_URL.rstrip('/')}/finance/#exceptions" if config.PUBLIC_URL else ""
    th = (f"text-align:left;padding:8px 10px;background:{sky};color:{ink};font-size:12px;font-weight:600;"
          f"border-bottom:1px solid {line}")
    thn = th.replace("text-align:left", "text-align:right")
    td = f"padding:8px 10px;border-bottom:1px solid #f0f1f2;font-size:13px;color:{ink};vertical-align:top"
    tdn = td + ";text-align:right;white-space:nowrap"
    tdm = td + f";color:{muted};font-size:12px"
    table_open = (f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;"
                  f"border-collapse:collapse;border:1px solid {line}'>")

    def pill(sev):
        fg, bg, label = (red, redbg, "Critical") if sev == "critical" else (amber, amberbg, "Warning")
        return (f"<span style='display:inline-block;background:{bg};color:{fg};font-size:11px;font-weight:600;"
                f"padding:2px 6px;border-radius:5px'>{label}</span>")

    def kpi(label, value, sub, warn=False):
        return (f"<td style='padding:12px 14px;background:#fff;border:1px solid {line};border-radius:10px;"
                f"width:25%;vertical-align:top'><div style='font-size:12px;color:{muted}'>{e(label)}</div>"
                f"<div style='font-size:20px;font-weight:700;color:{red if warn else ink};margin:4px 0'>"
                f"{e(value)}</div><div style='font-size:11px;color:{muted}'>{e(sub)}</div></td>")

    def pct(a, b):
        return f"{a / b * 100:.0f}% of open" if b else ""

    ar, ap = d["ar"], d["ap"]
    kpis = ("<table role='presentation' cellspacing='8' cellpadding='0' style='width:100%;margin:0 -8px'><tr>"
            + kpi("Receivables open", _m(ar["total"]), cur)
            + kpi("Receivables overdue", _m(ar["overdue"]), pct(ar["overdue"], ar["total"]))
            + kpi(f"Over {ar['limit']:.0f} days", _m(ar["beyond"]), "critical limit", ar["beyond"] > 0)
            + kpi("Concentration", f"{ar['concentration']:.0f}%" if ar["concentration"] is not None else "–",
                  "largest customer")
            + "</tr><tr>"
            + kpi("Payables open", _m(ap["total"]), cur)
            + kpi("Payables overdue", _m(ap["overdue"]), pct(ap["overdue"], ap["total"]))
            + kpi(f"Over {ap['limit']:.0f} days", _m(ap["beyond"]), "critical limit", ap["beyond"] > 0)
            + kpi("Suppliers in debit", _m(ap["debit"]), "owed to us: credit notes or double payments",
                  ap["debit"] > 0)
            + "</tr></table>")

    labels = [b["label"] for b in ar["buckets"]]
    ageing = (table_open + f"<tr><th style='{th}'>Ageing</th>"
              + "".join(f"<th style='{thn}'>{e(l)}</th>" for l in labels) + f"<th style='{thn}'>Total</th></tr>"
              + "".join(f"<tr><td style='{td}'><b>{name}</b></td>"
                        + "".join(f"<td style='{tdn}'>{_m(b['value'])}</td>" for b in sec["buckets"])
                        + f"<td style='{tdn}'><b>{_m(sec['total'])}</b></td></tr>"
                        for name, sec in (("Receivables", ar), ("Payables", ap)))
              + "</table>")

    rows = []
    for g in d["groups"][:MAX_GROUPS]:
        docs = ", ".join(f"{x['document']}" + (f" ({x['days']}d)" if x.get("days") else "")
                         for x in g["documents"][:6])
        more = f" and {len(g['documents']) - 6} more" if len(g["documents"]) > 6 else ""
        new = (f" <span style='color:{red};font-size:11px;font-weight:600'>{g['new']} new</span>"
               if g["new"] else "")
        what = (f"{g['count']} document{'s' if g['count'] != 1 else ''}"
                + (f", oldest {g['oldest']} days" if g.get("oldest") else "")) if g["documents"] else g["detail"]
        rows.append(f"<tr><td style='{td}'>{pill(g['severity'])}</td>"
                    f"<td style='{td}'><b>{e(g['party'])}</b>{new}<div style='color:{muted};font-size:12px'>"
                    f"{'Receivable' if g['area'] == 'AR' else 'Payable'} · {e(g['rule'])}</div>"
                    f"<div style='color:{muted};font-size:11px'>{e(what)}"
                    f"{(' · ' + e(docs + more)) if docs else ''}</div></td>"
                    f"<td style='{tdm}'>{e(g['next_action'] or '')}</td>"
                    f"<td style='{tdn}'><b>{_m(g['impact'])}</b></td></tr>")
    if len(d["groups"]) > MAX_GROUPS:
        rows.append(f"<tr><td colspan='4' style='{tdm}'>And {len(d['groups']) - MAX_GROUPS} more; "
                    f"open the tower for the full list.</td></tr>")
    exc_block = (table_open + f"<tr><th style='{th}'></th><th style='{th}'>Customer / supplier</th>"
                 f"<th style='{th}'>Next action</th><th style='{thn}'>Amount ({e(cur)})</th></tr>"
                 + "".join(rows) + "</table>") if rows else \
        f"<p style='color:{muted};font-size:13px'>No exceptions in receivables or payables today.</p>"

    def top_table(title, sec):
        if not sec["top"]:
            return ""
        return (f"<td style='width:50%;vertical-align:top;padding:0 6px'>{table_open}"
                f"<tr><th style='{th}'>{e(title)}</th><th style='{thn}'>Open</th><th style='{thn}'>Overdue</th>"
                f"<th style='{thn}'>Oldest</th></tr>"
                + "".join(f"<tr><td style='{td}'>{e(p['party'])}</td><td style='{tdn}'>{_m(p['total'])}</td>"
                          f"<td style='{tdn}'>{_m(p['overdue'])}</td>"
                          f"<td style='{tdn}'>{(str(p['oldest_days']) + ' d') if p.get('oldest_days') else '–'}</td></tr>"
                          for p in sec["top"]) + "</table></td>")
    tops = (f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;margin:0 -6px'><tr>"
            + top_table("Largest customers", ar) + top_table("Largest suppliers", ap) + "</tr></table>")

    day = date.fromisoformat(d["today"]).strftime("%A %d %B %Y")
    banner = (f"<div style='background:{redbg};color:{red};padding:8px 12px;border-radius:8px;font-size:12px;"
              f"margin-bottom:14px'>Test email. Nothing was marked as reported.</div>") if test else ""
    button = (f"<p style='margin:24px 0 0'><a href='{e(link)}' style='background:{primary};color:#fff;"
              f"text-decoration:none;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600;"
              f"display:inline-block'>Open the Finance tower</a></p>") if link else ""
    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif;color:{ink}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ecebeb"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:880px">
<tr><td style="background:{navy};color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · RHG FINANCE</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">Receivables and payables: exceptions</div>
<div style="font-size:12px;color:#cfe3ea;margin-top:4px">{e(d['company'])} · {e(day)} · amounts in {e(cur)}</div></td></tr>
<tr><td style="background:#fff;padding:22px;border-radius:0 0 12px 12px">
{banner}{kpis}
<h3 style="font-size:15px;color:{ink};margin:22px 0 4px">Exceptions</h3>
<p style="color:{muted};font-size:12px;margin:0 0 10px">The control rules of the Finance tower: receivables or payables overdue beyond the critical limit ({ar['limit']:.0f} / {ap['limit']:.0f} days) above {_m(d['materiality'])} {e(cur)}, supplier accounts in debit, and one customer or supplier holding a large share. Grouped by customer or supplier; "new" means not in the previous email.</p>
{exc_block}
<h3 style="font-size:15px;color:{ink};margin:26px 0 10px">Ageing</h3>
{ageing}
<h3 style="font-size:15px;color:{ink};margin:26px 0 10px">Largest balances</h3>
{tops}
{button}
<p style="color:{muted};font-size:11px;margin:26px 0 0;border-top:1px solid {line};padding-top:12px">Sent by RHG Control Towers. Open customer and supplier entries as Business Central holds them this morning; nothing was changed there. Recipients, time and limits are set in Finance → Settings.</p>
</td></tr></table></td></tr></table></body></html>"""

    text = [f"RHG Finance · Receivables and payables: exceptions · {d['company']} · {day}", ""]
    if test:
        text += ["TEST EMAIL. Nothing was marked as reported.", ""]
    text += [f"Receivables: open {_m(ar['total'])}, overdue {_m(ar['overdue'])}, over {ar['limit']:.0f} days "
             f"{_m(ar['beyond'])} {cur}",
             f"Payables: open {_m(ap['total'])}, overdue {_m(ap['overdue'])}, over {ap['limit']:.0f} days "
             f"{_m(ap['beyond'])}, suppliers in debit {_m(ap['debit'])} {cur}", "",
             f"Exceptions: {d['exceptions']} ({d['new']} new)"]
    for g in d["groups"][:MAX_GROUPS]:
        text.append(f"  [{g['severity']}] {g['area']} {g['party'][:40]:<40} {_m(g['impact']):>14}  "
                    f"{g['count']} doc(s){' · ' + str(g['new']) + ' new' if g['new'] else ''}")
    if link:
        text += ["", f"Open: {link}"]
    return html_body, "\n".join(text)


# ---------------------------------------------------------------- running it
class Notifier:
    def __init__(self, client, currency_of, default_company):
        self.client = client
        self.currency_of = currency_of
        self.default_company = default_company
        self._tests, self._tests_lock = {}, threading.Lock()

    @staticmethod
    def settings():
        return store.get_settings().get("arap_notify", {})

    def companies(self, s=None):
        s = s or self.settings()
        names = [c.strip() for c in (s.get("companies") or "").split(",") if c.strip()]
        return names or [self.default_company]

    def recipients(self, s=None):
        s = s or self.settings()
        to, bad_to = parse_emails(s.get("to"))
        cc, bad_cc = parse_emails(s.get("cc"))
        return {"to": to, "cc": [c for c in cc if c not in to], "invalid": bad_to + bad_cc}

    def digest(self, company, today=None):
        today = today or date.today()
        settings = store.get_settings()
        vendors = self.client.open_vendor_entries(company, max_age=60)
        customers = self.client.open_customer_entries(company, max_age=60)
        with _state_lock:
            seen = _read_state().get("seen", {}).get(company, {})
        return build_digest(company, vendors, customers, settings, self.currency_of(company, settings),
                            seen, today)

    def preview(self, company):
        return render(self.digest(company), test=True)[0]

    def send(self, company, test=False, only_to=None, actor="scheduler"):
        with _run_lock:
            entry = {"ts": datetime.now().isoformat(timespec="seconds"), "company": company,
                     "test": bool(test), "actor": actor}
            try:
                s = self.settings()
                rec = {"to": only_to, "cc": []} if only_to else self.recipients(s)
                d = self.digest(company)
                entry.update(exceptions=d["exceptions"], new=d["new"], to=rec["to"], cc=rec["cc"])
                if not rec["to"]:
                    entry.update(status="skipped", detail="No recipient is set in Settings.")
                elif not d["exceptions"] and not s.get("send_empty", True) and not test:
                    entry.update(status="skipped", detail="Nothing to report.")
                else:
                    html_body, text_body = render(d, test)
                    j = send_mail(rec["to"], rec["cc"], subject(d, test), html_body, text_body)
                    entry.update(status="sent", detail=subject(d, test), sender=j.get("from"))
                if not test and entry["status"] == "sent":
                    self._mark(company, d)
            except Exception as exc:                             # noqa: BLE001
                traceback.print_exc()
                entry.update(status="failed", detail=str(exc)[:400])
            _log_run(entry)
            store.audit("email", f"receivables & payables · {company}", actor, {},
                        {k: entry.get(k) for k in ("status", "exceptions", "new", "to", "cc", "test")},
                        entry.get("detail", ""))
            return entry

    def _mark(self, company, d):
        with _state_lock:
            state = _read_state()
            seen = state.setdefault("seen", {}).setdefault(company, {})
            for code in d["codes"]:
                seen.setdefault(code, d["today"])
            cutoff = (date.fromisoformat(d["today"]) - timedelta(days=400)).isoformat()
            state["seen"][company] = {k: v for k, v in seen.items() if v >= cutoff}
            _write_state(state)

    # ---- tests run in the background: BC and SMTP must not hold a request open
    def start_test(self, company, only_to, actor):
        with self._tests_lock:
            for j in self._tests.values():
                if j["actor"] == actor and j["status"] == "running":
                    return dict(j)
            job = {"id": uuid.uuid4().hex, "actor": actor, "company": company, "to": only_to,
                   "status": "running", "result": None, "started": datetime.now().isoformat(timespec="seconds")}
            self._tests = {k: v for k, v in list(self._tests.items())[-20:]}
            self._tests[job["id"]] = job

        def run():
            result = self.send(company, test=True, only_to=only_to, actor=actor)
            with self._tests_lock:
                job.update(status=result["status"], result=result)
        threading.Thread(target=run, daemon=True, name="arap-test").start()
        return dict(job)

    def test_status(self, actor):
        with self._tests_lock:
            mine = [j for j in self._tests.values() if j["actor"] == actor]
            return dict(mine[-1]) if mine else None

    # ---- the daily schedule
    @staticmethod
    def _target(now, s):
        hh, _, mm = (s.get("time") or "08:00").partition(":")
        try:
            return now.replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)
        except ValueError:
            return None

    def next_run(self, now=None):
        s = self.settings()
        if not s.get("enabled"):
            return None
        now = now or datetime.now()
        target = self._target(now, s)
        if target is None:
            return None
        with _state_lock:
            last = _read_state().get("last_daily")
        if last == now.date().isoformat() or target + timedelta(hours=6) < now:
            target += timedelta(days=1)
        while s.get("working_days_only", True) and target.weekday() >= 5:
            target += timedelta(days=1)
        return target.isoformat(timespec="minutes")

    def due(self, now=None):
        s = self.settings()
        if not s.get("enabled"):
            return False
        now = now or datetime.now()
        if s.get("working_days_only", True) and now.weekday() >= 5:
            return False
        target = self._target(now, s)
        if target is None or now < target or now > target + timedelta(hours=6):
            return False
        with _state_lock:
            state = _read_state()
        today = now.date().isoformat()
        if state.get("last_daily") == today:
            return False
        tries = state.get("tries", {})
        if tries.get("day") == today and (tries.get("count", 0) >= 3 or time.time() - tries.get("at", 0) < 600):
            return False
        return True

    def run_daily(self):
        today = date.today().isoformat()
        with _state_lock:
            done = _read_state().get("done", {})
        already = set(done.get("companies", [])) if done.get("day") == today else set()
        results = [self.send(c) for c in self.companies() if c not in already]
        with _state_lock:
            state = _read_state()
            state["done"] = {"day": today, "companies": sorted(already | {r["company"] for r in results
                                                                          if r["status"] != "failed"})}
            if any(r["status"] == "failed" for r in results):
                tries = state.get("tries", {})
                count = tries.get("count", 0) + 1 if tries.get("day") == today else 1
                state["tries"] = {"day": today, "count": count, "at": time.time()}
                if count >= 3:
                    state["last_daily"] = today
            else:
                state["last_daily"] = today
                state.pop("tries", None)
            _write_state(state)
        return results

    def loop(self):
        while True:
            try:
                if self.due():
                    self.run_daily()
            except Exception:                                    # noqa: BLE001
                traceback.print_exc()
            time.sleep(30)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="arap-notifier").start()
