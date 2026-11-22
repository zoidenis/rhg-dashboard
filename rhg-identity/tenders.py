"""Tenders & procurement: the public tenders of Albania that RHG could bid for (accommodation, catering, restaurant,
events and the rest of HoReCa), from the Public Procurement Agency (Agjencia e Prokurimit Publik, app.gov.al).

Source: APP's open data, "Export public calls data" (https://app.gov.al/export-public-calls/): one CSV per year of
every procurement procedure, generated again every day (GetData/ExportDocument?year=YYYY): contracting authority,
reference number, object, procedure, contract type, framework agreement, limit fund, dates of publication, opening and
closing, cancelled and why, suspended, winner, its NIPT and value, offers submitted and qualified, CPV codes. Read with
one GET a day (kept six hours; Read again reads at once), row by row so the file is never held in memory: only the
procedures that match are kept (data/tenders.json). Nothing is sent to APP.

A procedure matches when its object holds one of the keywords of a category (accents and case set aside, at the start
of a word) or one of its CPV codes starts with the category's CPV prefixes. The categories and their keywords are a
setting (Tenders & procurement → Keywords), so a false match is cut there.
"""
import csv
import io
import json
import re
import threading
import time
import unicodedata
from datetime import date, datetime

import requests

import config

SOURCE = "https://app.gov.al/GetData/ExportDocument?year={year}"
LIST_URL = "https://www.app.gov.al/njoftimi-i-kontrat%C3%ABs-s%C3%AB-shpallur/"
FILE = config.BASE_DIR / "data" / "tenders.json"
KEEP = 6 * 3600
TIMEOUT = 120
NEW_DAYS = 7
SOON_DAYS = 3
# one line per category: name | keywords, comma separated | cpv=prefixes; "related" marks a category of less weight
DEFAULT_RULES = """\
Catering | catering, katering, kokteil, koktej, coffee break, pushim kafe, bufe, dreke, dreka, darke, darka, mengjes, vakt, ushqim te gatuar, ushqim i gatuar, gatim, mense, sherbim ushqimi, sherbime ushqimi, sherbimi i ushqimit | cpv=5552,5551,5532,555
Accommodation | akomodim, fjetje, hotel, bujtin, resort | cpv=551,552
Restaurant & bar | restorant, kafene, bar kafe, bar-kafe, pije freskuese, sherbim kafeje | cpv=553,554
Events | eventi, event, ceremoni, konferenc, seminar, pritje zyrtare, pritje-percjellje, pritje percjellje, festiv, gala, organizim aktiviteti, organizim aktivitetesh | cpv=79952,79951,79950,79956
Food supplies | ushqim, ushqimor, artikuj ushqimore, produkte ushqimore | cpv=158,156,153 | related"""
COLUMNS = {"authority": "Autoriteti_kontraktues", "ref": "Numri_i_references", "object": "Objekti_i_prokurimit",
           "procedure": "Lloji_i_procedures", "type": "Tipi_i_kontrates", "framework": "Lloji_i_marreveshjes_kuader",
           "fund": "Fondi_limit", "published": "Data_e_publikimit", "opens": "Data_e_hapjes", "closes": "Data_e_mbylljes",
           "cancelled": "Anulluar", "cancel_reason": "Arsyeja_e_anullimit", "suspended": "Pezulluar", "winner": "Fituesi",
           "winner_nipt": "NIPT_i_fituesit", "winner_value": "Vlera_e_fituesit",
           "contract_value": "Vlera_e_fituesit_ne_lidhjen_e_kontrates", "contract_vat": "Lidhja_e_kontrates_me_TVSH",
           "offers": "Numri_i_ofertave_te_dorezuara", "qualified": "Numri_i_ofertave_te_kualifikuara", "cpv": "Kodet_CPV"}
CPV_RE = re.compile(r"(\d{8})-\d\s*([^,]*(?:,(?!\s*\d{8}-\d)[^,]*)*)")
STATUS = {"open": "Open", "soon": "Closing soon", "closed": "Closed, no winner yet", "awarded": "Awarded",
          "cancelled": "Cancelled", "suspended": "Suspended"}
_lock = threading.Lock()
_held = {"at": 0, "data": None}
_reading = threading.Lock()


def fold(s):
    """Lower case, without accents (ë → e, ç → c), single spaces."""
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


def parse_rules(text):
    """[{name, keywords: [folded], cpv: [prefixes], related}] from the setting's lines."""
    out = []
    for line in (text or DEFAULT_RULES).splitlines():
        parts = [p.strip() for p in line.split("|")]
        if not parts or not parts[0] or parts[0].startswith("#"):
            continue
        r = {"name": parts[0][:60], "keywords": [], "cpv": [], "related": False}
        for p in parts[1:]:
            if p.lower().startswith("cpv="):
                r["cpv"] = [c for c in re.findall(r"\d+", p[4:])]
            elif p.lower() == "related":
                r["related"] = True
            else:
                r["keywords"] = [fold(k) for k in p.split(",") if fold(k)]
        out.append(r)
    return out


def _num(v):
    try:
        return float(str(v or "").replace(",", "").strip()) if str(v or "").strip() else None
    except ValueError:
        return None


def _day(v):
    try:
        return datetime.strptime(str(v or "").strip(), "%d.%m.%Y").date().isoformat()
    except ValueError:
        return ""


def cpv_codes(text):
    """[(code, name)] of the CPV column ("55000000-0 Shërbime hotelerie, restorantesh …, 55300000-3 …")."""
    return [(m.group(1), m.group(2).strip().rstrip(",").strip()) for m in CPV_RE.finditer(str(text or ""))]


def match(row, rules, _cache={}):
    """(categories, what matched) of one procedure, or ([], [])."""
    obj = " " + fold(row.get("object")) + " "
    codes = [c for c, _n in row.get("cpvs") or []]
    cats, why = [], []
    for r in rules:
        hit = []
        for k in r["keywords"]:
            pat = _cache.get(k)
            if pat is None:
                pat = _cache[k] = re.compile(r"(?<![0-9a-z])" + re.escape(k))
            if pat.search(obj):
                hit.append(k)
        hit += [f"CPV {c}" for c in codes if any(c.startswith(p) for p in r["cpv"])]
        if hit:
            cats.append(r["name"])
            why += hit[:3]
    return cats, why


def status_of(t, today):
    if t["cancelled"]:
        return "cancelled"
    if t["suspended"]:
        return "suspended"
    if t["winner"]:
        return "awarded"
    if t["closes"] and t["closes"] >= today:
        d = (date.fromisoformat(t["closes"]) - date.fromisoformat(today)).days
        return "soon" if d <= SOON_DAYS else "open"
    return "closed"


def rows_of(lines, rules):
    """The procedures of the CSV's lines that match, one at a time."""
    rd = csv.DictReader(lines)
    for raw in rd:
        x = {k: (raw.get(c) or "").strip() for k, c in COLUMNS.items()}
        t = {"authority": x["authority"], "ref": x["ref"], "object": x["object"], "procedure": x["procedure"],
             "type": x["type"], "framework": x["framework"], "fund": _num(x["fund"]), "published": _day(x["published"]),
             "opens": _day(x["opens"]), "closes": _day(x["closes"]), "cancelled": x["cancelled"].lower() == "po",
             "cancel_reason": x["cancel_reason"], "suspended": x["suspended"].lower() == "po", "winner": x["winner"],
             "winner_nipt": x["winner_nipt"], "winner_value": _num(x["winner_value"]),
             "contract_value": _num(x["contract_value"]), "contract_vat": x["contract_vat"].lower() == "po",
             "offers": int(_num(x["offers"]) or 0), "qualified": int(_num(x["qualified"]) or 0),
             "cpvs": cpv_codes(x["cpv"])}
        cats, why = match(t, rules)
        if cats:
            t["categories"], t["matched"] = cats, why
            t["related_only"] = all(next((r["related"] for r in rules if r["name"] == c), False) for c in cats)
            yield t


def settings():
    try:
        return json.loads(FILE.read_text()).get("settings") or {}
    except (OSError, ValueError):
        return {}


def save_settings(rules_text, actor):
    """The categories and keywords (an empty text: back to the default ones)."""
    rules = parse_rules(rules_text or DEFAULT_RULES)
    if not rules:
        raise ValueError("Give at least one category: name | keywords | cpv=prefixes.")
    with _lock:
        try:
            held = json.loads(FILE.read_text())
        except (OSError, ValueError):
            held = {}
        held["settings"] = {"rules": (rules_text or "").strip(), "by": actor, "at": datetime.now().isoformat(timespec="seconds")}
        FILE.parent.mkdir(exist_ok=True)
        FILE.write_text(json.dumps(held, ensure_ascii=False))
        _held.update(at=0, data=None)
    return rules


def _download(year, rules):
    r = requests.get(SOURCE.format(year=year), timeout=TIMEOUT, stream=True,
                     headers={"User-Agent": "RHG-Pulse/1.0 (tenders monitor)"})
    if r.status_code != 200:
        raise RuntimeError(f"APP answered {r.status_code} for {year}.")
    lines = (ln.decode("utf-8-sig", errors="replace") if isinstance(ln, bytes) else ln
             for ln in r.iter_lines(decode_unicode=False))
    return list(rows_of(lines, rules))


def read(refresh=False, today=None):
    """{tenders, read_at, years, errors, rules}: this year's procedures that match (and last year's in January, for
    the tenders still open), kept six hours."""
    with _lock:
        if _held["data"] and not refresh and time.time() - _held["at"] < KEEP:
            return _held["data"]
    with _reading:                                           # one read at a time; the others wait for it
        with _lock:
            if _held["data"] and not refresh and time.time() - _held["at"] < 60:
                return _held["data"]
        today = today or date.today()
        rules = parse_rules(settings().get("rules") or DEFAULT_RULES)
        years = [today.year] + ([today.year - 1] if today.month == 1 else [])
        out, errors = [], []
        for y in years:
            try:
                out += _download(y, rules)
            except (requests.RequestException, RuntimeError, csv.Error) as exc:
                errors.append(f"{y}: {str(exc)[:200]}")
        if errors and not out:
            try:                                             # APP not reachable: the last read is kept
                held = json.loads(FILE.read_text())
                if held.get("tenders"):
                    held["errors"] = errors
                    return held
            except (OSError, ValueError):
                pass
        data = {"tenders": out, "read_at": datetime.now().isoformat(timespec="seconds"), "years": years,
                "errors": errors, "source": SOURCE.format(year=years[0])}
        with _lock:
            try:
                held = json.loads(FILE.read_text())
            except (OSError, ValueError):
                held = {}
            if not errors:
                held.update(data)
                FILE.parent.mkdir(exist_ok=True)
                FILE.write_text(json.dumps(held, ensure_ascii=False))
            _held.update(at=time.time(), data=data)
        return data


def view(data, today=None):
    """What the screen shows: every match with its status and days left, the counts, the categories."""
    today = (today or date.today()).isoformat()
    rules = parse_rules(settings().get("rules") or DEFAULT_RULES)
    rows = []
    for t in data.get("tenders") or []:
        t = dict(t)
        t["status"] = status_of(t, today)
        t["days_left"] = (date.fromisoformat(t["closes"]) - date.fromisoformat(today)).days if t["closes"] else None
        t["new"] = bool(t["published"]) and (date.fromisoformat(today) - date.fromisoformat(t["published"])).days < NEW_DAYS
        rows.append(t)
    # the open ones closing first, then the rest newest first
    live = sorted((t for t in rows if t["status"] in ("soon", "open")), key=lambda t: (t["closes"], t["published"]))
    rest = sorted((t for t in rows if t["status"] not in ("soon", "open")), key=lambda t: t["published"] or "", reverse=True)
    rows = live + rest
    kpis = {"open": len(live), "open_fund": round(sum(t["fund"] or 0 for t in live if not t["related_only"]), 2),
            "soon": sum(1 for t in live if t["status"] == "soon"),
            "new": sum(1 for t in rows if t["new"]), "awarded": sum(1 for t in rows if t["status"] == "awarded"),
            "awarded_value": round(sum(t["winner_value"] or 0 for t in rows if t["status"] == "awarded"), 2),
            "total": len(rows)}
    # who wins the hospitality tenders: the winners of the year by value (the related categories apart)
    wins = {}
    for t in rows:
        if t["status"] == "awarded" and not t["related_only"]:
            w = wins.setdefault(t["winner_nipt"] or t["winner"], {"winner": t["winner"], "nipt": t["winner_nipt"], "n": 0, "value": 0.0})
            w["n"] += 1
            w["value"] += t["winner_value"] or 0
    return {"tenders": rows, "kpis": kpis, "statuses": STATUS, "read_at": data.get("read_at"), "errors": data.get("errors") or [],
            "categories": [{"name": r["name"], "related": r["related"]} for r in rules],
            "winners": sorted(wins.values(), key=lambda w: -w["value"])[:30], "list_url": LIST_URL,
            "source_url": "https://app.gov.al/export-public-calls/", "rules": settings().get("rules") or DEFAULT_RULES,
            "rules_default": DEFAULT_RULES}
