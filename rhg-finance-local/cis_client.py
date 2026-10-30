"""Read-only client for the Tax Administration's e-invoice service (CIS).

What it does: asks the central invoice platform for the e-invoices of our NIPT for a period
(the same invoices the Self-Care portal shows), so Finance can reconcile them with the
purchase invoices posted in Business Central.

Safety:
- Only reading operations can be sent. The request built here is checked against
  ALLOWED before it is signed; the registering operations of the service (register an
  invoice, a TCR, a cash deposit ...) cannot be called from this application.
- The fiscalization certificate (.p12) stays on the server, outside the code and outside
  the repository: FISK_CERT_PATH (default /app/secrets/fisk.p12, a read-only mount) and
  FISK_CERT_PASSWORD in .env. Nothing about it is logged or sent to the browser except its
  subject, issuer and validity dates.

The request follows the signing rules of the Fiscalization Service technical specification
(v09, section 5.3): enveloped XML signature of the element with Id="Request", exclusive
canonicalization, SHA-256 digest, RSA-SHA256, the certificate in KeyInfo/X509Data.

The address, namespace and field names of the e-invoice service come from its own technical
specification ("Einvoice Service - Technical"). Until it is in hand they are settings
(FISK_EINVOICE_URL, FISK_REQUEST_TEMPLATE, FISK_FIELD_MAP); with no address set the client
reports "not configured" and makes no call.
"""
import base64
import hashlib
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import config

DS = "http://www.w3.org/2000/09/xmldsig#"
C14N_EXC = "http://www.w3.org/2001/10/xml-exc-c14n#"
ENVELOPED = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"
RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"

# The only operations this application may send: reading, never registering.
ALLOWED = ("GetEinvoicesRequest",)

DEFAULT_TEMPLATE = (
    '<GetEinvoicesRequest xmlns="https://Einvoice.tatime.gov.al/EinvoiceService/schema" Id="Request" Version="1">'
    '<Header SendDateTime="{send}" UUID="{uuid}"/>'
    '<PartyType>{party}</PartyType>'
    '<DateTimeFrom>{date_from}T00:00:00{tz_from}</DateTimeFrom>'
    '<DateTimeTo>{date_to}T23:59:59{tz_to}</DateTimeTo>'
    '</GetEinvoicesRequest>')

# candidate names of each field in the response (lower case, without separators); the
# specification's exact names can be added with FISK_FIELD_MAP, e.g. "total=TotPrice"
FIELDS = {
    "eic": ["eic", "einvoicecode"],
    "iic": ["iic", "invoiceidentificationcode"],
    "inv_no": ["docnumber", "invoicenumber", "invnum", "invoiceno", "number", "documentnumber"],
    "date": ["docissuedate", "issuedate", "invoicedate", "issuedatetime", "dateissued", "docdate"],
    "seller_nipt": ["sellertin", "sellernuis", "sellernipt", "sellertaxid", "sellerid"],
    "seller_name": ["sellername", "suppliername", "partyname"],
    "buyer_nipt": ["buyertin", "buyernuis", "buyernipt", "buyertaxid", "buyerid"],
    "total": ["amount", "totprice", "totalprice", "invoicetotal", "totamount", "amountincludingvat",
              "payableamount", "taxinclusiveamount"],
    "currency": ["currency", "currencycode", "doccurrencycode"],
    "doc_type": ["doctype", "documenttype", "einvoicedocumenttype", "typeofinv"],
    "status": ["status", "einvoicestatus"],
}


class CISError(Exception):
    pass


class NotConfigured(CISError):
    pass


def _settings():
    return {
        "url": (config.get("FISK_EINVOICE_URL", "") or "").strip(),
        "action": config.get("FISK_SOAP_ACTION", "") or "",
        "template": config.get("FISK_REQUEST_TEMPLATE", "") or DEFAULT_TEMPLATE,
        "cert_path": config.get("FISK_CERT_PATH", "/app/secrets/fisk.p12"),
        "password": config.get("FISK_CERT_PASSWORD", "") or "",
        "nipt": (config.get("FISK_NIPT", "") or "").strip().upper(),
        "party": (config.get("FISK_PARTY_TYPE", "BUYER") or "BUYER").strip().upper(),
        "window_days": max(1, int(config.get("FISK_WINDOW_DAYS", "7") or 7)),
        "timeout": int(config.get("FISK_TIMEOUT", "60") or 60),
        "field_map": config.get("FISK_FIELD_MAP", "") or "",
    }


# ---------------------------------------------------------------- the certificate
_cert_lock = threading.Lock()
_cert_cache = {}


def _load():
    """(private key, certificate, chain) from the .p12, cached until the file changes."""
    from cryptography.hazmat.primitives.serialization import pkcs12
    s = _settings()
    p = Path(s["cert_path"])
    if not p.is_file():
        raise NotConfigured(f"The fiscalization certificate is not on the server ({p}).")
    key = (str(p), p.stat().st_mtime, hashlib.sha256(s["password"].encode()).hexdigest())
    with _cert_lock:
        if _cert_cache.get("key") == key:
            return _cert_cache["value"]
        try:
            pk, cert, chain = pkcs12.load_key_and_certificates(p.read_bytes(),
                                                               s["password"].encode() or None)
        except ValueError as exc:
            raise CISError("The certificate could not be opened: wrong password or not a .p12 file.") from exc
        if pk is None or cert is None:
            raise CISError("The .p12 file holds no private key and certificate.")
        _cert_cache.update(key=key, value=(pk, cert, chain or []))
        return pk, cert, chain or []


def certificate_info():
    """What can be shown about the certificate: never the key or the password."""
    s = _settings()
    out = {"path_set": bool(s["cert_path"]), "present": Path(s["cert_path"]).is_file(),
           "password_set": bool(s["password"]), "url_set": bool(s["url"]), "nipt": s["nipt"],
           "party": s["party"], "loaded": False}
    if not out["present"]:
        return out
    try:
        _, cert, _ = _load()
        nb = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before
        na = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
        out.update(loaded=True, subject=cert.subject.rfc4514_string(), issuer=cert.issuer.rfc4514_string(),
                   valid_from=nb.date().isoformat(), valid_to=na.date().isoformat(),
                   expired=na.date() < date.today(), serial=format(cert.serial_number, "x"))
    except CISError as exc:
        out["error"] = str(exc)
    return out


# ---------------------------------------------------------------- signing
def _c14n(el):
    from lxml import etree
    return etree.tostring(el, method="c14n", exclusive=True, with_comments=False)


def sign(xml, pk=None, cert=None):
    """Signs the element carrying Id="Request" (enveloped signature) and returns the bytes."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from lxml import etree
    if pk is None or cert is None:
        pk, cert, _ = _load()
    root = etree.fromstring(xml if isinstance(xml, bytes) else xml.encode("utf-8"))
    target = root if root.get("Id") else root.find(".//*[@Id]")
    if target is None:
        raise CISError("The request has no element with an Id to sign.")
    if etree.QName(target).localname not in ALLOWED:
        raise CISError(f"{etree.QName(target).localname} is not a reading operation; it is not sent.")
    ref_id = target.get("Id")
    digest = base64.b64encode(hashlib.sha256(_c14n(target)).digest()).decode()

    def E(tag, parent=None, **attrs):
        el = etree.SubElement(parent, f"{{{DS}}}{tag}", **attrs) if parent is not None else \
            etree.Element(f"{{{DS}}}{tag}", nsmap={None: DS}, **attrs)
        return el
    sig = E("Signature")
    si = E("SignedInfo", sig)
    E("CanonicalizationMethod", si, Algorithm=C14N_EXC)
    E("SignatureMethod", si, Algorithm=RSA_SHA256)
    ref = E("Reference", si, URI=f"#{ref_id}")
    tr = E("Transforms", ref)
    E("Transform", tr, Algorithm=ENVELOPED)
    E("Transform", tr, Algorithm=C14N_EXC)
    E("DigestMethod", ref, Algorithm=SHA256)
    E("DigestValue", ref).text = digest
    sv = E("SignatureValue", sig)
    ki = E("KeyInfo", sig)
    xd = E("X509Data", ki)
    E("X509Certificate", xd).text = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
    target.append(sig)
    signature = pk.sign(_c14n(si), padding.PKCS1v15(), hashes.SHA256())
    sv.text = base64.b64encode(signature).decode()
    return etree.tostring(root, xml_declaration=False, encoding="utf-8")


def verify(xml):
    """Checks a signature made by sign() (used by the tests and by the connection check)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from lxml import etree
    root = etree.fromstring(xml)
    sig = root.find(f".//{{{DS}}}Signature")
    target = sig.getparent()
    si = sig.find(f"{{{DS}}}SignedInfo")
    cert = x509.load_der_x509_certificate(base64.b64decode(sig.find(f".//{{{DS}}}X509Certificate").text))
    cert.public_key().verify(base64.b64decode(sig.find(f"{{{DS}}}SignatureValue").text), _c14n(si),
                             padding.PKCS1v15(), hashes.SHA256())
    clone = etree.fromstring(etree.tostring(target))
    clone.remove(clone.find(f"{{{DS}}}Signature"))
    digest = base64.b64encode(hashlib.sha256(_c14n(clone)).digest()).decode()
    return digest == si.find(f".//{{{DS}}}DigestValue").text


# ---------------------------------------------------------------- the request
def _offset(day, end=False):
    """UTC offset of Tirana on that day ("+01:00" in winter, "+02:00" in summer)."""
    try:
        from zoneinfo import ZoneInfo
        tzinfo = ZoneInfo("Europe/Tirane")
    except Exception:                                            # noqa: BLE001
        tzinfo = datetime.now().astimezone().tzinfo
    d = date.fromisoformat(str(day)[:10])
    z = datetime(d.year, d.month, d.day, 23 if end else 0, 59 if end else 0, tzinfo=tzinfo).strftime("%z")
    return z[:3] + ":" + z[3:]


def build_request(date_from, date_to, s=None):
    s = s or _settings()
    now = datetime.now().astimezone()
    return s["template"].format(send=now.isoformat(timespec="seconds"), uuid=str(uuid.uuid4()),
                                party=s["party"], nipt=s["nipt"], date_from=date_from, date_to=date_to,
                                tz=_offset(now.date()), tz_from=_offset(date_from), tz_to=_offset(date_to, True))


def _envelope(signed):
    return (f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{SOAP}"><SOAP-ENV:Header/><SOAP-ENV:Body>'.encode()
            + signed + b"</SOAP-ENV:Body></SOAP-ENV:Envelope>")


def _flat(el):
    """Attributes and simple child texts of an element, keyed by a normalised name."""
    from lxml import etree
    out = {}

    def norm(n):
        return "".join(ch for ch in etree.QName(n).localname.lower() if ch.isalnum())
    for k, v in el.attrib.items():
        out.setdefault(norm(k), v)
    for ch in el.iter():
        if ch is el or not isinstance(ch.tag, str):
            continue
        for k, v in ch.attrib.items():
            out.setdefault(norm(ch.tag) + norm(k), v)
            out.setdefault(norm(k), v)
        if ch.text and ch.text.strip() and len(ch) == 0:
            out.setdefault(norm(ch.tag), ch.text.strip())
    return out


def parse_response(xml, field_map=""):
    from lxml import etree
    root = etree.fromstring(xml)
    fault = root.find(f".//{{{SOAP}}}Fault")
    if fault is not None:
        code = (fault.findtext("faultcode") or "").strip()
        msg = (fault.findtext("faultstring") or "").strip()
        raise CISError(f"The Tax Administration answered with an error {code}: {msg[:300]}")
    extra = {}
    for part in field_map.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            extra[k.strip()] = "".join(ch for ch in v.strip().lower() if ch.isalnum())
    out = []
    seen = set()
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        f = _flat(el)
        eic = f.get(extra.get("eic", "")) or f.get("eic")
        name = etree.QName(el).localname.lower()
        if not eic or "invoice" not in name or eic in seen:
            continue
        seen.add(eic)
        rec = {"raw": {k: v for k, v in list(f.items())[:60]}}
        for key, names in FIELDS.items():
            names = ([extra[key]] if key in extra else []) + names
            rec[key] = next((f[n] for n in names if f.get(n) not in (None, "")), None)
        rec["eic"] = eic.lower()
        try:
            rec["total"] = float(str(rec["total"]).replace(",", ".")) if rec["total"] is not None else None
        except ValueError:
            rec["total"] = None
        rec["date"] = (rec["date"] or "")[:10] or None
        out.append(rec)
    return out


def get_einvoices(date_from, date_to, progress=None):
    """Every e-invoice of the period, read in windows of FISK_WINDOW_DAYS days."""
    s = _settings()
    if not s["url"]:
        raise NotConfigured("The address of the e-invoice service is not set (FISK_EINVOICE_URL).")
    pk, cert, _ = _load()
    a, b = date.fromisoformat(str(date_from)[:10]), date.fromisoformat(str(date_to)[:10])
    out, d = [], a
    while d <= b:
        e = min(b, d + timedelta(days=s["window_days"] - 1))
        signed = sign(build_request(d.isoformat(), e.isoformat(), s).encode("utf-8"), pk, cert)
        headers = {"Content-Type": "text/xml; charset=utf-8"}
        if s["action"]:
            headers["SOAPAction"] = s["action"]
        try:
            r = requests.post(s["url"], data=_envelope(signed), headers=headers, timeout=s["timeout"])
        except requests.RequestException as exc:
            raise CISError(f"Could not reach the Tax Administration ({s['url']}): {str(exc)[:200]}") from exc
        if r.status_code >= 400 and b"Fault" not in r.content:
            raise CISError(f"The Tax Administration returned HTTP {r.status_code}: {r.text[:300]}")
        out.extend(parse_response(r.content, s["field_map"]))
        if progress:
            progress(d, e, len(out))
        d = e + timedelta(days=1)
    return out
