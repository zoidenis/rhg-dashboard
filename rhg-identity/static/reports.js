/* reports.js · scheduled reports: any screen of pulse sent by email (same file for Finance, Purchasing and the
   Executive dashboard; served by identity at the root of the site).

   1. On every screen, for a person who may schedule reports (reports.manage): a "Schedule as report" button in the
      header that opens the editor with the screen and company that are open.
   2. Where the page has <div id="pulseReports"> (Settings of Finance and Purchasing): the list of the reports, how
      often each goes, to whom, the next and the last send, Send now / Test to me, and the emails that keep their own
      settings.
   Each person receives the screen as they themselves see it in pulse (their screens, companies and locations): the
   report sender opens it with that person's access. So the people are chosen from the accounts, by the locations
   they work in, and someone whose role does not open the screen cannot be chosen. */
(function () {
  "use strict";
  const API = "/" + "api/";                // built from parts: the towers' proxy rewrites the literal
  const APP = location.pathname.indexOf("/finance") === 0 ? "finance"
    : location.pathname.indexOf("/purchasing") === 0 ? "purchasing" : "platform";
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const TOWERS = {finance: "Finance & Inventory", purchasing: "Purchasing", platform: "Platform"};
  // Emails that keep their own settings (their own schedule and the Email recipients by role)
  const BUILTIN = [
    {app: "finance", label: "Receivables & payables email", view: "settings", card: "arCard", pill: "arPill", when: "Every day"},
    {app: "finance", label: "Negative stock daily email", view: "settings", card: "ngCard", pill: "ngPill", when: "Every day"},
    {app: "finance", label: "Daily controls and the owners' email", view: "register", card: "rgSetCard", pill: "rgPill", when: "Every day"},
    {app: "finance", label: "Cost control weekly email", view: "settings", card: "crCard", pill: "crPill", when: "Every week"},
    {app: "finance", label: "Sales weekly email", view: "settings", card: "srCard", pill: "srPill", when: "Every week"},
    {app: "finance", label: "Brand performance email", view: "settings", card: "brCard", pill: "brPill", when: "Every week"},
    {app: "finance", label: "P&L memo · monthly", view: "settings", card: "pmCard", pill: "pmPill", when: "Every month"},
    {app: "purchasing", label: "Email notifications", view: "settings", card: "ntCard", pill: "", when: "Every day"},
    {app: "purchasing", label: "Price changes · weekly email", view: "prices", card: "pqMailCard", pill: "pqPillT", when: "Every week"}
  ];
  let ME = null, D = null, LOCS = null, LOCS_ERR = "", EDIT = null, busy = false;

  /* ------------------------------------------------------------------ data */
  async function api(path, body) {
    const opt = {credentials: "same-origin", headers: {}};
    if (body) {
      opt.method = "POST"; opt.body = JSON.stringify(body);
      opt.headers = {"Content-Type": "application/json", "X-CSRF-Token": (ME && ME.csrf) || ""};
    }
    const r = await fetch(API + path, opt);
    let j = {};
    try { j = await r.json(); } catch (e) { /* not json */ }
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
  }
  async function load() { D = await api("reports"); return D; }
  async function locs() {
    if (LOCS) return LOCS;
    try { LOCS = await api("reports/locations"); LOCS_ERR = ""; }
    catch (e) { LOCS = {companies: []}; LOCS_ERR = e.message; }
    return LOCS;
  }
  const screenOf = key => (D.catalogue || []).find(s => s.key === key);
  function companies() {
    const set = new Set();
    ((LOCS && LOCS.companies) || []).forEach(c => set.add(c.company));
    const sel = document.getElementById("company") || document.getElementById("fCompany");
    if (sel) Array.from(sel.options).forEach(o => o.value && set.add(o.value));
    (D.reports || []).forEach(r => set.add(r.company));
    return [...set].filter(Boolean).sort();
  }
  function locsOf(company) {
    const c = ((LOCS && LOCS.companies) || []).find(x => x.company === company);
    return c ? c.locations : [];
  }
  function locName(company, code) {
    const l = locsOf(company).find(x => x.code === code);
    return l && l.name ? l.name + " (" + code + ")" : code;
  }
  // Where a person works in a company: "*" (all of it), a list of location codes, or null (not in it)
  function whereIn(p, company) {
    const sc = p.scope || {companies: "*", locations: {}};
    if (sc.companies !== "*" && !(sc.companies || []).includes(company)) return null;
    const l = (sc.locations || {})[company];
    return l && l !== "*" ? l : "*";
  }
  // The same rules as identity (reports.py): may this person receive this screen of this company, and why not
  function why(p, s, company) {
    if (!p.active) return "Account disabled";
    if (!String(p.email || "").trim()) return "No email address on the account";
    if (!(p.screens || []).includes(s.key)) return "The role does not open this screen";
    const w = whereIn(p, company);
    if (w === null) return "The account does not cover " + company;
    if (s.kind === "company" && w !== "*") return "This screen shows the whole company; the account covers some locations only";
    return "";
  }

  /* ------------------------------------------------------------------ texts */
  const span = t => `<span>${esc(t)}</span>`;
  // a screen's name, each part its own text so the Albanian dictionary finds it
  const lab = (key, fallback) => { const s = screenOf(key);
    return s ? [s.tower, s.menu].concat(s.tab && s.tab !== s.menu ? [s.tab] : []).map(span).join(" › ") : esc(fallback || key); };
  function when(r) {
    const parts = [D.frequencies[r.freq] || r.freq];
    if (r.freq === "weekly") parts.push(DAYS[r.day || 0]);
    if (r.freq === "monthly") parts.push(r.day ? "Day of the month " + r.day : "Last day of the month");
    parts.push(r.time);
    return parts.map(span).join(" · ");
  }
  const dt = s => s ? s.slice(8, 10) + "." + s.slice(5, 7) + " " + s.slice(11, 16) : "–";
  const chip = (cls, t) => `<span class="pr-chip ${cls}">${esc(t)}</span>`;
  function lastChip(l) {
    if (!l) return `<span class="pr-mute">Not sent yet</span>`;
    const cls = {sent: "ok", partial: "warn", failed: "bad"}[l.status] || "";
    const t = {sent: "Sent", partial: "Partly sent", failed: "Failed"}[l.status] || l.status;
    const title = [].concat(l.errors || [], l.detail ? [l.detail] : [], (l.skipped || []).map(x => x.username + ": " + x.why)).join("\n");
    return `<span title="${esc(title)}">${chip(cls, t)}${l.test ? " " + chip("", "Test") : ""} <span class="pr-mute">${esc(dt(l.finished_at))}</span></span>`;
  }

  /* ------------------------------------------------------------------ styles */
  function css() {
    if (document.getElementById("prCss")) return;
    const st = document.createElement("style");
    st.id = "prCss";
    st.textContent = `
.pr-chip{display:inline-block;font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:999px;background:var(--rhg-sky,#efede6);color:var(--ink,#16181d);white-space:nowrap}
.pr-chip.ok{background:var(--down-bg,#e3f4ea);color:var(--down,#0b7a4a)}.pr-chip.warn{background:var(--warn-bg,#f8eedf);color:var(--warn,#b86e0e)}
.pr-chip.bad{background:var(--up-bg,#f9e9e5);color:var(--up,#c2412d)}.pr-chip.off{opacity:.75}
.pr-mute{color:var(--sub,var(--rhg-muted,#5f636b));font-size:12px}
.pr-bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:4px 0 10px}
.pr-bar select,.pr-bar input{font:inherit;padding:6px 8px;border:1px solid var(--line,#e3e1d9);border-radius:8px;background:var(--card,#fff);color:var(--ink,#16181d)}
.pr-tbl{width:100%;border-collapse:collapse;font-size:13px}
.pr-tbl th{text-align:left;font-size:11.5px;color:var(--sub,#5f636b);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--line,#e3e1d9);white-space:nowrap}
.pr-tbl td{padding:8px;border-bottom:1px solid var(--line,#e3e1d9);vertical-align:top}
.pr-tbl td.pr-act{white-space:nowrap;text-align:right}
.pr-link{all:unset;cursor:pointer;color:var(--accent,var(--rhg-primary,#0c475e));font-weight:600;font-size:12.5px;margin-left:10px}
.pr-link:hover{text-decoration:underline}.pr-link:focus-visible{outline:2px solid currentColor;outline-offset:2px}
.pr-link.danger{color:var(--up,#c2412d)}
.pr-scroll{overflow-x:auto}
.pr-sub{font-size:12px;color:var(--sub,#5f636b);margin-top:2px}
.pr-btn{font:inherit;font-weight:600;font-size:13px;padding:7px 12px;border-radius:10px;border:1px solid var(--line,#e3e1d9);background:var(--card,#fff);color:var(--ink,#16181d);cursor:pointer}
.pr-btn.dark{background:var(--accent,var(--rhg-primary,#0c475e));border-color:transparent;color:#fff}
.pr-btn:disabled{opacity:.55;cursor:default}
@media (prefers-color-scheme: dark){.pr-btn.dark{color:var(--bg,#111316)}}
#pulseRepBtn{margin-right:6px}
.pr-dlg{position:fixed;inset:0;background:rgba(10,14,18,.45);z-index:1000;display:flex;align-items:flex-start;justify-content:center;padding:4vh 16px;overflow:auto}
.pr-box{background:var(--card,#fff);color:var(--ink,#16181d);border-radius:14px;max-width:880px;width:100%;padding:20px 22px;box-shadow:0 12px 40px rgba(0,0,0,.25);box-sizing:border-box}
.pr-box h3{margin:0 0 4px;font-size:18px}
.pr-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px 14px;margin:14px 0}
.pr-grid label,.pr-f{display:flex;flex-direction:column;gap:4px;font-size:12px;font-weight:600;color:var(--sub,#5f636b);min-width:0}
.pr-grid select,.pr-grid input,.pr-grid textarea,.pr-box .pr-bar input,.pr-box .pr-bar select{font:inherit;font-size:13.5px;font-weight:400;color:var(--ink,#16181d);padding:7px 9px;border:1px solid var(--line,#e3e1d9);border-radius:8px;background:var(--card,#fff);min-width:0;box-sizing:border-box;width:100%}
.pr-box .pr-bar input,.pr-box .pr-bar select{width:auto}
.pr-wide{grid-column:1/-1}
.pr-checks{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;margin:-4px 0 6px}
.pr-checks label{display:flex;gap:6px;align-items:center}
.pr-people{border:1px solid var(--line,#e3e1d9);border-radius:10px;max-height:340px;overflow:auto}
.pr-person{display:grid;grid-template-columns:22px minmax(0,1.3fr) minmax(0,1fr) minmax(0,1.2fr);gap:4px 10px;align-items:start;padding:7px 10px;border-bottom:1px solid var(--line,#e3e1d9);font-size:13px;cursor:pointer}
.pr-person:last-child{border-bottom:0}
.pr-person.no{cursor:default;opacity:.6}
.pr-person small{display:block;color:var(--sub,#5f636b);font-size:11.5px;overflow-wrap:anywhere}
.pr-grp{padding:6px 10px;font-size:11.5px;font-weight:700;color:var(--sub,#5f636b);background:var(--rhg-sky,#efede6);letter-spacing:.02em}
.pr-msg{min-height:18px;font-size:12.5px;color:var(--up,#c2412d);margin-top:8px}
.pr-foot{display:flex;justify-content:flex-end;gap:8px;align-items:center;margin-top:12px;flex-wrap:wrap}
.pr-hint{font-size:12.5px;color:var(--sub,#5f636b);margin:4px 0 0}
.pr-sec{margin-top:14px}.pr-sec summary{cursor:pointer;font-weight:600;font-size:13px}
@media (max-width:640px){.pr-person{grid-template-columns:22px minmax(0,1fr)}.pr-person>:nth-child(n+3){grid-column:2}
  .pr-tbl thead{display:none}.pr-tbl tr{display:block;border-bottom:1px solid var(--line,#e3e1d9);padding:6px 0}
  .pr-tbl td{display:block;border:0;padding:3px 4px}.pr-tbl td.pr-act{text-align:left}.pr-link{margin:0 12px 0 0}}`;
    document.head.appendChild(st);
  }

  /* ------------------------------------------------------------------ the list (Settings) */
  let F = {app: "", q: ""};
  function paint() {
    const host = document.getElementById("pulseReports");
    if (!host || !D) return;
    host.hidden = false;
    const rep = D.reporter || {};
    const sender = rep.online ? chip("ok", "Report sender on") : chip("bad", "Report sender not answering");
    const rows = (D.reports || []).filter(r => (!F.app || r.app === F.app)
      && (!F.q || (r.label + " " + r.name + " " + r.company).toLowerCase().includes(F.q)));
    const ppl = new Map((D.people || []).map(p => [p.username, p]));
    const recips = r => {
      const s = screenOf(r.screen);
      const ok = r.recipients.filter(u => ppl.has(u) && s && !why(ppl.get(u), s, r.company));
      const names = r.recipients.map(u => (ppl.get(u) || {name: u}).name).join(", ");
      return `<span title="${esc(names)}">${span(ok.length + (ok.length === 1 ? " person" : " people"))}</span>`
        + (ok.length < r.recipients.length ? `<div class="pr-sub">${span((r.recipients.length - ok.length) + " cannot receive it")}</div>` : "");
    };
    host.innerHTML = `
      <div class="card-head"><div><h2>Scheduled reports</h2>
        <p class="hint"><span>Any screen of pulse sent by email, as often as you choose, to the people you choose.</span> <span>Each person receives the screen as they see it in pulse: their screens, companies and locations.</span> <span>The picture is in the email and the whole screen comes as a PDF.</span></p></div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">${sender}<button class="pr-btn dark" type="button" id="prNew">New report</button></div></div>
      ${rep.online ? "" : `<p class="pr-hint" style="color:var(--warn,#b86e0e)"><span>The report sender has not called in the last minutes, so nothing goes until it does.</span> <span>It runs on the server next to the towers (the reporter service).</span></p>`}
      <div class="pr-bar"><select id="prFApp" aria-label="Tower"><option value="">All towers</option>${["finance", "purchasing", "platform"].map(a => `<option value="${a}"${F.app === a ? " selected" : ""}>${esc(TOWERS[a])}</option>`).join("")}</select>
        <input id="prFQ" placeholder="Search reports" aria-label="Search reports" value="${esc(F.q)}"></div>
      ${rows.length ? `<div class="pr-scroll"><table class="pr-tbl"><thead><tr><th>Report</th><th>How often</th><th>Who receives it</th><th>Next</th><th>Last</th><th></th></tr></thead><tbody>
        ${rows.map(r => `<tr data-id="${esc(r.id)}">
          <td><b>${r.name ? esc(r.name) : lab(r.screen, r.label)}</b>${r.missing ? " " + chip("bad", "Screen no longer in pulse") : ""}
            <div class="pr-sub">${r.name ? lab(r.screen, r.label) + " · " : ""}${esc(r.company)}${r.period !== "screen" ? " · " + span(D.periods[r.period] || r.period) : ""}</div></td>
          <td>${r.enabled ? when(r) : chip("off", "Off")}</td>
          <td>${recips(r)}</td>
          <td>${r.queued ? chip("warn", "Going now") : esc(dt(r.next))}</td>
          <td>${lastChip(r.last)}</td>
          <td class="pr-act"><button class="pr-link" data-a="edit">Edit</button><button class="pr-link" data-a="send">Send now</button><button class="pr-link" data-a="test">Test to me</button><button class="pr-link danger" data-a="del">Delete</button></td></tr>`).join("")}
      </tbody></table></div>` : `<p class="pr-mute">${(D.reports || []).length ? "No report matches." : "No report yet. Open any screen and use Schedule as report, or New report here."}</p>`}
      <p class="pr-msg" id="prListMsg" role="status"></p>
      <details class="pr-sec"><summary>Recent sends</summary>${(D.runs || []).length ? `<div class="pr-scroll"><table class="pr-tbl"><thead><tr><th>When</th><th>Report</th><th>Result</th><th>Sent to</th><th>By</th></tr></thead><tbody>
        ${(D.runs || []).slice(0, 25).map(x => { const r = (D.reports || []).find(y => y.id === x.report);
          return `<tr><td>${esc(dt(x.finished_at))}</td><td>${r ? (r.name ? esc(r.name) : lab(r.screen, r.label)) + " · " + esc(r.company) : esc(x.report)}</td><td>${lastChip({status: x.status, test: x.test, detail: x.detail, skipped: x.skipped})}</td><td>${span(x.to + (x.to === 1 ? " person" : " people"))}</td><td>${esc(x.by === "scheduler" ? "Schedule" : x.by)}</td></tr>`; }).join("")}
        </tbody></table></div>` : `<p class="pr-mute">Nothing sent yet.</p>`}</details>
      <details class="pr-sec"><summary>Emails with their own settings</summary>
        <p class="pr-hint">These emails are built for their purpose (exceptions, memos, digests) and keep their own schedule and Email recipients by role.</p>
        <div class="pr-scroll"><table class="pr-tbl"><tbody>${BUILTIN.map(b => {
          const pill = APP === b.app && b.pill && document.getElementById(b.pill);
          return `<tr><td><b>${esc(b.label)}</b><div class="pr-sub">${esc(TOWERS[b.app])}</div></td><td>${span(b.when)}</td><td>${pill ? chip(/off/i.test(pill.textContent) ? "off" : "ok", pill.textContent.trim()) : ""}</td>
            <td class="pr-act"><a class="pr-link" href="/${b.app}/#${b.view}" data-card="${b.card}" data-app="${b.app}">Its settings ›</a></td></tr>`; }).join("")}</tbody></table></div></details>`;
    host.querySelector("#prNew").onclick = () => editor(null);
    host.querySelector("#prFApp").onchange = e => { F.app = e.target.value; paint(); };
    host.querySelector("#prFQ").oninput = e => { F.q = e.target.value.toLowerCase(); const at = e.target.selectionStart; paint();
      const q = document.getElementById("prFQ"); q.focus(); q.setSelectionRange(at, at); };
    host.querySelectorAll("a[data-card]").forEach(a => a.onclick = ev => {
      if (a.dataset.app !== APP) return;
      const card = document.getElementById(a.dataset.card);
      const nav = document.querySelector(`#nav button[data-view="${a.getAttribute("href").split("#")[1]}"]`);
      ev.preventDefault();
      if (nav && !nav.classList.contains("on")) nav.click();
      if (card) setTimeout(() => card.scrollIntoView({behavior: "smooth", block: "start"}), 60);
    });
    host.querySelectorAll("tr[data-id] button[data-a]").forEach(b => b.onclick = () => act(b, b.closest("tr").dataset.id));
  }
  async function act(b, id) {
    const msg = document.getElementById("prListMsg");
    const r = D.reports.find(x => x.id === id);
    if (!r) return;
    if (b.dataset.a === "edit") return editor(r);
    if (b.dataset.a === "del" && !b.dataset.sure) {
      b.dataset.sure = "1"; b.textContent = "Delete for sure?";
      setTimeout(() => { if (b.isConnected) { delete b.dataset.sure; b.textContent = "Delete"; } }, 4000);
      return;
    }
    b.disabled = true;
    try {
      if (b.dataset.a === "del") await api("reports/delete", {id});
      else await api("reports/send", {id, test: b.dataset.a === "test"});
      await load(); paint();
      const m = document.getElementById("prListMsg");
      if (m && b.dataset.a !== "del") { m.style.color = "var(--down,#0b7a4a)";
        m.textContent = b.dataset.a === "test" ? "The test goes to you within a minute." : "It goes within a minute."; }
    } catch (e) { if (msg) { msg.style.color = ""; msg.textContent = e.message; } b.disabled = false; }
  }

  /* ------------------------------------------------------------------ the editor */
  function editor(r, preset) {
    css();
    EDIT = r ? JSON.parse(JSON.stringify(r)) : Object.assign({screen: "", company: "", freq: "weekly", day: 0, time: "08:00",
      period: "screen", recipients: [], locations: [], name: "", note: "", pdf: true, enabled: true}, preset || {});
    if (!EDIT.company) EDIT.company = ((document.getElementById("company") || document.getElementById("fCompany") || {}).value) || companies()[0] || "";
    const old = document.getElementById("prDlg");
    if (old) old.remove();
    const dlg = document.createElement("div");
    dlg.className = "pr-dlg"; dlg.id = "prDlg";
    const opts = () => {
      const groups = {};
      D.catalogue.forEach(s => { const g = s.tower + " › " + s.menu; (groups[g] = groups[g] || []).push(s); });
      return `<option value="">Choose a screen</option>` + Object.keys(groups).map(g => `<optgroup label="${esc(g)}">${groups[g].map(s =>
        `<option value="${esc(s.key)}"${s.key === EDIT.screen ? " selected" : ""}>${esc(s.tab || s.menu)}</option>`).join("")}</optgroup>`).join("");
    };
    dlg.innerHTML = `<div class="pr-box" role="dialog" aria-modal="true" aria-labelledby="prDlgT">
      <h3 id="prDlgT">${r ? "Edit report" : "New report"}</h3>
      <p class="pr-hint"><span>Each person receives the screen as they see it in pulse: their screens, companies and locations.</span> <span>Someone limited to some restaurants receives those restaurants only.</span></p>
      <div class="pr-grid">
        <label class="pr-wide">Screen<select id="prScreen">${opts()}</select></label>
        <label>Company<select id="prCo">${companies().map(c => `<option${c === EDIT.company ? " selected" : ""}>${esc(c)}</option>`).join("")}</select></label>
        <label id="prPerL">Period<select id="prPer">${Object.keys(D.periods).map(k => `<option value="${k}"${k === EDIT.period ? " selected" : ""}>${esc(D.periods[k])}</option>`).join("")}</select></label>
        <label>How often<select id="prFreq">${Object.keys(D.frequencies).map(k => `<option value="${k}"${k === EDIT.freq ? " selected" : ""}>${esc(D.frequencies[k])}</option>`).join("")}</select></label>
        <label id="prDayL">Day<select id="prDay"></select></label>
        <label>Time<input type="time" id="prTime" value="${esc(EDIT.time)}"></label>
        <label class="pr-wide">Name in the email (optional)<input id="prName" maxlength="120" value="${esc(EDIT.name)}" placeholder="The screen's name"></label>
        <label class="pr-wide">Message in the email (optional)<textarea id="prNote" rows="2" maxlength="600">${esc(EDIT.note)}</textarea></label>
      </div>
      <div class="pr-checks"><label><input type="checkbox" id="prPdf"${EDIT.pdf ? " checked" : ""}> Attach the whole screen as PDF</label>
        <label><input type="checkbox" id="prOn"${EDIT.enabled ? " checked" : ""}> On</label></div>
      <div class="pr-f" style="margin-top:6px">Who receives it</div>
      <div class="pr-bar"><select id="prLoc" aria-label="Location"></select><input id="prQ" placeholder="Search people" aria-label="Search people">
        <button type="button" class="pr-btn" id="prAll">Tick shown</button><button type="button" class="pr-btn" id="prNone">Clear</button>
        <span class="pr-mute" id="prCount"></span></div>
      <div class="pr-people" id="prPeople"></div>
      <p class="pr-hint" id="prLocNote"></p>
      <div class="pr-msg" id="prMsg" role="alert"></div>
      <div class="pr-foot"><button type="button" class="pr-btn" id="prCancel">Cancel</button><button type="button" class="pr-btn dark" id="prSave">Save</button></div>
    </div>`;
    document.body.appendChild(dlg);
    const g = id => document.getElementById(id);
    const close = () => { dlg.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = e => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    dlg.addEventListener("mousedown", e => { if (e.target === dlg) close(); });
    g("prCancel").onclick = close;
    const days = () => {
      const f = g("prFreq").value;
      g("prDayL").style.display = f === "weekly" || f === "monthly" ? "" : "none";
      g("prDay").innerHTML = f === "weekly" ? DAYS.map((d, i) => `<option value="${i}"${+EDIT.day === i ? " selected" : ""}>${esc(d)}</option>`).join("")
        : Array.from({length: 28}, (_, i) => `<option value="${i + 1}"${+EDIT.day === i + 1 ? " selected" : ""}>${i + 1}</option>`).join("")
          + `<option value="0"${f === "monthly" && !+EDIT.day ? " selected" : ""}>Last day of the month</option>`;
      if (f === "monthly" && EDIT.freq !== "monthly" && !r) g("prDay").value = "1";
    };
    const per = () => { const s = screenOf(g("prScreen").value); g("prPerL").style.display = s && s.period ? "" : "none"; };
    const locSel = () => {
      const co = g("prCo").value, ls = locsOf(co);
      const cur = (EDIT.locations || [])[0] || "";
      g("prLoc").innerHTML = `<option value="">All locations</option>` + ls.map(l =>
        `<option value="${esc(l.code)}"${l.code === cur ? " selected" : ""}>${esc(l.name ? l.name + " · " + l.code : l.code)}</option>`).join("");
      g("prLocNote").textContent = LOCS_ERR ? "The locations could not be read: " + LOCS_ERR : "";
    };
    const people = () => {
      const s = screenOf(g("prScreen").value), co = g("prCo").value, loc = g("prLoc").value, q = g("prQ").value.toLowerCase();
      const chosen = new Set(EDIT.recipients);
      if (!s) { g("prPeople").innerHTML = `<p class="pr-mute" style="padding:10px">Choose the screen first.</p>`; g("prCount").textContent = ""; return; }
      const row = p => {
        const w = whereIn(p, co), no = why(p, s, co);
        const where = w === null ? "" : w === "*" ? "Whole company" : w.map(c => locName(co, c)).join(", ");
        return `<label class="pr-person${no ? " no" : ""}" data-u="${esc(p.username)}"><input type="checkbox" ${chosen.has(p.username) ? "checked" : ""} ${no && !chosen.has(p.username) ? "disabled" : ""}>
          <span><b>${esc(p.name)}</b><small>${esc(p.email || "no email")}</small></span><span>${esc(p.role)}<small>${esc(where)}</small></span>
          <span>${no ? `<small style="color:var(--up,#c2412d)">${esc(no)}</small>` : ""}</span></label>`;
      };
      const match = p => !q || (p.name + " " + p.username + " " + p.email + " " + p.role).toLowerCase().includes(q);
      const all = (D.people || []).filter(p => p.active || chosen.has(p.username)).filter(match);
      const there = all.filter(p => { const w = whereIn(p, co); return Array.isArray(w) && (!loc || w.includes(loc)); });
      const whole = all.filter(p => whereIn(p, co) === "*");
      const rest = all.filter(p => whereIn(p, co) === null && chosen.has(p.username));
      const ok = p => !why(p, s, co);
      const sortp = a => a.sort((x, y) => (ok(y) - ok(x)) || x.name.localeCompare(y.name));
      g("prPeople").innerHTML =
        (there.length ? `<div class="pr-grp">${esc(loc ? "Working at " + locName(co, loc) : "Working at some locations of " + co)}</div>` + sortp(there).map(row).join("") : "")
        + (whole.length ? `<div class="pr-grp">${esc("Whole company · " + co)}</div>` + sortp(whole).map(row).join("") : "")
        + (rest.length ? `<div class="pr-grp">Not in this company</div>` + rest.map(row).join("") : "")
        || `<p class="pr-mute" style="padding:10px">Nobody matches.</p>`;
      const n = EDIT.recipients.length, okN = EDIT.recipients.filter(u => { const p = (D.people || []).find(x => x.username === u); return p && ok(p); }).length;
      g("prCount").innerHTML = span(n + " chosen") + (okN < n ? " · " + span((n - okN) + " cannot receive it") : "");
      g("prPeople").querySelectorAll("label[data-u] input").forEach(cb => cb.onchange = () => {
        const u = cb.closest("label").dataset.u;
        EDIT.recipients = EDIT.recipients.filter(x => x !== u).concat(cb.checked ? [u] : []);
        people();
      });
    };
    g("prScreen").onchange = () => { per(); people(); if (!g("prName").value) g("prName").placeholder = (screenOf(g("prScreen").value) || {}).label || "The screen's name"; };
    g("prCo").onchange = () => { EDIT.locations = []; locSel(); people(); };
    g("prLoc").onchange = () => { EDIT.locations = g("prLoc").value ? [g("prLoc").value] : []; people(); };
    g("prQ").oninput = people;
    g("prFreq").onchange = () => { days(); };
    g("prAll").onclick = () => {
      g("prPeople").querySelectorAll("label[data-u]:not(.no) input").forEach(cb => {
        const u = cb.closest("label").dataset.u; if (!EDIT.recipients.includes(u)) EDIT.recipients.push(u); });
      people();
    };
    g("prNone").onclick = () => {
      const shown = new Set([...g("prPeople").querySelectorAll("label[data-u]")].map(l => l.dataset.u));
      EDIT.recipients = EDIT.recipients.filter(u => !shown.has(u)); people();
    };
    g("prSave").onclick = async () => {
      if (busy) return;
      const body = {id: r ? r.id : undefined, screen: g("prScreen").value, company: g("prCo").value, period: g("prPer").value,
        freq: g("prFreq").value, day: +g("prDay").value || 0, time: g("prTime").value || "08:00", name: g("prName").value,
        note: g("prNote").value, pdf: g("prPdf").checked, enabled: g("prOn").checked, recipients: EDIT.recipients,
        locations: EDIT.locations};
      g("prMsg").textContent = "";
      if (!body.screen) { g("prMsg").textContent = "Choose the screen to send."; return; }
      if (!body.recipients.length && body.enabled) { g("prMsg").textContent = "Choose who receives it."; return; }
      busy = true; g("prSave").disabled = true;
      try {
        await api("reports", body);
        close(); await load(); paint();
        const m = document.getElementById("prListMsg");
        if (m) { m.style.color = "var(--down,#0b7a4a)"; m.textContent = "Saved."; }
        else flash("Saved. It is in Settings → Scheduled reports.");
      } catch (e) { g("prMsg").textContent = e.message; }
      finally { busy = false; const b = g("prSave"); if (b) b.disabled = false; }
    };
    days(); per(); locSel(); people();
    g("prScreen").focus();
    if (!LOCS) locs().then(() => { if (!document.getElementById("prDlg")) return;
      const co = g("prCo").value, list = companies();
      g("prCo").innerHTML = list.map(c => `<option${c === co ? " selected" : ""}>${esc(c)}</option>`).join("");
      locSel(); people(); });
  }
  function flash(t) {
    const d = document.createElement("div");
    d.setAttribute("role", "status");
    d.style.cssText = "position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--accent,#0c475e);color:#fff;padding:10px 16px;border-radius:10px;font-weight:600;font-size:13px;z-index:1001;box-shadow:0 6px 20px rgba(0,0,0,.2)";
    d.textContent = t;
    document.body.appendChild(d);
    setTimeout(() => d.remove(), 3500);
  }

  /* ------------------------------------------------------------------ "Schedule as report" on every screen */
  function current() {
    let list = [];
    if (APP === "platform") {
      if (location.pathname.indexOf("/executive") === 0) {
        const t = document.querySelector("#tabs button.on");
        list = D.catalogue.filter(s => s.app === "platform" && s.view === "executive" && (!t || s.tab_key === t.dataset.tab));
      } else list = D.catalogue.filter(s => s.app === "platform" && s.view === "control");
    } else {
      const v = document.querySelector("#nav button.on");
      list = D.catalogue.filter(s => s.app === APP && v && s.view === v.dataset.view);
      if (list.length > 1) {
        const bar = list[0].bar && document.getElementById(list[0].bar);
        const on = bar && bar.querySelector("button.on");
        const hit = on && list.find(s => s.tab_key === on.dataset.t);
        if (hit) list = [hit];
      }
    }
    return list[0] || null;
  }
  function button() {
    const box = document.querySelector(".hdr-btns");
    if (!box || document.getElementById("pulseRepBtn")) return;
    const b = document.createElement("button");
    b.className = "btn"; b.id = "pulseRepBtn"; b.type = "button";
    b.textContent = "Schedule as report";
    b.title = "Send this screen by email, as often as you choose, to the people you choose";
    b.onclick = async () => {
      try { if (!D) await load(); } catch (e) { flash(e.message); return; }
      const s = current();
      const co = (document.getElementById("company") || document.getElementById("fCompany") || {}).value || "";
      editor(null, {screen: s ? s.key : "", company: co, freq: "weekly"});
    };
    box.insertBefore(b, box.firstChild);
  }

  /* ------------------------------------------------------------------ start */
  async function start() {
    try {
      const r = await fetch(API + "session", {credentials: "same-origin"});
      const j = await r.json();
      ME = j && j.user;
    } catch (e) { return; }
    if (!ME || ME.report || !(ME.permissions || []).includes("reports.manage")) return;
    css();
    button();
    if (document.getElementById("pulseReports")) {
      try { await load(); paint(); }
      catch (e) { const h = document.getElementById("pulseReports"); h.hidden = false; h.innerHTML = `<h2>Scheduled reports</h2><p class="pr-msg">${esc(e.message)}</p>`; }
    }
  }
  window.PulseReports = {open: () => editor(null), reload: () => load().then(paint)};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
