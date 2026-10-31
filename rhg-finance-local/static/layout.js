/* layout.js · rearrange and hide sections of a control tower (same file in Finance and Purchasing).

   What can move or be hidden: the groups and items of the side menu, the tabs of every tab bar, and the
   blocks of every screen (cards, KPI rows and their KPI cards, grids). Each is known by a stable key
   taken from the page as it is served (the screen, the block's id or its heading), so a layout keeps
   working when figures change.

   Whose layout: a role's layout is what everyone in the role sees; a person's own layout wins over it
   until they reset it. Both are held by identity (GET/POST /api/layout at the root of the site) under
   the rights layout.personal and layout.roles.manage. Hidden = left out of view only; nothing is
   removed, no data access changes, and every hidden section comes back with Show or a reset.

   The page runs as before if identity cannot be reached: no layout is applied, no button is shown. */
(function () {
  "use strict";
  const SCRIPT = document.currentScript;
  const APP = (SCRIPT && SCRIPT.dataset.app) || "finance";
  // Built from parts: the reverse proxy rewrites the literal "/api/" of the towers to their own prefix,
  // and the layout lives with identity at the root.
  const ROOT_API = "/" + "api/";
  const CACHE_KEY = "rhg-layout-" + APP;
  const UNIT_SEL = ".card, .kpis, .grid, .brief";

  const L = {
    me: null, info: null, units: new Map(), conts: new Map(), nav: null, navTokens: new Map(),
    hidden: new Set(), navHidden: new Set(), editing: false, target: "me", base: null, drag: null
  };

  /* ------------------------------------------------------------------ keys */
  function slug(t) {
    return String(t || "").toLowerCase().replace(/&amp;/g, "and").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);
  }
  // A heading with an id is filled by the page (its words change with the period): its id is the key.
  function headingOf(el) {
    const h = el.querySelector("h2, h3, summary, .l");
    return !h ? "" : h.id ? h.id : slug(h.textContent);
  }
  function typeOf(el) {
    return el.classList.contains("kpis") ? "kpis" : el.classList.contains("grid") ? "grid"
      : el.classList.contains("brief") ? "brief" : el.classList.contains("kpi") ? "kpi" : "card";
  }
  // The key of the element that holds a group of movable siblings.
  function contKey(p, viewId) {
    if (p.dataset.lay) return p.dataset.lay;
    if (p.id) return "#" + p.id;
    const up = p.parentElement;
    if (!up || up.tagName === "MAIN" || up === document.body) return viewId + ">?";
    return contKey(up, viewId) + ">" + Array.prototype.indexOf.call(up.children, p);
  }

  function scan() {
    // screen blocks
    document.querySelectorAll("main section.view").forEach(v => {
      const view = v.id.replace(/^v-/, "");
      const seen = {};
      v.querySelectorAll(UNIT_SEL).forEach((el, i) => {
        if (el.dataset.lay || el.closest(".ov-tabs")) return;
        let k = view + "/" + typeOf(el) + ":" + (el.id || headingOf(el) || "n" + i);
        if (seen[k]) k += "-" + (++seen[k]); else seen[k] = 1;
        el.dataset.lay = k;
      });
      v.querySelectorAll("[data-lay]").forEach(el => {
        const p = el.parentElement;
        const ck = contKey(p, view);
        el.dataset.layC = ck;
        addUnit(el, ck, p);
      });
    });
    // tab bars (wherever they sit: some bars move between screens)
    document.querySelectorAll("main .ov-tabs[id]").forEach(bar => {
      const ck = "#" + bar.id;
      bar.querySelectorAll(":scope > button").forEach(b => {
        b.dataset.lay = "tab:" + bar.id + "/" + (b.dataset.t || slug(b.textContent));
        b.dataset.layC = ck;
        addUnit(b, ck, bar);
      });
    });
    // side menu: group headers and items, one flat list
    L.nav = document.getElementById("nav");
    if (L.nav) {
      [...L.nav.children].forEach((el, i) => {
        let t = null;
        if (el.classList.contains("grp")) t = "g:" + slug(el.textContent);
        else if (el.tagName === "BUTTON") t = "i:" + (el.dataset.view || slug(el.textContent));
        if (!t) return;
        el.dataset.layN = t;
        el.dataset.layI = i;
        L.navTokens.set(t, el);
      });
    }
  }
  function addUnit(el, ck, p) {
    let c = L.conts.get(ck);
    if (!c) { c = { el: p, ids: [] }; L.conts.set(ck, c); }
    if (c.el !== p) return;                        // two holders with one key: leave the second alone
    el.dataset.layI = c.ids.length;
    c.ids.push(el.dataset.lay);
    L.units.set(el.dataset.lay, el);
  }

  /* ------------------------------------------------------------------ apply */
  // Puts the given elements back into the places the movable ones hold now, leaving the rest where it is.
  function placeInSlots(parent, current, wanted) {
    const marks = current.map(el => { const m = document.createComment("lay"); parent.insertBefore(m, el); return m; });
    current.forEach(el => el.remove());
    wanted.forEach((el, i) => parent.insertBefore(el, marks[i]));
    marks.forEach(m => m.remove());
  }
  // Saved order first; anything the saved order does not know (new on the page) keeps its own place.
  function arrange(current, saved, keyOf) {
    const byKey = new Map(current.map(el => [keyOf(el), el]));
    const known = (saved || []).filter(k => byKey.has(k)).map(k => byKey.get(k));
    const knownSet = new Set(known);
    const fresh = current.filter(el => !knownSet.has(el)).sort((a, b) => a.dataset.layI - b.dataset.layI);
    const out = [];
    let ki = 0, fi = 0;
    for (let pos = 0; pos < current.length; pos++) {
      if (fi < fresh.length && (+fresh[fi].dataset.layI <= pos || ki >= known.length)) out.push(fresh[fi++]);
      else out.push(known[ki++]);
    }
    return out;
  }

  function apply(layout) {
    const lay = layout || {};
    const order = lay.order || {};
    L.conts.forEach((c, ck) => {
      if (!c.el.isConnected) return;
      const current = [...c.el.children].filter(el => el.dataset.layC === ck && L.units.get(el.dataset.lay) === el);
      if (current.length < 2) return;
      const wanted = arrange(current, order[ck], el => el.dataset.lay);
      if (wanted.some((el, i) => el !== current[i])) placeInSlots(c.el, current, wanted);
    });
    L.hidden = new Set(lay.hidden || []);
    L.units.forEach((el, k) => el.classList.toggle("lay-off", L.hidden.has(k)));
    // side menu
    if (L.nav) {
      const nav = lay.nav || {};
      const current = [...L.nav.children].filter(el => el.dataset.layN);
      const wanted = arrange(current, nav.order, el => el.dataset.layN);
      if (wanted.some((el, i) => el !== current[i])) placeInSlots(L.nav, current, wanted);
      L.navHidden = new Set(nav.hidden || []);
      paintNav();
    }
  }
  // A hidden group hides its items too.
  function paintNav() {
    let groupOff = false;
    [...L.nav.children].forEach(el => {
      const t = el.dataset.layN;
      if (!t) return;
      if (t.startsWith("g:")) groupOff = L.navHidden.has(t);
      const off = L.navHidden.has(t) || (groupOff && !t.startsWith("g:"));
      el.classList.toggle(L.editing ? "lay-ghost" : "lay-off", off);
      el.classList.toggle(L.editing ? "lay-off" : "lay-ghost", false);
    });
  }

  function snapshot() {
    const order = {};
    L.conts.forEach((c, ck) => {
      if (!c.el.isConnected) return;
      const ids = [...c.el.children].filter(el => el.dataset.layC === ck && el.dataset.lay).map(el => el.dataset.lay);
      if (ids.length > 1) order[ck] = ids;
    });
    const navOrder = L.nav ? [...L.nav.children].filter(el => el.dataset.layN).map(el => el.dataset.layN) : [];
    return { v: 1, order, hidden: [...L.hidden], nav: { order: navOrder, hidden: [...L.navHidden] } };
  }

  /* ------------------------------------------------------------------ server */
  async function call(path, body) {
    const opt = { credentials: "same-origin", headers: { "Content-Type": "application/json" } };
    if (body !== undefined) {
      opt.method = "POST";
      opt.headers["X-CSRF-Token"] = (L.me && L.me.csrf) || "";
      opt.body = JSON.stringify(body);
    }
    const r = await fetch(ROOT_API + path, opt);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
  }
  function effective(info) { return (info && (info.personal || info.role_layout)) || null; }
  function remember(layout) {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(layout || null)); } catch (e) { /* storage off */ }
  }

  /* ------------------------------------------------------------------ editing */
  const ICON = {
    grip: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="6" r="1.4"/><circle cx="15" cy="6" r="1.4"/><circle cx="9" cy="12" r="1.4"/><circle cx="15" cy="12" r="1.4"/><circle cx="9" cy="18" r="1.4"/><circle cx="15" cy="18" r="1.4"/></svg>',
    up: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 15l6-6 6 6"/></svg>',
    down: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>',
    eye: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
    off: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18M10.6 5.1A10.7 10.7 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6C3.7 8.4 2 12 2 12s3.6 7 10 7a10 10 0 0 0 5.4-1.6"/></svg>'
  };
  function ctrl(act, label, icon) {
    return `<span class="lay-x" role="button" tabindex="0" data-lay-act="${act}" title="${label}" aria-label="${label}">${icon}</span>`;
  }

  function startEdit() {
    if (L.editing) return;
    L.editing = true;
    document.body.classList.add("lay-edit");
    L.units.forEach((el, k) => {
      if (!el.isConnected) return;
      el.classList.remove("lay-off");
      el.classList.toggle("lay-ghost", L.hidden.has(k));
      el.draggable = true;
      const tab = el.tagName === "BUTTON";
      const row = el.classList.contains("kpis") || el.classList.contains("grid");
      const bar = document.createElement("span");
      bar.className = "lay-ctl" + (tab ? " lay-ctl-in" : row ? " lay-ctl-row" : "");
      bar.innerHTML = (row ? `<em>${el.classList.contains("kpis") ? "KPI row" : "Row"}</em>` : "") + (tab ? ctrl("hide", "Hide or show this tab", L.hidden.has(k) ? ICON.off : ICON.eye)
        : ctrl("grip", "Drag to move", ICON.grip) + ctrl("up", "Move up", ICON.up) + ctrl("down", "Move down", ICON.down)
          + ctrl("hide", L.hidden.has(k) ? "Show this section" : "Hide this section", L.hidden.has(k) ? ICON.off : ICON.eye));
      el.appendChild(bar);
    });
    if (L.nav) {
      L.navTokens.forEach(el => {
        if (el.tagName === "BUTTON" && (el.dataset.parent || el.style.display === "none")) return;
        el.draggable = true;
        const bar = document.createElement("span");
        bar.className = "lay-ctl lay-ctl-in";
        bar.innerHTML = ctrl("hide", el.classList.contains("grp") ? "Hide or show this group" : "Hide or show this item",
          L.navHidden.has(el.dataset.layN) ? ICON.off : ICON.eye);
        el.appendChild(bar);
      });
      paintNav();
    }
    buildBar();
  }
  function stopEdit() {
    L.editing = false;
    document.body.classList.remove("lay-edit");
    document.querySelectorAll(".lay-ctl").forEach(x => x.remove());
    document.querySelectorAll("[draggable=true][data-lay], [draggable=true][data-lay-n]").forEach(el => { el.draggable = false; });
    document.querySelectorAll(".lay-ghost").forEach(el => el.classList.remove("lay-ghost"));
    const bar = document.getElementById("layBar");
    if (bar) bar.remove();
  }
  function setHidden(el, on) {
    const k = el.dataset.lay, t = el.dataset.layN;
    if (k) { if (on) L.hidden.add(k); else L.hidden.delete(k); el.classList.toggle("lay-ghost", on); }
    if (t) { if (on) L.navHidden.add(t); else L.navHidden.delete(t); paintNav(); }
    const x = el.querySelector(':scope > .lay-ctl [data-lay-act="hide"]');
    if (x) {
      x.innerHTML = on ? ICON.off : ICON.eye;
      if (!x.title.startsWith("Hide or")) { x.title = on ? "Show this section" : "Hide this section"; x.setAttribute("aria-label", x.title); }
    }
  }
  function siblingsOf(el) {
    const ck = el.dataset.layC;
    return [...el.parentElement.children].filter(s => s.dataset.layC === ck && s.dataset.lay);
  }
  function step(el, dir) {
    const sib = siblingsOf(el), i = sib.indexOf(el), j = i + dir;
    if (j < 0 || j >= sib.length) return;
    if (dir < 0) sib[j].before(el); else sib[j].after(el);
    el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  // controls (capture: they sit inside buttons and cards that have their own clicks)
  function onCtl(e) {
    const x = e.target.closest && e.target.closest(".lay-x");
    if (!x || !L.editing) return;
    e.preventDefault(); e.stopPropagation();
    if (e.type === "keydown" && e.key !== "Enter" && e.key !== " ") return;
    const el = x.closest("[data-lay], [data-lay-n]");
    const act = x.dataset.layAct;
    if (act === "hide") {
      const k = el.dataset.lay || el.dataset.layN;
      setHidden(el, !(el.dataset.lay ? L.hidden.has(k) : L.navHidden.has(k)));
    } else if (act === "up") step(el, -1);
    else if (act === "down") step(el, 1);
  }
  document.addEventListener("click", onCtl, true);
  document.addEventListener("keydown", e => { if (e.target.closest && e.target.closest(".lay-x")) onCtl(e); }, true);

  /* drag and drop */
  function navBlock(grp) {
    const out = [grp];
    let n = grp.nextElementSibling;
    while (n && !(n.dataset.layN || "").startsWith("g:")) { out.push(n); n = n.nextElementSibling; }
    return out;
  }
  function groupOf(el) {
    let n = el;
    while (n && !(n.dataset.layN || "").startsWith("g:")) n = n.previousElementSibling;
    return n;
  }
  // Side by side (KPI rows, tabs, grids): left or right of the half; stacked: above or below.
  function after(e, el) {
    const r = el.getBoundingClientRect();
    const sideBySide = el.tagName === "BUTTON" || siblingsOf(el).some(s => s !== el && s !== (L.drag && L.drag.el)
      && Math.abs(s.getBoundingClientRect().top - r.top) < 6);
    return sideBySide ? e.clientX > r.left + r.width / 2 : e.clientY > r.top + r.height / 2;
  }
  document.addEventListener("dragstart", e => {
    if (!L.editing) return;
    const el = e.target.closest && e.target.closest("[data-lay], [data-lay-n]");
    if (!el || el !== e.target) return;
    L.drag = { el };
    el.classList.add("lay-dragging");
    try { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", el.dataset.lay || el.dataset.layN); } catch (x) { /* old browser */ }
  });
  document.addEventListener("dragover", e => {
    if (!L.editing || !L.drag) return;
    const d = L.drag.el;
    if (d.dataset.layN) {
      const over = e.target.closest && e.target.closest("[data-lay-n]");
      if (!over || over === d || !L.nav.contains(over)) return;
      e.preventDefault();
      const below = e.clientY > over.getBoundingClientRect().top + over.getBoundingClientRect().height / 2;
      if (d.dataset.layN.startsWith("g:")) {
        const target = groupOf(over);
        if (!target || target === d) return;
        const block = navBlock(d), tb = navBlock(target);
        if (below) tb[tb.length - 1].after(...block); else target.before(...block);
      } else if (over.dataset.layN.startsWith("g:")) {
        if (below) over.after(d); else over.before(d);
      } else if (below) over.after(d); else over.before(d);
      return;
    }
    let over = e.target.closest && e.target.closest("[data-lay]");
    while (over && over.dataset.layC !== d.dataset.layC) over = over.parentElement && over.parentElement.closest("[data-lay]");
    if (!over || over === d || d.contains(over)) return;
    e.preventDefault();
    try { e.dataTransfer.dropEffect = "move"; } catch (x) { /* old browser */ }
    if (after(e, over)) { if (over.nextElementSibling !== d) over.after(d); }
    else if (over.previousElementSibling !== d) over.before(d);
  });
  document.addEventListener("drop", e => { if (L.editing && L.drag) e.preventDefault(); });
  document.addEventListener("dragend", () => {
    if (L.drag) L.drag.el.classList.remove("lay-dragging");
    L.drag = null;
  });
  // keyboard on menu items: Alt + arrow moves the focused item while editing
  document.addEventListener("keydown", e => {
    if (!L.editing || !e.altKey || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
    const el = document.activeElement;
    if (!el || !el.dataset || !(el.dataset.layN || el.dataset.lay)) return;
    e.preventDefault();
    if (el.dataset.lay) { step(el, e.key === "ArrowUp" ? -1 : 1); return; }
    const sib = e.key === "ArrowUp" ? el.previousElementSibling : el.nextElementSibling;
    if (sib) { if (e.key === "ArrowUp") sib.before(el); else sib.after(el); el.focus(); }
  });

  /* the bar at the foot of the screen while editing */
  function targets() {
    const out = [];
    if (L.info.can.personal) out.push(["me", "Only me"]);
    if (L.info.can.roles) (L.info.roles || []).forEach(r => out.push(["role:" + r, "Role · " + r]));
    return out;
  }
  function buildBar() {
    const t = targets();
    const bar = document.createElement("div");
    bar.id = "layBar";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", "Customize layout");
    bar.innerHTML = `<div class="lay-h"><b>Customize layout</b>
        <label>For <select id="layFor">${t.map(([v, l]) => `<option value="${v}"${v === L.target ? " selected" : ""}>${l.replace(/</g, "&lt;")}</option>`).join("")}</select></label></div>
      <p class="lay-tip" id="layTip"></p>
      <div class="lay-b"><button type="button" id="layReset"></button><button type="button" id="layCancel">Cancel</button><button type="button" id="laySave" class="lay-primary">Save</button></div>
      <p class="lay-msg" id="layMsg" role="status" aria-live="polite"></p>`;
    document.body.appendChild(bar);
    paintBar();
    document.getElementById("layFor").onchange = e => switchTarget(e.target.value);
    document.getElementById("layCancel").onclick = () => { stopEdit(); apply(effective(L.info)); };
    document.getElementById("laySave").onclick = save;
    document.getElementById("layReset").onclick = reset;
  }
  function paintBar() {
    const me = L.target === "me";
    const role = me ? "" : L.target.slice(5);
    document.getElementById("layTip").textContent = me
      ? "Drag a section, tab or menu item to move it; the eye hides or shows it. Only you see this layout; it wins over your role's."
      : `Drag a section, tab or menu item to move it; the eye hides or shows it. Everyone with the role ${role} sees this layout, unless they keep one of their own.`;
    document.getElementById("layReset").textContent = me ? "Use my role's layout" : "Back to the standard layout";
  }
  function say(t, err) {
    const m = document.getElementById("layMsg");
    if (m) { m.textContent = t || ""; m.classList.toggle("err", !!err); }
  }
  async function switchTarget(v) {
    L.target = v;
    say("Reading the layout…");
    try {
      let lay;
      if (v === "me") lay = effective(L.info);
      else {
        const j = await call("layout?app=" + APP + "&role=" + encodeURIComponent(v.slice(5)));
        lay = j.asked_layout || null;
      }
      stopEdit(); apply(lay); startEdit(); say("");
      document.getElementById("layFor").value = v;
    } catch (e) { say(e.message, true); }
  }
  async function save() {
    const layout = snapshot();
    const me = L.target === "me";
    say("Saving…");
    try {
      await call("layout", me ? { app: APP, scope: "me", layout } : { app: APP, scope: "role", role: L.target.slice(5), layout });
      await refresh();
      stopEdit();
      apply(effective(L.info));
      note(me ? "Your layout is saved." : `The layout of ${L.target.slice(5)} is saved.`
        + (!me && L.info.personal ? " You keep your own layout; reset it to see the role's." : ""));
    } catch (e) { say(e.message, true); }
  }
  async function reset() {
    const me = L.target === "me";
    const what = me ? "your own layout (your role's layout comes back)" : `the layout of ${L.target.slice(5)} (the standard layout comes back)`;
    if (!confirm(`Remove ${what}? Hidden sections show again.`)) return;
    say("Resetting…");
    try {
      await call("layout", me ? { app: APP, scope: "me", layout: null } : { app: APP, scope: "role", role: L.target.slice(5), layout: null });
      await refresh();
      stopEdit(); apply(effective(L.info));
      note(me ? "Back to your role's layout." : `${L.target.slice(5)} is back to the standard layout.`);
    } catch (e) { say(e.message, true); }
  }
  function note(t) {
    const n = document.createElement("div");
    n.className = "lay-note"; n.setAttribute("role", "status"); n.textContent = t;
    document.body.appendChild(n);
    setTimeout(() => n.remove(), 4200);
  }

  async function refresh() {
    L.info = await call("layout?app=" + APP);
    remember(effective(L.info));
    return L.info;
  }

  function addButton() {
    if (!L.info || !(L.info.can.personal || L.info.can.roles)) return;
    const foot = document.querySelector("aside .side-foot") || document.querySelector("aside");
    if (!foot || document.getElementById("layOpen")) return;
    const b = document.createElement("button");
    b.type = "button"; b.id = "layOpen"; b.className = "lay-open";
    b.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>Customize layout';
    b.onclick = () => {
      L.target = L.info.can.personal ? "me" : "role:" + (L.info.role || (L.info.roles || [])[0] || "");
      if (L.target !== "me") switchTarget(L.target); else startEdit();
    };
    foot.parentNode.insertBefore(b, foot);
  }


  /* ------------------------------------------------------------------ look (pulse tokens from brand.css) */
  const CSS = `
.lay-off{display:none!important}
.lay-open{all:unset;box-sizing:border-box;cursor:pointer;display:flex;align-items:center;gap:8px;margin:10px 6px 0;padding:8px 10px;
  border-radius:9px;font-size:12px;font-weight:600;color:#fff;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.14)}
.lay-open:hover,.lay-open:focus-visible{background:rgba(255,255,255,.16)}
.lay-open svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.8}
body.lay-edit main section.view [data-lay]:not(button){position:relative;outline:1px dashed rgba(95,99,107,.45);outline-offset:3px}
body.lay-edit [data-lay][draggable=true]:not(button){cursor:grab}
body.lay-edit .lay-ghost{opacity:.38}
body.lay-edit .lay-ghost:hover{opacity:.7}
body.lay-edit #nav .lay-ghost{opacity:.4;text-decoration:line-through}
body.lay-edit #nav [data-lay-n]{cursor:grab}
.lay-dragging{opacity:.5!important;outline:2px solid var(--pulse-green,#12a866)!important}
.lay-ctl{position:absolute;top:6px;right:6px;z-index:5;display:flex;gap:2px;align-items:center;padding:2px;border-radius:8px;
  background:var(--pulse-graphite,#16181d);box-shadow:0 2px 8px rgba(22,24,29,.25)}
.lay-ctl em{font-style:normal;font-size:10.5px;font-weight:600;color:#fff;padding:0 6px 0 4px;letter-spacing:.02em}
.lay-ctl-row{top:-13px;right:auto;left:10px}
body.lay-edit main [data-lay]:not(button) > .lay-ctl{opacity:0;transition:opacity .12s}
body.lay-edit main [data-lay]:not(button):hover > .lay-ctl,body.lay-edit main [data-lay]:not(button):focus-within > .lay-ctl,
body.lay-edit main [data-lay].lay-ghost:not(button) > .lay-ctl{opacity:1}
@media (hover:none){ body.lay-edit main [data-lay]:not(button) > .lay-ctl{opacity:1} }
.lay-ctl-in{position:static;display:inline-flex;margin-left:6px;vertical-align:middle;background:transparent;box-shadow:none;padding:0}
.lay-x{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:6px;cursor:pointer;color:#fff}
.lay-x:hover,.lay-x:focus-visible{background:rgba(255,255,255,.16);outline:none}
.lay-x svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.lay-x[data-lay-act=grip] svg{fill:currentColor;stroke:none}
.lay-ctl-in .lay-x{width:20px;height:20px;color:inherit;opacity:.75}
.lay-ctl-in .lay-x:hover{background:rgba(127,127,127,.18);opacity:1}
#layBar{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:60;width:min(760px,calc(100vw - 32px));
  background:var(--card,#fff);color:var(--ink,#16181d);border:1px solid var(--line,#e3e1d9);border-radius:14px;
  box-shadow:0 12px 36px rgba(22,24,29,.18);padding:14px 16px;display:grid;gap:8px;font-size:13px}
#layBar .lay-h{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
#layBar .lay-h b{font-family:var(--rhg-font-display,inherit);font-size:15px;font-weight:800}
#layBar label{display:flex;gap:8px;align-items:center;color:var(--sub,#5f636b);font-size:12px}
#layBar select{font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--line,#e3e1d9);border-radius:8px;background:var(--card,#fff);color:var(--ink,#16181d)}
#layBar .lay-tip{margin:0;color:var(--sub,#5f636b);font-size:12px;line-height:1.5}
#layBar .lay-b{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}
#layBar button{font:inherit;font-size:12.5px;font-weight:600;padding:8px 14px;border-radius:9px;cursor:pointer;
  border:1px solid var(--line,#e3e1d9);background:var(--card,#fff);color:var(--ink,#16181d)}
#layBar button.lay-primary{background:var(--rhg-primary,#0c475e);border-color:var(--rhg-primary,#0c475e);color:#fff}
#layBar .lay-msg{margin:0;font-size:12px;color:var(--sub,#5f636b);min-height:0}
#layBar .lay-msg.err{color:var(--rhg-red,#c2412d);font-weight:600}
body.lay-edit main{padding-bottom:190px}
.lay-note{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:61;background:var(--pulse-graphite,#16181d);color:#fff;
  padding:10px 16px;border-radius:10px;font-size:13px;box-shadow:0 8px 24px rgba(22,24,29,.25)}
@media (prefers-color-scheme:dark){ #layBar button.lay-primary{background:var(--rhg-blue,#a0d8ea);border-color:var(--rhg-blue,#a0d8ea);color:#08303f} }
@media (max-width:760px){ #layBar{bottom:8px;padding:12px} .lay-ctl{top:4px;right:4px} }
`;
  function addStyle() {
    if (document.getElementById("layStyle")) return;
    const st = document.createElement("style");
    st.id = "layStyle"; st.textContent = CSS;
    document.head.appendChild(st);
  }

  /* ------------------------------------------------------------------ start */
  async function start() {
    addStyle();
    scan();
    try {
      const cached = JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
      if (cached) apply(cached);
    } catch (e) { /* storage off */ }
    try {
      const s = await call("session");
      L.me = s.user;
      if (!L.me) return;
      await refresh();
      apply(effective(L.info));
      addButton();
    } catch (e) { /* identity not reachable: the page as served */ }
  }
  window.RhgLayout = { snapshot: () => snapshot(), edit: () => startEdit(), state: L };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
