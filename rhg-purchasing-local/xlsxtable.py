"""One table of a screen as an Excel file: the screen sends what it shows (its rows, with the numbers unformatted,
and what each column holds), this writes the workbook. Nothing is read from Business Central here: the person
gets the rows they already see, with the filters they chose; the export permission is checked by the server.

sheets: [{"title", "header": [..], "rows": [[..]], "formats": ["text" | "int" | "num" | "qty" | "pct" | "date"],
          "widths": [..] (optional)}]; notes: lines of a "Read me" sheet (what, where from, the filters).
"""
import io
import re

FORMATS = {"int": "#,##0;[Red]-#,##0", "num": "#,##0;[Red]-#,##0", "qty": "#,##0.00;[Red]-#,##0.00",
           "pct": "0.0%;[Red]-0.0%", "date": "yyyy-mm-dd"}
MAX_CELLS = 600_000
MAX_SHEETS = 8


def _title(t, used):
    t = re.sub(r"[\[\]:*?/\\]", " ", str(t or "Sheet")).strip()[:31] or "Sheet"
    base, n = t, 2
    while t.lower() in used:
        t = f"{base[:28]} {n}"
        n += 1
    used.add(t.lower())
    return t


def _value(v, fmt):
    if v is None or v == "":
        return None
    if fmt in FORMATS and fmt != "date":
        try:
            return float(v)
        except (TypeError, ValueError):
            return str(v)[:500]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    return str(v)[:2000]


def filename(name):
    name = re.sub(r"[^A-Za-z0-9._ -]+", "", str(name or "table")).strip().replace(" ", "_")[:120] or "table"
    return name if name.lower().endswith(".xlsx") else name + ".xlsx"


def render(sheets, notes=()):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    if not isinstance(sheets, list) or not sheets:
        raise ValueError("Nothing to export.")
    sheets = sheets[:MAX_SHEETS]
    cells = sum(len(s.get("rows") or []) * max(1, len(s.get("header") or [])) for s in sheets if isinstance(s, dict))
    if cells > MAX_CELLS:
        raise ValueError(f"The table is too large to export here ({cells:,} cells); use the report's own Excel.")
    wb = Workbook()
    wb.remove(wb.active)
    fill, bold = PatternFill("solid", fgColor="E4F3F9"), Font(bold=True)
    used = set()
    for s in sheets:
        if not isinstance(s, dict):
            continue
        ws = wb.create_sheet(_title(s.get("title"), used))
        header = [str(h)[:200] for h in (s.get("header") or [])]
        fmts = list(s.get("formats") or [])
        fmts += ["text"] * (len(header) - len(fmts))
        ws.append(header)
        for c in ws[1]:
            c.fill, c.font = fill, bold
        for row in s.get("rows") or []:
            if not isinstance(row, list):
                continue
            ws.append([_value(v, fmts[i] if i < len(fmts) else "text") for i, v in enumerate(row[:len(header) or None])])
            for i, c in enumerate(ws[ws.max_row]):
                f = fmts[i] if i < len(fmts) else "text"
                if isinstance(c.value, str):
                    c.data_type = "s"               # a text that starts with = + - @ stays text, never a formula
                elif f in FORMATS:
                    c.number_format = FORMATS[f]
        widths = s.get("widths") or []
        for i in range(len(header)):
            w = widths[i] if i < len(widths) and isinstance(widths[i], (int, float)) else (
                34 if i == 1 and fmts[i] == "text" else 14)
            ws.column_dimensions[get_column_letter(i + 1)].width = max(6, min(80, w))
        ws.freeze_panes = "A2"
        if ws.max_row > 1 and header:
            ws.auto_filter.ref = ws.dimensions
    notes = [str(n)[:2000] for n in (notes or [])][:60]
    if notes:
        rd = wb.create_sheet("Read me")
        for n in notes:
            rd.append([n])
            rd[rd.max_row][0].data_type = "s"
        rd.column_dimensions["A"].width = 130
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
