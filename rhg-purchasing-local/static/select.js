/* Replaces the browser's native dropdown with one the application can style.
   It enhances any <select> on the page - including ones rendered later into
   innerHTML - and keeps the original element as the source of truth, so every
   existing .value read, .onchange handler and form reset keeps working. */
(function () {
  "use strict";
  const ENHANCED = "data-rhgsel";

  function build(sel) {
    if (sel.hasAttribute(ENHANCED) || sel.multiple || sel.dataset.native === "true") return;
    sel.setAttribute(ENHANCED, "1");

    const wrap = document.createElement("div");
    wrap.className = "rsel";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "rsel-btn";
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    const label = document.createElement("span");
    label.className = "rsel-label";
    const caret = document.createElement("span");
    caret.className = "rsel-caret";
    caret.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';
    btn.append(label, caret);

    const list = document.createElement("div");
    list.className = "rsel-list";
    list.setAttribute("role", "listbox");
    list.hidden = true;

    sel.parentNode.insertBefore(wrap, sel);
    wrap.append(btn, list, sel);          // the <select> stays, visually hidden

    function sync() {
      const o = sel.options[sel.selectedIndex];
      label.textContent = o ? o.textContent : "";
      btn.disabled = sel.disabled;
      btn.title = o ? o.textContent : "";
    }

    function paint() {
      list.innerHTML = "";
      Array.from(sel.options).forEach((o, i) => {
        const item = document.createElement("div");
        item.className = "rsel-opt" + (i === sel.selectedIndex ? " on" : "");
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", i === sel.selectedIndex ? "true" : "false");
        item.textContent = o.textContent;
        if (o.disabled) item.classList.add("off");
        else item.onclick = () => { choose(i); };
        list.appendChild(item);
      });
    }

    function choose(i) {
      if (sel.selectedIndex !== i) {
        sel.selectedIndex = i;
        // Fire the events the page already listens for.
        sel.dispatchEvent(new Event("input", { bubbles: true }));
        sel.dispatchEvent(new Event("change", { bubbles: true }));
      }
      close();
      sync();
    }

    function open() {
      if (sel.disabled) return;
      document.querySelectorAll(".rsel.open").forEach(w => { if (w !== wrap) closeOne(w); });
      paint();
      list.hidden = false;
      wrap.classList.add("open");
      btn.setAttribute("aria-expanded", "true");
      // Flip upwards when there is no room below.
      const r = btn.getBoundingClientRect();
      wrap.classList.toggle("up", r.bottom + 260 > window.innerHeight && r.top > 260);
      const on = list.querySelector(".rsel-opt.on");
      if (on) on.scrollIntoView({ block: "nearest" });
    }

    function close() { closeOne(wrap); }
    function closeOne(w) {
      w.classList.remove("open", "up");
      const l = w.querySelector(".rsel-list");
      if (l) l.hidden = true;
      const b = w.querySelector(".rsel-btn");
      if (b) b.setAttribute("aria-expanded", "false");
    }

    btn.onclick = e => { e.preventDefault(); e.stopPropagation(); wrap.classList.contains("open") ? close() : open(); };

    btn.onkeydown = e => {
      const openNow = wrap.classList.contains("open");
      if (e.key === "Escape") { close(); return; }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!openNow) { open(); return; }
        const items = Array.from(list.querySelectorAll(".rsel-opt:not(.off)"));
        const cur = items.findIndex(x => x.classList.contains("on"));
        const next = e.key === "ArrowDown" ? Math.min(cur + 1, items.length - 1) : Math.max(cur - 1, 0);
        choose(Array.from(sel.options).indexOf(
          Array.from(sel.options).filter(o => !o.disabled)[next]));
        open();
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault(); openNow ? close() : open();
      }
    };

    // The page may set .value directly; keep the label honest.
    sel.addEventListener("change", sync);
    new MutationObserver(sync).observe(sel, { childList: true, subtree: true });
    sync();
  }

  function scan(root) {
    (root || document).querySelectorAll("select:not([" + ENHANCED + "])").forEach(build);
  }

  document.addEventListener("click", e => {
    if (!e.target.closest(".rsel")) document.querySelectorAll(".rsel.open").forEach(w => {
      w.classList.remove("open", "up");
      const l = w.querySelector(".rsel-list"); if (l) l.hidden = true;
      const b = w.querySelector(".rsel-btn"); if (b) b.setAttribute("aria-expanded", "false");
    });
  });
  window.addEventListener("resize", () => document.querySelectorAll(".rsel.open").forEach(w => {
    w.classList.remove("open", "up");
    const l = w.querySelector(".rsel-list"); if (l) l.hidden = true;
  }));

  // Views are rendered into innerHTML long after load, so watch for new ones.
  new MutationObserver(ms => {
    for (const m of ms) for (const n of m.addedNodes) {
      if (n.nodeType !== 1) continue;
      if (n.tagName === "SELECT") build(n); else scan(n);
    }
  }).observe(document.documentElement, { childList: true, subtree: true });

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", () => scan());
  else scan();
})();
