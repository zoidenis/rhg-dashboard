"""Exceptions → Send by email: one exception, to the roles of Email recipients by company and / or addresses.

The roles and their addresses come from the directory kept in Purchasing (Settings → Email recipients by company,
recipients.py): the company's row plus the whole group's. The person sending picks roles, adds addresses, writes
a note; the email carries the exception (what, impact, what to do, the rule, period), a link to the screen and,
when the screen holds them, the supporting records as an Excel file. It goes out through Purchasing's mail server,
answers going back to the sender. Optionally the exception's item in the Exceptions register (or a new one tracked by hand) is owned by those
it was sent to. Every send is kept in data/exception_emails.json and the audit trail. Nothing touches BC.
"""
import html
import io
import json
import threading
import time

import arap_notify
import config
import recipients
import store

LOG_FILE = store.DATA_DIR / "exception_emails.json"
_lock = threading.Lock()

# who an exception goes to by default, by its area: (To roles, Cc roles)
BY_AREA = {"AP": (["finance"], ["accounting"]), "AR": (["finance"], ["accounting"]), "CASH": (["finance"], ["cfo"]),
           "G/L": (["accounting"], ["finance"]), "BALANCE SHEET": (["accounting"], ["finance"]),
           "DATA QUALITY": (["accounting"], []), "INVENTORY": (["stock_manager"], ["cost_controller"]),
           "P&L": (["cfo"], ["finance"])}
DEFAULT = (["finance"], ["accounting"])


def suggested(area):
    return BY_AREA.get(str(area or "").strip().upper(), DEFAULT)


def targets(company):
    """[{key, label, emails}] of every role, the company's addresses plus the group's; and an error, if any."""
    d = recipients.directory() or {}
    people = {str(k).strip().upper(): v for k, v in (d.get("people") or {}).items()}
    rows = [people.get(str(company or "").strip().upper()) or {}, people.get(recipients.GROUP) or {}]
    out = []
    for r in d.get("roles") or []:
        emails = list(dict.fromkeys(e for row in rows for e in recipients.parse(row.get(r["key"], ""))[0]))
        out.append({"key": r["key"], "label": r["label"], "emails": emails})
    err = "" if d else (recipients.last_error() or "The directory of Email recipients could not be read.")
    return out, err


def log(company=None):
    with _lock:
        try:
            data = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
    if company:
        data = [x for x in data if x.get("company") == company]
    by = {}
    for x in data:
        by.setdefault(x["code"], []).append({k: x.get(k) for k in ("at", "by", "to", "cc", "roles", "action")})
    return by


def _remember(entry):
    with _lock:
        try:
            data = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
        data.append(entry)
        LOG_FILE.write_text(json.dumps(data[-2000:], ensure_ascii=False, indent=1), encoding="utf-8")


def _xlsx(drill):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Supporting records"
    ws.append([drill.get("title") or "Supporting records"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    cols = drill.get("columns") or []
    ws.append(cols)
    for c in ws[3]:
        c.font = Font(bold=True)
    for r in drill.get("rows") or []:
        ws.append([r.get(c) for c in cols])
    for i, c in enumerate(cols, 1):
        ws.column_dimensions[ws.cell(3, i).column_letter].width = max(10, min(40, len(str(c)) + 4))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def compose(exc, company, period, currency, sender, note, drill_count):
    e = lambda v: html.escape(str(v or ""))
    impact = exc.get("impact")
    imp = f"{abs(float(impact)):,.0f} {currency}" if impact not in (None, "") else ""
    sev = {"critical": ("#c2412d", "Critical"), "warning": ("#b86e0e", "Warning")}.get(exc.get("severity"), ("#5f636b", "Info"))
    link = f"{config.PUBLIC_URL.rstrip('/')}/finance/#register" if config.PUBLIC_URL else ""
    rows = [("Company", company), ("Period", period), ("Area", exc.get("area")), ("Rule", exc.get("rule")),
            ("Document", exc.get("document")), ("Impact", imp)]
    subject = f"[{company}] Exception: {exc.get('title') or ''}"[:250]
    body = f"""<div style="font-family:Inter,Segoe UI,Arial,sans-serif;color:#16181d;max-width:680px">
<p style="font-size:12px;color:#0b7a4a;font-weight:700;margin:0 0 4px">RHG FINANCE · EXCEPTION</p>
<h2 style="font-size:19px;margin:0 0 10px">{e(exc.get('title'))}</h2>
<p style="margin:0 0 12px"><span style="display:inline-block;background:{sev[0]};color:#fff;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:700">{sev[1]}</span></p>
{f'<div style="background:#f5f4ef;border-left:3px solid #0c475e;padding:10px 14px;margin:0 0 14px;white-space:pre-wrap">{e(note)}</div>' if note else ''}
<p style="margin:0 0 8px">{e(exc.get('detail'))}</p>
<p style="margin:0 0 14px"><b>To do:</b> {e(exc.get('next_action'))}</p>
<table style="border-collapse:collapse;font-size:13px;margin:0 0 14px">{''.join(f'<tr><td style="padding:4px 14px 4px 0;color:#5f636b">{k}</td><td style="padding:4px 0"><b>{e(v)}</b></td></tr>' for k, v in rows if v)}</table>
{f'<p style="margin:0 0 10px">The supporting records ({drill_count}) are attached as an Excel file.</p>' if drill_count else ''}
{f'<p style="margin:0 0 14px"><a href="{e(link)}" style="color:#0c475e;font-weight:700">Open the Exceptions screen</a></p>' if link else ''}
<p style="font-size:12px;color:#5f636b;margin:18px 0 0">Sent by {e(sender)} from pulse (RHG Finance), read from Business Central. Reply to this email to answer {e(sender)}.</p></div>"""
    text = "\n".join([f"RHG Finance · exception · {company}", "", str(exc.get("title") or ""), f"Severity: {sev[1]}", ""]
                     + ([note, ""] if note else []) + [str(exc.get("detail") or ""), f"To do: {exc.get('next_action') or ''}", ""]
                     + [f"{k}: {v}" for k, v in rows if v] + ([f"\n{link}"] if link else []) + [f"\nSent by {sender} from pulse."])
    return subject, body, text


def send(exc, company, period, currency, to_roles, to_emails, cc_roles, cc_emails, note, user, drill=None,
         assign=False, on_assign=None):
    """Sends one exception; returns the log entry. Raises ValueError for a choice that cannot be sent."""
    roles, _err = targets(company)
    by_role = {r["key"]: r for r in roles}
    unknown = [k for k in list(to_roles) + list(cc_roles) if k not in by_role]
    if unknown:
        raise ValueError("Unknown role: " + ", ".join(unknown))
    empty = [by_role[k]["label"] for k in to_roles if not by_role[k]["emails"]]
    extra_to, bad_to = recipients.parse(" ".join(to_emails or []))
    extra_cc, bad_cc = recipients.parse(" ".join(cc_emails or []))
    if bad_to or bad_cc:
        raise ValueError("Not an email address: " + ", ".join(bad_to + bad_cc))
    to = list(dict.fromkeys([m for k in to_roles for m in by_role[k]["emails"]] + extra_to))
    cc = [m for m in dict.fromkeys([m for k in cc_roles for m in by_role[k]["emails"]] + extra_cc) if m not in to]
    if not to:
        raise ValueError("Nobody to send to: " + (f"no address for {', '.join(empty)} in Email recipients by company. "
                                                  if empty else "") + "Choose a role with an address or type one.")
    if len(to) + len(cc) > 20:
        raise ValueError("Twenty recipients at most.")
    sender = user.get("name") or user.get("username") or "pulse"
    reply = (user.get("email") or "").strip()
    drill_rows = (drill or {}).get("rows") or []
    subject, body, text = compose(exc, company, period, currency, sender, (note or "").strip()[:2000], len(drill_rows))
    files = [(f"Exception_{company}_{str(exc.get('code') or 'records')[:40]}.xlsx".replace("/", "-").replace(" ", "_"),
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _xlsx(drill))] if drill_rows else []
    arap_notify.send_mail(to, cc, subject, body, text, files, source="Finance · exceptions", reply_to=reply or None)
    action = None
    if assign and on_assign:            # the recipients in To own it in the Exceptions register
        action = on_assign(", ".join(to)[:300], f"Sent by email to {', '.join(to)}" + (f" (Cc {', '.join(cc)})" if cc else ""))
    entry = {"at": time.strftime("%Y-%m-%d %H:%M"), "company": company, "code": exc.get("code") or "",
             "title": exc.get("title") or "", "by": sender, "to": to, "cc": cc, "roles": list(to_roles) + list(cc_roles),
             "action": action, "records": len(drill_rows)}
    _remember(entry)
    store.audit("email", f"exception {exc.get('code') or ''}", user.get("username", ""), {},
                {"to": to, "cc": cc, "action": action}, subject)
    return entry
