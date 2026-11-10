"""Any Excel export of the towers as a PDF: the workbook a screen already builds, page by page.

Export → PDF on a screen asks the same address as Export → Excel with format=pdf; the server builds the workbook as
always (same data, same permission) and this lays it out: landscape A4, one section per sheet, the header row
repeated on every page, numbers right-aligned as their Excel number format shows them (thousands, decimals, %,
negatives in red), long text wrapped, a one-column sheet (Read me) as paragraphs. A very long sheet is cut at
MAX_ROWS rows, said on the page: the Excel file has every row. A large workbook (a sales book of weeks) is read as
a stream (read-only: only the rows the PDF shows are held, the columns as wide as their text). The same file sits
in Finance and Purchasing.
"""
import io
import re
from datetime import date, datetime
from xml.sax.saxutils import escape

MAX_ROWS = 3000
BIG_BYTES = 3_000_000           # above this the workbook is streamed (read-only), not loaded whole
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# Helvetica (the PDF's built-in font) holds Windows-1252: Albanian letters are there, a few signs are not
SIGNS = {"−": "-", "→": "->", "←": "<-", "↔": "<->", "≥": ">=", "≤": "<=", "≠": "!=",
         "≈": "~", "…": "...", "▲": "+", "▼": "-", "✓": "v", "✗": "x", " ": " ",
         " ": " ", " ": " ", "→︎": "->"}


def text(s):
    s = str(s)
    out = []
    for ch in s:
        if ch in SIGNS:
            out.append(SIGNS[ch])
            continue
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            out.append("?")
    return "".join(out)


def pdf_name(name):
    name = str(name or "export")
    return re.sub(r"\.xlsx$", "", name, flags=re.I) + ".pdf"


def _decimals(fmt):
    sec = fmt.split(";")[0]
    m = re.search(r"\.(0+)", sec)
    return len(m.group(1)) if m else 0


def show(v, fmt):
    """(the text of a cell as Excel shows it, is it a number, is it below zero)"""
    if v is None:
        return "", False, False
    if isinstance(v, bool):
        return ("yes" if v else "no"), False, False
    if isinstance(v, datetime):
        return (v.strftime("%Y-%m-%d") if not (v.hour or v.minute) else v.strftime("%Y-%m-%d %H:%M")), False, False
    if isinstance(v, date):
        return v.isoformat(), False, False
    if isinstance(v, (int, float)):
        fmt = fmt or "General"
        if "%" in fmt:
            d = _decimals(fmt)
            return f"{v * 100:,.{d}f}%", True, v < 0
        if fmt == "General":
            if isinstance(v, int) or float(v).is_integer():
                return (f"{int(v):,}" if abs(v) >= 10000 else f"{int(v)}"), True, v < 0
            return f"{v:,.2f}".rstrip("0").rstrip("."), True, v < 0
        d = _decimals(fmt)
        sep = "," if "#,##" in fmt or "," in fmt.split(";")[0] else ""
        return (f"{v:{sep}.{d}f}"), True, v < 0
    return text(v), False, False


def _count_rows(xlsx_bytes, path):
    """The rows of a sheet, by its <row> tags read as bytes (no cell is parsed)."""
    import zipfile
    n, tail = 0, b""
    try:
        with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as z, z.open(path) as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                buf = tail + chunk
                n += buf.count(b"<row ") + buf.count(b"<row>") - tail.count(b"<row ") - tail.count(b"<row>")
                tail = buf[-5:]
    except (KeyError, OSError, zipfile.BadZipFile):
        return 0
    return n


def render(xlsx_bytes, title=""):
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    big = len(xlsx_bytes) > BIG_BYTES
    wb = load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=big)
    ink, muted, line, sky, red = (colors.HexColor(c) for c in ("#0e2733", "#5c7482", "#d9dcdd", "#e4f3f9", "#c0392b"))
    page_w, page_h = landscape(A4)
    margin = 22
    avail = page_w - 2 * margin
    h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=ink)
    h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=ink, spaceBefore=4, spaceAfter=4)
    para = ParagraphStyle("p", fontName="Helvetica", fontSize=8.5, leading=11.5, textColor=ink, spaceAfter=3)
    note = ParagraphStyle("n", fontName="Helvetica-Oblique", fontSize=7.5, leading=9.5, textColor=muted, spaceBefore=3)
    story = [Paragraph(escape(text(title or "Export")), h1),
             Paragraph(escape(f"Exported {datetime.now():%d %b %Y %H:%M} from RHG Control Towers, as the Excel file of the same export."), note),
             Spacer(1, 6)]
    first = True
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            continue
        rows, more = [], 0
        it = ws.iter_rows()
        for r in it:
            rows.append(r)
            if big and len(rows) >= MAX_ROWS + 20:          # streamed: the rest only counted, in the sheet's XML
                more = max(0, _count_rows(xlsx_bytes, getattr(ws, "_worksheet_path", "")) - len(rows))
                break
        # the used area: no trailing empty rows or columns
        while rows and all(c.value in (None, "") for c in rows[-1]):
            rows.pop()
        if not rows:
            continue
        ncol = max((max((i + 1 for i, c in enumerate(r) if c.value not in (None, "")), default=0) for r in rows), default=0)
        if not ncol:
            continue
        if not first:
            story.append(PageBreak())
        first = False
        story.append(Paragraph(escape(text(ws.title)), h2))
        if ncol == 1:                                       # a Read me: its lines as paragraphs
            for r in rows[:MAX_ROWS]:
                v = r[0].value
                if v not in (None, ""):
                    story.append(Paragraph(escape(show(v, r[0].number_format)[0]), para))
            continue
        # title and note lines above the table (only their first cell filled): as paragraphs, the table after them
        while len(rows) > 1 and sum(1 for c in rows[0][:ncol] if c.value not in (None, "")) <= 1:
            v = rows[0][0].value if rows[0] else None
            if v not in (None, ""):
                story.append(Paragraph(escape(show(v, rows[0][0].number_format)[0]), para))
            rows = rows[1:]
        fs = 7.5 if ncol <= 12 else 6.6 if ncol <= 18 else 5.8 if ncol <= 26 else 5.0
        if big:                                             # read-only has no column widths: as wide as the text
            widths = [min(60.0, max([6.0] + [len(show(r[i].value, r[i].number_format)[0]) * 1.1 + 2
                                              for r in rows[:200] if i < len(r) and r[i].value not in (None, "")]))
                      for i in range(ncol)]
        else:
            widths = [float(ws.column_dimensions[get_column_letter(i + 1)].width or 10) for i in range(ncol)]
        widths = [max(4.0, min(60.0, w)) for w in widths]
        scale = avail / sum(widths)
        widths = [w * scale for w in widths]
        cell = ParagraphStyle("c", fontName="Helvetica", fontSize=fs, leading=fs * 1.22, textColor=ink)
        head = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=fs, leading=fs * 1.22, textColor=ink)
        header = any(c.font is not None and c.font.b for c in rows[0][:ncol] if c.value not in (None, ""))
        total = len(rows) + more - (1 if header else 0)
        cut = total > MAX_ROWS
        body_rows = rows[:MAX_ROWS + (1 if header else 0)]
        data, styles = [], [("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), fs),
                            ("TEXTCOLOR", (0, 0), (-1, -1), ink), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LINEBELOW", (0, 0), (-1, -1), 0.25, line),
                            ("TOPPADDING", (0, 0), (-1, -1), 1.6), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6),
                            ("LEFTPADDING", (0, 0), (-1, -1), 2.5), ("RIGHTPADDING", (0, 0), (-1, -1), 2.5)]
        if header:
            styles += [("BACKGROUND", (0, 0), (-1, 0), sky), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
        for ri, r in enumerate(body_rows):
            out = []
            for ci in range(ncol):
                c = r[ci] if ci < len(r) else None
                v = c.value if c is not None else None
                s, num, neg = show(v, c.number_format if c is not None else "General")
                is_head = header and ri == 0
                if num and not is_head:
                    out.append(s)
                    styles.append(("ALIGN", (ci, ri), (ci, ri), "RIGHT"))
                    if neg:
                        styles.append(("TEXTCOLOR", (ci, ri), (ci, ri), red))
                elif is_head or (s and stringWidth(s, "Helvetica", fs) > widths[ci] - 5):
                    out.append(Paragraph(escape(s), head if is_head else cell))
                else:
                    out.append(s)
                if not is_head and c is not None and c.font is not None and c.font.b and s:
                    styles.append(("FONTNAME", (ci, ri), (ci, ri), "Helvetica-Bold"))
            data.append(out)
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
        t.setStyle(TableStyle(styles))
        story.append(t)
        if cut:
            story.append(Paragraph(escape(f"The first {MAX_ROWS:,} rows of {total:,}: "
                                          f"the Excel file of the same export has every row."), note))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(muted)
        canvas.drawString(margin, 12, text(title or "")[:150])
        canvas.drawRightString(page_w - margin, 12, f"page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(page_w, page_h), leftMargin=margin, rightMargin=margin, topMargin=margin,
                            bottomMargin=margin + 6, title=text(title or "Export"), author="RHG Control Towers")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
