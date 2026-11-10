"""Export → Send by email: the file an Export button gives (Excel or PDF), sent from the tower to the addresses the
person types, with their message. The server builds the file itself, as the download would (same address, same
permission, same location scope): the page sends which export, never the file. The reply goes to the sender, who
can take a copy; every email is in the audit log. The same file sits in Finance and Purchasing.
"""
import html
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
MAX_TO = 10
MAX_BYTES = 9_500_000
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def emails(text):
    good, bad = [], []
    for part in re.split(r"[,;\s]+", str(text or "")):
        part = part.strip()
        if part:
            (good if EMAIL_RE.match(part) else bad).append(part)
    return list(dict.fromkeys(good)), bad


def clean_url(u):
    """The export's address as this tower answers it: /finance or /purchasing in front (nginx) dropped, an
    address under /api/ ending in /xlsx only, format left out (the email chooses it)."""
    p = urlparse(str(u or ""))
    path = p.path
    i = path.find("/api/")
    if i < 0:
        raise ValueError("This export cannot be sent by email.")
    path = path[i:]
    if not path.endswith("/xlsx") or ".." in path:
        raise ValueError("This export cannot be sent by email.")
    rest = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "format"]
    return path + ("?" + urlencode(rest) if rest else "")


def check(body):
    to, bad = emails(body.get("to"))
    if bad:
        raise ValueError("Not an email address: " + ", ".join(bad[:5]))
    if not to:
        raise ValueError("Write at least one address.")
    if len(to) > MAX_TO:
        raise ValueError(f"At most {MAX_TO} addresses.")
    fmt = "pdf" if body.get("format") == "pdf" else "xlsx"
    return to, fmt


def compose(user, title, fname, fmt, note, tower, screen=""):
    e = html.escape
    who = (user.get("name") or user.get("username") or "").strip()
    mail = (user.get("email") or "").strip()
    title = str(title or fname)[:200]
    note = str(note or "").strip()[:2000]
    kind = "PDF" if fmt == "pdf" else "Excel file"
    navy, ink, muted, line = "#08303f", "#0e2733", "#5c7482", "#d9dcdd"
    note_html = (f"<div style='white-space:pre-wrap;background:#f6f8f9;border-left:3px solid #0c475e;padding:10px 14px;"
                 f"margin:14px 0;font-size:14px;color:{ink}'>{e(note)}</div>") if note else ""
    body = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif;color:{ink}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ecebeb"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px">
<tr><td style="background:{navy};color:#fff;padding:16px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · RHG {e(tower.upper())}</div>
<div style="font-size:18px;font-weight:700;margin-top:4px">{e(title)}</div></td></tr>
<tr><td style="background:#fff;padding:18px 22px;border-radius:0 0 12px 12px">
<p style="font-size:14px;margin:0">{e(who or "A colleague")} sent you <b>{e(title)}</b> from RHG Control Towers{f" ({e(screen)})" if screen and screen != title else ""}, as a {kind}.</p>
{note_html}
<p style="font-size:13px;color:{muted};margin:12px 0 0">Attached: <b>{e(fname)}</b>, exported {datetime.now():%d %b %Y %H:%M}, with the filters chosen on the screen.</p>
<p style="color:{muted};font-size:11px;margin:20px 0 0;border-top:1px solid {line};padding-top:10px">Sent by {e(who)}{f" ({e(mail)})" if mail else ""} from RHG {e(tower)}; a reply goes to them.</p>
</td></tr></table></td></tr></table></body></html>"""
    text = (f"{who or 'A colleague'} sent you {title} from RHG Control Towers"
            + (f" ({screen})" if screen and screen != title else "") + f", as a {kind}.\n\n"
            + (note + "\n\n" if note else "") + f"Attached: {fname}.\n\nSent by {who}{f' ({mail})' if mail else ''} from RHG {tower}.")
    subject = f"[RHG {tower}] {title}"[:200]
    return subject, body, text
