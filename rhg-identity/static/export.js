/* export.js · Export ▾ on the pages of the platform (Executive dashboard, Control center, Action Center, Unicorn,
   Reports center, Administration), as the towers have it: Excel, PDF or Send by email of what the page or the
   pop-up shows. Served by identity at the root of the site.

   * Any element with class "exp" opens the menu; its own click handler builds the table and calls
     PulseExport.table(name, sheets, notes) (the menu sets the format, or captures the table for the email).
   * PulseExport.view(el, name, notes): the KPI tiles and tables an element shows (pulse.js reads them).
   * Pulse's pop-ups (pulse.js) get "Export ▾" through Pulse.exporter.
   * Only for the roles that may export (a tower's export right); for the others the buttons are hidden.
   The file is built by Purchasing (xlsxtable.py, xlsxpdf.py), as every export of pulse; nothing is changed in any
   system. */
(function () {
  "use strict";
  if (window.PulseExport) return;
  const API = "/" + "api/";
  let fmt = "xlsx", mode = "", captured = null, ME = null, ALLOWED = false;
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

  const CSS = `
.exp-menu{position:absolute;z-index:200;background:var(--card,#fff);border:1px solid var(--line,#e3e1d9);border-radius:10px;box-shadow:0 10px 28px rgba(0,0,0,.22);padding:4px;min-width:190px}
.exp-menu button{display:flex;gap:10px;align-items:center;width:100%;text-align:left;background:none;border:0;padding:8px 12px;border-radius:7px;font:inherit;font-size:13px;color:var(--ink,#16181d);cursor:pointer}
.exp-menu button:hover,.exp-menu button:focus-visible{background:var(--bg,#f5f4f0);outline:none}
.exp-menu .k{font-size:10.5px;font-weight:700;letter-spacing:.04em;padding:2px 6px;border-radius:5px;background:var(--bg,#f5f4f0);color:var(--sub,#6b6f76);min-width:34px;text-align:center}
.exp-menu hr{border:0;border-top:1px solid var(--line,#e3e1d9);margin:4px 2px}
.exp-dlg-bg{position:fixed;inset:0;z-index:210;background:rgba(8,30,40,.55);display:flex;align-items:flex-start;justify-content:center;padding:60px 16px;overflow:auto}
.exp-dlg{background:var(--card,#fff);color:var(--ink,#16181d);border:1px solid var(--line,#e3e1d9);border-radius:14px;width:100%;max-width:480px;padding:18px 20px;box-shadow:0 18px 50px rgba(0,0,0,.3);box-sizing:border-box}
.exp-dlg h3{margin:0 0 2px;font-size:16px}.exp-dlg .meta{font-size:12px;color:var(--sub,#6b6f76)}
.exp-dlg label{display:block;font-size:12.5px;font-weight:600;margin:12px 0 4px}
.exp-dlg input[type=text],.exp-dlg textarea{width:100%;box-sizing:border-box;font:inherit;font-size:13px;padding:8px 10px;border:1px solid var(--line,#e3e1d9);border-radius:8px;background:var(--bg,#f5f4f0);color:inherit}
.exp-dlg textarea{min-height:70px;resize:vertical}
.exp-dlg .fmt{display:flex;gap:8px}.exp-dlg .fmt label{display:flex;align-items:center;gap:6px;margin:0;font-weight:500;padding:7px 12px;border:1px solid var(--line,#e3e1d9);border-radius:8px;cursor:pointer}
.exp-dlg .fmt input{margin:0}.exp-dlg .row{display:flex;gap:8px;justify-content:flex-end;align-items:center;margin-top:16px;flex-wrap:wrap}
.exp-dlg .row button{font:inherit;font-size:13px;font-weight:600;padding:8px 14px;border-radius:9px;border:1px solid var(--line,#e3e1d9);background:var(--card,#fff);color:inherit;cursor:pointer}
.exp-dlg .row button.dark{background:var(--rhg-primary,#0c475e);border-color:var(--rhg-primary,#0c475e);color:#fff}
.exp-dlg .msg{font-size:12.5px;margin-top:10px;min-height:1em}.exp-dlg .msg.err{color:var(--rhg-red,#c0392b)}.exp-dlg .msg.ok{color:var(--rhg-green,#0e8a57)}
.exp-dlg .chk{display:flex;align-items:center;gap:8px;font-weight:500}.exp-dlg .chk input{margin:0}`;
  const st = document.createElement("style"); st.textContent = CSS; (document.head || document.documentElement).appendChild(st);

  // ---------------------------------------------------------------- the file
  async function table(name, sheets, notes) {
    const t = {name: String(name || document.title).slice(0, 150), sheets: sheets || [], notes: notes || []};
    if (mode === "capture") { captured = t; return; }
    if (!t.sheets.length) throw new Error("This shows nothing to export yet.");
    const r = await fetch(API + "export/table", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": (ME && ME.csrf) || ""}, body: JSON.stringify({table: t, format: fmt})});
    if (!r.ok) { let j = {}; try { j = await r.json(); } catch (e) { /* not JSON */ } throw new Error(j.error || `failed (${r.status})`); }
    const url = URL.createObjectURL(await r.blob()), a = document.createElement("a");
    a.href = url; a.download = t.name.replace(/[^A-Za-z0-9._ -]+/g, "").replace(/ /g, "_").slice(0, 120) + (fmt === "pdf" ? ".pdf" : ".xlsx");
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  const stamp = () => "What the page showed, " + new Date().toLocaleString("en-GB");
  // the lists drawn as rows of boxes (the Action Center's items, Unicorn's findings): one sheet each, a column per box
  const LISTS = [[".it", ["Severity · status", "Item", "Owner", "Deadline · value"]], [".u-row", ["Severity · status", "Finding", "Value · impact"]]];
  function text(el) {
    const c = el.cloneNode(true);
    c.querySelectorAll("button, input, select, textarea, svg").forEach(x => x.remove());
    const h = c.innerHTML.replace(/<(br|\/?div|\/?p|\/li)\b[^>]*>/gi, " · ").replace(/<[^>]+>/g, " ");
    const t = document.createElement("textarea"); t.innerHTML = h;
    return t.value.replace(/\s+/g, " ").replace(/(\s*·\s*)+/g, " · ").replace(/^\s*·\s*|\s*·\s*$/g, "").trim();
  }
  const shown = el => !!el.getClientRects().length;
  function lists(el) {
    const out = [];
    for (const [sel, head] of LISTS) {
      const rows = [...el.querySelectorAll(sel)].filter(shown);
      if (!rows.length) continue;
      const groups = new Map();
      rows.forEach(r => { const k = r.parentElement; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(r); });
      for (const [box, rs] of groups) {
        const sec = box.closest("section, .card, .grid2 > *");
        const h = sec && sec.querySelector("h2, h3");
        out.push({title: ((h && text(h)) || "List").slice(0, 31), header: head,
                  rows: rs.map(r => [...r.children].slice(0, head.length).map(text)), formats: head.map(() => "text")});
      }
    }
    return out;
  }
  function view(el, name, notes) {
    if (!window.Pulse || !Pulse.sheets) return Promise.reject(new Error("The page is not ready yet."));
    return table(name, lists(el).concat(Pulse.sheets(el)), [name, stamp(), "Read from pulse; nothing was changed in any system."].concat(notes || []));
  }
  const fail = e => { if (e) alert("Could not export: " + e.message); };

  // ---------------------------------------------------------------- the menu
  let menu = null, owner = null, openedAt = 0;
  const close = () => { if (menu) { menu.remove(); menu = null; if (owner) owner.setAttribute("aria-expanded", "false"); owner = null; } };
  // the button's own export: its handler (its promise kept, so a failure is said), else a click (Pulse's pop-ups)
  const call = el => el.__export ? el.__export() : el.onclick ? el.onclick.call(el, new MouseEvent("click")) : (el.click(), null);
  function run(el, f) {
    fmt = f; el.__expRun = true;
    try { const p = call(el); if (p && p.catch) p.catch(fail); }
    finally { el.__expRun = false; setTimeout(() => { fmt = "xlsx"; }, 0); }
  }
  function capture(el) {
    mode = "capture"; captured = null; el.__expRun = true;
    try { const p = call(el); if (p && p.catch) p.catch(() => {}); } finally { el.__expRun = false; mode = ""; }
    return captured;
  }
  function mailDialog(el) {
    const what = capture(el);
    const bg = document.createElement("div");
    bg.className = "exp-dlg-bg";
    let last = "";
    try { last = localStorage.getItem("pulse-export-to") || ""; } catch (e) { /* storage off */ }
    const title = what ? what.name : document.title;
    bg.innerHTML = `<div class="exp-dlg" role="dialog" aria-modal="true" aria-labelledby="expDlgT">
      <h3 id="expDlgT">Send by email</h3><div class="meta" data-no-i18n>${esc(title)}</div>
      ${what && what.sheets.length ? `<label for="expTo">To</label><input type="text" id="expTo" placeholder="name@rhg.al, other@rhg.al" value="${esc(last)}" autocomplete="email">
      <label>File</label><div class="fmt"><label><input type="radio" name="expFmt" value="xlsx" checked> Excel</label><label><input type="radio" name="expFmt" value="pdf"> PDF</label></div>
      <label for="expNote">Message (optional)</label><textarea id="expNote" placeholder="A line for the people who receive it"></textarea>
      <label class="chk"><input type="checkbox" id="expMe" checked> Send me a copy</label>` :
      `<p class="msg err">This shows nothing to export yet: wait for it to finish reading, then try again.</p>`}
      <div class="msg" id="expMsg" aria-live="polite"></div>
      <div class="row"><button type="button" id="expCancel">${what && what.sheets.length ? "Cancel" : "Close"}</button>${what && what.sheets.length ? `<button type="button" class="dark" id="expSend">Send</button>` : ""}</div></div>`;
    document.body.appendChild(bg);
    const onKey = ev => { if (ev.key === "Escape") { ev.stopPropagation(); ev.preventDefault(); shut(); } };
    const shut = () => { document.removeEventListener("keydown", onKey, true); bg.remove(); el.focus({preventScroll: true}); };
    bg.addEventListener("click", ev => { if (ev.target === bg) shut(); });
    document.addEventListener("keydown", onKey, true);
    bg.querySelector("#expCancel").onclick = shut;
    if (!what || !what.sheets.length) { bg.querySelector("#expCancel").focus(); return; }
    const to = bg.querySelector("#expTo"); to.focus();
    bg.querySelector("#expSend").onclick = async () => {
      const msg = bg.querySelector("#expMsg"), btn = bg.querySelector("#expSend");
      const addr = to.value.trim();
      if (!addr) { msg.className = "msg err"; msg.textContent = "Write at least one address."; to.focus(); return; }
      const f = bg.querySelector('input[name="expFmt"]:checked').value;
      btn.disabled = true; msg.className = "msg"; msg.textContent = f === "pdf" ? "Building the PDF and sending…" : "Building the file and sending…";
      try {
        const r = await fetch(API + "export/email", {method: "POST", credentials: "same-origin",
          headers: {"Content-Type": "application/json", "X-CSRF-Token": (ME && ME.csrf) || ""},
          body: JSON.stringify({table: what, to: addr, format: f, note: bg.querySelector("#expNote").value,
                                copy_me: bg.querySelector("#expMe").checked, title, where: document.title})});
        let j = {}; try { j = await r.json(); } catch (e) { /* not JSON */ }
        if (!r.ok) throw new Error(j.error || `failed (${r.status})`);
        try { localStorage.setItem("pulse-export-to", addr); } catch (e) { /* storage off */ }
        const s = j.sent || {};
        msg.className = "msg ok"; msg.textContent = `Sent to ${(s.to || []).join(", ")}${(s.cc || []).length ? " · copy to " + s.cc.join(", ") : ""}: ${s.file || ""}`;
        btn.textContent = "Sent"; bg.querySelector("#expCancel").textContent = "Close";
      } catch (e) { msg.className = "msg err"; msg.textContent = "Not sent: " + e.message; btn.disabled = false; }
    };
  }
  function open(el) {
    close();
    owner = el; el.setAttribute("aria-expanded", "true");
    menu = document.createElement("div");
    menu.className = "exp-menu"; menu.setAttribute("role", "menu");
    menu.innerHTML = `<button type="button" role="menuitem" data-f="xlsx"><span class="k">XLSX</span><span>Excel</span></button>` +
      `<button type="button" role="menuitem" data-f="pdf"><span class="k">PDF</span><span>PDF</span></button><hr>` +
      `<button type="button" role="menuitem" data-f="mail"><span class="k">@</span><span>Send by email…</span></button>`;
    document.body.appendChild(menu);
    const r = el.getBoundingClientRect();
    menu.style.top = (r.bottom + window.scrollY + 4) + "px";
    menu.style.left = Math.max(8, Math.min(r.left + window.scrollX, document.documentElement.clientWidth + window.scrollX - menu.offsetWidth - 8)) + "px";
    menu.querySelectorAll("button").forEach(b => b.onclick = ev => { ev.stopPropagation(); const f = b.dataset.f; close(); if (f === "mail") mailDialog(el); else run(el, f); });
    menu.querySelector("button").focus({preventScroll: true});
    openedAt = Date.now();
  }
  document.addEventListener("click", ev => {
    const t = ev.target, el = t.closest && t.closest(".exp");
    if (menu && !(t.closest && t.closest(".exp-menu")) && !(el && el.__expRun)) { const same = el === owner; close(); if (same) { ev.preventDefault(); ev.stopPropagation(); return; } }
    if (!el || el.__expRun || el.disabled) return;
    ev.preventDefault(); ev.stopPropagation();
    if (!ALLOWED) { alert("Your role cannot export (it needs the export right of a tower)."); return; }
    open(el);
  }, true);
  document.addEventListener("keydown", ev => {
    if (!menu) return;
    if (ev.key === "Escape") { const o = owner; close(); if (o) o.focus(); ev.stopPropagation(); }
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      const bs = [...menu.querySelectorAll("button")], i = bs.indexOf(document.activeElement);
      bs[(i + (ev.key === "ArrowDown" ? 1 : bs.length - 1)) % bs.length].focus(); ev.preventDefault();
    }
  }, true);
  window.addEventListener("resize", close);
  document.addEventListener("scroll", ev => { if (menu && Date.now() - openedAt > 300 && !menu.contains(ev.target)) close(); }, true);

  // ---------------------------------------------------------------- who may
  function paint() {
    document.querySelectorAll(".exp").forEach(b => { if (!ALLOWED) b.hidden = true; });
    document.documentElement.classList.toggle("no-export", !ALLOWED);
  }
  (async () => {
    try { const r = await fetch(API + "session", {credentials: "same-origin"}); ME = (await r.json()).user; } catch (e) { ME = null; }
    const P = (ME && ME.permissions) || [];
    ALLOWED = !!ME && !ME.report && P.some(p => /\.export$/.test(p));
    if (window.Pulse && Pulse.exporter) Pulse.exporter((name, sheets, notes) => table(name, sheets, notes), () => ALLOWED);
    paint();
    new MutationObserver(() => { if (!ALLOWED) paint(); }).observe(document.documentElement, {childList: true, subtree: true});
  })();
  window.PulseExport = {table, view, allowed: () => ALLOWED};
})();
