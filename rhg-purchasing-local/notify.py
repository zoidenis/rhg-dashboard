"""Email notices sent by the Purchasing tower.

The first notice: invoices posted in Business Central without a purchase order,
sent once a day to the people named in Settings → Email notifications.

What each email holds:
- New since the last notice: invoices the tower has not reported before.
- Still open: invoices already reported that nobody has closed yet. An invoice
  counts as closed when its tracked action in the Action center is Resolved or
  Dismissed; nothing is written back to Business Central.

The mail server can be entered in Settings and is kept in a private data file.
The .env values remain defaults until someone saves the form. Recipients, the
time and the window are application settings too.
"""
import html
import json
import re
import smtplib
import ssl
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import config
import currency
import smtp_config
import store

STATE_FILE = store.DATA_DIR / "notifications.json"
_state_lock = threading.Lock()
_run_lock = threading.Lock()

RULE = "invoices_without_po"
ROLES = [("coordinator", "Purchasing Coordinator", "To"),
         ("manager", "Purchasing Manager", "Cc"),
         ("cost_controller", "Cost Controller", "Cc")]
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
MAX_ROWS = 60          # rows per table in one email; the rest are counted and linked


# ---------------------------------------------------------------- mail server

def smtp_info():
    return smtp_config.info()


def parse_emails(text):
    out, bad = [], []
    for part in re.split(r"[,;\s]+", text or ""):
        part = part.strip()
        if not part:
            continue
        (out if EMAIL_RE.match(part) else bad).append(part)
    return out, bad


def send_mail(to, cc, subject, html_body, text_body):
    smtp = smtp_config.current()
    if not smtp_config.ready(smtp):
        raise RuntimeError("Complete the mail server settings before sending email.")
    msg = EmailMessage()
    msg["From"] = smtp["from"]
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(smtp["from"].split("@")[-1] or "rhg.al").strip(">"))
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    if smtp["security"] == "ssl":
        server = smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=30, context=context)
    else:
        server = smtplib.SMTP(smtp["host"], smtp["port"], timeout=30)
    try:
        server.ehlo()
        if smtp["security"] == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if smtp["user"]:
            server.login(smtp["user"], smtp["password"])
        server.send_message(msg, to_addrs=list(dict.fromkeys(to + cc)))
    finally:
        try:
            server.quit()
        except Exception:                                        # noqa: BLE001
            pass


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


def recent_runs(limit=30):
    with _state_lock:
        return list(reversed(_read_state().get("runs", [])))[:limit]


def _log_run(entry):
    with _state_lock:
        state = _read_state()
        runs = state.setdefault("runs", [])
        runs.append(entry)
        state["runs"] = runs[-200:]
        _write_state(state)


# ---------------------------------------------------------------- the digest

def action_key(company, invoice):
    return f"nopo-{company}-{invoice}"


def owner_for(inv):
    cat = (inv.get("category") or "").upper()
    if inv.get("state") == "No order" and ("SHERBIME" in cat or "INVESTIME" in cat):
        return "Purchasing Manager"
    return "Purchasing Coordinator"


def build_digest(company, cpl, seen, actions, today, settings, first_run):
    """Splits the invoices without an order into new and still open.

    cpl is compliance.build() over the reminder window; seen maps invoice number to
    the day it was first reported; actions is the Action center list.
    """
    by_key = {}
    for a in actions:
        if a.get("source_exception", "").startswith("nopo-"):
            by_key.setdefault(a["source_exception"], a)
    yesterday = (today - timedelta(days=1)).isoformat()
    new, still = [], []
    for inv in cpl.get("invoices", []):
        act = by_key.get(action_key(company, inv["invoice"]))
        if act and act.get("status") in ("Resolved", "Dismissed"):
            continue
        row = dict(inv, owner=owner_for(inv),
                   tracked=(act or {}).get("status") or "", tracked_owner=(act or {}).get("owner") or "")
        try:
            row["days"] = (today - date.fromisoformat(inv["posted"])).days
        except ValueError:
            row["days"] = None
        first_seen = seen.get(inv["invoice"])
        is_new = (inv["posted"] >= yesterday) if first_run else not first_seen
        if is_new:
            new.append(row)
        else:
            row["first_reported"] = first_seen or ""
            still.append(row)
    new.sort(key=lambda r: -r["unlinked_value"])
    still.sort(key=lambda r: (-(r["days"] or 0), -r["unlinked_value"]))
    k = cpl.get("kpis", {})
    return {
        "company": company, "today": today.isoformat(),
        "window_days": int(settings.get("lookback_days") or 30),
        "new": new, "still": still,
        "new_value": sum(r["unlinked_value"] for r in new),
        "still_value": sum(r["unlinked_value"] for r in still),
        "window": {"invoices": k.get("invoices", 0), "breaches": k.get("no_order", 0) + k.get("partial", 0),
                   "rate": k.get("rate", 0), "value": k.get("value_breach", 0)},
        "currency": currency.code(),
    }


def _money(v):
    return f"{v:,.0f}"


def _subject(d, test=False):
    parts = [f"[RHG Purchasing] {d['company']}"]
    if d["new"]:
        parts.append(f"{len(d['new'])} new invoice{'s' if len(d['new']) != 1 else ''} without a PO "
                     f"({_money(d['new_value'])} {d['currency']})")
    else:
        parts.append("no new invoices without a PO")
    if d["still"]:
        parts.append(f"{len(d['still'])} still open")
    return ("[Test] " if test else "") + " · ".join(parts)


def render_email(d, recipients, test=False):
    """HTML (inline styles, for mail clients) and plain-text versions of one digest."""
    e = html.escape
    public_url = smtp_config.current()["public_url"]
    link = f"{public_url.rstrip('/')}/purchasing/#compliance" if public_url else ""
    navy, primary, ink, muted, line, sky = "#08303f", "#0c475e", "#0e2733", "#5c7482", "#d9dcdd", "#e4f3f9"
    red, redbg = "#c0392b", "#fbecea"
    th = (f"text-align:left;padding:8px 10px;background:{sky};color:{ink};font-size:12px;"
          f"font-weight:600;border-bottom:1px solid {line}")
    thn = th.replace("text-align:left", "text-align:right")
    td = f"padding:8px 10px;border-bottom:1px solid #f0f1f2;font-size:13px;color:{ink};vertical-align:top"
    tdn = td + ";text-align:right;white-space:nowrap"
    tdm = td + f";color:{muted};font-size:12px"

    def what(r):
        if r.get("state") == "Partly without an order":
            return "Lines with no order" + (f" ({', '.join(r.get('line_types') or [])})"
                                            if r.get("line_types") else "")
        return "No order on the invoice"

    def table(rows, still=False):
        head = (f"<tr><th style='{th}'>Invoice</th><th style='{th}'>Supplier</th>"
                f"<th style='{th}'>Posted</th><th style='{th}'>Unit · category</th>"
                f"<th style='{th}'>{'Status' if still else 'What is missing'}</th>"
                f"<th style='{th}'>Owner</th><th style='{thn}'>Value</th></tr>")
        body = []
        for r in rows[:MAX_ROWS]:
            ref = f"<div style='color:{muted};font-size:11px'>supplier ref {e(r['vendor_invoice'])}</div>" \
                if r.get("vendor_invoice") else ""
            ago = {0: "today", 1: "yesterday"}.get(r.get("days"), f"{r.get('days')} days ago")
            posted = e(r["posted"]) + (f"<div style='color:{red};font-size:11px'>{ago}</div>"
                                       if still and r.get("days") is not None else "")
            status = (f"Tracked · {e(r['tracked'])}" if r.get("tracked") else "Not tracked yet") if still \
                else e(what(r))
            owner = e(r.get("tracked_owner") or r["owner"])
            body.append(f"<tr><td style='{td}'><b>{e(r['invoice'])}</b>{ref}</td>"
                        f"<td style='{td}'>{e(r['vendor'])}</td><td style='{tdm};white-space:nowrap'>{posted}</td>"
                        f"<td style='{tdm}'>{e(r['unit'])}<br>{e(r['category'])}</td>"
                        f"<td style='{tdm}'>{status}</td><td style='{tdm}'>{owner}</td>"
                        f"<td style='{tdn}'><b>{_money(r['unlinked_value'])}</b></td></tr>")
        more = ""
        if len(rows) > MAX_ROWS:
            more = (f"<tr><td colspan='7' style='{tdm}'>And {len(rows) - MAX_ROWS} more, "
                    f"{_money(sum(r['unlinked_value'] for r in rows[MAX_ROWS:]))} {d['currency']}. "
                    f"Open the tower for the full list.</td></tr>")
        total = (f"<tr><td colspan='6' style='{td};font-weight:600'>Total</td>"
                 f"<td style='{tdn}'><b>{_money(sum(r['unlinked_value'] for r in rows))}</b></td></tr>")
        return (f"<table role='presentation' cellspacing='0' cellpadding='0' "
                f"style='width:100%;border-collapse:collapse;border:1px solid {line};border-radius:8px'>"
                f"{head}{''.join(body)}{more}{total}</table>")

    def kpi(label, value, sub):
        return (f"<td style='padding:14px 16px;background:#fff;border:1px solid {line};border-radius:10px;"
                f"width:33%;vertical-align:top'><div style='font-size:12px;color:{muted}'>{e(label)}</div>"
                f"<div style='font-size:22px;font-weight:700;color:{ink};margin:4px 0'>{e(value)}</div>"
                f"<div style='font-size:11px;color:{muted}'>{e(sub)}</div></td>")

    w = d["window"]
    day = datetime.fromisoformat(d["today"]).strftime("%A %d %B %Y")
    kpis = ("<table role='presentation' cellspacing='8' cellpadding='0' style='width:100%;margin:0 -8px'><tr>"
            + kpi("New since the last notice", str(len(d["new"])), f"{_money(d['new_value'])} {d['currency']}")
            + kpi("Earlier, still open", str(len(d["still"])), f"{_money(d['still_value'])} {d['currency']}, "
                                                                f"not closed yet")
            + kpi(f"Last {d['window_days']} days", f"{w['rate']:.1f}%",
                  f"{w['breaches']} of {w['invoices']} posted invoices had no order")
            + "</tr></table>")
    new_block = (table(d["new"]) if d["new"] else
                 f"<p style='color:{muted};font-size:13px'>No invoice without an order was posted since the "
                 f"last notice.</p>")
    still_block = ""
    if d["still"]:
        still_block = (f"<h3 style='font-size:15px;color:{ink};margin:26px 0 4px'>Earlier, still open</h3>"
                       f"<p style='color:{muted};font-size:12px;margin:0 0 10px'>Posted before the last notice "
                       f"in the last {d['window_days']} days and not yet closed. Track each one in the Action center and mark it Resolved once the approval is "
                       f"recorded; it then leaves this list.</p>" + table(d["still"], still=True))
    button = (f"<p style='margin:24px 0 0'><a href='{e(link)}' style='background:{primary};color:#fff;"
              f"text-decoration:none;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600;"
              f"display:inline-block'>Open Invoices without PO</a></p>") if link else ""
    banner = (f"<div style='background:{redbg};color:{red};padding:8px 12px;border-radius:8px;font-size:12px;"
              f"margin-bottom:14px'>Test email. Nothing was marked as reported.</div>") if test else ""
    to_line = ", ".join(f"{label}" for label in recipients.get("roles", []))
    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif;color:{ink}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ecebeb"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:860px">
<tr><td style="background:{navy};color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · RHG PURCHASING</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">Invoices without a purchase order</div>
<div style="font-size:12px;color:#cfe3ea;margin-top:4px">{e(d['company'])} · {e(day)}</div></td></tr>
<tr><td style="background:#fff;padding:22px;border-radius:0 0 12px 12px">
{banner}{kpis}
<h3 style="font-size:15px;color:{ink};margin:22px 0 4px">New since the last notice</h3>
<p style="color:{muted};font-size:12px;margin:0 0 10px">RHG's rule is that a purchase has an approved order before it happens. These invoices were posted in Business Central with no order number, so the approval cannot be traced. Amounts exclude VAT; for an invoice that came from an order, only the lines with no order are counted.</p>
{new_block}{still_block}{button}
<p style="color:{muted};font-size:11px;margin:26px 0 0;border-top:1px solid {line};padding-top:12px">Sent by RHG Control Towers to {e(to_line or 'the configured recipients')}. Recipients and time are set in Purchasing → Settings → Email notifications. Business Central is read-only: nothing was changed there.</p>
</td></tr></table></td></tr></table></body></html>"""

    def text_rows(rows):
        return "\n".join(f"  {r['invoice']:<10} {r['posted']}  {r['vendor'][:38]:<38} "
                         f"{_money(r['unlinked_value']):>14}  {r['unit']}" for r in rows[:MAX_ROWS])
    text = [f"RHG Purchasing · Invoices without a purchase order · {d['company']} · {day}", ""]
    if test:
        text += ["TEST EMAIL. Nothing was marked as reported.", ""]
    text += [f"New since the last notice: {len(d['new'])} · {_money(d['new_value'])} {d['currency']}",
             text_rows(d["new"]) or "  None.", "",
             f"Earlier, still open: {len(d['still'])} · {_money(d['still_value'])} {d['currency']}",
             text_rows(d["still"]) or "  None.", "",
             f"Last {d['window_days']} days: {w['breaches']} of {w['invoices']} posted invoices had no order "
             f"({w['rate']:.1f}%)."]
    if link:
        text += ["", f"Open: {link}"]
    return html_body, "\n".join(text)


# ---------------------------------------------------------------- running it

class Notifier:
    """Owns the daily schedule. The server supplies how to read the invoices."""

    def __init__(self, read_window, default_company):
        self.read_window = read_window          # (company, date_from, date_to) -> compliance.build()
        self.default_company = default_company

    def settings(self):
        return store.get_settings().get("notifications", {})

    def companies(self, s=None):
        s = s or self.settings()
        names = [c.strip() for c in (s.get("companies") or "").split(",") if c.strip()]
        return names or [self.default_company]

    def recipients(self, s=None):
        s = s or self.settings()
        to, cc, roles, bad = [], [], [], []
        for key, label, kind in ROLES:
            good, wrong = parse_emails(s.get(key, ""))
            bad += wrong
            if good:
                roles.append(label)
                (to if kind == "To" else cc).extend(good)
        if not to and cc:                       # nobody in To: the first Cc moves up
            to, cc = cc[:1], cc[1:]
        cc = [c for c in cc if c not in to]
        return {"to": to, "cc": cc, "roles": roles, "invalid": bad}

    def digest(self, company, today=None, test=False):
        today = today or date.today()
        s = self.settings()
        days = max(1, min(120, int(float(s.get("lookback_days") or 30))))
        cpl = self.read_window(company, today - timedelta(days=days), today)
        if not cpl.get("available", True):
            raise RuntimeError(cpl.get("reason") or cpl.get("error") or "Business Central could not be read.")
        with _state_lock:
            state = _read_state()
        comp = state.get("seen", {}).get(company)
        first_run = comp is None
        d = build_digest(company, cpl, comp or {}, store.list_actions(), today, s, first_run)
        d["first_run"] = first_run
        return d

    def preview(self, company):
        d = self.digest(company, test=True)
        html_body, _ = render_email(d, self.recipients(), test=True)
        return html_body

    def send(self, company, test=False, only_to=None, actor="scheduler"):
        """Builds and sends one company's notice. A test leaves the reported list as it was."""
        with _run_lock:
            entry = {"ts": datetime.now().isoformat(timespec="seconds"), "rule": RULE, "company": company,
                     "test": bool(test), "actor": actor}
            try:
                s = self.settings()
                rec = self.recipients(s)
                if only_to:
                    rec = {"to": only_to, "cc": [], "roles": ["a test recipient"], "invalid": []}
                d = self.digest(company, test=test)
                entry.update({"new": len(d["new"]), "still": len(d["still"]),
                              "new_value": d["new_value"], "still_value": d["still_value"],
                              "to": rec["to"], "cc": rec["cc"]})
                if not rec["to"]:
                    entry.update(status="skipped", detail="No recipient email is set in Settings.")
                elif not d["new"] and not d["still"] and not s.get("send_empty") and not test:
                    entry.update(status="skipped", detail="Nothing to report.")
                else:
                    html_body, text_body = render_email(d, rec, test=test)
                    send_mail(rec["to"], rec["cc"], _subject(d, test), html_body, text_body)
                    entry.update(status="sent", detail=_subject(d, test))
                if not test and entry["status"] in ("sent", "skipped") and rec["to"]:
                    self._mark_reported(company, d)
            except Exception as exc:                             # noqa: BLE001
                traceback.print_exc()
                entry.update(status="failed", detail=str(exc)[:400])
            _log_run(entry)
            store.audit("notification", f"{RULE} · {company}", actor, {},
                        {k: entry.get(k) for k in ("status", "new", "still", "to", "cc", "test")},
                        entry.get("detail", ""))
            print(f"[{datetime.now():%H:%M:%S}] notice {RULE} {company}: {entry['status']} "
                  f"{entry.get('detail', '')}")
            return entry

    def _mark_reported(self, company, d):
        with _state_lock:
            state = _read_state()
            seen = state.setdefault("seen", {}).setdefault(company, {})
            for r in d["new"] + d["still"]:
                seen.setdefault(r["invoice"], d["today"])
            cutoff = (date.fromisoformat(d["today"]) - timedelta(days=180)).isoformat()
            state["seen"][company] = {k: v for k, v in seen.items() if v >= cutoff}
            _write_state(state)

    # ---- the daily schedule ----

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
        while s.get("working_days_only") and target.weekday() >= 5:
            target += timedelta(days=1)
        return target.isoformat(timespec="minutes")

    @staticmethod
    def _target(now, s):
        hh, _, mm = (s.get("time") or "08:00").partition(":")
        try:
            return now.replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)
        except ValueError:
            return None

    def due(self, now=None):
        """True when today's notice should go out now."""
        s = self.settings()
        if not s.get("enabled"):
            return False
        now = now or datetime.now()
        if s.get("working_days_only") and now.weekday() >= 5:
            return False
        target = self._target(now, s)
        if target is None or now < target or now > target + timedelta(hours=6):
            return False                       # a restart late in the day waits for tomorrow
        with _state_lock:
            state = _read_state()
        today = now.date().isoformat()
        if state.get("last_daily") == today:
            return False
        tries = state.get("tries", {})
        if tries.get("day") == today:
            if tries.get("count", 0) >= 3:
                return False
            if time.time() - tries.get("at", 0) < 600:
                return False                   # failed a moment ago: try again in ten minutes
        return True

    def run_daily(self):
        today = date.today().isoformat()
        with _state_lock:
            done = _read_state().get("done", {})
        already = set(done.get("companies", [])) if done.get("day") == today else set()
        results = [self.send(c) for c in self.companies() if c not in already]
        with _state_lock:
            state = _read_state()
            ok = already | {r["company"] for r in results if r["status"] != "failed"}
            state["done"] = {"day": today, "companies": sorted(ok)}
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
        threading.Thread(target=self.loop, daemon=True, name="notifier").start()
