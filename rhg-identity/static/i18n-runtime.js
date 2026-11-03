/* pulse in Albanian (shqip) or English.

   The pages are written in English. In Albanian, every text node and every title / placeholder / aria-label
   is looked up in the dictionary (window.PULSE_SQ, served with this file by identity at /i18n.js from
   rhg-identity/i18n/sq.json) as the page draws it, so screens drawn later by the scripts are translated too.

   A text is cut at " · " into segments; each segment is looked up
     1. as it is,
     2. with its numbers as "#" ("13 restaurants" → "# restaurants" → "# restorante"),
     3. against the dictionary entries whose "#" stand for any text ("Net sales # against #"),
   and the numbers or names go back in the same order. What is not in the dictionary stays in English.
   Month and day names next to a number are translated on their own.

   The language is the person's (identity: users.lang, default Albanian), switched with SQ / EN in the side
   menu (or at the top right of the sign-in page) and kept in this browser too. */
(function () {
  "use strict";
  const KEY = "pulse-lang", ROOT_API = "/" + "api/";
  let lang = "sq";
  try { lang = localStorage.getItem(KEY) || "sq"; } catch (e) { /* storage off */ }
  const D = window.PULSE_SQ || {};
  document.documentElement.lang = lang === "sq" ? "sq" : "en";

  const NUM = /[−+\-]?(?:\d[\d.,]*)(?:\s?(?:%|k|M|B|pts|s|h|d)\b)?/g;
  const SEP = /\s+·\s+/;
  const WORDS = {Mon: "Hën", Tue: "Mar", Wed: "Mër", Thu: "Enj", Fri: "Pre", Sat: "Sht", Sun: "Die",
    Monday: "e hënë", Tuesday: "e martë", Wednesday: "e mërkurë", Thursday: "e enjte", Friday: "e premte",
    Saturday: "e shtunë", Sunday: "e diel",
    Jan: "Jan", Feb: "Shk", Mar: "Mar", Apr: "Pri", May: "Maj", Jun: "Qer", Jul: "Kor", Aug: "Gus", Sep: "Sht",
    Sept: "Sht", Oct: "Tet", Nov: "Nën", Dec: "Dhj",
    January: "Janar", February: "Shkurt", March: "Mars", April: "Prill", June: "Qershor", July: "Korrik",
    August: "Gusht", September: "Shtator", October: "Tetor", November: "Nëntor", December: "Dhjetor"};
  const WORD_RE = new RegExp("\\b(" + Object.keys(WORDS).sort((a, b) => b.length - a.length).join("|") + ")\\b", "g");

  // entries with "#" as patterns, found by their first long word
  let patterns = null;
  function buildPatterns() {
    patterns = new Map();
    for (const k in D) {
      if (k.indexOf("#") < 0 || D[k] === k) continue;
      const lits = k.split("#");
      if (lits.join("").replace(/[^A-Za-z]/g, "").length < 4) continue;
      const word = (k.match(/[A-Za-z]{3,}/) || [""])[0].toLowerCase();
      if (!word) continue;
      const re = new RegExp("^" + lits.map(s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("(.+?)") + "$");
      if (!patterns.has(word)) patterns.set(word, []);
      patterns.get(word).push([re, D[k]]);
    }
  }
  const fill = (t, vals) => { let i = 0; return t.replace(/#/g, () => (i < vals.length ? vals[i++] : "#")); };

  const memo = new Map();
  function seg(s, depth) {
    depth = depth || 0;
    const m = s.match(/^([\s,;:]*)(.*?)([\s,;:]*)$/s);
    const pre = m[1], core = m[2], post = m[3];
    if (!core || !/[A-Za-z]{2}/.test(core)) return s;
    let out = D[core];
    if (out === undefined) {
      // numbers become "#"; numbers that touch (a date 2026-10-01) or stand side by side are one value
      const spans = [];
      for (const m of core.matchAll(NUM)) {
        const last = spans[spans.length - 1];
        if (last && /^\s*$/.test(core.slice(last[1], m.index))) last[1] = m.index + m[0].length;
        else spans.push([m.index, m.index + m[0].length]);
      }
      if (spans.length) {
        let k = "", at = 0;
        const vals = [];
        for (const [x, y] of spans) { k += core.slice(at, x) + "#"; vals.push(core.slice(x, y)); at = y; }
        k = (k + core.slice(at)).trim();
        if (D[k] !== undefined) out = fill(D[k], vals);
      }
    }
    if (out === undefined) {
      if (!patterns) buildPatterns();
      const words = new Set((core.match(/[A-Za-z]{3,}/g) || []).map(w => w.toLowerCase()));
      outer: for (const w of words) {
        for (const [re, target] of patterns.get(w) || []) {
          const mm = core.match(re);
          if (mm) { out = fill(target, mm.slice(1).map(v => (depth < 2 ? seg(v, depth + 1) : v))); break outer; }
        }
      }
    }
    if (out === undefined) out = /\d/.test(core) ? core.replace(WORD_RE, w => WORDS[w]) : core;
    return pre + out + post;
  }
  function tr(text) {
    if (!text || !/[A-Za-z]{2}/.test(text)) return text;
    const hit = memo.get(text);
    if (hit !== undefined) return hit;
    const lead = text.match(/^\s*/)[0], trail = text.match(/\s*$/)[0];
    const body = text.trim().replace(/\s+/g, " ");
    const out = lead + body.split(SEP).map(x => seg(x)).join(" · ") + trail;
    if (memo.size > 20000) memo.clear();
    memo.set(text, out);
    return out;
  }

  const SKIP = new Set(["SCRIPT", "STYLE", "TEXTAREA", "CODE", "PRE", "NOSCRIPT"]);
  const ATTRS = ["title", "placeholder", "aria-label"];
  const done = new WeakMap();                    // node → the text this script put there
  function skipEl(el) {
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      if (SKIP.has(e.tagName) || e.hasAttribute("data-no-i18n") || e.isContentEditable) return true;
    }
    return false;
  }
  function textNode(n) {
    if (done.get(n) === n.data) return;
    if (!n.parentElement || skipEl(n.parentElement)) return;
    // an <option> without a value has its text as value: keep the English one so the page still reads it
    const op = n.parentElement;
    if (op.tagName === "OPTION" && !op.hasAttribute("value")) op.setAttribute("value", op.textContent);
    const t = tr(n.data);
    if (t !== n.data) n.data = t;
    done.set(n, n.data);
  }
  function attrs(el) {
    for (const a of ATTRS) {
      const v = el.getAttribute(a);
      if (!v) continue;
      const t = tr(v);
      if (t !== v) el.setAttribute(a, t);
    }
  }
  function walk(root) {
    if (root.nodeType === 3) { textNode(root); return; }
    if (root.nodeType !== 1 || skipEl(root)) return;
    attrs(root);
    const w = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode: n => n.nodeType === 1 && SKIP.has(n.tagName) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT});
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      if (n.nodeType === 3) textNode(n); else attrs(n);
    }
  }

  function start() {
    if (lang !== "sq") return;
    walk(document.documentElement);
    if (document.title) document.title = tr(document.title);
    new MutationObserver(ms => {
      for (const m of ms) {
        if (m.type === "characterData") textNode(m.target);
        else if (m.type === "attributes") { if (m.target.nodeType === 1 && !skipEl(m.target)) attrs(m.target); }
        else m.addedNodes.forEach(walk);
      }
    }).observe(document.documentElement, {subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ATTRS});
  }

  // ---------------------------------------------------------------- the switch
  let csrf = "";
  function setLang(l) {
    try { localStorage.setItem(KEY, l); } catch (e) { /* storage off */ }
    const done_ = () => location.reload();
    if (!csrf) { done_(); return; }
    fetch(ROOT_API + "me/lang", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify({lang: l})})
      .catch(() => {}).finally(done_);
  }
  function addSwitch() {
    if (document.getElementById("langSw")) return;
    const box = document.createElement("div");
    box.id = "langSw";
    box.setAttribute("data-no-i18n", "");
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", lang === "sq" ? "Gjuha" : "Language");
    box.innerHTML = ["sq", "en"].map(l => `<button type="button" data-lang="${l}" aria-pressed="${l === lang}">${l.toUpperCase()}</button>`).join("");
    const st = document.createElement("style");
    st.textContent = `#langSw{display:inline-flex;gap:2px;padding:2px;border-radius:8px;border:1px solid rgba(127,127,127,.35);font-size:11px;margin:10px 6px 0}
#langSw button{all:unset;cursor:pointer;padding:3px 8px;border-radius:6px;font-weight:700;letter-spacing:.04em;opacity:.7}
#langSw button[aria-pressed="true"]{background:rgba(127,127,127,.25);opacity:1}
#langSw button:focus-visible{outline:2px solid currentColor}
#langSw.float{position:fixed;top:12px;right:12px;z-index:50;background:var(--card,#fff);color:var(--ink,#16181d)}`;
    document.head.appendChild(st);
    const aside = document.querySelector("aside");
    const anchor = aside && aside.querySelector(".powered");
    if (anchor) anchor.parentNode.insertBefore(box, anchor);
    else if (aside) aside.appendChild(box);
    else { box.className = "float"; document.body.appendChild(box); }
    box.addEventListener("click", e => {
      const b = e.target.closest("button[data-lang]");
      if (b && b.dataset.lang !== lang) setLang(b.dataset.lang);
    });
  }

  // the person's language from identity wins over this browser's, once signed in
  function fromSession() {
    fetch(ROOT_API + "session", {credentials: "same-origin"}).then(r => r.json()).then(j => {
      const u = j && j.user;
      if (!u) return;
      csrf = u.csrf || "";
      const l = u.lang === "en" ? "en" : "sq";
      if (l !== lang) {
        try { localStorage.setItem(KEY, l); } catch (e) { /* storage off */ }
        location.reload();
      }
    }).catch(() => {});
  }

  window.PulseI18n = {lang: () => lang, t: tr, set: setLang};
  start();
  const ready = () => { addSwitch(); fromSession(); };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready); else ready();
})();
