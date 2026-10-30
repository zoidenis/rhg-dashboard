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

The e-invoice service (WSDL https://einvoice.tatime.gov.al/EinvoiceService-v1/EinvoiceService.wsdl,
schema alimc-einvoice-service.xsd, namespace https://Einvoice.tatime.gov.al/EinvoiceService/schema):
- GetEinvoicesRequest: Header(UUID, SendDateTime), PartyType BUYER|SELLER, RecDateTimeFrom/To (the
  date-time the platform received the invoice), or one EIC (then the signed PDF comes back).
  The service compares only the dates, with RecDateTimeTo excluded: 29 00:00 to 29 23:59 finds
  nothing, 29 00:00 to 30 00:00 finds the 29th. So a window always ends at 00:00 of the next day.
  Each Einvoice element carries the attributes EIC, FIC, DocNumber, RecDateTime, DueDateTime,
  Status, Amount, BuyerTin, SellerTin, PartyType and the currency. A window of several days that
  returns 100 or more is split by whole days (answers of 2,500 in one month were seen, so the
  documented cap of 100 does not seem to apply).
- GetTaxpayersRequest: Filter Tin or Name; Taxpayer Name, Tin, Address, Town, Country.
The registering operations of the same service (RegisterEinvoice, EinvoiceChangeStatus) are refused.
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
ALLOWED = ("GetEinvoicesRequest", "GetTaxpayersRequest")
NS = "https://Einvoice.tatime.gov.al/EinvoiceService/schema"
DEFAULT_URL = "https://einvoice.tatime.gov.al/EinvoiceService-v1"
ACTION = "https://Einvoice.tatime.gov.al/EinvoiceService/"
MAX_PER_ANSWER = 100
# what the last GetEinvoices read saw: calls, answers, and (when nothing was found) a sample of
# the answer without the signature or PDFs, so a read that finds nothing can be explained
last_diag = {}
NIPT_RE = __import__("re").compile(r"\b[A-Z]\d{8}[A-Z]\b")

DEFAULT_TEMPLATE = (
    '<GetEinvoicesRequest xmlns="' + NS + '" Id="Request" Version="1">'
    '<Header SendDateTime="{send}" UUID="{uuid}"/>'
    '<PartyType>{party}</PartyType>'
    '<RecDateTimeFrom>{dt_from}</RecDateTimeFrom>'
    '<RecDateTimeTo>{dt_to}</RecDateTimeTo>'
    '</GetEinvoicesRequest>')
TAXPAYER_TEMPLATE = (
    '<GetTaxpayersRequest xmlns="' + NS + '" Id="Request" Version="1">'
    '<Header SendDateTime="{send}" UUID="{uuid}"/><Filter><Tin>{tin}</Tin></Filter></GetTaxpayersRequest>')

# candidate names of each field in the response (lower case, without separators); the
# specification's exact names can be added with FISK_FIELD_MAP, e.g. "total=TotPrice"
FIELDS = {
    "eic": ["eic", "einvoicecode"],
    "iic": ["iic", "invoiceidentificationcode"],
    "inv_no": ["docnumber", "invoicenumber", "invnum", "invoiceno", "number", "documentnumber"],
    "date": ["recdatetime", "docissuedate", "issuedate", "invoicedate", "issuedatetime", "dateissued", "docdate"],
    "due": ["duedatetime", "duedate"],
    "fic": ["fic"],
    "party": ["partytype"],
    "seller_nipt": ["sellertin", "sellernuis", "sellernipt", "sellertaxid", "sellerid"],
    "seller_name": ["sellername", "suppliername", "partyname"],
    "buyer_nipt": ["buyertin", "buyernuis", "buyernipt", "buyertaxid", "buyerid"],
    "total": ["amount", "totprice", "totalprice", "invoicetotal", "totamount", "amountincludingvat",
              "payableamount", "taxinclusiveamount"],
    "currency": ["currencycodestype", "currency", "currencycode", "doccurrencycode"],
    "doc_type": ["doctype", "documenttype", "einvoicedocumenttype", "typeofinv"],
    "status": ["status", "einvoicestatus"],
}


class CISError(Exception):
    pass


class NotConfigured(CISError):
    pass


def _settings():
    return {
        "url": (config.get("FISK_EINVOICE_URL", "") or DEFAULT_URL).strip(),
        "action": config.get("FISK_SOAP_ACTION", "") or ACTION + "GetEinvoices",
        "template": config.get("FISK_REQUEST_TEMPLATE", "") or DEFAULT_TEMPLATE,
        "cert_path": config.get("FISK_CERT_PATH", "/app/secrets/fisk.p12"),
        "password": config.get("FISK_CERT_PASSWORD", "") or "",
        "nipt": (config.get("FISK_NIPT", "") or "").strip().upper(),
        "party": (config.get("FISK_PARTY_TYPE", "BUYER") or "BUYER").strip().upper(),
        "window_days": max(1, int(config.get("FISK_WINDOW_DAYS", "1") or 1)),
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
           "party": s["party"], "loaded": False, "cert_nipt": ""}
    if not out["present"]:
        return out
    try:
        _, cert, _ = _load()
        nb = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before
        na = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
        out.update(loaded=True, subject=cert.subject.rfc4514_string(), issuer=cert.issuer.rfc4514_string(),
                   valid_from=nb.date().isoformat(), valid_to=na.date().isoformat(),
                   expired=na.date() < date.today(), serial=format(cert.serial_number, "x"))
        m = NIPT_RE.search(out["subject"])
        out["cert_nipt"] = m.group(0) if m else ""
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


def _dt(x):
    """A naive Tirana date-time as the service wants it: 2026-10-05T13:00:00+02:00."""
    return x.strftime("%Y-%m-%dT%H:%M:%S") + _offset(x.date(), x.hour >= 12)


def build_request(dt_from, dt_to, s=None):
    s = s or _settings()
    now = datetime.now().astimezone()
    if not isinstance(dt_from, datetime):
        dt_from = datetime.combine(date.fromisoformat(str(dt_from)[:10]), datetime.min.time())
    if not isinstance(dt_to, datetime):
        dt_to = datetime.combine(date.fromisoformat(str(dt_to)[:10]), datetime.max.time().replace(microsecond=0))
    return s["template"].format(send=now.isoformat(timespec="seconds"), uuid=str(uuid.uuid4()),
                                party=s["party"], nipt=s["nipt"], dt_from=_dt(dt_from), dt_to=_dt(dt_to),
                                date_from=dt_from.date().isoformat(), date_to=dt_to.date().isoformat(),
                                tz=_offset(now.date()), tz_from=_offset(dt_from.date()),
                                tz_to=_offset(dt_to.date(), True))


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
        if ch.text and ch.text.strip() and len(ch) == 0 and norm(ch.tag) != "pdf":
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
        # The key matched against BC is the FIC: BC keeps it as NIVF and the Self-Care book as
        # "Numri Serial". The platform's own EIC never appears in BC; it is kept apart.
        rec["einvoice_code"] = eic.lower()
        rec["eic"] = (rec["fic"] or eic).lower()
        try:
            rec["total"] = float(str(rec["total"]).replace(",", ".")) if rec["total"] is not None else None
        except ValueError:
            rec["total"] = None
        rec["date"] = (rec["date"] or "")[:10] or None
        out.append(rec)
    return out


def _call(xml, s, action, pk, cert):
    signed = sign(xml.encode("utf-8"), pk, cert)
    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": action}
    try:
        r = requests.post(s["url"], data=_envelope(signed), headers=headers, timeout=s["timeout"])
    except requests.RequestException as exc:
        raise CISError(f"Could not reach the Tax Administration ({s['url']}): {str(exc)[:200]}") from exc
    if r.status_code >= 400 and b"Fault" not in r.content:
        raise CISError(f"The Tax Administration returned HTTP {r.status_code}: {r.text[:300]}")
    return r.content


def _sample(xml):
    """The answer with the signature and any PDF taken out, cut short, plus the texts of elements
    that look like messages or codes."""
    from lxml import etree
    try:
        root = etree.fromstring(xml)
    except Exception:                                            # noqa: BLE001
        return {"sample": (xml or b"")[:1500].decode("utf-8", "replace"), "messages": []}
    msgs = []
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        name = etree.QName(el).localname.lower()
        if name in ("signature", "pdf"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
            continue
        if any(w in name for w in ("message", "error", "code", "description", "result", "status")) and (el.text or "").strip():
            msgs.append(f"{etree.QName(el).localname}: {el.text.strip()[:200]}")
        for k, v in el.attrib.items():
            if any(w in k.lower() for w in ("message", "error", "code", "result")):
                msgs.append(f"{etree.QName(el).localname}@{k}: {str(v)[:200]}")
    body = etree.tostring(root, encoding="unicode")
    return {"sample": body[:1500], "messages": msgs[:10], "root": etree.QName(root).localname,
            "elements": sum(1 for _ in root.iter())}


def get_einvoices(date_from, date_to, progress=None, party=None):
    """Every e-invoice received by the platform in the period, where we are the BUYER (default,
    FISK_PARTY_TYPE) or the SELLER. A window of several days that returns 100 or more is split
    by whole days, since the service ignores the time of day."""
    s = _settings()
    if party:
        s["party"] = party.upper()
    if not s["url"]:
        raise NotConfigured("The address of the e-invoice service is not set (FISK_EINVOICE_URL).")
    pk, cert, _ = _load()
    a, b = date.fromisoformat(str(date_from)[:10]), date.fromisoformat(str(date_to)[:10])
    found, calls = {}, [0]
    last_diag.clear()
    last_diag.update(calls=0, with_rows=0, party=s["party"], cert_nipt=(NIPT_RE.search(cert.subject.rfc4514_string()) or [""])[0])

    def read(d0, d1):
        """The days d0 to d1, both included: the request ends at 00:00 of the day after d1."""
        calls[0] += 1
        if calls[0] > 2000:
            raise CISError("Too many requests for one period; stopped.")
        t0 = datetime.combine(d0, datetime.min.time())
        t1 = datetime.combine(d1 + timedelta(days=1), datetime.min.time())
        answer = _call(build_request(t0, t1, s), s, s["action"], pk, cert)
        rows = parse_response(answer, s["field_map"])
        last_diag["calls"] = calls[0]
        if rows:
            last_diag["with_rows"] += 1
        elif "empty_answer" not in last_diag:
            last_diag["empty_answer"] = dict(_sample(answer), window=f"{t0.isoformat()} to {t1.isoformat()}")
        if len(rows) >= MAX_PER_ANSWER and d1 > d0:
            mid = d0 + (d1 - d0) // 2
            read(d0, mid)
            read(mid + timedelta(days=1), d1)
            return
        for r in rows:
            found[r["eic"]] = r
    d = a
    while d <= b:
        e = min(b, d + timedelta(days=s["window_days"] - 1))
        read(d, e)
        if progress:
            progress(d, e, len(found))
        d = e + timedelta(days=1)
    return list(found.values())


def probe(eic=None, party="BUYER", date_from=None, date_to=None):
    """One GetEinvoices request, as given, to see what the service answers: by EIC (one e-invoice,
    as the service documents it) and/or by received date-time. Reading only, like every request
    here. Returns the invoices found (without PDFs) and a sample of the answer."""
    import re as _re
    from xml.sax.saxutils import escape
    s = _settings()
    pk, cert, _ = _load()
    party = (party or "").upper()
    if party not in ("", "BUYER", "SELLER"):
        raise ValueError("party must be BUYER or SELLER")
    eic = (eic or "").strip().lower()
    if eic and not _re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", eic):
        raise ValueError("An EIC looks like 8-4-4-4-12 hexadecimal characters.")
    now = datetime.now().astimezone()
    parts = [f'<Header SendDateTime="{now.isoformat(timespec="seconds")}" UUID="{uuid.uuid4()}"/>']
    if eic:
        parts.append(f"<EIC>{escape(eic)}</EIC>")
    if party:
        parts.append(f"<PartyType>{party}</PartyType>")
    window = ""
    if date_from and date_to:
        a = datetime.combine(date.fromisoformat(str(date_from)[:10]), datetime.min.time())
        # the service excludes the end date: end at 00:00 of the day after date_to
        b = datetime.combine(date.fromisoformat(str(date_to)[:10]) + timedelta(days=1), datetime.min.time())
        parts += [f"<RecDateTimeFrom>{_dt(a)}</RecDateTimeFrom>", f"<RecDateTimeTo>{_dt(b)}</RecDateTimeTo>"]
        window = f"{_dt(a)} to {_dt(b)}"
    xml = f'<GetEinvoicesRequest xmlns="{NS}" Id="Request" Version="1">' + "".join(parts) + "</GetEinvoicesRequest>"
    answer = _call(xml, s, s["action"], pk, cert)
    rows = parse_response(answer, s["field_map"])
    for r in rows:
        r.pop("raw", None)
    return {"request": {"eic": eic, "party": party, "window": window,
                        "cert_nipt": (NIPT_RE.search(cert.subject.rfc4514_string()) or [""])[0]},
            "found": len(rows), "rows": rows[:20], "answer": _sample(answer)}


def get_taxpayer(tin):
    """Name, address and town of a NIPT, from the Tax Administration."""
    s = _settings()
    pk, cert, _ = _load()
    xml = TAXPAYER_TEMPLATE.format(send=datetime.now().astimezone().isoformat(timespec="seconds"),
                                   uuid=str(uuid.uuid4()), tin=str(tin).strip().upper())
    from lxml import etree
    root = etree.fromstring(_call(xml, s, ACTION + "GetTaxpayers", pk, cert))
    fault = root.find(f".//{{{SOAP}}}Fault")
    if fault is not None:
        raise CISError((fault.findtext("faultstring") or "").strip()[:300])
    for el in root.iter():
        if isinstance(el.tag, str) and etree.QName(el).localname == "Taxpayer":
            return {k.lower(): v for k, v in el.attrib.items()}
    return None
