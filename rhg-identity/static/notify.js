/* notify.js · the bell of pulse (identity, loaded on every page by gear.js; the page /notifications uses its pieces).

   A bell beside the logo (and beside the Menu button on a phone) with the number of unread notifications: what
   changed on the person's tasks (assigned, handed over, a comment, a change of status or deadline, near or past
   the deadline, escalated, new items, proposals to approve). Click: the latest, Unread or All, mark as read, open the
   task. While a page is open the count is asked again every 45 s (at once when the page comes back into view) and a
   new notification shows for a few seconds at the bottom of the screen. Notifications are made on the server
   (notifications.py); this file only shows them. */
(function () {
  "use strict";
  if (window.PulseNotify) return;
  const framed = (() => { try { return window.top !== window; } catch (e) { return true; } })();
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const API = "/" + "api/notifications";
  let ME = null, UNREAD = 0, LATEST = 0, ITEMS = [], SHOW = "unread", OPEN = false, BASE_TITLE = document.title;

  const I = {
    assigned: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6M22 11h-6"/>',
    handed_over: '<path d="M17 1l4 4-4 4"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><path d="M7 23l-4-4 4-4"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>',
    comment: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    status: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    closed: '<path d="M20 6 9 17l-5-5"/>',
    reopened: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
    due: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    due_soon: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    overdue: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.5"/>',
    escalated: '<path d="M12 19V5M5 12l7-7 7 7"/>',
    new: '<path d="M12 5v14M5 12h14"/>',
    proposal: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>'
  };
  const TONE = {overdue: "bad", escalated: "bad", due_soon: "warn", assigned: "acc", proposal: "acc", new: "acc", closed: "good"};
  const icon = k => `<span class="pn-ic ${TONE[k] || ""}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${I[k] || I.status}</svg></span>`;
  const who = a => a ? `<b class="pn-who" data-no-i18n>${esc(a)}</b> ` : "";
  // the sentence: the person's name kept as it is, the words translated by the page (i18n)
  function sentence(n){
    const a = n.actor, w = who(a);
    switch (n.kind) {
      case "assigned": return a ? `${w}<span>assigned you</span>` : `<span>Assigned to you</span>`;
      case "handed_over": return `${w}<span>${a ? "handed it over to" : "Handed over to"}</span> <b data-no-i18n>${esc(n.to || "")}</b>`;
      case "comment": return a ? `${w}<span>commented</span>` : `<span>New comment</span>`;
      case "status": return `${w}<span>${a ? "moved it to" : "Moved to"}</span> <b>${esc(n.status || "")}</b>`;
      case "closed": return a ? `${w}<span>closed it</span>` : `<span>Closed: no longer found in the data</span>`;
      case "reopened": return a ? `${w}<span>reopened it</span>` : `<span>Reopened</span>`;
      case "due": return `${w}<span>${a ? "moved the deadline to" : "Deadline moved to"}</span> <b data-no-i18n>${esc(n.due || "")}</b>`;
      case "due_soon": return `<span>Due on</span> <b data-no-i18n>${esc(n.due || "")}</b>`;
      case "overdue": return `<span>Overdue since</span> <b data-no-i18n>${esc(n.due || "")}</b>`;
      case "escalated": return n.to === "you" ? `<span>Escalated to you</span> <b data-no-i18n>L${esc(n.level)}</b>` : `<span>Escalated</span> <b data-no-i18n>L${esc(n.level)}</b>`;
      case "new": return n.n ? `<b data-no-i18n>${esc(n.n)}</b> <span>new items for you</span>` : `<span>New item for you</span>`;
      case "proposal": return `<span>Proposal waiting for your approval</span>`;
      default: return esc(n.kind);
    }
  }
  function ago(iso){
    const t = Date.parse(iso), s = (Date.now() - t) / 1000;
    if (!t) return "";
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    if (s < 7 * 86400) return `${Math.floor(s / 86400)} d ago`;
    return new Date(t).toLocaleDateString("en-GB");
  }
  function row(n){
    const where = [n.company, n.location].filter(Boolean).join(" · ");
    return `<a class="pn-row${n.read ? "" : " unread"}" href="${esc(n.link || "/notifications")}" data-pn="${n.id}">
      ${icon(n.kind)}<span class="pn-tx"><span class="pn-s">${sentence(n)}</span>
      ${n.kind === "new" && n.n ? "" : `<span class="pn-t" data-no-i18n>${esc(n.title || "")}</span>`}
      ${n.text ? `<span class="pn-q" data-no-i18n>“${esc(n.text)}”</span>` : ""}
      <span class="pn-m">${esc(ago(n.at))}${where ? ` · <span data-no-i18n>${esc(where)}</span>` : ""}</span></span>
      ${n.read ? "" : '<span class="pn-dot" aria-label="Unread"></span>'}</a>`;
  }

  const CSS = `
.pn-bell{position:relative;display:inline-grid;place-items:center;flex:none;width:34px;height:34px;border-radius:10px;margin-left:auto;
  color:#fff;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18);cursor:pointer;padding:0;transition:background .15s}
.pn-bell + .pg-gear{margin-left:6px}
.pn-bell svg{width:18px;height:18px}
.pn-bell:hover,.pn-bell:focus-visible{background:rgba(255,255,255,.2);outline:none}
.pn-bell:focus-visible{box-shadow:0 0 0 2px #a0d8ea}
.pn-n{position:absolute;top:-6px;right:-7px;min-width:18px;height:18px;padding:0 5px;border-radius:999px;background:#d64545;color:#fff;
  font:700 10.5px/18px Inter,system-ui,sans-serif;text-align:center;box-shadow:0 0 0 2px #08303f}
.pn-n[hidden]{display:none}
.pn-bell.pn-m{display:none;color:var(--ink,#16181d);background:var(--card,#fff);border:1px solid var(--line,#e3e1d9);margin-left:8px;width:38px;height:38px;vertical-align:top}
.pn-bell.pn-m .pn-n{box-shadow:0 0 0 2px var(--card,#fff)}
@media(max-width:760px){.pn-bell.pn-m{display:inline-grid}}
.pn-pop{position:fixed;z-index:9000;width:min(400px,calc(100vw - 24px));max-height:min(620px,calc(100vh - 80px));display:flex;flex-direction:column;
  background:var(--card,#fff);color:var(--ink,#16181d);border:1px solid var(--line,#e3e1d9);border-radius:14px;box-shadow:0 18px 50px rgba(8,48,63,.22);overflow:hidden}
.pn-pop[hidden]{display:none}
.pn-hd{display:flex;align-items:center;gap:8px;padding:14px 16px 10px}
.pn-hd h3{margin:0;font-size:15px;font-weight:700;flex:1}
.pn-lnk{all:unset;cursor:pointer;font-size:12.5px;color:var(--pulse-blue,#3b6fd8);font-weight:600}
.pn-lnk:hover{text-decoration:underline}
.pn-seg{display:flex;gap:6px;padding:0 16px 10px;border-bottom:1px solid var(--line,#e3e1d9)}
.pn-seg button{all:unset;cursor:pointer;font-size:12.5px;padding:5px 12px;border-radius:999px;border:1px solid var(--line,#e3e1d9);color:var(--sub,#5f636b)}
.pn-seg button.on{background:var(--rhg-primary,#0c475e);border-color:var(--rhg-primary,#0c475e);color:#fff}
.pn-list{overflow:auto;flex:1}
.pn-row{display:flex;gap:10px;align-items:flex-start;padding:11px 16px;border-bottom:1px solid var(--line,#e3e1d9);text-decoration:none;color:inherit;position:relative}
.pn-row:hover,.pn-row:focus-visible{background:color-mix(in srgb,var(--rhg-primary,#0c475e) 6%,transparent);outline:none}
.pn-row.unread{background:color-mix(in srgb,var(--pulse-blue,#3b6fd8) 7%,transparent)}
.pn-ic{flex:none;display:grid;place-items:center;width:30px;height:30px;border-radius:50%;background:var(--soft,#eceae3);color:var(--sub,#5f636b)}
.pn-ic svg{width:15px;height:15px}
.pn-ic.bad{background:color-mix(in srgb,#d64545 14%,transparent);color:#c0392b}
.pn-ic.warn{background:color-mix(in srgb,#e0a44a 18%,transparent);color:#b26a00}
.pn-ic.acc{background:color-mix(in srgb,#3b6fd8 14%,transparent);color:#3b6fd8}
.pn-ic.good{background:color-mix(in srgb,#12a866 14%,transparent);color:#0e8a57}
.pn-tx{display:flex;flex-direction:column;gap:2px;min-width:0;flex:1}
.pn-s{font-size:13px;line-height:1.35}
.pn-t{font-size:12.5px;color:var(--sub,#5f636b);overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.pn-q{font-size:12.5px;color:var(--ink,#16181d);font-style:italic;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.pn-m{font-size:11.5px;color:var(--sub,#5f636b)}
.pn-dot{flex:none;width:8px;height:8px;border-radius:50%;background:var(--pulse-blue,#3b6fd8);margin-top:6px}
.pn-empty{padding:28px 16px;text-align:center;color:var(--sub,#5f636b);font-size:13px}
.pn-ft{display:flex;justify-content:center;padding:10px 16px}
.pn-toasts{position:fixed;right:16px;bottom:16px;z-index:9100;display:flex;flex-direction:column;gap:8px;width:min(360px,calc(100vw - 32px))}
.pn-toast{background:var(--card,#fff);border:1px solid var(--line,#e3e1d9);border-left:4px solid var(--pulse-blue,#3b6fd8);border-radius:12px;box-shadow:0 12px 32px rgba(8,48,63,.2);
  animation:pnIn .25s ease}
.pn-toast .pn-row{border:0;padding:10px 12px}
@keyframes pnIn{from{transform:translateY(12px);opacity:0}to{transform:none;opacity:1}}
@media (prefers-reduced-motion: reduce){.pn-toast{animation:none}}
@media print{.pn-bell,.pn-pop,.pn-toasts{display:none!important}}`;

  const BELL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>';
  let pop = null, toasts = null;
  const bells = [];

  async function get(path){
    const r = await fetch(path, {credentials: "same-origin"});
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }
  async function post(path, body){
    const r = await fetch(path, {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": (ME && ME.csrf) || ""}, body: JSON.stringify(body || {})});
    return r.ok ? r.json() : null;
  }
  function paintCount(){
    bells.forEach(b => {
      const n = b.querySelector(".pn-n");
      n.hidden = !UNREAD; n.textContent = UNREAD > 99 ? "99+" : String(UNREAD);
      b.setAttribute("aria-label", UNREAD ? `Notifications: ${UNREAD} unread` : "Notifications");
      b.title = b.getAttribute("aria-label");
    });
    document.title = (UNREAD ? `(${UNREAD > 99 ? "99+" : UNREAD}) ` : "") + BASE_TITLE.replace(/^\(\d+\+?\) /, "");
  }
  function makeBell(cls){
    const b = document.createElement("button");
    b.type = "button"; b.className = "pn-bell" + (cls ? " " + cls : "");
    b.setAttribute("aria-haspopup", "dialog"); b.setAttribute("aria-expanded", "false");
    b.innerHTML = BELL + '<span class="pn-n" hidden></span>';
    b.addEventListener("click", e => { e.stopPropagation(); toggle(b); });
    bells.push(b);
    return b;
  }
  function place(b){
    const r = b.getBoundingClientRect(), w = Math.min(400, window.innerWidth - 24);
    let left = r.left;
    if (left + w > window.innerWidth - 12) left = window.innerWidth - 12 - w;
    pop.style.left = Math.max(12, left) + "px";
    pop.style.top = Math.min(r.bottom + 8, window.innerHeight - 200) + "px";
  }
  function paintList(){
    if (!pop) return;
    const shown = SHOW === "unread" ? ITEMS.filter(n => !n.read) : ITEMS;
    pop.querySelector(".pn-list").innerHTML = shown.length ? shown.map(row).join("")
      : `<div class="pn-empty">${SHOW === "unread" ? "No unread notifications." : "No notifications yet."}</div>`;
    pop.querySelectorAll(".pn-seg button").forEach(x => x.classList.toggle("on", x.dataset.s === SHOW));
  }
  async function load(){
    try { const d = await get(API + "?limit=30"); ITEMS = d.items; UNREAD = d.unread; LATEST = Math.max(LATEST, ...ITEMS.map(n => n.id), 0); paintCount(); paintList(); }
    catch (e) { if (pop) pop.querySelector(".pn-list").innerHTML = `<div class="pn-empty">Could not read the notifications.</div>`; }
  }
  function buildPop(){
    pop = document.createElement("div");
    pop.className = "pn-pop"; pop.hidden = true; pop.setAttribute("role", "dialog"); pop.setAttribute("aria-label", "Notifications");
    pop.innerHTML = `<div class="pn-hd"><h3>Notifications</h3><button class="pn-lnk" type="button" data-all>Mark all as read</button></div>
      <div class="pn-seg"><button type="button" data-s="unread" class="on">Unread</button><button type="button" data-s="all">All</button></div>
      <div class="pn-list"></div><div class="pn-ft"><a class="pn-lnk" href="/notifications">See all and choose what you receive</a></div>`;
    document.body.appendChild(pop);
    pop.addEventListener("click", async e => {
      e.stopPropagation();
      const s = e.target.closest("[data-s]");
      if (s) { SHOW = s.dataset.s; paintList(); return; }
      if (e.target.closest("[data-all]")) { const d = await post(API + "/read", {all: true}); if (d) { ITEMS.forEach(n => n.read = true); UNREAD = d.unread; paintCount(); paintList(); } return; }
      const a = e.target.closest("[data-pn]");
      if (a) { e.preventDefault(); go(a); }
    });
    document.addEventListener("click", () => { if (OPEN) close(); });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && OPEN) { close(); (bells.find(b => b.offsetParent) || bells[0]).focus(); } });
    window.addEventListener("resize", () => { if (OPEN) close(); });
  }
  async function go(a){
    const id = +a.dataset.pn, n = ITEMS.find(x => x.id === id);
    if (n && !n.read) { n.read = true; UNREAD = Math.max(0, UNREAD - 1); paintCount(); post(API + "/read", {ids: [id]}); }
    const href = a.getAttribute("href");
    try {
      const u = new URL(href, location.origin);
      if (u.pathname === location.pathname && u.hash) { location.hash = u.hash; close(); window.dispatchEvent(new HashChangeEvent("hashchange")); return; }
    } catch (e) { /* a plain path */ }
    location.href = href;
  }
  function toggle(b){ OPEN ? close() : open(b); }
  function open(b){
    if (!pop) buildPop();
    OPEN = true; pop.hidden = false; place(b); b.setAttribute("aria-expanded", "true");
    SHOW = UNREAD ? "unread" : "all"; paintList(); load();
  }
  function close(){ OPEN = false; if (pop) pop.hidden = true; bells.forEach(b => b.setAttribute("aria-expanded", "false")); }
  function toast(n){
    if (!toasts) { toasts = document.createElement("div"); toasts.className = "pn-toasts"; toasts.setAttribute("aria-live", "polite"); document.body.appendChild(toasts); }
    const t = document.createElement("div");
    t.className = "pn-toast"; t.innerHTML = row(n);
    t.addEventListener("click", e => { const a = e.target.closest("[data-pn]"); if (a) { e.preventDefault(); if (!ITEMS.some(x => x.id === n.id)) ITEMS.unshift(n); go(a); } });
    toasts.appendChild(t);
    while (toasts.children.length > 3) toasts.firstChild.remove();
    setTimeout(() => t.remove(), 9000);
  }
  async function poll(first){
    if (document.visibilityState !== "visible" && !first) return;
    try {
      const c = await get(API + "/count");
      const before = LATEST;
      UNREAD = c.unread; paintCount();
      if (c.latest > LATEST) {
        LATEST = c.latest;
        if (!first && before) {
          const d = await get(API + "?after=" + before + "&limit=5");
          d.items.filter(n => !n.read).reverse().forEach(toast);
          ITEMS = d.items.concat(ITEMS.filter(x => !d.items.some(y => y.id === x.id))).slice(0, 60);
          if (OPEN) paintList();
        }
      }
      window.dispatchEvent(new CustomEvent("pulse-notify", {detail: {unread: UNREAD, latest: LATEST}}));
    } catch (e) { /* signed out or offline: the next poll tries again */ }
  }

  window.PulseNotify = {sentence, row, icon, ago, esc, refresh: () => poll(false), setUnread: n => { UNREAD = n; paintCount(); }};

  async function start(){
    if (framed || /[?&]embed=/.test(location.search)) return;
    let u = null;
    try { u = (await get("/" + "api/session")).user; } catch (e) { return; }
    if (!u || u.report || u.must_change) return;
    ME = u; BASE_TITLE = document.title;
    const st = document.createElement("style"); st.textContent = CSS; document.head.appendChild(st);
    const brand = document.querySelector("aside .brand, aside .abrand");
    if (brand && !brand.querySelector(".pn-bell")) {
      const b = makeBell(), g = brand.querySelector(".pg-gear");
      g ? brand.insertBefore(b, g) : brand.appendChild(b);
    }
    const m = document.querySelector(".mnav-btn");
    if (m && !document.querySelector(".pn-bell.pn-m")) m.insertAdjacentElement("afterend", makeBell("pn-m"));
    await poll(true);
    setInterval(() => poll(false), 45000);
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") poll(false); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
