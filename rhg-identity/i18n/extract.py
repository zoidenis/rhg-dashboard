"""Collects the English UI text of pulse (HTML markup and the strings of the page scripts) as normalized keys:
whitespace collapsed, split on " · ", every ${...} and every number replaced by "#"."""
import html
import json
import re
import sys
from html.parser import HTMLParser

ROOT = sys.argv[1] if len(sys.argv) > 1 else str(__import__("pathlib").Path(__file__).resolve().parents[2])
FILES = ["rhg-finance-local/static/index.html", "rhg-purchasing-local/static/index.html",
         "rhg-identity/static/admin.html", "rhg-identity/static/control.html", "rhg-identity/static/executive.html",
         "rhg-identity/static/login.html", "rhg-identity/static/unicorn.html",
         "rhg-finance-local/static/layout.js", "rhg-finance-local/static/recipients.js",
         "rhg-identity/static/locintel.js", "rhg-finance-local/static/tables.js"]

NUM = re.compile(r"[−+\-]?(?:\d[\d.,]*)(?:\s?(?:%|k|M|B|pts|s|h|d)\b)?")
SEP = re.compile(r"\s+·\s+|\s*·\s*$|^\s*·\s*")
CODEY = re.compile(r"[{}=<>\\]|^[a-z-]+:\S|;\s*[a-z-]+:|\b[a-z]+[A-Z][a-zA-Z]*\(|^[a-z0-9_.\-/:#]+$|^\W*$|https?:|\.(js|css|svg|png|json)\b|^[a-z]+(-[a-z0-9]+)+$")

keys = {}


def norm(t):
    t = html.unescape(t).replace("\x00", "#")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def add(text, src):
    text = norm(text)
    if not text:
        return
    for seg in SEP.split(text):
        seg = seg.strip(" ,;:")
        seg = NUM.sub("#", seg).strip()
        seg = re.sub(r"(#\s*){2,}", "# ", seg).strip()
        seg = re.sub(r"#\s?(?:%|k\b|M\b|B\b|pts\b)", "#", seg)
        if len(re.findall(r"[A-Za-z]", seg)) < 2:
            continue
        if not re.search(r"[a-z]", seg) and not re.match(r"^[A-Z][A-Z ]{2,}$", seg):
            continue
        if CODEY.search(seg) or seg.startswith(("[", ".", "#")) and re.match(r"^[#.\[][A-Za-z]", seg) \
                or "/api" in seg or "@" in seg or re.match(r"^[a-z]+(\s[a-z]+(-[a-z]+)+)+$", seg):
            continue
        if re.match(r"^[a-z]", seg) and " " not in seg:
            continue                                     # one lower-case word: almost always a key or class
        k = keys.setdefault(seg, {"n": 0, "src": set()})
        k["n"] += 1
        k["src"].add(src)


class P(HTMLParser):
    def __init__(self, src):
        super().__init__(convert_charrefs=False)
        self.src, self.skip = src, 0
        self.scripts = []
        self.cur = None

    buf = None

    def flush(self):
        if self.buf:
            add("".join(self.buf), self.src)
        self.buf = []

    def handle_starttag(self, tag, attrs):
        self.flush()
        if tag in ("script", "style"):
            self.skip += 1
            if tag == "script":
                self.cur = []
        for a, v in attrs:
            if a in ("title", "placeholder", "aria-label", "alt") and v:
                add(v, self.src)

    def handle_endtag(self, tag):
        self.flush()
        if tag in ("script", "style"):
            self.skip -= 1
            if tag == "script" and self.cur is not None:
                self.scripts.append("".join(self.cur))
                self.cur = None

    def handle_data(self, data):
        if self.skip:
            if self.cur is not None:
                self.cur.append(data)
            return
        if self.buf is None:
            self.buf = []
        self.buf.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


def js_strings(code):
    """String and template literals of a script; ${...} becomes \\x00 (nested templates are scanned too)."""
    out, i, n = [], 0, len(code)
    while i < n:
        c = code[i]
        if c == "/" and i + 1 < n and code[i + 1] == "/":
            j = code.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and code[i + 1] == "*":
            j = code.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "\"'":
            j, buf = i + 1, []
            while j < n and code[j] != c:
                if code[j] == "\\" and j + 1 < n:
                    buf.append(code[j + 1]); j += 2; continue
                if code[j] == "\n":
                    break
                buf.append(code[j]); j += 1
            out.append("".join(buf))
            i = j + 1
            continue
        if c == "`":
            j, buf = i + 1, []
            while j < n and code[j] != "`":
                if code[j] == "\\" and j + 1 < n:
                    buf.append(code[j + 1]); j += 2; continue
                if code[j] == "$" and j + 1 < n and code[j + 1] == "{":
                    depth, k = 1, j + 2
                    while k < n and depth:
                        if code[k] == "{":
                            depth += 1
                        elif code[k] == "}":
                            depth -= 1
                        elif code[k] == "`":           # nested template: skip to its end roughly
                            pass
                        k += 1
                    inner = code[j + 2:k - 1]
                    out.extend(js_strings(inner))
                    buf.append("\x00")
                    j = k
                    continue
                buf.append(code[j]); j += 1
            out.append("".join(buf))
            i = j + 1
            continue
        i += 1
    return out


def add_js_literal(s, src):
    # a literal glued to a value with "+" (" without", "% of all", "Owed ") shows up next to that value
    if "<" not in s and re.match(r"^[\s%]", s) and re.search(r"[A-Za-z]{2}", s):
        add("\x00" + s, src)
    if "<" not in s and re.search(r"\s$", s) and re.search(r"[A-Za-z]{2}", s):
        add(s + "\x00", src)
    # an HTML fragment: each text between tags is a text node; attribute values that are text count too
    for m in re.finditer(r'\b(?:title|placeholder|aria-label)="([^"]*)"', s):
        add(m.group(1), src)
    for piece in re.split(r"<[^>]*>", s):
        add(piece, src)


for f in FILES:
    path = f"{ROOT}/{f}"
    text = open(path, encoding="utf-8").read()
    if f.endswith(".html"):
        p = P(f)
        p.feed(text)
        for code in p.scripts:
            for s in js_strings(code):
                add_js_literal(s, f)
    else:
        for s in js_strings(text):
            add_js_literal(s, f)

out = {k: {"n": v["n"], "src": sorted(v["src"])} for k, v in sorted(keys.items())}
json.dump(out, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=0)
print(len(out))
