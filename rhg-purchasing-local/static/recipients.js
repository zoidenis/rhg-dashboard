// Email recipients by company — the same card in Finance and Purchasing → Settings.
// One directory for both towers, kept in Purchasing next to the mail server: for every company the people
// in each role, a row for the whole group added to every company, and which roles each report goes to.
// The tower's page holds <div class="card" id="rcCard" data-header="RHG-Finance"></div> and calls rcLoad().
(function () {
  const box = document.getElementById("rcCard");
  if (!box) return;
  const HDR = box.dataset.header || "";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const allowed = () => typeof can !== "function" || can("settings");
  const RE = /^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$/;
  const parse = t => String(t || "").split(/[,;\s]+/).map(x => x.trim()).filter(Boolean);
  let D = null, pick = "";

  const css = document.createElement("style");
  css.textContent = `
#rcCard details.rc-co{border:1px solid var(--line);border-radius:10px;margin:0 0 8px;background:var(--card)}
#rcCard details.rc-co>summary{cursor:pointer;padding:10px 12px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;list-style:none}
#rcCard details.rc-co>summary::-webkit-details-marker{display:none}
#rcCard details.rc-co>summary::before{content:"▸";color:var(--sub);font-size:11px}
#rcCard details.rc-co[open]>summary::before{content:"▾"}
#rcCard details.rc-co>summary span{font-size:11.5px;color:var(--sub);font-weight:400}
#rcCard details.rc-co .formgrid{padding:0 12px 4px;margin-bottom:8px}
#rcCard .rc-bad input{border-color:var(--up);background:var(--up-bg)}
#rcCard table.rc-m th,#rcCard table.rc-m td{white-space:nowrap}
#rcCard table.rc-m td:first-child{white-space:normal;min-width:150px;max-width:230px}
#rcCard table.rc-m td select{padding:4px 6px;border:1px solid var(--line);border-radius:7px;background:var(--bg);font:inherit;font-size:12px}
#rcCard table.rc-m td select.to{background:var(--down-bg);color:var(--down);font-weight:600}
#rcCard table.rc-m td select.cc{background:var(--warn-bg);color:var(--warn);font-weight:600}
#rcCard .rc-h{font-size:14px;margin:18px 0 4px}
#rcCard .rc-gen{color:var(--sub);font-style:italic}
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
      D = j; render();
      if (j.error) msg(j.error);
    } catch (e) { box.innerHTML = `<h2>Email recipients by company</h2><p class="hint">Could not read: ${esc(e.message)}</p>`; }
  }
  const msg = t => { const m = box.querySelector("#rcMsg"); if (m) m.textContent = t; };

  // the companies on screen: the group row, Business Central's companies, and any kept that BC no longer lists
  function companies() {
    const seen = new Set(), out = [];
    for (const c of [...(D.companies || []), ...Object.keys(D.people || {})]) {
      if (!c || c === D.group || seen.has(c.toUpperCase())) continue;
      seen.add(c.toUpperCase()); out.push(c);
    }
    return out;
  }
  const row = c => { const k = Object.keys(D.people || {}).find(x => x.toUpperCase() === String(c).toUpperCase()); return k ? D.people[k] : {}; };

  function render() {
    const roles = D.roles || [], reports = D.reports || [], cos = companies(), G = D.group || "*";
    if (!pick || !cos.includes(pick)) pick = cos[0] || "";
    const block = (c, title, sub) => {
      const p = row(c), n = roles.filter(r => parse(p[r.key]).length).length;
      return `<details class="rc-co"><summary><b>${esc(title)}</b><span>${n ? `${n} role${n > 1 ? "s" : ""} filled · ${roles.filter(r => parse(p[r.key]).length).map(r => esc(r.label.split(" · ")[0])).join(", ")}` : "nobody yet"}${sub ? " · " + esc(sub) : ""}</span></summary>
        <div class="formgrid">${roles.map(r => `<label class="field">${esc(r.label)}<input data-co="${esc(c)}" data-role="${esc(r.key)}" value="${esc(p[r.key] || "")}"></label>`).join("")}</div></details>`;
    };
    const opt = (rep, role) => { const R = (D.routing || {})[rep] || {to: [], cc: []}; return R.to.includes(role) ? "to" : R.cc.includes(role) ? "cc" : ""; };
    box.innerHTML = `
      <div class="card-head"><div><h2>Email recipients by company</h2>
        <p class="hint">One list for both towers. For every company, the people in each role; each report goes to the roles set below, one email per company with that company's people. <b>All companies</b> is added to every company (for example a group CFO). When a company has nobody in the roles of a report, the report keeps the addresses of its own card, as before; a brand's own addresses stay in To of the brand performance email.</p></div>
        <span class="rhg-pill info" style="white-space:nowrap" title="${esc(D.updated_at ? `Saved ${D.updated_at.replace("T", " ").slice(0, 16)}${D.updated_by ? " by " + D.updated_by : ""}; kept in Purchasing` : "Kept in Purchasing")}">${D.updated_at ? "Saved " + esc(new Date(D.updated_at).toLocaleDateString("en-GB", {day: "numeric", month: "short"})) : "Not set yet"}</span></div>
      <div class="rc-h"><b>People by company</b></div>
      ${block(G, "All companies", "added to every company")}
      ${cos.map(c => block(c, c)).join("")}
      <div class="rc-h"><b>Which report goes to which role</b></div>
      <p class="hint" style="margin:0 0 6px">To or Cc for each role; – leaves the role out of that report.</p>
      <div class="scroll"><table class="rc-m" data-nosort><tr><th>Report</th>${roles.map(r => `<th title="${esc(r.label)}">${esc(r.label.split(" · ")[0])}</th>`).join("")}</tr>
        ${reports.map(rep => `<tr><td><b>${esc(rep.label)}</b><div class="meta">${esc(rep.tower)}</div></td>${roles.map(r => { const v = opt(rep.key, r.key);
          return `<td><select data-rep="${esc(rep.key)}" data-role="${esc(r.key)}" class="${v}" aria-label="${esc(rep.label)}: ${esc(r.label)}"><option value="">–</option><option value="to"${v === "to" ? " selected" : ""}>To</option><option value="cc"${v === "cc" ? " selected" : ""}>Cc</option></select></td>`; }).join("")}</tr>`).join("")}</table></div>
      <div style="margin:6px 0 0"><button class="mini" id="rcDefault">Back to the suggested roles</button></div>
      <div class="rc-h" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><b>Who gets what</b>
        <select id="rcPick" style="padding:5px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);font:inherit">${cos.map(c => `<option${c === pick ? " selected" : ""}>${esc(c)}</option>`).join("")}</select></div>
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
    box.querySelector("#rcSave").onclick = save;
    const ok = allowed();
    box.querySelectorAll("input,select[data-rep],#rcSave,#rcDefault").forEach(x => { x.disabled = !ok; if (!ok) x.title = "Your role cannot change Settings."; });
    box.querySelectorAll("input[data-co]").forEach(mark);
    who();
  }
  function mark(i) { const bad = parse(i.value).filter(x => !RE.test(x)); i.closest(".field").classList.toggle("rc-bad", !!bad.length); i.title = bad.length ? "Not an email address: " + bad.join(", ") : ""; }

  // what is on screen now, as the server stores it
  function collect() {
    const people = {}, routing = {};
    box.querySelectorAll("input[data-co]").forEach(i => { if (i.value.trim()) (people[i.dataset.co] = people[i.dataset.co] || {})[i.dataset.role] = i.value.trim(); });
    (D.reports || []).forEach(r => routing[r.key] = {to: [], cc: []});
    box.querySelectorAll("select[data-rep]").forEach(s => { if (s.value) routing[s.dataset.rep][s.value].push(s.dataset.role); });
    return {people, routing};
  }
  // the same rule as the servers' resolve_in
  function resolve(dir, rep, cos) {
    const P = {}; Object.entries(dir.people).forEach(([k, v]) => P[k.toUpperCase()] = v);
    const R = dir.routing[rep] || {to: [], cc: []};
    const em = roles => [...new Set([...cos, D.group || "*"].flatMap(c => roles.flatMap(r => parse((P[String(c).toUpperCase()] || {})[r]).filter(x => RE.test(x)))))];
    const to = em(R.to), cc = em(R.cc);
    if (!to.length && !cc.length) return null;
    return {to, cc: cc.filter(x => !to.includes(x))};
  }
  function who() {
    const dir = collect(), t = box.querySelector("#rcWho");
    if (!t) return;
    t.innerHTML = `<tr><th>Report</th><th>To</th><th>Cc</th></tr>` + (D.reports || []).map(rep => {
      const r = resolve(dir, rep.key, [pick]);
      const gen = t => `<span class="rc-gen">${t}</span>`, brand = rep.key === "brand_performance";
      let to, cc;
      if (!r) { to = brand ? gen("the brand's own addresses") : gen("the addresses in its own card"); cc = gen(brand ? "the Cc in its own card" : "its own card's Cc"); }
      else {
        to = [brand ? gen("the brand's own addresses") : "", esc(r.to.join(", "))].filter(Boolean).join(", ")
          || gen("its own card's To (else the first Cc)");
        cc = esc(r.cc.join(", "));
      }
      return `<tr><td>${esc(rep.label)}</td><td data-l="To" style="white-space:normal">${to}</td><td data-l="Cc" style="white-space:normal">${cc || "–"}</td></tr>`;
    }).join("");
  }
  async function save() {
    msg("Saving…");
    try {
      const r = await fetch("/api/recipients", {method: "POST", headers: {"Content-Type": "application/json", "X-Requested-With": HDR}, body: JSON.stringify(collect())});
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.status);
      const keep = [...box.querySelectorAll("details.rc-co")].map(d => d.open);
      D = j; render();
      box.querySelectorAll("details.rc-co").forEach((d, i) => d.open = !!keep[i]);
      msg("Saved. Both towers use it from the next email.");
    } catch (e) { msg("Could not save: " + e.message); }
  }
  window.rcLoad = load;
})();
