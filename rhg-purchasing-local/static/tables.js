/* Sort and filter for every table of the towers.

   Any <table> with a header row (a row of <th> cells) and at least three rows of data gets:
     * sortable headers: a click sorts by that column (numbers and dates largest first, text A to Z),
       a second click reverses, a third gives back the order the screen drew;
     * a filter box above it once it has eight rows or more: the rows that hold every word typed.
   Tables are drawn into innerHTML long after load and redrawn on every refresh, so they are
   watched and the order and filter chosen are put back after each redraw.

   Left alone: statements (a total row in the middle, such as a P&L), tables with fewer than three
   rows, and any table inside an element with data-nosort. A table that sorts itself (its headers
   have their own click handlers) keeps its sorting and only gets the filter. Total rows stay at the bottom; rows that
   belong to the row above (detail and family rows, full-width notes) move and filter with it.
   The data and the screen's own handlers are untouched: rows are only reordered or hidden. */
(function () {
  "use strict";
  if (window.RHGTables) return;
  const CSS = `
th.rt-th{cursor:pointer;-webkit-user-select:none;user-select:none}
th.rt-th:hover{text-decoration:underline;text-decoration-color:currentColor;text-underline-offset:3px}
th.rt-th:focus-visible{outline:2px solid var(--accent,#0c475e);outline-offset:-2px}
th.rt-th::after{content:"\\2195";opacity:.28;margin-left:4px;font-size:.85em;font-weight:400}
th.rt-th[aria-sort=ascending]::after{content:"\\25B2";opacity:.75;font-size:.7em}
th.rt-th[aria-sort=descending]::after{content:"\\25BC";opacity:.75;font-size:.7em}
tr.rt-hide{display:none!important}
.rt-bar{display:flex;align-items:center;gap:8px;margin:2px 0 8px;flex-wrap:wrap}
.rt-bar input{font:inherit;font-size:12.5px;color:inherit;background:var(--card,#fff);border:1px solid var(--line,#d9dcdd);
  border-radius:8px;padding:5px 9px;width:min(260px,100%);box-sizing:border-box}
.rt-bar input:focus{outline:2px solid var(--accent,#0c475e);outline-offset:0;border-color:transparent}
.rt-bar .rt-n{font-size:11.5px;color:var(--sub,var(--muted,#5c7482))}
.rt-none td{color:var(--sub,#5c7482);font-style:italic}`;
  const style = document.createElement("style");
  style.textContent = CSS;
  (document.head || document.documentElement).appendChild(style);

  const FILTER_FROM = 8, SORT_FROM = 3;
  const state = {};                       // table key -> {col, dir, q}
  const TOTAL = /(^|\s)(total|ty-total|grand|rt-pin)(\s|$)/;
  const CHILD = /(^|\s)(ty-fam|sub|child|detail|drill-row|rt-child)(\s|$)/;
  const EMPTY = /^(|–|—|-|n\/a|none)$/i;

  // ---- reading a cell
  function cellAt(row, col) {
    let i = 0;
    for (const c of row.cells) {
      const span = c.colSpan || 1;
      if (col >= i && col < i + span) return span > 1 && col !== i ? null : c;
      i += span;
    }
    return null;
  }
  function text(c) {
    if (!c) return "";
    if (c.dataset && c.dataset.sort != null) return c.dataset.sort;
    const t = (c.innerText || c.textContent || "").trim();
    return t.split("\n")[0].trim();
  }
  const MULT = {k: 1e3, m: 1e6, bn: 1e9, b: 1e9};
  function num(s) {
    let t = s.replace(/[\s  ]/g, "").replace(/[−–—]/g, "-").replace(/^[▲▼→▲▼→]+/, "");
    t = t.replace(/(ALL|EUR|LEK|USD|L|days?|pts|×|x|d)$/i, "");
    let pct = false;
    if (t.endsWith("%")) { pct = true; t = t.slice(0, -1); }
    t = t.replace(/\+$/, "");                                    // 999+
    const m = t.match(/^([+-]?)(\d[\d,]*(?:\.\d+)?|\.\d+)(k|m|bn|b)?$/i);
    if (!m) return null;
    let v = parseFloat(m[2].replace(/,/g, ""));
    if (m[3]) v *= MULT[m[3].toLowerCase()];
    if (m[1] === "-") v = -v;
    return pct || isFinite(v) ? v : null;
  }
  const MON = {jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11};
  function day(s) {
    let m = s.match(/^(\d{4})-(\d{2})(?:-(\d{2}))?(?:[T ](\d{2}):(\d{2}))?/);
    if (m) return Date.UTC(+m[1], +m[2] - 1, +(m[3] || 1), +(m[4] || 0), +(m[5] || 0));
    m = s.match(/^(\d{1,2})[./](\d{1,2})[./](\d{4})/);
    if (m) return Date.UTC(+m[3], +m[2] - 1, +m[1]);
    const mo = w => MON[w.slice(0, 3).toLowerCase()];
    m = s.match(/^(\d{1,2})\s+([A-Za-z]{3,})\.?\s+(\d{4})/);
    if (m && mo(m[2]) != null) return Date.UTC(+m[3], mo(m[2]), +m[1]);
    m = s.match(/^([A-Za-z]{3,})\.?\s+(\d{4})$/);
    if (m && mo(m[1]) != null) return Date.UTC(+m[2], mo(m[1]), 1);
    return null;
  }

  // ---- the table's shape
  function headerRow(t) {
    let h = -1;
    for (let i = 0; i < t.rows.length; i++) {
      const r = t.rows[i];
      if (!r.cells.length || [...r.cells].some(c => c.tagName !== "TH")) break;
      h = i;
    }
    return h;
  }
  function shape(t) {
    if (t.closest("[data-nosort]") || t.closest("svg")) return null;
    const h = headerRow(t);
    if (h < 0) return null;
    const head = t.rows[h];
    let ncols = 0;
    for (const c of head.cells) ncols += c.colSpan || 1;
    const rows = [...t.rows].slice(h + 1).filter(r => !(r.parentElement && r.parentElement.tagName === "TFOOT"));
    const units = [], pinned = [];
    let pinnedSeen = false, statement = false;
    for (const r of rows) {
      const cls = r.className || "";
      const full = r.cells.length === 1 && (r.cells[0].colSpan || 1) >= Math.max(2, ncols - 1);
      if (TOTAL.test(cls)) { pinned.push(r); pinnedSeen = true; continue; }
      if (pinnedSeen) { statement = true; break; }          // a total in the middle: a statement
      if ((CHILD.test(cls) || r.dataset.parent != null || full) && units.length) {
        units[units.length - 1].rows.push(r); continue;
      }
      if (full) continue;                                     // a note or an empty state on its own
      units.push({rows: [r], i: units.length});
    }
    if (statement) return null;
    // the order the screen drew: stamped on first sight, so sorting can be undone
    let next = 0;
    units.forEach(u => { const o = u.rows[0].dataset.rtO; if (o != null) next = Math.max(next, +o + 1); });
    units.forEach(u => { const r = u.rows[0]; if (r.dataset.rtO == null) r.dataset.rtO = next++; u.i = +r.dataset.rtO; });
    return {head, h, ncols, units, pinned, body: rows.length ? rows[0].parentElement : null};
  }

  function key(t) {
    if (t.id) return "#" + t.id;
    let p = t.parentElement;
    while (p && !p.id) p = p.parentElement;
    const scope = p || document.body;
    return (p ? p.id : "body") + ":" + [...scope.querySelectorAll("table")].indexOf(t);
  }

  // ---- putting the order and the filter in place
  const busy = new WeakSet();
  function apply(t) {
    const sh = shape(t);
    if (!sh || !sh.body) { unmark(t); return; }
    const k = key(t), st = state[k] || (state[k] = {col: null, dir: 0, q: ""});
    const n = sh.units.length;
    // a table that sorts itself (its own header handlers) keeps its sorting; it still gets the filter
    const own = [...sh.head.cells].some(c => typeof c.onclick === "function");
    if (own) { st.col = null; st.dir = 0; }
    busy.add(t);
    try {
      // headers
      let col = 0;
      for (const c of sh.head.cells) {
        const span = c.colSpan || 1;
        if (!own && n >= SORT_FROM && span === 1 && (c.textContent || "").trim() && !c.hasAttribute("data-nosort")) {
          c.classList.add("rt-th");
          c.dataset.rtCol = col;
          if (!c.hasAttribute("tabindex")) c.tabIndex = 0;
          c.setAttribute("aria-sort", st.col === col && st.dir ? (st.dir > 0 ? "ascending" : "descending") : "none");
          if (!c.title) c.title = "Sort by this column";
        } else if (c.classList.contains("rt-th")) {
          c.classList.remove("rt-th"); c.removeAttribute("aria-sort");
        }
        col += span;
      }
      t.dataset.rtKey = k;
      // order
      let order = own ? sh.units : [...sh.units].sort((a, b) => a.i - b.i);
      if (st.col != null && st.dir && n >= SORT_FROM) {
        const vals = sh.units.map(u => text(cellAt(u.rows[0], st.col)));
        const full = vals.filter(v => !EMPTY.test(v));
        const nums = full.map(num), days = full.map(day);
        const kind = full.length && days.filter(v => v != null).length >= full.length * 0.7 ? "day"
          : full.length && nums.filter(v => v != null).length >= full.length * 0.7 ? "num" : "text";
        const val = v => EMPTY.test(v) ? null : kind === "num" ? num(v) : kind === "day" ? day(v) : v;
        const keyed = sh.units.map((u, i) => ({u, v: val(vals[i])}));
        keyed.sort((a, b) => {
          if (a.v == null && b.v == null) return a.u.i - b.u.i;
          if (a.v == null) return 1;                            // blanks last, whichever way
          if (b.v == null) return -1;
          const d = kind === "text" ? String(a.v).localeCompare(String(b.v), undefined, {numeric: true, sensitivity: "base"})
            : a.v - b.v;
          return d * st.dir || a.u.i - b.u.i;
        });
        order = keyed.map(x => x.u);
      }
      const frag = document.createDocumentFragment();
      order.forEach(u => u.rows.forEach(r => frag.appendChild(r)));
      sh.pinned.forEach(r => frag.appendChild(r));
      sh.body.appendChild(frag);
      // filter
      const words = st.q.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      sh.units.forEach(u => {
        const hit = !words.length || words.every(w => u.rows.some(r => (r.textContent || "").toLowerCase().includes(w)));
        u.rows.forEach(r => r.classList.toggle("rt-hide", !hit));
        if (hit) shown++;
      });
      bar(t, k, st, n, shown);
    } finally {
      watcher.takeRecords();
      busy.delete(t);
    }
  }
  function unmark(t) {
    t.querySelectorAll("th.rt-th").forEach(c => { c.classList.remove("rt-th"); c.removeAttribute("aria-sort"); });
    const b = barOf(t); if (b && !b.querySelector("input").value) b.remove();
  }
  function anchor(t) {
    const s = t.parentElement && t.parentElement.classList.contains("scroll") && t.parentElement.children.length === 1
      ? t.parentElement : t;
    return s;
  }
  function barOf(t) {
    const a = anchor(t), p = a.previousElementSibling;
    return p && p.classList.contains("rt-bar") ? p : null;
  }
  function bar(t, k, st, n, shown) {
    let b = barOf(t);
    if (n < FILTER_FROM && !st.q) { if (b) b.remove(); return; }
    if (!b) {
      b = document.createElement("div");
      b.className = "rt-bar";
      b.innerHTML = `<input type="search" placeholder="Filter rows" aria-label="Filter the rows of this table" autocomplete="off"><span class="rt-n" aria-live="polite"></span>`;
      const a = anchor(t);
      a.parentNode.insertBefore(b, a);
    }
    b.dataset.rtFor = k;
    const inp = b.querySelector("input");
    if (document.activeElement !== inp && inp.value !== st.q) inp.value = st.q;
    b.querySelector(".rt-n").textContent = st.q ? `${shown} of ${n} rows` : `${n} rows`;
  }

  // ---- what the user does
  function tableFor(el) {
    const b = el.closest(".rt-bar");
    if (!b) return null;
    let a = b.nextElementSibling;
    if (!a) return null;
    return a.tagName === "TABLE" ? a : a.querySelector("table");
  }
  document.addEventListener("input", e => {
    if (!e.target.closest || !e.target.closest(".rt-bar")) return;
    const t = tableFor(e.target); if (!t) return;
    const st = state[key(t)] || (state[key(t)] = {col: null, dir: 0, q: ""});
    st.q = e.target.value;
    apply(t);
  });
  function sortBy(th) {
    const t = th.closest("table"); if (!t) return;
    const st = state[key(t)] || (state[key(t)] = {col: null, dir: 0, q: ""});
    const col = +th.dataset.rtCol;
    if (st.col !== col) {
      // numbers and dates start with the largest; text starts at A
      const sh = shape(t);
      const vals = sh ? sh.units.map(u => text(cellAt(u.rows[0], col))).filter(v => !EMPTY.test(v)) : [];
      const numeric = vals.length && vals.filter(v => num(v) != null || day(v) != null).length >= vals.length * 0.7;
      st.col = col; st.dir = st.first = numeric ? -1 : 1;
    } else if (st.dir === st.first) {
      st.dir = -st.first;                                     // the other way
    } else {
      st.col = null; st.dir = 0; st.first = 0;                // the order the screen drew
    }
    apply(t);
  }
  document.addEventListener("click", e => {
    const th = e.target.closest && e.target.closest("th.rt-th");
    if (!th || e.target.closest("a,button,input,select,label")) return;
    sortBy(th);
  });
  document.addEventListener("keydown", e => {
    if ((e.key === "Enter" || e.key === " ") && e.target.classList && e.target.classList.contains("rt-th")) {
      e.preventDefault(); sortBy(e.target);
    }
  });

  // ---- watching the screen
  const pending = new Set();
  let timer = null;
  function queue(t) {
    if (!t || busy.has(t)) return;
    pending.add(t);
    if (!timer) timer = setTimeout(flush, 0);
  }
  function flush() {
    timer = null;
    const list = [...pending]; pending.clear();
    list.forEach(t => { if (t.isConnected) apply(t); });
  }
  const watcher = new MutationObserver(ms => {
    for (const m of ms) {
      const t = m.target.nodeType === 1 ? (m.target.tagName === "TABLE" ? m.target : m.target.closest("table")) : null;
      if (t) queue(t);
      for (const n of m.addedNodes) {
        if (n.nodeType !== 1) continue;
        if (n.tagName === "TABLE") queue(n);
        else if (n.querySelectorAll) n.querySelectorAll("table").forEach(queue);
      }
    }
  });
  function start() {
    watcher.observe(document.body || document.documentElement, {childList: true, subtree: true});
    document.querySelectorAll("table").forEach(queue);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
  window.RHGTables = {refresh: t => (t ? [t] : document.querySelectorAll("table")).forEach(queue)};
})();
