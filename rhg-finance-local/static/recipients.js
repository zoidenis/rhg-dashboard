// Email recipients by company and location — the same card in Finance and Purchasing → Settings.
// One directory for both towers, kept in Purchasing next to the mail server: for every company the people
// in each role, a row for the whole group added to every company, people for single locations of a
// company, and which roles each report goes to. A location with its own people gets, for the reports
// that can be cut by location, an email of that location only.
// The tower's page holds <div class="card" id="rcCard" data-header="RHG-Finance"></div> and calls rcLoad().
(function () {
  const box = document.getElementById("rcCard");
  if (!box) return;
  const HDR = box.dataset.header || "";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const allowed = () => typeof can !== "function" || can("settings");
  const RE = /^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$/;
  const parse = t => String(t || "").split(/[,;\s]+/).map(x => x.trim()).filter(Boolean);
  const SEP = " · ";
  let D = null, pick = "", codes = {};

  const css = document.createElement("style");
  css.textContent = `
#rcCard details.rc-co{border:1px solid var(--line);border-radius:10px;margin:0 0 8px;background:var(--card)}
#rcCard details.rc-co>summary{cursor:pointer;padding:10px 12px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;list-style:none}
#rcCard details.rc-co>summary::-webkit-details-marker{display:none}
#rcCard details.rc-co>summary::before{content:"▸";color:var(--sub);font-size:11px}
#rcCard details.rc-co[open]>summary::before{content:"▾"}
#rcCard details.rc-co>summary span{font-size:11.5px;color:var(--sub);font-weight:400}
#rcCard details.rc-co .formgrid{padding:0 12px 4px;margin-bottom:8px}
#rcCard .rc-locs{margin:0 12px 12px;padding:10px 0 0;border-top:1px dashed var(--line)}
#rcCard .rc-locs details.rc-co{background:var(--bg)}
#rcCard .rc-locs .formgrid{padding:0 12px 4px}
#rcCard .rc-sub{font-size:12px;color:var(--sub);margin:0 0 8px}
#rcCard .rc-add{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
#rcCard .rc-add select,#rcCard .rc-add input{padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);font:inherit;font-size:12.5px}
#rcCard .rc-bad input{border-color:var(--up);background:var(--up-bg)}
#rcCard table.rc-m th,#rcCard table.rc-m td{white-space:nowrap}
#rcCard table.rc-m td:first-child{white-space:normal;min-width:150px;max-width:230px}
#rcCard table.rc-m td select{padding:4px 6px;border:1px solid var(--line);border-radius:7px;background:var(--bg);font:inherit;font-size:12px}
#rcCard table.rc-m td select.to{background:var(--down-bg);color:var(--down);font-weight:600}
#rcCard table.rc-m td select.cc{background:var(--warn-bg);color:var(--warn);font-weight:600}
#rcCard .rc-h{font-size:14px;margin:18px 0 4px}
#rcCard .rc-gen{color:var(--sub);font-style:italic}
#rcCard .rc-tag{display:inline-block;font-size:10.5px;padding:1px 6px;border-radius:6px;background:var(--down-bg);color:var(--down);font-weight:600;margin-left:4px}
@media (max-width:640px){
  #rcCard table#rcWho tr{display:block;border-bottom:1px solid var(--line);padding:6px 0}
  #rcCard table#rcWho tr:first-child{display:none}
  #rcCard table#rcWho td{display:block;border:0;padding:2px 4px}
  #rcCard table#rcWho td:first-child{font-weight:600}
  #rcCard table#rcWho td[data-l]::before{content:attr(data-l) " ";color:var(--sub);font-size:11px;font-weight:600}
}`;
  document.head.appendChild(css);

  async function load() {
    box.querySelector("#rcMsg") && (box.querySelector("#rcMsg").textContent = "Reading…");
    try {
      const r = await fetch("/api/recipients");
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.status);
      D = j; D.locations = D.locations || {}; render();
      if (j.error) msg(j.error);
    } catch (e) { box.innerHTML = `<h2>Email recipients by company</h2><p class="hint">Could not read: ${esc(e.message)}</p>`; }
  }
  const msg = t => { const m = box.querySelector("#rcMsg"); if (m) m.textContent = t; };
  const ci = (obj, k) => { const key = Object.keys(obj || {}).find(x => x.toUpperCase() === String(k).toUpperCase()); return key; };

  // the companies on screen: Business Central's companies, and any kept that BC no longer lists
  function companies() {
    const seen = new Set(), out = [];
    for (const c of [...(D.companies || []), ...Object.keys(D.people || {}), ...Object.keys(D.locations || {})]) {
      if (!c || c === D.group || seen.has(c.toUpperCase())) continue;
      seen.add(c.toUpperCase()); out.push(c);
    }
    return out;
  }
  const row = c => { const k = ci(D.people, c); return k ? D.people[k] : {}; };
  const locsOf = c => { const k = ci(D.locations, c); return k ? D.locations[k] : {}; };
  // the restaurants of the registry (Finance → Location intelligence) of a company: {key, name}; the key is the
  // restaurant's BC location code (else its department or Simphony reference), so both towers find it
  const restOf = c => { const k = ci(D.restaurants, c); return k ? D.restaurants[k] : []; };
  const lname = (c, l) => { const r = restOf(c).find(x => x.key.toUpperCase() === String(l).toUpperCase()); return r && r.name !== l ? `${r.name} (${l})` : l; };
  function addOptions(c) {
    const have = Object.keys(locsOf(c)).map(x => x.toUpperCase());
    const rs = restOf(c).filter(x => !have.includes(x.key.toUpperCase()));
    const rk = restOf(c).map(x => x.key.toUpperCase());
    const other = (codes[c] || []).filter(x => !have.includes(x.toUpperCase()) && !rk.includes(x.toUpperCase()));
    const head = rs.length || other.length ? (rs.length ? "Add a restaurant or location…" : "Add a location…") : (codes[c] ? "No locations read" : "Add a location…");
    return `<option value="">${head}</option>`
      + (rs.length ? `<optgroup label="Restaurants">${rs.map(x => `<option value="${esc(x.key)}">${esc(x.name)}${x.name !== x.key ? " · " + esc(x.key) : ""}</option>`).join("")}</optgroup>` : "")
      + (other.length ? `<optgroup label="${rs.length ? "Other BC locations" : "BC locations"}">${other.map(x => `<option>${esc(x)}</option>`).join("")}</optgroup>` : "");
  }
  const filled = p => (D.roles || []).filter(r => parse(p[r.key]).length);
  const roleInputs = (p, attrs) => `<div class="formgrid">${(D.roles || []).map(r => `<label class="field">${esc(r.label)}<input ${attrs} data-role="${esc(r.key)}" value="${esc(p[r.key] || "")}"></label>`).join("")}</div>`;
  const summary = p => { const f = filled(p); return f.length ? `${f.length} role${f.length > 1 ? "s" : ""} · ${f.map(r => esc(r.label.split(SEP)[0])).join(", ")}` : "nobody yet"; };

  function render() {
    const roles = D.roles || [], reports = D.reports || [], cos = companies(), G = D.group || "*";
    const open = new Set([...box.querySelectorAll("details[open]")].map(d => d.dataset.k));
    const byLoc = reports.filter(r => r.by_location).map(r => r.label.split(SEP)[0]);
    const choices = [...cos, ...cos.flatMap(c => Object.keys(locsOf(c)).sort().map(l => c + SEP + l))];
    const choiceLabel = x => { const i = x.lastIndexOf(SEP); return i > 0 && cos.includes(x.slice(0, i)) ? x.slice(0, i) + SEP + lname(x.slice(0, i), x.slice(i + SEP.length)) : x; };
    if (!pick || !choices.includes(pick)) pick = cos[0] || "";
    const coBlock = c => {
      const L = locsOf(c), names = Object.keys(L).sort();
      return `<details class="rc-co" data-k="${esc(c)}"${open.has(c) ? " open" : ""}><summary><b>${esc(c)}</b><span>${summary(row(c))}${names.length ? ` · ${names.length} location${names.length > 1 ? "s" : ""}: ${esc(names.map(l => lname(c, l)).join(", "))}` : ""}</span></summary>
        ${roleInputs(row(c), `data-co="${esc(c)}"`)}
        <div class="rc-locs"><p class="rc-sub"><b>Restaurants and locations of ${esc(c)}</b> · people set here (a unit manager, a head chef…) get their own email with only that restaurant's or location's figures for ${esc(byLoc.join(", "))}; the company email does not change.${restOf(c).length ? " Restaurants come from Settings → Location intelligence." : ""}</p>
          ${names.map(l => `<details class="rc-co" data-k="${esc(c + SEP + l)}"${open.has(c + SEP + l) ? " open" : ""}><summary><b>${esc(lname(c, l))}</b><span>${summary(L[l])}</span></summary>
            ${roleInputs(L[l], `data-co="${esc(c)}" data-loc="${esc(l)}"`)}
            <div style="padding:0 12px 10px"><button class="mini" data-rmloc="${esc(l)}" data-co="${esc(c)}">Remove ${esc(lname(c, l))}</button></div></details>`).join("")}
          <div class="rc-add"><select data-addco="${esc(c)}" aria-label="Add a restaurant or location of ${esc(c)}">${addOptions(c)}</select>
            <button class="mini" data-addbtn="${esc(c)}">Add</button><span class="meta" data-addmsg="${esc(c)}"></span></div></div></details>`;
    };
    box.innerHTML = `
      <div class="card-head"><div><h2>Email recipients by company</h2>
        <p class="hint">One list for both towers. For every company, the people in each role; each report goes to the roles set below, one email per company with that company's people. <b>All companies</b> is added to every company (for example a group CFO). A <b>location</b> of a company (a restaurant, the warehouse) can have people of its own: they get an email with only their location's figures for the reports marked <span class="rc-tag">by location</span>. When a company has nobody in the roles of a report, the report keeps the addresses of its own card, as before; a brand's own addresses stay in To of the brand performance email.</p></div>
        <span class="rhg-pill info" style="white-space:nowrap" title="${esc(D.updated_at ? `Saved ${D.updated_at.replace("T", " ").slice(0, 16)}${D.updated_by ? " by " + D.updated_by : ""}; kept in Purchasing` : "Kept in Purchasing")}">${D.updated_at ? "Saved " + esc(new Date(D.updated_at).toLocaleDateString("en-GB", {day: "numeric", month: "short"})) : "Not set yet"}</span></div>
      <div class="rc-h"><b>People by company and location</b></div>
      <details class="rc-co" data-k="${esc(G)}"${open.has(G) ? " open" : ""}><summary><b>All companies</b><span>${summary(row(G))} · added to every company</span></summary>${roleInputs(row(G), `data-co="${esc(G)}"`)}</details>
      ${cos.map(coBlock).join("")}
      <div class="rc-h"><b>Which report goes to which role</b></div>
      <p class="hint" style="margin:0 0 6px">To or Cc for each role; – leaves the role out of that report. The same roles apply to the location emails.</p>
      <div class="scroll"><table class="rc-m" data-nosort><tr><th>Report</th>${roles.map(r => `<th title="${esc(r.label)}">${esc(r.label.split(SEP)[0])}</th>`).join("")}</tr>
        ${reports.map(rep => `<tr><td><b>${esc(rep.label)}</b><div class="meta">${esc(rep.tower)}${rep.by_location ? ` <span class="rc-tag">by location</span>` : ""}</div></td>${roles.map(r => { const v = opt(rep.key, r.key);
          return `<td><select data-rep="${esc(rep.key)}" data-role="${esc(r.key)}" class="${v}" aria-label="${esc(rep.label)}: ${esc(r.label)}"><option value="">–</option><option value="to"${v === "to" ? " selected" : ""}>To</option><option value="cc"${v === "cc" ? " selected" : ""}>Cc</option></select></td>`; }).join("")}</tr>`).join("")}</table></div>
      <div style="margin:6px 0 0"><button class="mini" id="rcDefault">Back to the suggested roles</button></div>
      <div class="rc-h" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><b>Who gets what</b>
        <select id="rcPick" style="padding:5px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);font:inherit;max-width:100%">${choices.map(c => `<option value="${esc(c)}"${c === pick ? " selected" : ""}>${esc(choiceLabel(c))}</option>`).join("")}</select></div>
      <div class="scroll"><table id="rcWho" data-nosort></table></div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px">
        <button class="btn dark" id="rcSave">Save recipients</button><span class="meta" id="rcMsg"></span></div>`;
    box.querySelectorAll("input[data-co]").forEach(i => i.oninput = () => { mark(i); who(); });
    box.querySelectorAll("select[data-rep]").forEach(s => s.onchange = () => { s.className = s.value; who(); });
    box.querySelector("#rcPick").onchange = e => { pick = e.target.value; who(); };
    box.querySelector("#rcDefault").onclick = () => {
      box.querySelectorAll("select[data-rep]").forEach(s => { const rep = reports.find(r => r.key === s.dataset.rep), d = rep.default;
        s.value = d.to.includes(s.dataset.role) ? "to" : d.cc.includes(s.dataset.role) ? "cc" : ""; s.className = s.value; });
      who(); msg("Suggested roles set; Save to keep them.");
    };
    // locations: the codes Business Central holds for the company, read when the select is first used
    box.querySelectorAll("select[data-addco]").forEach(s => s.onfocus = s.onmousedown = () => loadCodes(s.dataset.addco));
    box.querySelectorAll("[data-addbtn]").forEach(b => b.onclick = () => {
      const c = b.dataset.addbtn, sel = box.querySelector(`select[data-addco="${CSS.escape(c)}"]`), l = (sel.value || "").trim();
      if (!l) { box.querySelector(`[data-addmsg="${CSS.escape(c)}"]`).textContent = "Choose a restaurant or location first."; return; }
      keep(); const k = ci(D.locations, c) || c; (D.locations[k] = D.locations[k] || {})[l] = {};
      render(); const d = box.querySelector(`details[data-k="${CSS.escape(c + SEP + l)}"]`); if (d) { d.open = true; d.closest("details[data-k]") && (box.querySelector(`details[data-k="${CSS.escape(c)}"]`).open = true); }
      msg(`${lname(c, l)} added to ${c}. Fill its people and Save.`);
    });
    box.querySelectorAll("[data-rmloc]").forEach(b => b.onclick = () => {
      keep(); const k = ci(D.locations, b.dataset.co); if (k) delete D.locations[k][b.dataset.rmloc];
      render(); msg(`${lname(b.dataset.co, b.dataset.rmloc)} removed; Save to keep it.`);
    });
    box.querySelector("#rcSave").onclick = save;
    const ok = allowed();
    box.querySelectorAll("input,select[data-rep],select[data-addco],[data-addbtn],[data-rmloc],#rcSave,#rcDefault").forEach(x => { x.disabled = !ok; if (!ok) x.title = "Your role cannot change Settings."; });
    box.querySelectorAll("input[data-co]").forEach(mark);
    who();
  }
  function opt(rep, role) { const R = (D.routing || {})[rep] || {to: [], cc: []}; return R.to.includes(role) ? "to" : R.cc.includes(role) ? "cc" : ""; }
  async function loadCodes(c) {
    if (codes[c]) return;
    codes[c] = [];
    try {
      const r = await fetch("/api/recipients/locations?company=" + encodeURIComponent(c));
      const j = await r.json();
      codes[c] = (j.locations || []).filter(Boolean);
    } catch (e) { codes[c] = []; }
    const sel = box.querySelector(`select[data-addco="${CSS.escape(c)}"]`);
    if (!sel) return;
    const keepV = sel.value;
    sel.innerHTML = addOptions(c);
    if (keepV) sel.value = keepV;
  }
  function mark(i) { const bad = parse(i.value).filter(x => !RE.test(x)); i.closest(".field").classList.toggle("rc-bad", !!bad.length); i.title = bad.length ? "Not an email address: " + bad.join(", ") : ""; }

  // what is on screen now, as the server stores it
  function collect() {
    const people = {}, locations = {}, routing = {};
    box.querySelectorAll("input[data-co]").forEach(i => {
      const c = i.dataset.co, l = i.dataset.loc, v = i.value.trim();
      if (l) { const t = ((locations[c] = locations[c] || {})[l] = locations[c][l] || {}); if (v) t[i.dataset.role] = v; }
      else if (v) (people[c] = people[c] || {})[i.dataset.role] = v;
    });
    (D.reports || []).forEach(r => routing[r.key] = {to: [], cc: []});
    box.querySelectorAll("select[data-rep]").forEach(s => { if (s.value) routing[s.dataset.rep][s.value].push(s.dataset.role); });
    return {people, locations, routing};
  }
  // keep what was typed before the card is drawn again
  function keep() { const c = collect(); D.people = c.people; D.locations = c.locations; D.routing = c.routing; }
  // the same rules as the servers' resolve_in and location_targets_in
  function resolve(dir, rep, cos) {
    const P = {}; Object.entries(dir.people).forEach(([k, v]) => P[k.toUpperCase()] = v);
    const R = dir.routing[rep] || {to: [], cc: []};
    const em = roles => [...new Set([...cos, D.group || "*"].flatMap(c => roles.flatMap(r => parse((P[String(c).toUpperCase()] || {})[r]).filter(x => RE.test(x)))))];
    const to = em(R.to), cc = em(R.cc);
    if (!to.length && !cc.length) return null;
    return {to, cc: cc.filter(x => !to.includes(x))};
  }
  function resolveLoc(dir, rep, c, l) {
    const L = ((dir.locations[c] || {})[l]) || {}, R = dir.routing[rep] || {to: [], cc: []};
    const em = roles => [...new Set(roles.flatMap(r => parse(L[r]).filter(x => RE.test(x))))];
    let to = em(R.to), cc = em(R.cc);
    if (!to.length) { to = cc.slice(0, 1); cc = cc.slice(1); }
    return to.length ? {to, cc: cc.filter(x => !to.includes(x))} : null;
  }
  function who() {
    const dir = collect(), t = box.querySelector("#rcWho");
    if (!t) return;
    const gen = s => `<span class="rc-gen">${s}</span>`;
    const isLoc = pick.includes(SEP), [c, l] = isLoc ? [pick.slice(0, pick.lastIndexOf(SEP)), pick.slice(pick.lastIndexOf(SEP) + SEP.length)] : [pick, ""];
    t.innerHTML = `<tr><th>Report</th><th>To</th><th>Cc</th></tr>` + (D.reports || []).map(rep => {
      let to, cc;
      if (isLoc) {
        const r = rep.by_location ? resolveLoc(dir, rep.key, c, l) : null;
        if (!rep.by_location) { to = gen("company email only"); cc = ""; }
        else if (!r) { to = gen(`no ${esc(lname(c, l))} email: nobody in these roles`); cc = ""; }
        else { to = esc(r.to.join(", ")); cc = esc(r.cc.join(", ")); }
      } else {
        const r = resolve(dir, rep.key, [c]), brand = rep.key === "brand_performance";
        if (!r) { to = brand ? gen("the brand's own addresses") : gen("the addresses in its own card"); cc = gen(brand ? "the Cc in its own card" : "its own card's Cc"); }
        else {
          to = [brand ? gen("the brand's own addresses") : "", esc(r.to.join(", "))].filter(Boolean).join(", ") || gen("its own card's To (else the first Cc)");
          cc = esc(r.cc.join(", "));
        }
      }
      return `<tr><td>${esc(rep.label)}${rep.by_location ? ` <span class="rc-tag">by location</span>` : ""}</td><td data-l="To" style="white-space:normal">${to}</td><td data-l="Cc" style="white-space:normal">${cc || "–"}</td></tr>`;
    }).join("");
  }
  async function save() {
    msg("Saving…");
    try {
      const r = await fetch("/api/recipients", {method: "POST", headers: {"Content-Type": "application/json", "X-Requested-With": HDR}, body: JSON.stringify(collect())});
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.status);
      D = j; D.locations = D.locations || {}; render();
      msg("Saved. Both towers use it from the next email.");
    } catch (e) { msg("Could not save: " + e.message); }
  }
  window.rcLoad = load;
})();
