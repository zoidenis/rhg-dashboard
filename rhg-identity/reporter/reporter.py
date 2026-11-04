"""pulse report sender: opens screens of pulse as the people who receive them and emails what they would see.

Every 30 seconds it asks identity (/internal/reports/claim, service token) for the reports due now. Each comes with
the address of the screen, the tab to open and, for every group of recipients who see the same thing, a short
read-only session of one of them. For each group it:
  1. opens the screen in a headless Chromium with that session (through the internal nginx, like a browser would),
     in the person's language, and opens the tab;
  2. waits until the screen has read its data (no request for a few seconds);
  3. takes a picture of the screen (shown in the email) and, when the report asks for it, a PDF (attached);
  4. emails both through the Purchasing mail server (/internal/mail), to the people of the group;
and tells identity how it went (/internal/reports/done), which ends the sessions.

Nothing is stored here and nothing is changed anywhere: the sessions only read, and they end with the run.
"""
import base64
import html
import os
import re
import time
import traceback
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

IDENTITY = os.environ.get("IDENTITY_URL", "http://identity:8767").rstrip("/")
SITE = os.environ.get("SITE_URL", "http://nginx").rstrip("/")
PURCHASING = os.environ.get("PURCHASING_URL", "http://purchasing:8765").rstrip("/")
TOKEN = os.environ.get("SERVICE_TOKEN", "")
COOKIE = os.environ.get("COOKIE_NAME", "rhg_session")
POLL = int(os.environ.get("REPORT_POLL_SECONDS", "30"))
QUIET_MS = 3500                 # no request for this long = the screen has read its data
MAX_WAIT = 180                  # seconds a screen may take to read its data
MAX_SHOT_HEIGHT = 6000          # px of the picture in the email; the PDF has the whole screen
MAX_PDF_HEIGHT = 18000          # px: a PDF page may be up to 200 inches tall
VERSION = "1"

# Counts the requests the page makes, so the wait ends when the screen has its data rather than after a fixed time.
INIT_JS = """(() => {
  let n = 0, last = Date.now();
  const tick = () => { last = Date.now(); };
  const f = window.fetch;
  window.fetch = function () { n++; tick(); return f.apply(this, arguments).finally(() => { n--; tick(); }); };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () { n++; tick(); this.addEventListener("loadend", () => { n--; tick(); });
    return send.apply(this, arguments); };
  window.__pulseQuiet = ms => n <= 0 && Date.now() - last > ms;
  try { localStorage.setItem("pulse-lang", "%LANG%"); } catch (e) {}
})();"""

# The screen without what only makes sense on screen: the side menu, the filters' controls, the buttons.
REPORT_CSS = """
aside, .side, #nav, .hdr-btns, .hdr-meta, .hdr-side, .banner, #langSw, #layBar, #layOpen, .lay-open, .lay-ctl,
#unicornFab, #pulseRepBtn, .pr-dlg, button.btn, .refresh, .no-report { display: none !important; }
html, body { overflow: visible !important; height: auto !important; }
.app { display: block !important; }
main { margin: 0 !important; max-width: none !important; }
"""

TEXT = {
    "en": {"seen": "As {name} sees it in pulse, {at}.", "seen_all": "As you see it in pulse, {at}.",
           "open": "Open in pulse",
           "foot": "Sent by pulse: a scheduled report. The screen is shown above{pdf}. To change who receives it or "
                   "how often, see Settings → Scheduled reports.", "pdf": "; the whole screen is attached as PDF",
           "test": "Test: sent to you only.", "slow": "The screen was still reading some figures when it was taken."},
    "sq": {"seen": "Siç e sheh {name} në pulse, {at}.", "seen_all": "Siç e shihni në pulse, {at}.",
           "open": "Hape në pulse",
           "foot": "Dërguar nga pulse: raport i planifikuar. Faqja shihet më sipër{pdf}. Për të ndryshuar kush e merr "
                   "ose sa shpesh: Cilësimet → Raportet e planifikuara.", "pdf": "; e plota është bashkëngjitur si PDF",
           "test": "Provë: dërguar vetëm juve.", "slow": "Faqja po lexonte ende disa shifra kur u fotografua."},
}


def _post(url, body, timeout=60):
    r = requests.post(url, json=body, timeout=timeout, headers={"X-Service-Token": TOKEN})
    try:
        j = r.json()
    except ValueError:
        j = {}
    if not r.ok:
        raise RuntimeError(j.get("error") or f"{url} answered {r.status_code}")
    return j


def slug(t):
    return re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_")[:60] or "report"


# ---------------------------------------------------------------- rendering
def wait_quiet(page, max_s=MAX_WAIT):
    """True when the page has made no request for QUIET_MS; False when it was still busy after max_s."""
    end = time.time() + max_s
    page.wait_for_timeout(1500)
    while time.time() < end:
        try:
            if page.evaluate(f"window.__pulseQuiet ? window.__pulseQuiet({QUIET_MS}) : true"):
                return True
        except Exception:                                           # noqa: BLE001  (navigating)
            pass
        page.wait_for_timeout(500)
    return False


def render(browser, run, group):
    """(jpeg bytes, pdf bytes or None, complete) of the screen as group["as"] sees it."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=1,
                              color_scheme="light", service_workers="block", locale="sq-AL",
                              timezone_id="Europe/Tirane")
    try:
        host = SITE.split("://", 1)[-1].split("/")[0].split(":")[0]
        ctx.add_cookies([{"name": COOKIE, "value": group["token"], "domain": host, "path": "/",
                          "httpOnly": True, "secure": False, "sameSite": "Lax"}])
        ctx.add_init_script(INIT_JS.replace("%LANG%", "en" if group.get("lang") == "en" else "sq"))
        page = ctx.new_page()
        page.goto(SITE + run["address"], wait_until="load", timeout=90_000)
        signed = page.evaluate("fetch('/' + 'api/session', {credentials: 'same-origin'}).then(r => r.json())"
                               ".then(j => !!(j && j.user)).catch(() => false)")
        if not signed:
            raise RuntimeError("The screen could not be opened as this person (the session was refused).")
        complete = wait_quiet(page)
        if run.get("tab") and run["app"] in ("finance", "purchasing") and run.get("bar"):
            sel = f'#{run["bar"]} button[data-t="{run["tab"]}"]'
            try:
                page.wait_for_selector(sel, state="attached", timeout=30_000)
            except Exception:                                       # noqa: BLE001
                raise RuntimeError("The tab is not on the screen for this person.") from None
            page.evaluate("s => { const b = document.querySelector(s); if (b && !b.classList.contains('on')) "
                          "b.click(); }", sel)
            complete = wait_quiet(page) and complete
        page.add_style_tag(content=REPORT_CSS)
        page.wait_for_timeout(400)
        h = page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
        shot = page.screenshot(type="jpeg", quality=82, full_page=True,
                               clip={"x": 0, "y": 0, "width": 1280, "height": min(int(h or 900), MAX_SHOT_HEIGHT)})
        pdf = None
        if run.get("pdf", True):
            if h and h <= MAX_PDF_HEIGHT:
                # one page as tall as the screen, exactly as it looks (the screen's styles, not the print ones)
                page.emulate_media(media="screen")
                pdf = page.pdf(width="1280px", height=f"{int(h) + 2}px", print_background=True,
                               margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
            else:                                   # longer than a PDF page may be: A4 pages, the print styles
                page.emulate_media(media="print")
                pdf = page.pdf(format="A4", landscape=True, print_background=True,
                               margin={"top": "10mm", "bottom": "10mm", "left": "8mm", "right": "8mm"})
        return shot, pdf, complete
    finally:
        ctx.close()


# ---------------------------------------------------------------- the email
def email(run, group, shot, pdf, complete):
    lang = "en" if group.get("lang") == "en" else "sq"
    t = TEXT[lang]
    at = datetime.now().strftime("%d.%m.%Y %H:%M")
    title = group["title"]
    head = " · ".join(x for x in (group.get("tower"), run["company"], run.get("period")) if x)
    seen = t["seen"].format(name=group["to"][0]["name"], at=at) if len(group["to"]) == 1 else \
        t["seen_all"].format(at=at)
    subject = f"{title} · {run['company']}" + (f" · {run['period']}" if run.get("period") else "") + \
              (" [TEST]" if run.get("test") and lang == "en" else " [PROVË]" if run.get("test") else "")
    note = html.escape(run.get("note") or "").replace("\n", "<br>")
    body = f"""<div style="font-family:Segoe UI,Arial,sans-serif;color:#16181d;max-width:1280px">
<div style="font-size:12px;color:#5b6570;letter-spacing:.03em">{html.escape(head)}</div>
<h2 style="margin:4px 0 6px;font-size:20px">{html.escape(title)}</h2>
<div style="font-size:13px;color:#5b6570;margin-bottom:10px">{html.escape(seen)}
{(' <b>' + html.escape(t['test']) + '</b>') if run.get('test') else ''}</div>
{f'<div style="font-size:14px;margin:0 0 12px;padding:10px 12px;background:#f3f6f8;border-radius:8px">{note}</div>' if note else ''}
<p style="margin:0 0 14px"><a href="{html.escape(run['public'])}" style="display:inline-block;background:#1e78a6;color:#fff;
text-decoration:none;padding:8px 14px;border-radius:8px;font-weight:600;font-size:13px">{html.escape(t['open'])}</a></p>
{'' if complete else f'<div style="font-size:12px;color:#9a5b00;margin-bottom:8px">{html.escape(t["slow"])}</div>'}
<img src="cid:screen" alt="{html.escape(title)}" style="max-width:100%;border:1px solid #dfe4e8;border-radius:8px">
<p style="font-size:11.5px;color:#7a8590;margin-top:14px">{html.escape(t['foot'].format(pdf=t['pdf'] if pdf else ''))}</p>
</div>"""
    text = f"{title}\n{head}\n{seen}\n\n{run.get('note') or ''}\n\n{t['open']}: {run['public']}\n"
    files = [{"filename": "screen.jpg", "content_type": "image/jpeg", "cid": "screen",
              "data": base64.b64encode(shot).decode("ascii")}]
    if pdf:
        files.append({"filename": f"{slug(title)}_{slug(run['company'])}_{datetime.now():%Y-%m-%d}.pdf",
                      "content_type": "application/pdf", "data": base64.b64encode(pdf).decode("ascii")})
    return subject, body, text, files


def send(run, group, shot, pdf, complete):
    subject, body, text, files = email(run, group, shot, pdf, complete)
    emails = [x["email"] for x in group["to"] if x.get("email")]
    for i in range(0, len(emails), 20):                 # the mail server takes 20 addresses per email
        _post(PURCHASING + "/internal/mail", {"to": emails[i:i + 20], "cc": [], "subject": subject, "html": body,
                                              "text": text, "source": "pulse · scheduled reports",
                                              "attachments": files}, timeout=120)
    return len(emails)


# ---------------------------------------------------------------- the loop
def do_run(pw, run):
    results = []
    browser = pw.chromium.launch(args=["--disable-dev-shm-usage"])
    try:
        for g in run["groups"]:
            try:
                shot, pdf, complete = render(browser, run, g)
                n = send(run, g, shot, pdf, complete)
                results.append({"as": g["as"], "status": "sent", "detail": "" if complete else "still reading",
                                "emails": n})
            except Exception as exc:                                # noqa: BLE001
                traceback.print_exc()
                results.append({"as": g["as"], "status": "failed", "detail": str(exc)[:300]})
    finally:
        browser.close()
    return results


def main():
    if not TOKEN:
        print("SERVICE_TOKEN is empty: the report sender cannot ask identity for reports.")
    print(f"pulse report sender: identity {IDENTITY}, screens through {SITE}, mail through {PURCHASING}")
    with sync_playwright() as pw:
        while True:
            try:
                runs = _post(IDENTITY + "/internal/reports/claim", {"reporter": {"version": VERSION}})["runs"]
            except Exception as exc:                                # noqa: BLE001
                print("identity:", exc)
                time.sleep(max(POLL, 60))
                continue
            for run in runs:
                try:
                    results = do_run(pw, run)
                    detail = ""
                except Exception as exc:                            # noqa: BLE001
                    traceback.print_exc()
                    results, detail = [], str(exc)[:300]
                try:
                    _post(IDENTITY + "/internal/reports/done", {"run": run["run"], "results": results,
                                                                "detail": detail})
                except Exception as exc:                            # noqa: BLE001
                    print("identity (done):", exc)
            time.sleep(POLL if not runs else 2)


if __name__ == "__main__":
    main()
