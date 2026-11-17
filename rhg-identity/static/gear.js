/* gear.js · the way into Settings, on every page of pulse (same file for Finance, Purchasing and identity; served by
   identity at the root of the site).

   1. On a page of its own: a gear button in the top left corner (beside the logo in the menu, and beside the Menu
      button on a phone) for a person whose role holds any setting. It opens /settings, which comes back here.
   2. Inside Settings (the page is in its frame, with ?embed=...): links open in the whole window, not in the frame. */
(function () {
  "use strict";
  if (window.PulseGear) return;
  window.PulseGear = true;
  const framed = (() => { try { return window.top !== window; } catch (e) { return true; } })();
  const embedded = /[?&]embed=/.test(location.search);

  if (framed && embedded) {
    // a link inside a setting leaves Settings for the page it names
    document.addEventListener("click", e => {
      const a = e.target.closest && e.target.closest("a[href]");
      if (!a || e.defaultPrevented || a.target || a.hasAttribute("download")) return;
      const href = a.getAttribute("href") || "";
      if (!href || href.startsWith("#") || /^(javascript|blob|data|mailto|tel):/i.test(href)) return;
      e.preventDefault();
      try {
        const t = window.top.location;
        if (a.pathname === t.pathname && a.hash) t.hash = a.hash;      // another setting: Settings opens it
        else t.href = a.href;
      } catch (err) { location.href = a.href; }
    });
    return;
  }
  if (framed) return;

  const PERMS = ["admin.users.manage", "reports.manage", "finance.settings.manage", "purchasing.settings.manage",
                 "finance.s.settings", "purchasing.s.settings", "actioncenter.manage", "platform.s.actions.matrix"];
  const GEAR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>';
  const CSS = `
.pg-gear{display:inline-grid;place-items:center;flex:none;width:34px;height:34px;border-radius:10px;margin-left:auto;
  color:#fff;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18);text-decoration:none;transition:background .15s}
.pg-gear svg{width:18px;height:18px;transition:transform .3s}
.pg-gear:hover,.pg-gear:focus-visible{background:rgba(255,255,255,.2);outline:none}
.pg-gear:focus-visible{box-shadow:0 0 0 2px #a0d8ea}
.pg-gear:hover svg{transform:rotate(45deg)}
.pg-gear.pg-m{display:none;color:var(--ink,#16181d);background:var(--card,#fff);border:1px solid var(--line,#e3e1d9);margin-left:8px;width:38px;height:38px;vertical-align:top}
@media(max-width:760px){.pg-gear.pg-m{display:inline-grid}}
@media (prefers-reduced-motion: reduce){.pg-gear svg{transition:none}}`;

  function link(cls) {
    const a = document.createElement("a");
    a.className = "pg-gear" + (cls ? " " + cls : "");
    a.href = "/settings?from=" + encodeURIComponent(location.pathname + location.search + location.hash);
    a.title = "Settings"; a.setAttribute("aria-label", "Settings");
    a.innerHTML = GEAR;
    a.addEventListener("click", () => { a.href = "/settings?from=" + encodeURIComponent(location.pathname + location.search + location.hash); });
    return a;
  }
  async function start() {
    let u = null;
    try { const r = await fetch("/" + "api/session", {credentials: "same-origin"}); u = (await r.json()).user; } catch (e) { return; }
    if (!u || u.report || u.must_change || !PERMS.some(p => (u.permissions || []).includes(p))) return;
    if (location.pathname.indexOf("/settings") === 0) return;
    const st = document.createElement("style"); st.textContent = CSS; document.head.appendChild(st);
    const brand = document.querySelector("aside .brand, aside .abrand");
    if (brand && !brand.querySelector(".pg-gear")) brand.appendChild(link());
    const m = document.querySelector(".mnav-btn");
    if (m && !document.querySelector(".pg-gear.pg-m")) m.insertAdjacentElement("afterend", link("pg-m"));
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
