"""Email through Microsoft Graph (OAuth), in place of SMTP AUTH with a password.

Microsoft turns SMTP AUTH Basic off by default for existing tenants at the end of December 2026. The towers send
through one function (notify.send_mail); when Graph is configured, that function hands the finished message here.

How it signs in: an app registration in Entra ID with a certificate (never a client secret). The private key stays
on the server (GRAPH_CERT_PATH, mounted read-only); the public certificate is uploaded to the app registration. The
app's mail rights are limited in Exchange (RBAC for Applications) to the one mailbox Pulse sends from, so it cannot
send as anyone else.

How it sends:
- up to ~3.5 MB: the MIME message as built for SMTP, base64, to POST /users/{from}/sendMail (Mail.Send);
- larger (exports up to 9.5 MB): a draft in that mailbox, attachments through upload sessions, then send
  (Mail.ReadWrite on the same mailbox). The draft is deleted if anything fails.
"""
import base64
import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

import config

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
SCOPE = "https://graph.microsoft.com/.default"
MIME_LIMIT = 3_500_000          # base64 bytes in one sendMail request (Graph refuses requests over 4 MB)
ATTACH_DIRECT = 3_000_000       # attachments below this go in one request; larger ones through an upload session
CHUNK = 320 * 1024 * 10         # upload session chunks must be multiples of 320 KiB (3.2 MB here)
TIMEOUT = 60

_lock = threading.Lock()
_token = {"value": "", "expires": 0.0}
_cred = {"key": None, "stamp": None, "path": "", "error": "", "expires": None, "x5t": ""}


# ---------------------------------------------------------------- configuration

def active():
    """Is Graph the transport? MAIL_TRANSPORT=graph|smtp; blank means Graph once tenant and app are set."""
    mode = (config.MAIL_TRANSPORT or "").strip().lower()
    if mode == "smtp":
        return False
    if mode == "graph":
        return True
    return bool(config.GRAPH_TENANT_ID and config.GRAPH_CLIENT_ID)


def _load_credential():
    """Read the private key and certificate once per file change. Returns an error text, or ''."""
    path = (config.GRAPH_CERT_PATH or "").strip()
    if not path:
        return "GRAPH_CERT_PATH is not set."
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return f"The certificate file {path} was not found on the server."
    stamp = (st.st_mtime_ns, st.st_size)
    if _cred["path"] == path and _cred["stamp"] == stamp:
        return _cred["error"]
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    _cred.update(path=path, stamp=stamp, key=None, error="", expires=None, x5t="")
    password = (config.GRAPH_CERT_PASSWORD or "").encode() or None
    try:
        data = p.read_bytes()
        if p.suffix.lower() in (".pfx", ".p12"):
            key, cert, _extra = pkcs12.load_key_and_certificates(data, password)
        else:
            text = data.decode("ascii", "replace")
            key_pem = re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.+?-----END [A-Z ]*PRIVATE KEY-----", text, re.S)
            cert_pem = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", text, re.S)
            if not key_pem or not cert_pem:
                raise ValueError("the file must hold both the private key and the certificate (PEM)")
            key = serialization.load_pem_private_key(key_pem.group(0).encode(), password)
            cert = x509.load_pem_x509_certificate(cert_pem.group(0).encode())
        if key is None or cert is None:
            raise ValueError("the file must hold both the private key and the certificate")
    except Exception as exc:                                       # noqa: BLE001
        _cred["error"] = f"The certificate could not be read: {str(exc)[:200]}"
        return _cred["error"]
    der = cert.public_bytes(serialization.Encoding.DER)
    _cred.update(key=key, x5t=_b64url(hashlib.sha1(der).digest()),  # noqa: S324 - the thumbprint Entra ID expects
                 expires=cert.not_valid_after_utc)
    return ""


def ready():
    """Graph is the transport and everything it needs is in place."""
    if not active():
        return False
    if not (config.GRAPH_TENANT_ID and config.GRAPH_CLIENT_ID):
        return False
    with _lock:
        return not _load_credential()


def status():
    """What Settings shows: never the key, only whether it works and when the certificate ends."""
    with _lock:
        error = _load_credential() if active() else ""
        expires = _cred["expires"]
    missing = [name for name, value in (("GRAPH_TENANT_ID", config.GRAPH_TENANT_ID),
                                        ("GRAPH_CLIENT_ID", config.GRAPH_CLIENT_ID)) if not value]
    out = {"tenant": config.GRAPH_TENANT_ID or "", "client": config.GRAPH_CLIENT_ID or "",
           "error": ("Missing in .env: " + ", ".join(missing)) if missing else error,
           "certificate_expires": expires.date().isoformat() if expires and not error else ""}
    if expires and not error:
        days = (expires - datetime.now(timezone.utc)).days
        out["certificate_days_left"] = days
        if days < 0:
            out["error"] = f"The certificate expired on {out['certificate_expires']}; upload a new one."
    return out


# ---------------------------------------------------------------- token

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _assertion(now):
    """The signed JWT that proves the app holds the certificate's private key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    tenant, client = config.GRAPH_TENANT_ID, config.GRAPH_CLIENT_ID
    header = {"alg": "RS256", "typ": "JWT", "x5t": _cred["x5t"]}
    claims = {"aud": f"{LOGIN}/{tenant}/oauth2/v2.0/token", "iss": client, "sub": client,
              "jti": str(uuid.uuid4()), "nbf": int(now), "iat": int(now), "exp": int(now) + 600}
    signing = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
               + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    signature = _cred["key"].sign(signing.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return signing + "." + _b64url(signature)


def _access_token():
    with _lock:
        now = time.time()
        if _token["value"] and now < _token["expires"] - 300:
            return _token["value"]
        error = _load_credential()
        if error:
            raise RuntimeError(error)
        r = requests.post(f"{LOGIN}/{quote(config.GRAPH_TENANT_ID, safe='')}/oauth2/v2.0/token", timeout=TIMEOUT, data={
            "client_id": config.GRAPH_CLIENT_ID, "scope": SCOPE, "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": _assertion(now)})
        try:
            j = r.json()
        except ValueError:
            j = {}
        if not r.ok or not j.get("access_token"):
            text = (j.get("error_description") or j.get("error") or f"HTTP {r.status_code}").splitlines()[0]
            raise RuntimeError(f"Microsoft sign-in failed: {text[:240]}")
        _token.update(value=j["access_token"], expires=now + int(j.get("expires_in") or 3599))
        return _token["value"]


def _forget_token():
    with _lock:
        _token.update(value="", expires=0.0)


# ---------------------------------------------------------------- requests

def _graph_error(r):
    try:
        e = r.json().get("error") or {}
        text = f"{e.get('code') or r.status_code}: {e.get('message') or ''}".strip(": ")
    except (ValueError, AttributeError):
        text = f"HTTP {r.status_code}"
    hint = ""
    if r.status_code == 403:
        hint = " (the app has no mail rights on this mailbox: check the Exchange role assignment)"
    elif r.status_code == 404:
        hint = " (the sending mailbox was not found: check the From address in Settings)"
    return RuntimeError(f"Microsoft Graph refused the email: {text[:240]}{hint}")


def _call(method, url, headers=None, **kwargs):
    """One Graph request with the app token: retries once on an expired token and twice when throttled."""
    for attempt in range(3):
        h = dict(headers or {}, Authorization="Bearer " + _access_token())
        r = requests.request(method, url, headers=h, timeout=TIMEOUT, **kwargs)
        if r.status_code == 401 and attempt == 0:
            _forget_token()
            continue
        if r.status_code in (429, 503, 504) and attempt < 2:
            try:
                wait = min(float(r.headers.get("Retry-After") or 2), 10.0)
            except ValueError:
                wait = 2.0
            time.sleep(wait)
            continue
        return r
    return r


def _mailbox(sender):
    return f"{GRAPH}/users/{quote(sender, safe='@')}"


def send(msg, sender):
    """Send an email.message.EmailMessage from the mailbox `sender`."""
    raw = base64.b64encode(msg.as_bytes())
    if len(raw) <= MIME_LIMIT:
        r = _call("POST", _mailbox(sender) + "/sendMail", data=raw, headers={"Content-Type": "text/plain"})
        if r.status_code != 202:
            raise _graph_error(r)
        return
    _send_large(msg, sender)


# ---------------------------------------------------------------- large messages

def _recipients(value):
    return [{"emailAddress": {"address": a.strip()}} for a in (value or "").split(",") if a.strip()]


def _parts(msg):
    """Body and attachments of the message notify.py builds: (html, text, [(name, type, bytes, inline, cid)])."""
    html_part = msg.get_body(("html",))
    text_part = msg.get_body(("plain",))
    files = []
    for part in msg.walk():
        if part.is_multipart() or part in (html_part, text_part):
            continue
        cid = (part.get("Content-ID") or "").strip("<>")
        disposition = part.get_content_disposition()
        if disposition != "attachment" and not cid:
            continue
        files.append((part.get_filename() or cid or "attachment", part.get_content_type(),
                      part.get_payload(decode=True) or b"", bool(cid) and disposition != "attachment", cid))
    return (html_part.get_content() if html_part else "", text_part.get_content() if text_part else "", files)


def _send_large(msg, sender):
    html_body, text_body, files = _parts(msg)
    body = {"subject": str(msg["Subject"] or ""),
            "body": {"contentType": "HTML", "content": html_body} if html_body
            else {"contentType": "Text", "content": text_body},
            "toRecipients": _recipients(msg["To"]), "ccRecipients": _recipients(msg["Cc"])}
    if msg["Reply-To"]:
        body["replyTo"] = _recipients(msg["Reply-To"])
    r = _call("POST", _mailbox(sender) + "/messages", json=body)
    if r.status_code != 201:
        raise _graph_error(r)
    draft = _mailbox(sender) + "/messages/" + quote(r.json()["id"], safe="")
    try:
        for name, ctype, data, inline, cid in files:
            if len(data) < ATTACH_DIRECT:
                item = {"@odata.type": "#microsoft.graph.fileAttachment", "name": name, "contentType": ctype,
                        "contentBytes": base64.b64encode(data).decode("ascii"), "isInline": inline}
                if cid:
                    item["contentId"] = cid
                r = _call("POST", draft + "/attachments", json=item)
                if r.status_code != 201:
                    raise _graph_error(r)
            else:
                _upload(draft, name, ctype, data, inline, cid)
        r = _call("POST", draft + "/send")
        if r.status_code != 202:
            raise _graph_error(r)
    except Exception:
        try:
            _call("DELETE", draft)
        except Exception:                                          # noqa: BLE001
            pass
        raise


def _upload(draft, name, ctype, data, inline, cid):
    item = {"attachmentType": "file", "name": name, "size": len(data), "contentType": ctype, "isInline": inline}
    if cid:
        item["contentId"] = cid
    r = _call("POST", draft + "/attachments/createUploadSession", json={"AttachmentItem": item})
    if r.status_code != 201:
        raise _graph_error(r)
    url = r.json()["uploadUrl"]
    for start in range(0, len(data), CHUNK):
        chunk = data[start:start + CHUNK]
        # the upload URL carries its own authorization: an Authorization header here is refused
        r = requests.put(url, data=chunk, timeout=TIMEOUT, headers={
            "Content-Type": "application/octet-stream", "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(data)}"})
        if r.status_code not in (200, 201):
            raise _graph_error(r)
