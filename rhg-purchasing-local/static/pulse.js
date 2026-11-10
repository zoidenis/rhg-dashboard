/* Pulse: the pop-ups and the small charts every screen of the towers shares (same file in Finance, Purchasing and
   identity; keep the three copies identical).

   1. One way back. Every pop-up of the towers is a .pl-bd backdrop holding a .pl-pop. When one opens while another
      is open, the one below is put aside and the new one gets "← Back"; Back closes it and shows the one below again,
      Close (or Escape) closes them all. The screens' own pop-ups need no change for this.
   2. Pulse.pop({title, sub, icon, body, wide}): a pop-up of its own (#pzBd) for the screens that had none; it joins
      the same way back. body: HTML, or a function (el) that fills it (and may fill it again when data comes).
   3. Pulse.on(kind, fn) and data-pulse="kind|id" on any element (with role="button" tabindex="0"): a click or
      Enter opens fn(id, el). One opener per kind of object (an account, a supplier, a day…), so the same object
      opens the same way wherever it is shown.
   4. The pieces: Pulse.tiles, Pulse.panel, Pulse.bars (horizontal bars), Pulse.cols (a column per day or month,
      clickable), Pulse.list (a table whose rows open something), Pulse.fold (a table kept closed under "Table").
   Texts are English; the towers' i18n runtime translates them as they are drawn. */
(function () {
  "use strict";
  if (window.Pulse) return;
  const CSS = `
.pz-back{background:transparent;border:1px solid #2b5a6c;color:#fff;border-radius:8px;padding:6px 12px;cursor:pointer;font:inherit}
.pz-back:focus-visible,.pl-pop .ph .close:focus-visible{outline:2px solid #a0d8ea;outline-offset:2px}
.pz-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}
.pz-tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 11px;min-width:0}
.pz-tile[data-pulse],.pz-tile[data-act]{cursor:pointer}.pz-tile[data-pulse]:hover,.pz-tile[data-act]:hover{border-color:var(--accent)}
.pz-tile .l{font-size:11px;color:var(--sub)}.pz-tile .v{font-size:18px;font-weight:800;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.pz-tile .v.up{color:var(--up)}.pz-tile .v.warn{color:var(--warn)}.pz-tile .v.down{color:var(--down)}.pz-tile .s{font-size:11px;color:var(--sub);overflow-wrap:anywhere}
.pz-panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-top:12px;min-width:0;overflow-x:auto}
.pz-panel>.pz-h{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.pz-panel>.pz-h h3{margin:0;font-size:14px}.pz-panel>.pz-h .meta{font-size:11.5px}
.pz-lg{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:11.5px;color:var(--sub);margin:0 0 6px}
.pz-sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.pz-hb{display:grid;gap:2px}
.pz-hr{display:grid;grid-template-columns:minmax(90px,200px) minmax(0,1fr) auto;gap:12px;align-items:center;padding:5px 8px;border-radius:8px}
.pz-hr[data-pulse],.pz-hr[data-act]{cursor:pointer}.pz-hr[data-pulse]:hover,.pz-hr[data-act]:hover,.pz-hr:focus-visible{background:var(--bg);outline:none}
.pz-hr:focus-visible{box-shadow:inset 0 0 0 2px var(--accent)}
.pz-hr .k{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pz-hr .b{display:flex;gap:2px;height:14px;min-width:0}.pz-hr .b span{height:100%;border-radius:2px}
.pz-hr .b span:first-child{border-radius:4px 2px 2px 4px}.pz-hr .b span:last-child{border-radius:2px 4px 4px 2px}.pz-hr .b span:only-child{border-radius:4px}
.pz-hr .t{font-size:12.5px;white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums}
.pz-cols svg{width:100%;height:auto;display:block}.pz-cols text{font-size:10px;fill:var(--sub)}
.pz-cols g[data-pulse],.pz-cols g[data-act]{cursor:pointer}.pz-cols g[data-pulse]:hover rect.m,.pz-cols g[data-act]:hover rect.m{opacity:.8}
.pz-cols g:focus-visible{outline:none}.pz-cols g:focus-visible rect.hit{stroke:var(--accent);stroke-width:2}
.pz-list tr[data-pulse],.pz-list tr[data-act]{cursor:pointer}.pz-list tr[data-pulse]:hover td,.pz-list tr[data-act]:hover td{background:var(--bg)}
details.pz-fold{margin-top:10px}details.pz-fold>summary{cursor:pointer;font-size:12.5px;color:var(--sub);padding:4px 0}
.pz-cases{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:0 0 12px}
.pz-case{all:unset;box-sizing:border-box;display:block;border:1px solid var(--line);border-radius:10px;padding:10px 12px;cursor:pointer;background:var(--card)}
.pz-case:hover,.pz-case:focus-visible{border-color:var(--accent)}.pz-case .n{font-size:20px;font-weight:700}.pz-case .n.zero{color:var(--sub)}
.pz-case .t{font-size:13px;font-weight:600;margin:2px 0}.pz-case .w{font-size:12px;color:var(--sub)}
@media (max-width:640px){.pz-hr{grid-template-columns:minmax(0,1fr) auto;gap:4px 10px}.pz-hr .b{grid-column:1/-1;grid-row:2}}`;
  const st = document.createElement("style");
  st.textContent = CSS;
  (document.head || document.documentElement).appendChild(st);

  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const num = (v, d) => v == null || isNaN(v) ? "–" : Number(v).toLocaleString("en-US", {maximumFractionDigits: d || 0, minimumFractionDigits: d || 0});
  const money = v => v == null || isNaN(v) ? "–" : Math.abs(v) >= 1e6 ? num(v / 1e6, 1) + " M" : Math.abs(v) >= 1e4 ? num(Math.round(v / 1e3)) + " k" : num(Math.round(v));

  // ---- the way back, over every .pl-bd of the page
  const stack = [];                       // the backdrops put aside, the newest last
  let going = false;                      // Back is closing the top one
  const shown = () => [...document.querySelectorAll(".pl-bd.show")];
  function addBack(bd){
    const ph = bd.querySelector(".pl-pop .ph"); if (!ph || ph.querySelector(".pz-back")) return;
    const b = document.createElement("button");
    b.type = "button"; b.className = "pz-back"; b.textContent = "← Back"; b.setAttribute("data-pzback", "1");
    ph.insertBefore(b, ph.firstChild);
  }
  function watch(bd){
    if (bd.__pz) return;
    bd.__pz = true;
    let was = bd.classList.contains("show");
    new MutationObserver(() => {
      const now = bd.classList.contains("show");
      if (now === was) return;
      was = now;
      if (now) {
        const others = shown().filter(x => x !== bd);
        others.forEach(o => { if (!stack.includes(o)) stack.push(o); o.__pzQuiet = true; o.classList.remove("show"); });
        if (stack.length) addBack(bd);
      } else {
        if (bd.__pzQuiet) { bd.__pzQuiet = false; return; }       // put aside under a new one
        if (going) {
          going = false;
          const prev = stack.pop();
          if (prev) { prev.__pzReturn = true; prev.classList.add("show"); }
        } else {
          while (stack.length) stack.pop();                         // Close or Escape: all of them
        }
      }
    }).observe(bd, {attributes: true, attributeFilter: ["class"]});
    // the screens draw their pop-ups again (another item, more data): Back stays
    const p = bd.querySelector(".pl-pop");
    if (p) new MutationObserver(() => { if (stack.length && bd.classList.contains("show")) addBack(bd); }).observe(p, {childList: true});
  }
  function scan(){ document.querySelectorAll(".pl-bd").forEach(watch); }
  new MutationObserver(scan).observe(document.documentElement, {childList: true, subtree: true});
  document.addEventListener("click", ev => {
    const b = ev.target.closest("[data-pzback]"); if (!b) return;
    ev.preventDefault(); ev.stopPropagation();
    const bd = b.closest(".pl-bd"); if (!bd) return;
    going = true; bd.classList.remove("show");
  }, true);

  // ---- Pulse's own pop-up
  let own = null, ownFill = null;
  const ownBack = [];
  function frame(){
    if (own) return own;
    own = document.createElement("div");
    own.className = "pl-bd"; own.id = "pzBd";
    own.innerHTML = `<div class="pl-pop" id="pzPop" role="dialog" aria-modal="true"></div>`;
    document.body.appendChild(own);
    watch(own);
    // closing Pulse's own pop-up forgets its trail
    new MutationObserver(() => { if (!own.classList.contains("show")) { ownBack.length = 0; ownFill = null; } })
      .observe(own, {attributes: true, attributeFilter: ["class"]});
    own.addEventListener("click", ev => { if (ev.target === own || ev.target.closest("[data-pzclose]")) own.classList.remove("show"); });
    return own;
  }
  document.addEventListener("keydown", ev => { if (ev.key === "Escape" && own && own.classList.contains("show")) own.classList.remove("show"); });
  function pop(o){
    const bd = frame(), p = bd.querySelector(".pl-pop");
    // a pop-up opened from Pulse's own: the one below is kept to come back to (it is drawn again)
    if (bd.classList.contains("show") && ownFill) { const prev = ownFill; pushOwn(prev); }
    ownFill = o;
    p.setAttribute("aria-label", o.label || o.title || "");
    p.style.width = o.wide === false ? "min(760px,100%)" : "";
    p.innerHTML = `<div class="ph">${ownBack.length ? `<button type="button" class="pz-back" data-pzownback="1">← Back</button>` : ""}${o.icon ? `<div class="ic">${o.icon}</div>` : ""}
      <div><h2>${o.title || ""}</h2><div class="sub">${o.sub || ""}</div></div><div class="sp"></div>${o.actions || ""}
      <button class="close" type="button" data-pzclose="1">Close ✕</button></div><div class="pl-body"></div>`;
    const body = p.querySelector(".pl-body");
    if (typeof o.body === "function") o.body(body); else body.innerHTML = o.body || "";
    if (!bd.classList.contains("show")) bd.classList.add("show");
    bd.scrollTop = 0;
    const f = p.querySelector("[data-pzownback]") || p.querySelector("[data-pzclose]"); if (f) f.focus({preventScroll: true});
    return body;
  }
  function pushOwn(o){ ownBack.push(o); }
  document.addEventListener("click", ev => {
    const b = ev.target.closest("[data-pzownback]"); if (!b) return;
    ev.preventDefault();
    const prev = ownBack.pop(); if (!prev) return;
    ownFill = null; pop(prev);
  });

  // ---- openers by kind
  const kinds = {};
  function on(kind, fn){ kinds[kind] = fn; }
  function open(kind, id, el){
    const fn = kinds[kind]; if (!fn) return false;
    fn(id, el || null); return true;
  }
  function act(el){
    const v = el.getAttribute("data-pulse") || "", i = v.indexOf("|");
    return open(i < 0 ? v : v.slice(0, i), i < 0 ? "" : v.slice(i + 1), el);
  }
  document.addEventListener("click", ev => {
    const el = ev.target.closest("[data-pulse]"); if (!el || ev.target.closest("a[href],input,select,textarea")) return;
    if (act(el)) ev.preventDefault();
  });
  document.addEventListener("keydown", ev => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const el = ev.target.closest && ev.target.closest("[data-pulse]"); if (!el || el !== ev.target) return;
    if (act(el)) ev.preventDefault();
  });

  // ---- the pieces
  const attr = a => a ? (/^data-/.test(a) ? ` ${a} role="button" tabindex="0"` : ` data-pulse="${esc(a)}" role="button" tabindex="0"`) : "";
  function tiles(list){
    return `<div class="pz-tiles">${list.filter(Boolean).map(t => `<div class="pz-tile"${attr(t.open)}><div class="l">${esc(t.l)}</div><div class="v ${t.cls || ""}">${t.v == null ? "–" : t.v}</div><div class="s">${t.s || ""}</div></div>`).join("")}</div>`;
  }
  function panel(title, html, meta){
    return `<div class="pz-panel"><div class="pz-h"><h3>${esc(title)}</h3>${meta ? `<span class="meta">${meta}</span>` : ""}</div>${html}</div>`;
  }
  function legend(items){ return `<div class="pz-lg">${items.map(([c, t]) => `<span><span class="pz-sw" style="background:${c}"></span>${esc(t)}</span>`).join("")}</div>`; }
  // items: [{key, label, segs: [[value, colour, name]], text, open}]
  function bars(items, leg, o){
    o = o || {};
    const max = Math.max(1e-9, ...items.map(it => it.segs.reduce((a, s) => a + Math.max(0, s[0] || 0), 0)));
    return (leg ? legend(leg) : "") + `<div class="pz-hb">` + (items.map(it => `<div class="pz-hr"${attr(it.open)}><div class="k" title="${esc(it.key)}">${esc(it.label || it.key)}</div>
      <div class="b" title="${esc(it.segs.filter(s => s[0]).map(s => `${s[2]}: ${num(s[0], o.dec)}`).join(" · "))}">${it.segs.filter(s => s[0] > 0).map(s => `<span style="width:${(s[0] / max * 100).toFixed(2)}%;background:${s[1]}"></span>`).join("")}</div>
      <div class="t">${it.text || ""}</div></div>`).join("") || `<p class="meta">${esc(o.none || "None.")}</p>`) + `</div>`;
  }
  // a column per point: pts [{label, sub, segs: [[value, colour, name]], line, open, tip}]; stacked, the line a dashed mark
  function cols(pts, leg, o){
    o = o || {};
    if (!pts.length) return `<p class="meta">${esc(o.none || "Nothing to show.")}</p>`;
    const W = o.w || 900, H = o.h || 180, top = 16, bot = 22, n = pts.length, bw = W / n, h = H - top - bot;
    const tot = p => p.segs.reduce((a, s) => a + Math.max(0, s[0] || 0), 0);
    const max = Math.max(1e-9, ...pts.map(p => Math.max(tot(p), p.line || 0)));
    const every = Math.ceil(n / (o.labels || 16));
    const g = pts.map((p, i) => {
      const x = i * bw, w = Math.max(2, bw - 3);
      let y = top + h, rects = "";
      p.segs.forEach(s => { const v = Math.max(0, s[0] || 0), hh = v / max * h; if (!hh) return; y -= hh; rects += `<rect class="m" x="${x + 1}" y="${y}" width="${w - 2}" height="${hh}" rx="2" fill="${s[1]}"/>`; });
      const ln = p.line != null ? `<line x1="${x}" x2="${x + w}" y1="${top + h - p.line / max * h}" y2="${top + h - p.line / max * h}" stroke="var(--ink)" stroke-width="2" stroke-dasharray="4 3"/>` : "";
      const tip = p.tip || `${p.label}: ` + p.segs.filter(s => s[0]).map(s => `${s[2]} ${num(s[0], o.dec)}`).join(", ");
      const a = p.open ? (/^data-/.test(p.open) ? ` ${p.open}` : ` data-pulse="${esc(p.open)}"`) + ` role="button" tabindex="0"` : "";
      return `<g${a}><title>${esc(tip)}</title><rect class="hit" x="${x}" y="${top}" width="${bw}" height="${h}" fill="transparent"/>${rects}${ln}
        ${i % every === 0 ? `<text x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${esc(p.label)}</text>` : ""}</g>`;
    }).join("");
    return (leg ? legend(leg) : "") + `<div class="pz-cols"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.aria || "")}">
      <line x1="0" x2="${W}" y1="${top + h}" y2="${top + h}" stroke="var(--line)"/><line x1="0" x2="${W}" y1="${top}" y2="${top}" stroke="var(--line)" stroke-dasharray="2 3"/>
      <text x="${W}" y="${top - 4}" text-anchor="end">${esc(o.maxLabel ? o.maxLabel(max) : money(max))}</text>${g}</svg></div>`;
  }
  // rows: [{cells: [html…], open}], head: [[label, numeric?]…]
  function list(head, rows, o){
    o = o || {};
    const shown = o.limit ? rows.slice(0, o.limit) : rows;
    return `<div class="scroll pz-list"${o.max ? ` style="max-height:${o.max}px"` : ""}><table data-nosort><tr>${head.map(([l, n]) => `<th${n ? ` class="num"` : ""}>${esc(l)}</th>`).join("")}</tr>` +
      (shown.map(r => `<tr${r.open ? (/^data-/.test(r.open) ? ` ${r.open}` : ` data-pulse="${esc(r.open)}"`) + ` tabindex="0"` : ""}>${r.cells.map((c, i) => `<td${head[i] && head[i][1] ? ` class="num"` : ""}>${c == null ? "" : c}</td>`).join("")}</tr>`).join("")
        || `<tr><td colspan="${head.length}" class="meta">${esc(o.none || "None.")}</td></tr>`) + `</table></div>`
      + (o.limit && rows.length > o.limit ? `<p class="meta">${esc(`The first ${num(o.limit)} of ${num(rows.length)}.`)}</p>` : "");
  }
  // a table of the screen kept closed under "Table": wrap(id) moves an existing element in
  function fold(inner, open){ return `<details class="pz-fold"${open ? " open" : ""}><summary>Table</summary>${inner}</details>`; }
  function foldEl(el, label){
    if (!el || el.closest("details.pz-fold")) return;
    const d = document.createElement("details"); d.className = "pz-fold";
    d.innerHTML = `<summary>${esc(label || "Table")}</summary>`;
    el.parentNode.insertBefore(d, el); d.appendChild(el);
  }
  function cases(list){
    return `<div class="pz-cases">${list.map(c => `<button class="pz-case" type="button"${c.open ? (/^data-/.test(c.open) ? ` ${c.open}` : ` data-pulse="${esc(c.open)}"`) : ""}><div class="n ${c.n ? "" : "zero"}">${num(c.n)}</div><div class="t">${esc(c.t)}</div>${c.w ? `<div class="w">${esc(c.w)}</div>` : ""}</button>`).join("")}</div>`;
  }

  window.Pulse = {pop, on, open, tiles, panel, legend, bars, cols, list, fold, foldEl, cases, esc, num, money, scan};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", scan); else scan();
})();
