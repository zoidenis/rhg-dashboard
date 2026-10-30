/* Location intelligence: every restaurant of a company, from Simphony and Business Central.
   Shared module: the Executive dashboard mounts it as its "Location intelligence" section with
   LocIntel.mount(element, {company, period, api, links, onState}). The data comes from the
   Finance tower (/finance/api/locations); the restaurant registry and the alert limits are kept in
   Finance → Settings → Location intelligence. Nothing here writes anywhere. */
(function(){
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function getJSON(path){
  const r = await fetch(path, {credentials: "same-origin"});
  if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
  const t = await r.text(); let j = {};
  try { j = JSON.parse(t); } catch (e) {
    throw new Error(r.status === 502 || r.status === 504 ? "The server took too long to answer; it keeps reading, try again in a minute." : "HTTP " + r.status);
  }
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
const MARKUP = `<div class="card" style="margin-bottom:14px">
      <div class="card-head"><div><h2>Location intelligence</h2>
        <p class="hint" style="margin-bottom:0">Every restaurant of the company as a square. Click a restaurant for its KPIs from Simphony and its Finance, Purchasing and Inventory from Business Central; the company totals are further down. Read only.</p></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <label class="meta" style="display:flex;gap:6px;align-items:center">Order
            <select id="liSort" style="font-size:12px;padding:5px 8px"><option value="net">by Simphony sales</option><option value="ly">by change vs last year</option><option value="score">by health, weakest first</option><option value="alerts">by alerts</option><option value="fc">by food cost</option><option value="name">by name</option></select></label>
          <button class="mini" id="liRefresh">Read again</button></div></div>
      <p class="meta" id="liStatus" style="margin:10px 0 0"></p>
    </div>
    <div id="liBody" style="display:none">
      <div class="li-sec"><h2>Restaurants<small id="liRestScope"></small></h2><span class="meta">click a restaurant for its KPIs</span></div>
      <div class="li-rest" id="liRest"></div>
      <div id="liOther"></div>
      <div class="li-sec" style="margin-top:22px"><h2><span class="li-src">Simphony</span>Whole company<small id="liScope"></small></h2><span class="meta">click a square for every restaurant, the daily trend and alerts</span></div>
      <div class="li-sqs" id="liSqs"></div>
      <div class="li-sec"><h2><span class="li-src">Business Central</span>Finance &amp; stock<small id="liBcScope"></small></h2></div>
      <div class="kpis li-kpis" id="liKpis"></div>
      <div class="card" id="liMapCard"><div class="card-head" style="margin-bottom:10px"><h2 id="liMapTitle">On the map · Tirana and the airport</h2><span class="meta">ring colour = health · label = Simphony net sales</span></div>
        <div class="li-mapwrap" id="liMap"></div>
        <div class="li-legend"><span><span class="li-dot" style="border-color:var(--down)"></span>Healthy 75+</span><span><span class="li-dot" style="border-color:var(--warn)"></span>Watch 55–74</span><span><span class="li-dot" style="border-color:var(--up)"></span>Act below 55</span><span>Positions are schematic; set them in Finance → Settings → Location intelligence</span></div>
      </div>
      <p class="note" id="liFoot" style="font-size:12px;color:var(--sub);line-height:1.6"></p>
    </div>
  `;
const key = () => `${opts.company || ""}|${opts.period || ""}`;
let liData = null, liTimer = null, liKey = "", host = null, opts = {}, liSort = "net";
document.body.insertAdjacentHTML("beforeend", `<div class="li-bd" id="liBd"><div class="li-pop" id="liPop" role="dialog" aria-modal="true"></div></div>
  <div class="li-bd" id="liBd2"><div class="li-pop" id="liPop2" role="dialog" aria-modal="true"></div></div><div id="liTip"></div>`);
const LI_G = {
  SALT: '<svg viewBox="0 0 24 24" fill="none" stroke="#0c475e" stroke-width="1.8" stroke-linecap="round"><path d="M3 15c2.2-2 4.3-2 6.5 0s4.3 2 6.5 0 3.3-1.6 5-1"/><path d="M3 19c2.2-2 4.3-2 6.5 0s4.3 2 6.5 0 3.3-1.6 5-1"/><path d="M12 3l3 4-3 4-3-4z" fill="#a0d8ea"/></svg>',
  LIFT: '<svg viewBox="0 0 24 24" fill="none" stroke="#0c475e" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21V9h5v12M10 21V4h5v17M16 21v-8h4v8"/><path d="M12 1.5l2 2h-4z" fill="#0c475e"/></svg>',
  OPA: '<svg viewBox="0 0 24 24" fill="none" stroke="#0c475e" stroke-width="1.8" stroke-linecap="round"><path d="M4 6h16M5 9h14M6 9v10M10 9v10M14 9v10M18 9v10M4 21h16M8 3h8"/></svg>',
  VESPER: '<svg viewBox="0 0 24 24" fill="none" stroke="#0c475e" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h14l-7 8z"/><path d="M12 12v7M8 21h8"/><circle cx="16.5" cy="6" r="1.4" fill="#a0d8ea"/></svg>',
  "LE DISTRICT": '<svg viewBox="0 0 24 24" fill="none" stroke="#0c475e" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21V8l8-5 8 5v13"/><path d="M9 21v-6h6v6M8 10h2M14 10h2"/></svg>',
  SIN: '<svg viewBox="0 0 24 24" fill="none" stroke="#0c475e" stroke-width="1.8" stroke-linecap="round"><path d="M5 19c0-8 6-14 14-14 0 8-6 14-14 14z"/><path d="M5 19L14 10"/></svg>',
  ZOI: '<svg viewBox="0 0 24 24" fill="none" stroke="#0c475e" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="10" r="4" fill="#a0d8ea"/><path d="M12 2v2M4 10H2M22 10h-2M5.6 3.6l1.4 1.4M18.4 3.6L17 5M3 19c3-2 6-2 9 0s6 2 9 0"/></svg>',
  KPI: '<svg viewBox="0 0 24 24" fill="none" stroke="#0c475e" stroke-width="1.8" stroke-linecap="round"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>',
  DOT: '<svg viewBox="0 0 24 24" fill="none" stroke="#0c475e" stroke-width="1.8"><circle cx="12" cy="12" r="6"/></svg>'
};
const liGlyph = b => LI_G[b] || LI_G.DOT;
const liM = v => v == null || !isFinite(v) ? "–" : (v / 1e6).toLocaleString("en-US", {minimumFractionDigits: 1, maximumFractionDigits: 1}) + " M";
const liN = v => v == null || !isFinite(v) ? "–" : Math.round(v).toLocaleString("en-US");
const liP1 = v => v.toFixed(1) + "%";
const liPct = v => v == null || !isFinite(v) ? "–" : Math.abs(v) < 0.5 ? "0%" : (v > 0 ? "+" : "") + v.toFixed(0) + "%";
const liMon = iso => new Date(iso + "T00:00:00").toLocaleString("en-US", {month: "short"});
const liDay = iso => +iso.slice(8, 10);
const LI_F = ["net", "discounts", "gross", "checks", "guests", "voids", "error_corrects", "tax"];
function liSum(arr, p){
  const o = {n: 0, gNet: 0, gGuests: 0}; LI_F.forEach(f => o[f] = 0);
  for (const l of arr) { const t = l.sim && l.sim[p]; if (!t) continue; o.n++; LI_F.forEach(f => o[f] += t[f] || 0);
    if (t.guests && t.guests >= t.checks) { o.gNet += t.net; o.gGuests += t.guests; } }
  return o.n ? o : null;
}
const liT = (l, p) => liSum([l], p);
const LI_K = [
  {k: "net", lab: "Net sales", unit: "incl. VAT", money: 1, v: t => t.net || null, dir: 1, daily: "net", a: t => `ex VAT ${liM(t.net - t.tax)}`,
   def: "Simphony net sales (netSlsTtl): what guests paid after item and check discounts, before service charge. VAT is inside this figure, so <b>ex VAT = net − tax collected</b>. Business Central posts the same sales with VAT to the restaurant's POS customer."},
  {k: "checks", lab: "Checks", v: t => t.checks || null, dir: 1, daily: "checks", a: t => `${liN(t.checks / LI.days)} a day`,
   def: "Checks closed in Simphony for the business day (chkCnt). At the OPA counters one check is one order; at SALT, LIFT and Vesper it is one table, so compare a restaurant with itself over time rather than with the others."},
  {k: "avg", lab: "Average check", unit: "incl. VAT", money: 1, v: t => t.checks ? t.net / t.checks : null, dir: 1, daily: "avg", a: () => "net sales ÷ checks",
   def: "Net sales ÷ checks. A table at SALT and a counter order at OPA are different things, so read it per restaurant; the company figure moves with the mix of restaurants."},
  {k: "guests", lab: "Guests", unit: "covers", v: t => t.guests || null, dir: 1, noLy: 1, daily: "guests", a: t => `${liN(t.guests / LI.days)} a day`,
   def: "Covers keyed at the POS (gstCnt). Reliable only where staff key covers on every check; the OPA counters mostly skip it, and last year covers were hardly keyed, so there is no comparison with last year."},
  {k: "spg", lab: "Spend per guest", unit: "incl. VAT", money: 1, noLy: 1, v: t => t.gGuests ? t.gNet / t.gGuests : null, dir: 1, a: () => "where covers are keyed",
   def: "Net sales ÷ covers, only for restaurants where covers are keyed on every check (guests ≥ checks). Elsewhere it is left blank rather than shown wrong."},
  {k: "disc", lab: "Discounts", unit: "% of gross", rate: 1, lim: "discounts", v: t => t.gross ? t.discounts / t.gross * 100 : null, dir: -1, a: t => `${liM(t.discounts)} of ${liM(t.gross)} gross`, amt: t => liM(t.discounts),
   def: "Item and check discounts as a share of gross sales (gross = net + discounts). A step change usually means a new promotion, staff meals rung as discounts, or a discount key used without approval."},
  {k: "voids", lab: "Voids", unit: "% of gross", rate: 1, lim: "voids", v: t => t.gross ? t.voids / t.gross * 100 : null, dir: -1, a: t => `${liM(t.voids)} voided`, amt: t => liM(t.voids),
   def: "Items taken off a check after they were sent to the kitchen or bar, as a share of gross sales. Each one is either waste or a control gap."},
  {k: "err", lab: "Error corrects", unit: "% of net", rate: 1, lim: "error_corrects", v: t => t.net ? t.error_corrects / t.net * 100 : null, dir: -1, a: t => `${liM(t.error_corrects)} corrected`, amt: t => liM(t.error_corrects),
   def: "Items removed before the order was sent (error corrects), as a share of net sales. Not a loss in itself, but a high share can hide removals that should have been voids and is worth checking by employee and hour."}
];
const LI_KB = Object.fromEntries(LI_K.map(k => [k.k, k]));
LI_K.forEach(k => k.f = k.rate ? liP1 : k.money && k.k === "net" ? liM : liN);
const liVal = (k, l, p) => { const t = liT(l, p); return t ? k.v(t) : null; };
const liDelta = (k, a, b) => a == null || b == null || !isFinite(a) || !isFinite(b) || (!k.rate && !b) ? null : k.rate ? a - b : (a / b - 1) * 100;
const liFd = (k, x) => x == null ? "–" : k.rate ? (Math.abs(x) < 0.05 ? "0.0" : (x > 0 ? "+" : "") + x.toFixed(1)) + " pp" : liPct(x);
const liDc = (k, x) => x == null ? "" : x * k.dir <= (k.rate ? -0.5 : -5) ? "up" : x * k.dir >= (k.rate ? 0.5 : 5) ? "ok" : "";
const liEur = l => l && l.currency && l.currency !== LI.ccy;
const liFv = (k, v, l) => v == null || !isFinite(v) ? "–" : liEur(l) && k.money ? `${liN(v)} ${l.currency}` : k.f(v);
const LI = {};    // the derived state of the current read

function liRows(k, list){
  return list.map(l => { const a = liVal(k, l, "cur"), b = liVal(k, l, "prev"), c = k.noLy ? null : liVal(k, l, "ly");
    return {l, a, b, c, dS: liDelta(k, a, b), dL: liDelta(k, a, c)}; }).sort((x, y) => (y.a ?? -1e18) - (x.a ?? -1e18));
}
function liDaily(k, list){
  const n = LI.days, out = new Array(n).fill(0), ch = new Array(n).fill(0);
  for (const l of list) { const d = l.daily; if (!d) continue;
    for (let i = 0; i < n; i++) { out[i] += k.daily === "avg" ? d.net[i] : (d[k.daily] || [])[i] || 0; ch[i] += d.checks[i] || 0; } }
  return k.daily === "avg" ? out.map((v, i) => ch[i] ? v / ch[i] : null) : out;
}
function liSpark(arr){
  const a = arr.map(v => v || 0), mx = Math.max(...a), mn = Math.min(...a), W = 200, H = 30;
  if (a.length < 2) return "";
  const pts = a.map((v, i) => `${(i / (a.length - 1) * W).toFixed(1)},${(H - 3 - (v - mn) / (mx - mn || 1) * (H - 6)).toFixed(1)}`).join(" ");
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--li-c1)" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}
function liLine(a, b, o = {}){
  const W = o.w || 520, H = o.h || 150, L = 46, R = 8, T0 = 10, B = 22, n = a.length, fy = o.fy || (v => (v / 1e6).toFixed(1) + "M");
  if (n < 2) return `<p class="meta">One day so far.</p>`;
  const all = [...a, ...(b || [])].filter(v => v != null && isFinite(v));
  const max = (Math.max(...all, 0) * 1.08) || 1, x = i => L + i * (W - L - R) / (n - 1), y = v => T0 + (1 - (v || 0) / max) * (H - T0 - B);
  const path = arr => arr.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  let s = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="${esc(o.label || "daily chart")}">`;
  for (const t of [0, .5, 1]) s += `<line x1="${L}" x2="${W - R}" y1="${y(max * t / 1.08)}" y2="${y(max * t / 1.08)}" stroke="var(--line)" stroke-opacity=".6"/><text x="${L - 6}" y="${y(max * t / 1.08) + 4}" font-size="10" fill="var(--sub)" text-anchor="end">${fy(max * t / 1.08)}</text>`;
  const ticks = [...new Set([0, Math.round((n - 1) / 4), Math.round((n - 1) / 2), Math.round(3 * (n - 1) / 4), n - 1])];
  for (const i of ticks) s += `<text x="${x(i)}" y="${H - 6}" font-size="10" fill="var(--sub)" text-anchor="middle">${liDay(LI.dates[i])}</text>`;
  if (b) s += `<path d="${path(b)}" fill="none" stroke="var(--li-c2)" stroke-width="2"/>`;
  s += `<path d="${path(a)}" fill="none" stroke="var(--li-c1)" stroke-width="2"/>`;
  s += `<line class="li-cx" x1="0" x2="0" y1="${T0}" y2="${H - B}" stroke="var(--ink)" stroke-width="1" opacity="0"/>`;
  const w = (W - L - R) / (n - 1);
  a.forEach((v, i) => s += `<rect x="${x(i) - w / 2}" y="${T0}" width="${w}" height="${H - T0 - B}" fill="transparent" data-i="${i}" data-tip="${esc(o.tip(i))}"/>`);
  return s + `</svg>`;
}
const liTile = (lab, v, s, cls, attrs) => `<div class="li-tile ${cls || ""}" ${attrs || ""}><div class="l">${lab}</div><div class="v">${v}</div><div class="s">${s || ""}</div></div>`;

// ---- read
async function loadLocations(refresh){
  clearTimeout(liTimer);
  const p = new URLSearchParams({company: opts.company || ""});
  if (opts.period) p.set("period", opts.period);
  if (refresh) p.set("refresh", "1");
  $("liStatus").textContent = "Reading Simphony and Business Central…";
  try{
    const j = await getJSON(opts.api + "?" + p.toString());
    if (!host || !host.isConnected) return;
    if (j.error) throw new Error(j.error);
    if (j.pending) { $("liStatus").textContent = `Reading · ${j.stage || "starting"} · ${j.elapsed || 0} s. The first read of a month reads every restaurant and day once; later reads come from what is held.`;
      liTimer = setTimeout(() => host && host.isConnected && loadLocations(false), 4000); return; }
    liData = j; liKey = key(); liRender();
    if (j.inventory && j.inventory.state === "pending") liTimer = setTimeout(() => host && host.isConnected && loadLocations(false), 15000);
  }catch(e){ $("liStatus").textContent = "Could not read: " + e.message; }
}
function bind(){
  $("liRefresh").onclick = () => loadLocations(true);
  $("liSort").value = liSort;
  $("liSort").onchange = () => { liSort = $("liSort").value; if (liData) liDrawRest(); };
}

// ---- derive
function liHealth(l){
  const th = LI.th, parts = [];
  if (l.lyD != null) parts.push(["Sales vs last year", Math.max(0, Math.min(25, 25 + l.lyD)), `${liPct(l.lyD)} vs ${LI.lyLab} (Simphony, same dates)`]);
  if (l.fc != null) parts.push(["Food cost", Math.max(0, Math.min(25, 25 - Math.max(0, l.fc - th.food_cost) * 2.5)), `${l.fc.toFixed(1)}% vs ${th.food_cost}%`]);
  if (l.inv && l.inv.doh != null) parts.push(["Stock cover", Math.max(0, Math.min(25, 25 - Math.max(0, l.inv.doh - th.cover_days) * 0.17)), `${l.inv.doh} days vs ${th.cover_days}`]);
  if (l.inv) parts.push(["Stock book", Math.max(0, Math.min(25, 25 - l.inv.neg_lines * 0.12 - l.inv.count * 0.08)), `${l.inv.neg_lines} negative · ${l.inv.count} to count`]);
  l.parts = parts;
  l.score = parts.length >= 2 ? Math.round(parts.reduce((s, p) => s + p[1], 0) * 4 / parts.length) : null;
  l.h = l.score == null ? "h-na" : l.score >= 75 ? "h-ok" : l.score >= 55 ? "h-mid" : "h-low";
}
function liSimAlerts(l){
  const al = [], th = LI.th; if (!l.sim || !l.sim.cur.net) return al;
  const c = liT(l, "cur"), p = liT(l, "prev"), y = liT(l, "ly");
  const er = c.error_corrects / c.net * 100, dp = c.gross ? c.discounts / c.gross * 100 : 0, vp = c.gross ? c.voids / c.gross * 100 : 0;
  if (y && y.net) { const d = (c.net / y.net - 1) * 100; if (d <= -th.sales_drop) al.push(["warn", `Sales ${liPct(d)} vs ${LI.lyLab}`, `Simphony ${liFv(LI_KB.net, c.net, l)} against ${liFv(LI_KB.net, y.net, l)} on the same dates.`]); }
  if (er >= th.error_corrects) al.push([er >= 2 * th.error_corrects ? "crit" : "warn", `Error corrects ${er.toFixed(1)}% of sales`, `${liM(c.error_corrects)} removed before sending; ${p ? liM(p.error_corrects) + " in " + LI.prevLab : ""}. Check by employee and hour.`]);
  if (dp >= th.discounts) al.push(["warn", `Discounts ${dp.toFixed(1)}% of gross`, `${liM(c.discounts)} this month${y && y.gross ? `, ${(y.discounts / y.gross * 100).toFixed(1)}% a year ago` : ""}.`]);
  if (vp >= th.voids) al.push(["warn", `Voids ${vp.toFixed(1)}% of gross`, `${liM(c.voids)} taken back after sending.`]);
  else if (p && p.voids && c.voids >= 3 * p.voids && c.voids > 50000) al.push(["warn", `Voids ×${(c.voids / p.voids).toFixed(1)} vs ${LI.prevLab}`, `${liN(c.voids)} against ${liN(p.voids)}.`]);
  return al;
}
function liPostAlerts(l){
  const al = [];
  if (!l.sim || !l.sim.cur.net || l.company !== LI.company) return al;
  if (!l.customer) { al.push(["warn", "No BC POS customer in the registry", `Simphony has ${liFv(LI_KB.net, l.sim.cur.net, l)} this month; add the customer the sales post to (Finance → Settings → Location intelligence) so posting can be checked.`]); return al; }
  const bp = l.bc_posted; if (!bp) return al;
  if (!bp.total) al.push(["crit", `${liFv(LI_KB.net, l.sim.cur.net, l)} sold in Simphony, nothing posted in BC`, `No invoice for POS customer ${esc(l.customer)} in ${esc(LI.company)} this month.`]);
  else if (bp.unposted.length) al.push(["warn", `${bp.unposted.length} day${bp.unposted.length > 1 ? "s" : ""} not posted in BC`, `Simphony sold on ${bp.unposted.slice(0, 6).map(d => liDay(d) + " " + liMon(d)).join(", ")}${bp.unposted.length > 6 ? "…" : ""} with no invoice for ${esc(l.customer)}${bp.last ? `; last posted ${liDay(bp.last)} ${liMon(bp.last)}` : ""}.`]);
  else if (Math.abs(bp.total / l.sim.cur.net - 1) > 0.03) al.push(["info", `Posted ${liPct((bp.total / l.sim.cur.net - 1) * 100)} against Simphony`, `BC ${liM(bp.total)} against Simphony ${liM(l.sim.cur.net)}, both with VAT.`]);
  return al;
}
function liDerive(){
  const j = liData, th = j.thresholds;
  Object.assign(LI, {th, company: j.company_bc || j.company, dates: j.period.days, days: Math.max(1, j.period.days.length),
    prevLab: liMon(j.period.prev_from), lyLab: liMon(j.period.ly_from) + " " + j.period.ly_from.slice(0, 4),
    curLab: `${liDay(j.period.from)}–${liDay(j.period.to)} ${liMon(j.period.to)} ${j.period.to.slice(0, 4)}`});
  const all = j.locations;
  all.forEach(l => { l.key = l.ref; });
  const mine = all.filter(l => l.company === LI.company);
  LI.scope = mine.filter(l => l.status === "open");
  const tally = {}; LI.scope.forEach(l => tally[l.currency] = (tally[l.currency] || 0) + (l.sim ? l.sim.cur.net : 0));
  LI.ccy = Object.keys(tally).sort((a, b) => tally[b] - tally[a])[0] || j.group_currency;
  LI.tot = LI.scope.filter(l => l.currency === LI.ccy && l.sim);
  LI.closed = mine.filter(l => l.status === "closed");
  LI.others = all.filter(l => l.company !== LI.company && l.registered && l.status !== "no sales");
  LI.unreg = all.filter(l => !l.registered);
  for (const l of all) {
    const c = l.sim && l.sim.cur, y = l.sim && l.sim.ly;
    l.snet = c ? c.net : null;
    l.lyD = c && y && y.net ? (c.net / y.net - 1) * 100 : null;
    l.exvat = c ? c.net - c.tax : null;
    l.fc = l.use != null && l.exvat > 0 ? l.use / l.exvat * 100 : null;
    liHealth(l);
    const al = [...liPostAlerts(l), ...liSimAlerts(l)], inv = l.inv;
    if (inv) {
      if (inv.count >= 50) al.push(["crit", `${inv.count} fresh lines to count`, `Book stock of fresh goods covers months${inv.count_top[0] ? `, e.g. ${esc(inv.count_top[0][0])} ${liN(inv.count_top[0][3])} days` : ""}. Count and correct.`]);
      else if (inv.count) al.push(["warn", `${inv.count} fresh line${inv.count > 1 ? "s" : ""} to count`, inv.count_top[0] ? `Largest: ${esc(inv.count_top[0][0])}, ${liM(inv.count_top[0][2])} in the book.` : ""]);
      if (inv.neg_lines >= 80) al.push(["crit", `${inv.neg_lines} lines with negative stock`, "Consumption posted before receipts or transfers; fix before the month-end count."]);
      if (inv.slow > 3e6) al.push(["info", `${liM(inv.slow)} slow or non-moving`, "Stock not used for 60+ days; see Movement & dead stock."]);
    }
    if (l.fc != null && l.fc > th.food_cost + 2) al.push(["warn", `Food cost ${l.fc.toFixed(1)}%`, `Above the ${th.food_cost}% target: use at cost ${liM(l.use)} for ${liM(l.exvat)} sales ex VAT.`]);
    if (!al.length && l.status === "open") al.push(["good", "No exception", "Nothing above the thresholds this month."]);
    const order = ["crit", "warn", "info", "good"];
    l.alerts = al.sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));
  }
  LI.G0 = liSum(LI.tot, "cur"); LI.G1 = liSum(LI.tot, "prev"); LI.G2 = liSum(LI.tot, "ly");
}

// ---- draw
function liRender(){
  const j = liData;
  liDerive();
  $("liBody").style.display = "";
  const errs = (j.errors || []).length ? ` · ${j.errors.length} read error${j.errors.length > 1 ? "s" : ""}: ${esc(j.errors[0])}` : "";
  $("liStatus").innerHTML = `${esc(j.period.label)} · whole days ${LI.curLab}, against ${liDay(j.period.prev_from)}–${liDay(j.period.prev_to)} ${LI.prevLab} and the same dates of ${j.period.ly_from.slice(0, 4)} · read ${esc(j.checked_at.replace("T", " "))}${j.inventory ? " · " + esc(j.inventory.note || "") : ""}${errs}`;
  if (!j.simphony) $("liStatus").innerHTML += " · Simphony is not connected (Settings → Simphony sales).";
  $("liScope").textContent = `${LI.company} · ${LI.scope.length} restaurant${LI.scope.length === 1 ? "" : "s"} open${LI.tot.length < LI.scope.length ? ` · totals in ${LI.ccy}` : ""} · ${LI.curLab}`;
  $("liRestScope").textContent = `${LI.company} · ${LI.scope.length} open · ${LI.curLab}`;
  liDrawRest(); liDrawSquares(); liDrawKpis(); liDrawMap();
  $("liFoot").innerHTML = Object.values(j.definitions || {}).map(esc).join(" ");
  if (opts.onState) opts.onState(j);
}
function liDrawSquares(){
  if (!LI.G0) { $("liSqs").innerHTML = `<p class="meta">No Simphony sales for ${esc(LI.company)} in this period.</p>`; return; }
  $("liSqs").innerHTML = LI_K.map(k => {
    const a = k.v(LI.G0), dS = liDelta(k, a, LI.G1 && k.v(LI.G1)), dL = k.noLy ? null : liDelta(k, a, LI.G2 && k.v(LI.G2));
    const r = liRows(k, LI.tot).filter(x => x.a != null);
    let ft;
    if (k.k === "spg") ft = `<div class="top">${r.slice(0, 3).map(x => `${esc(x.l.name.split(" ")[0])} <b>${liN(x.a)}</b>`).join(" · ") || "no restaurant keys covers"}</div>`;
    else if (k.rate) ft = `<div class="top">${r[0] ? `Highest: <b>${esc(r[0].l.name)} ${k.f(r[0].a)}</b>` : ""}${r[1] ? `<br>then ${esc(r[1].l.name)} ${k.f(r[1].a)}` : ""}</div>`;
    else ft = `<div style="flex:1;min-width:0">${liSpark(liDaily(k, LI.tot))}<div class="meta" style="font-size:10.5px">daily, ${LI.curLab}</div></div>`;
    return `<button class="li-sq" data-kpi="${k.k}"><div class="l">${k.lab}<span>${k.unit || ""}</span></div>
      <div class="v">${a == null ? "–" : k.f(a)}</div><div class="a">${k.a(LI.G0)}</div>
      <div class="cmp"><span>vs ${LI.prevLab} <b class="${liDc(k, dS)}">${liFd(k, dS)}</b></span><span>vs ${LI.lyLab} <b class="${liDc(k, dL)}">${k.noLy ? "n/a" : liFd(k, dL)}</b></span></div>
      <div class="ft">${ft}<span class="more">Details ›</span></div></button>`;
  }).join("");
}
function liDrawKpis(){
  const s = LI.tot, th = LI.th;
  const posted = s.filter(l => l.bc_posted).reduce((a, l) => a + l.bc_posted.total, 0), simPosted = s.filter(l => l.bc_posted).reduce((a, l) => a + l.sim.cur.net, 0);
  const useL = s.filter(l => l.fc != null), use = useL.reduce((a, l) => a + l.use, 0), exv = useL.reduce((a, l) => a + l.exvat, 0);
  const inv = s.filter(l => l.inv), stock = inv.reduce((a, l) => a + l.inv.stock, 0);
  const purch = s.filter(l => l.purch).reduce((a, l) => a + l.purch.amount, 0);
  const toAct = s.filter(l => l.h === "h-low").length, toWatch = s.filter(l => l.h === "h-mid").length, notPosted = s.filter(l => l.alerts.some(a => /posted|POS customer/.test(a[1]))).length;
  const kpi = (l, v, sub, c) => `<div class="card kpi"><div class="l">${l}</div><div class="v ${c || ""}">${v}</div><div class="s">${sub}</div></div>`;
  $("liBcScope").textContent = `${LI.company} · ${LI.curLab} · posted`;
  $("liKpis").innerHTML =
    kpi("Sales posted in BC", liM(posted), simPosted ? `with VAT · ${liPct((posted / simPosted - 1) * 100)} against Simphony (${liM(simPosted)})` : "no POS customer in the registry") +
    kpi("Food cost (estimated)", exv ? (use / exv * 100).toFixed(1) + "%" : "–", exv ? `use at cost ${liM(use)} · target ${th.food_cost}%` : (liData.inventory || {}).note || "", exv && use / exv * 100 > th.food_cost ? "warn" : "down") +
    kpi("Stock in restaurants", inv.length ? liM(stock) : "–", inv.length ? `at item cost · ${esc((liData.inventory || {}).as_of || "")}` : esc((liData.inventory || {}).note || "")) +
    kpi("Bought direct", liM(purch), "posted purchase invoices with the restaurant's location, VAT incl.") +
    kpi("Restaurants to act on", `${toAct} of ${s.length}`, `${toWatch} to watch${notPosted ? ` · ${notPosted} with posting gaps` : ""}`, toAct ? "up" : "");
}
function liDrawMap(){
  const pins = LI.scope.filter(l => l.x != null && !l.region);
  $("liMapCard").style.display = pins.length ? "" : "none";
  if (!pins.length) return;
  const W = 1000, H = 560;
  let s = `<svg class="base" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
    <rect width="${W}" height="${H}" fill="var(--li-map)"/>
    <path d="M0 330 C 200 300, 380 340, 560 310 S 860 260, 1000 280" fill="none" stroke="var(--li-water)" stroke-width="10"/>
    <ellipse cx="560" cy="330" rx="230" ry="150" fill="none" stroke="var(--li-road)" stroke-width="14"/>
    <ellipse cx="520" cy="340" rx="110" ry="70" fill="none" stroke="var(--li-road)" stroke-width="9"/>
    <path d="M520 340 L 140 220 M520 340 L 300 285 M520 340 L 870 470 M520 340 L 560 60 M520 340 L 300 500" stroke="var(--li-road)" stroke-width="9" fill="none"/>
    <path d="M140 220 L 80 70" stroke="var(--li-road)" stroke-width="6" stroke-dasharray="10 8" fill="none"/>
    <ellipse cx="500" cy="470" rx="80" ry="38" fill="var(--li-park)"/><ellipse cx="525" cy="478" rx="34" ry="14" fill="var(--li-water)"/>
    <text x="500" y="525" font-size="12" fill="#8a979e" text-anchor="middle">Parku i Madh</text>
    <text x="545" y="330" font-size="13" fill="#8a979e" text-anchor="middle" font-weight="600">Qendra</text>
    <text x="150" y="250" font-size="12" fill="#8a979e">Rr. Tiranë–Durrës</text>
    <text x="760" y="505" font-size="12" fill="#8a979e">Rr. Tiranë–Elbasan</text></svg>`;
  for (const l of pins) s += `<div class="li-pin ${l.h}" style="left:${l.x}%;top:${l.y}%" data-loc="${esc(l.ref)}" data-tip="${esc(`<b>${esc(l.name)}</b><br>Simphony ${liFv(LI_KB.net, l.snet, l)} · ${liPct(l.lyD)} vs ${LI.lyLab}${l.bc_posted ? `<br>posted in BC ${liM(l.bc_posted.total)}` : ""}<br>Health ${l.score ?? "–"}/100`)}">
      <div class="ic">${liGlyph(l.brand)}</div><div class="lb">${esc(l.name.replace(" Steak & Rooftop", "").replace(" Tirana", ""))} <span>${liFv(LI_KB.net, l.snet, l)}</span></div></div>`;
  $("liMap").innerHTML = s;
}
const LI_ORDER = {crit: 0, warn: 1, info: 2, good: 3};
function liRestSquare(l){
  const c = liT(l, "cur"), p = liT(l, "prev"), ring = {"h-ok": "var(--down)", "h-mid": "var(--warn)", "h-low": "var(--up)"}[l.h] || "var(--line)";
  const dS = c && p && p.net ? (c.net / p.net - 1) * 100 : null;
  const top = l.alerts.find(a => a[0] !== "good"), n = l.alerts.filter(a => a[0] !== "good").length;
  const stat = (lab, v, cls) => `<div><span>${lab}</span><b class="${cls || ""}">${v}</b></div>`;
  const posted = l.bc_posted && l.snet ? (l.bc_posted.total / l.snet - 1) * 100 : null;
  return `<button class="li-rsq" data-loc="${esc(l.ref)}" aria-label="${esc(l.name)}: open its KPIs">
    <div class="hd"><div class="ic" style="border-color:${ring}">${liGlyph(l.brand)}</div>
      <div class="nm"><b>${esc(l.name)}</b><span>${esc([l.area, l.region].filter(Boolean).join(" · "))}</span></div>
      <span class="li-score ${l.h}" title="Health score">${l.score ?? "–"}</span></div>
    <div class="big"><span class="v">${liFv(LI_KB.net, l.snet, l)}</span><span class="u">net sales, VAT incl.</span></div>
    <div class="cmp"><span>vs ${LI.lyLab} <b class="${l.lyD == null ? "" : l.lyD <= -LI.th.sales_drop ? "up" : l.lyD >= 5 ? "down" : ""}">${liPct(l.lyD)}</b></span><span>vs ${LI.prevLab} <b class="${dS != null && dS <= -LI.th.sales_drop ? "up" : ""}">${liPct(dS)}</b></span></div>
    ${l.daily ? `<div class="sp">${liSpark(l.daily.net)}</div>` : ""}
    <div class="st">${stat("Checks", c ? liN(c.checks) : "–")}${stat("Avg check", c && c.checks ? liN(c.net / c.checks) : "–")}
      ${stat("Food cost", l.fc != null ? l.fc.toFixed(1) + "%" : "–", l.fc != null && l.fc > LI.th.food_cost + 2 ? "warn" : "")}
      ${l.inv ? stat("Stock cover", l.inv.doh != null ? l.inv.doh + " d" : "–", l.inv.doh != null && l.inv.doh > LI.th.cover_days ? "up" : "")
              : stat("Posted in BC", posted != null ? liPct(posted) : "–", posted != null && posted < -3 ? "up" : "")}</div>
    <div class="al ${top ? top[0] : "good"}"><i></i><span>${top ? esc(top[1]) : "No exception"}</span>${n > 1 ? `<em>+${n - 1}</em>` : ""}</div>
  </button>`;
}
function liDrawRest(){
  const by = liSort;
  const key = {net: l => -(l.snet || 0), ly: l => l.lyD ?? 1e9, score: l => l.score ?? 1e9, fc: l => -(l.fc ?? -1), name: l => 0,
               alerts: l => (l.alerts[0] ? LI_ORDER[l.alerts[0][0]] * 100 : 400) - l.alerts.filter(a => a[0] !== "good").length}[by] || (l => 0);
  const list = [...LI.scope].sort((a, b) => key(a) - key(b) || (by === "name" ? a.name.localeCompare(b.name) : (b.snet || 0) - (a.snet || 0)));
  $("liRest").innerHTML = list.length ? list.map(liRestSquare).join("") : `<p class="meta">No restaurant of ${esc(LI.company)} sold in this period.</p>`;
  const small = (title, arr, txt) => arr.length ? `<div class="meta" style="margin:14px 0 6px">${title}</div><div class="li-small">${arr.map(l => `<button class="li-ssq" data-loc="${esc(l.ref)}">
      <span class="li-mini">${liGlyph(l.brand)}</span><span class="nm"><b>${esc(l.name)}</b><span>${txt(l)}</span></span></button>`).join("")}</div>` : "";
  $("liOther").innerHTML =
    small("Closed this period", LI.closed, l => l.sim ? `${LI.prevLab} ${liFv(LI_KB.net, l.sim.prev.net, l)}` : "no sales") +
    small("Other companies", LI.others, l => `${esc(l.company || "?")}${l.sim ? " · " + liFv(LI_KB.net, l.sim.cur.net || l.sim.prev.net, l) : ""}`) +
    small("In Simphony, not in the registry", LI.unreg, l => liFv(LI_KB.net, l.sim.cur.net, l));
}

// ---- restaurant popup
function liSimTiles(l, p){
  const t = liT(l, p);
  return LI_K.map(k => { const a = t ? k.v(t) : null;
    let sub;
    if (p === "cur") { const dS = liDelta(k, a, liVal(k, l, "prev")), dL = k.noLy ? null : liDelta(k, a, liVal(k, l, "ly"));
      sub = dL != null ? `LY <b class="${liDc(k, dL)}">${liFd(k, dL)}</b> · ${LI.prevLab} <b class="${liDc(k, dS)}">${liFd(k, dS)}</b>` : `${LI.prevLab} <b class="${liDc(k, dS)}">${liFd(k, dS)}</b>`; }
    else sub = LI.prevLab;
    if (a == null && (k.k === "spg" || k.k === "guests")) sub = "covers not keyed";
    return liTile(`${k.lab}${k.unit ? ` <span style="opacity:.75">· ${k.unit}</span>` : ""}`, liFv(k, a, l), sub, "click", `data-kpi="${k.k}" data-kloc="${esc(l.ref)}" title="${esc(k.lab)} for every restaurant"`);
  }).join("");
}
function liOpen(ref){
  const l = liData.locations.find(x => x.ref === ref); if (!l) return;
  $("liBd2").classList.remove("show");
  const p = l.status === "open" ? "cur" : "prev", C = 2 * Math.PI * 26, mine = l.company === LI.company;
  const ringC = l.h === "h-ok" ? "#3fc08c" : l.h === "h-mid" ? "#e0a44a" : l.h === "h-low" ? "#ef6a5c" : "#5c7482";
  const ring = l.score == null ? "" : `<div style="text-align:right;font-size:11px;color:#a0d8ea;line-height:1.3">Health<br>score</div><div class="li-ring"><svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="26" fill="none" stroke="#2b5a6c" stroke-width="6"/><circle cx="32" cy="32" r="26" fill="none" stroke="${ringC}" stroke-width="6" stroke-linecap="round" stroke-dasharray="${C * l.score / 100} ${C}"/></svg><b>${l.score}</b></div>`;
  const sub = [l.brand, l.area, l.bc && `BC location ${l.bc}`, `Simphony ${l.ref}`, l.company ? `company ${l.company}` : "not in the registry", l.currency !== LI.ccy && l.currency].filter(Boolean).map(esc).join(" · ");
  const ops = l.sim ? `<div class="li-panel li-span2"><div class="ttl"><h3>Operations</h3><span class="li-tag">Simphony · ${p === "cur" ? `${LI.curLab}, against ${LI.lyLab} (LY) and ${LI.prevLab}` : `${LI.prevLab}; closed this period`} · click a tile</span></div>
      <div class="li-tiles" style="grid-template-columns:repeat(4,minmax(0,1fr))">${liSimTiles(l, p)}</div></div>` : "";
  let fin = "", pur = "", inv = "";
  if (mine) {
    const bp = l.bc_posted;
    fin = `<div class="li-panel li-span2"><div class="ttl"><h3>Finance</h3><span class="li-tag">BC · sales posted to ${l.customer ? "POS customer " + esc(l.customer) : "no POS customer set"} · VAT incl.</span></div>
      <div class="li-tiles" style="grid-template-columns:repeat(4,minmax(0,1fr))">
        ${liTile("Posted in BC", bp ? liM(bp.total) : "–", bp ? `${bp.days_posted} of ${LI.days} days` : "set the POS customer", bp ? "" : "pending")}
        ${liTile("Simphony, same days", liM(l.snet), bp && l.snet ? `posted ${liPct((bp.total / l.snet - 1) * 100)} against it` : "net sales incl. VAT")}
        ${liTile("Sales ex VAT", liM(l.exvat), "Simphony net − tax")}
        ${liTile("Food cost (estimated)", l.fc != null ? `<span class="${l.fc > LI.th.food_cost + 2 ? "warn" : ""}">${l.fc.toFixed(1)}%</span>` : "–", l.use != null ? `use at cost ${liM(l.use)} · target ${LI.th.food_cost}%` : "item ledger not read", l.fc == null ? "pending" : "")}</div>
      ${l.daily ? `<div style="display:flex;gap:14px;font-size:11.5px;color:var(--sub);margin-bottom:2px;flex-wrap:wrap"><span><span class="li-lg li-c1"></span>Simphony, net sales a day</span>${bp ? `<span><span class="li-lg li-c2"></span>Posted in BC that day</span>` : ""}</div>
      ${liLine(l.daily.net, bp ? bp.daily : null, {label: "Daily sales", w: 760, h: 170, tip: i => `<b>${liDay(LI.dates[i])} ${liMon(LI.dates[i])}</b><br>Simphony ${liN(l.daily.net[i])}${bp ? `<br>BC ${liN(bp.daily[i])}` : ""}`})}` : ""}</div>`;
    if (l.purch) { const mx = Math.max(1, ...l.purch.suppliers.map(s => s[1]));
      pur = `<div class="li-panel"><div class="ttl"><h3>Purchasing</h3><span class="li-tag">BC · posted purchase invoices</span></div>
        <div class="li-tiles" style="grid-template-columns:repeat(2,minmax(0,1fr))">
          ${liTile("Bought direct", liM(l.purch.amount), `${liN(l.purch.n)} invoices to ${esc(l.bc)}${l.purch.fx ? ` · ${l.purch.fx} in another currency` : ""}`)}
          ${liTile("Direct buying / sales", l.exvat ? (l.purch.amount / l.snet * 100).toFixed(0) + "%" : "–", "VAT incl. on both; most goods come from the warehouse by transfer")}</div>
        <div class="meta" style="margin-bottom:6px">Largest suppliers</div>
        <div class="li-sup">${l.purch.suppliers.map(s => `<div>${esc(s[0])}<div class="b" style="width:${s[1] / mx * 100}%"></div></div><div class="num">${liM(s[1])}</div>`).join("") || `<div class="meta">None this period</div><div></div>`}</div></div>`; }
    const iv = l.inv, st = (liData.inventory || {});
    inv = `<div class="li-panel"><div class="ttl"><h3>Inventory</h3><span class="li-tag">BC · ${iv ? "stock as of " + esc(st.as_of || "") : esc(st.note || "not read")}</span></div>` + (iv ? `
        <div class="li-tiles">
          ${liTile("Stock value", liM(iv.stock), `food, beverage &amp; resale ${liM(iv.fb_stock)}`)}
          ${liTile("Days of cover", `<span class="${iv.doh != null && iv.doh > LI.th.cover_days ? "up" : ""}">${iv.doh ?? "–"}</span>`, `food, beverage &amp; resale · ${LI.th.cover_days} expected`)}
          ${liTile("Slow / non-moving", liM(iv.slow), "no use 60+ days")}
          ${liTile("Negative lines", liN(iv.neg_lines), "stock below zero")}
          ${liTile("Fresh lines to count", liN(iv.count), "cover beyond the count threshold")}
          ${liTile("Use in the history", liM(iv.fb_use), "food, beverage &amp; resale at cost")}</div>
        <div class="meta" style="margin-bottom:4px">Largest lines to count</div>
        <div class="li-sup">${iv.count_top.map(c => `<div>${esc(c[0])} <span class="meta">${c[3] != null ? liN(c[3]) + " days" : "no use"}</span></div><div class="num">${liM(c[2])}</div>`).join("") || `<div class="meta">None</div><div></div>`}</div>` : `<p class="meta">${l.bc ? "Opens Movement &amp; dead stock in the background; this panel fills in when it is ready." : "No BC location in the registry for this restaurant."}</p>`) + `</div>`;
  } else {
    fin = `<div class="li-panel li-span2"><div class="ttl"><h3>Finance · Purchasing · Inventory</h3><span class="li-tag">Business Central</span></div>
      <p class="meta">${l.company ? `This restaurant is in ${esc(l.company)}. Choose ${esc(l.company)} as the company above to read its Business Central panels.` : "Not in the registry yet: add a line for it in Finance → Settings → Location intelligence with its BC company, location and POS customer."}</p></div>`;
  }
  const al = l.alerts.length ? l.alerts : [["info", "Closed this period", "No Simphony sales in the period."]];
  $("liPop").innerHTML = `<div class="ph"><div class="ic">${liGlyph(l.brand)}</div>
      <div><h2>${esc(l.name)}</h2><div class="sub">${sub}</div></div><div class="sp"></div>${ring}
      <button class="close" data-liclose="liBd">Close ✕</button></div>
    <div class="body">${ops}
      <div class="li-panel" style="grid-row:span 3"><div class="ttl"><h3>Alerts</h3><span class="li-tag">${al.filter(a => a[0] !== "good").length}</span></div>
        <div class="li-alerts">${al.map(a => `<div class="li-al ${a[0]}"><b>${a[1]}</b>${a[2]}</div>`).join("")}</div>
        ${l.parts.length ? `<div class="ttl" style="margin-top:16px"><h3>Health score</h3><span class="li-tag">${l.score}/100${l.parts.length < 4 ? ` · ${l.parts.length} of 4 parts` : ""}</span></div>
        <div class="li-bk">${l.parts.map(p => `<span>${p[0]}</span><div class="tr"><i style="width:${p[1] / 25 * 100}%;background:${p[1] >= 18 ? "var(--down)" : p[1] >= 10 ? "var(--warn)" : "var(--up)"}"></i></div><span class="meta">${Math.round(p[1])}/25</span><span></span><span class="meta" style="grid-column:span 2;margin-top:-4px">${esc(p[2])}</span>`).join("")}</div>` : ""}
        <div class="li-links"><a href="${opts.links.sales}">Finance · Sales · Simphony ›</a>${mine && l.bc ? `<a href="${opts.links.typology}">Finance · Typology &amp; turnover ›</a><a href="${opts.links.movement}">Finance · Movement &amp; dead stock ›</a><a href="${opts.links.fsales}">Finance · Fiscal sales vs BC ›</a>` : ""}</div></div>
      ${fin}${pur}${inv}
    </div>`;
  $("liBd").classList.add("show");
}

// ---- KPI popup: one KPI across every restaurant
function liKpiAlerts(k, rows){
  const al = [], nm = r => esc(r.l.name), th = LI.th;
  if (k.k === "net" || k.k === "checks") {
    rows.filter(r => r.dL != null && r.dL <= -th.sales_drop).forEach(r => al.push(["warn", `${nm(r)} ${liPct(r.dL)} vs ${LI.lyLab}`, `${liFv(k, r.a, r.l)} against ${liFv(k, r.c, r.l)} on the same dates last year.`]));
    rows.filter(r => r.dL != null && r.dL >= 5).forEach(r => al.push(["good", `${nm(r)} ${liPct(r.dL)} vs ${LI.lyLab}`, "Ahead of last year on the same dates."]));
    if (k.k === "net") rows.forEach(r => r.l.alerts.filter(a => /posted|POS customer/.test(a[1])).forEach(a => al.unshift([a[0], `${nm(r)}: ${a[1]}`, a[2]])));
  }
  if (k.k === "avg") rows.filter(r => r.dL != null && Math.abs(r.dL) >= 8).forEach(r => al.push([r.dL < 0 ? "warn" : "info", `${nm(r)} ${liPct(r.dL)} vs ${LI.lyLab}`, `${liN(r.a)} a check against ${liN(r.c)} last year.`]));
  if (k.k === "guests" || k.k === "spg") { const keyed = rows.filter(r => { const t = liT(r.l, "cur"); return t && t.guests >= t.checks && t.guests; }).map(nm);
    al.push(["info", "Covers keyed only at table service", `${keyed.length ? keyed.join(", ") + " key" : "No restaurant keys"} covers on every check. Last year covers were hardly keyed, so there is no year-on-year comparison.`]); }
  if (k.lim) {
    const lim = th[k.lim];
    rows.filter(r => r.a != null && r.a >= lim).forEach(r => al.push([k.k === "err" && r.a >= 2 * lim ? "crit" : "warn", `${nm(r)} ${liP1(r.a)}${k.unit ? " " + k.unit.replace("% ", "") : ""}`, `${k.amt(liT(r.l, "cur"))} this period${r.b != null ? `; ${liP1(r.b)} in ${LI.prevLab}` : ""}. Limit ${lim}% (Settings).`]));
    if (k.k === "disc") { const jump = rows.filter(r => r.dL != null && r.dL >= 3);
      if (jump.length) al.push(["warn", `Discounts up since last year at ${jump.length} restaurant${jump.length > 1 ? "s" : ""}`, jump.map(r => `${nm(r)} ${liP1(r.c)} → ${liP1(r.a)}`).join(" · ") + "."]); }
  }
  if (!al.length) al.push(["good", "No exception", "Every restaurant is inside the limits."]);
  return al;
}
function liKpi(kk, ref){
  const k = LI_KB[kk]; if (!k || !LI.G0) return;
  const rows = liRows(k, LI.tot), others = liRows(k, [...LI.scope.filter(l => !LI.tot.includes(l)), ...LI.others.filter(l => l.sim && l.sim.cur.net > 0)]);
  const vals = rows.flatMap(r => [r.a, r.b, r.c]).filter(v => v != null && isFinite(v)), max = Math.max(...vals, 1e-9) * 1.04;
  const a = k.v(LI.G0), b = LI.G1 && k.v(LI.G1), c = k.noLy || !LI.G2 ? null : k.v(LI.G2), dS = liDelta(k, a, b), dL = liDelta(k, a, c);
  const tip = r => esc(`<b>${esc(r.l.name)}</b><br>${LI.curLab}: ${liFv(k, r.a, r.l)}<br>${LI.prevLab}: ${liFv(k, r.b, r.l)} (${liFd(k, r.dS)})<br>${LI.lyLab}: ${k.noLy ? "n/a" : liFv(k, r.c, r.l)}${r.dL != null ? ` (${liFd(k, r.dL)})` : ""}${k.amt ? `<br>amount ${k.amt(liT(r.l, "cur"))}` : ""}`);
  const bar = v => Math.max(0, v / max * 100);
  const row = r => `<div class="li-kr r ${r.l.ref === ref ? "sel" : ""}" data-loc="${esc(r.l.ref)}" data-tip="${tip(r)}">
      <span class="nm"><span class="li-mini">${liGlyph(r.l.brand)}</span>${esc(r.l.name)}</span>
      <div class="li-trk">${r.a != null ? `<i class="bar" style="width:${bar(r.a)}%"></i>` : ""}${r.b != null ? `<i class="tk li-c2" style="left:${bar(r.b)}%"></i>` : ""}${r.c != null ? `<i class="tk li-c3" style="left:${bar(r.c)}%"></i>` : ""}</div>
      <span class="num">${r.a == null ? `<span class="meta">${k.noLy ? "not keyed" : "–"}</span>` : liFv(k, r.a, r.l)}</span>
      <span class="num ${liDc(k, r.dS)}">${liFd(k, r.dS)}</span><span class="num lyc ${liDc(k, r.dL)}">${k.noLy ? "n/a" : liFd(k, r.dL)}</span></div>`;
  const focus = ref && LI.tot.find(l => l.ref === ref);
  const series = k.daily ? liDaily(k, focus ? [focus] : LI.tot) : null;
  const daily = series ? `<div class="li-panel"><div class="ttl"><h3>Day by day</h3><span class="li-tag">${focus ? esc(focus.name) : esc(LI.company) + ", all restaurants above"}</span></div>
      ${liLine(series, null, {label: k.lab + " a day", fy: k.k === "net" ? undefined : v => liN(v), tip: i => `<b>${new Date(LI.dates[i] + "T00:00:00").toLocaleDateString("en-GB", {weekday: "short", day: "numeric", month: "short"})}</b><br>${k.lab}: ${series[i] == null ? "–" : k.f(series[i])}`})}</div>` : "";
  const al = liKpiAlerts(k, rows);
  $("liPop2").innerHTML = `<div class="ph"><div class="ic">${LI_G.KPI}</div>
      <div><h2>${esc(k.lab)}${k.unit ? ` <span style="font-size:14px;color:#a0d8ea;font-weight:500">${k.unit}</span>` : ""}</h2><div class="sub">Simphony · ${esc(LI.company)}, ${LI.tot.length} restaurants · ${LI.curLab}${focus ? ` · ${esc(focus.name)} highlighted` : ""}</div></div><div class="sp"></div>
      <button class="close" data-liclose="liBd2">${$("liBd").classList.contains("show") ? "‹ Back to restaurant" : "Close ✕"}</button></div>
    <div class="li-hdr3">${liTile(LI.curLab, a == null ? "–" : k.f(a), k.a(LI.G0))}${liTile(`${LI.prevLab}, same days`, b == null ? "–" : k.f(b), `now <b class="${liDc(k, dS)}">${liFd(k, dS)}</b>`)}${liTile(`${LI.lyLab}, same dates`, k.noLy ? "n/a" : c == null ? "–" : k.f(c), k.noLy ? "covers were hardly keyed last year" : `now <b class="${liDc(k, dL)}">${liFd(k, dL)}</b>`)}</div>
    <div class="li-body2"><div class="li-col"><div class="li-panel"><div class="ttl"><h3>By restaurant</h3><span class="meta" style="display:flex;gap:12px;flex-wrap:wrap"><span><span class="li-lg li-c1"></span>${LI.curLab}</span><span><span class="li-lg li-c2"></span>${LI.prevLab}</span>${k.noLy ? "" : `<span><span class="li-lg li-c3"></span>${LI.lyLab}</span>`}</span></div>
      <div class="li-kr h"><span>Restaurant</span><span class="li-trk"></span><span class="num">Now</span><span class="num">vs ${LI.prevLab}</span><span class="num lyc">vs LY</span></div>
      ${rows.map(row).join("")}
      ${others.length ? `<div class="meta" style="margin:12px 0 2px">Not in the total above (other company or currency)</div>${others.map(r => `<div class="li-kr mut" data-loc="${esc(r.l.ref)}"><span class="nm"><span class="li-mini">${liGlyph(r.l.brand)}</span>${esc(r.l.name)}</span><span class="li-trk"></span><span class="num">${liFv(k, r.a, r.l)}</span><span class="num">${liFd(k, r.dS)}</span><span class="num lyc">${k.noLy ? "n/a" : liFd(k, r.dL)}</span></div>`).join("")}` : ""}
      <p class="meta" style="margin-top:10px">Click a restaurant to open its full picture.</p></div></div>
      <div class="li-col">${daily}
        <div class="li-panel"><div class="ttl"><h3>Alerts</h3><span class="li-tag">${al.filter(x => x[0] !== "good").length}</span></div><div class="li-alerts">${al.map(x => `<div class="li-al ${x[0]}"><b>${x[1]}</b>${x[2]}</div>`).join("")}</div></div>
        <div class="li-panel"><div class="ttl"><h3>How it is read</h3><span class="li-tag">Simphony BI · daily totals</span></div><p style="margin:0 0 8px;font-size:12.5px;line-height:1.5">${k.def}</p>
          <p class="meta" style="margin:0">The company figure adds the open restaurants of ${esc(LI.company)} that report in ${esc(LI.ccy)}. Closed restaurants, other companies and other currencies are kept out, so the comparison is like for like.</p></div></div></div>`;
  $("liBd2").classList.add("show");
}

// ---- events
document.addEventListener("click", e => {
  if (!e.target.closest(".li-wrap, #liBd, #liBd2")) return;
  $("liTip").style.opacity = 0;
  const c = e.target.closest("[data-liclose]"); if (c) { $(c.dataset.liclose).classList.remove("show"); return; }
  const k = e.target.closest("[data-kpi]"); if (k) { liKpi(k.dataset.kpi, k.dataset.kloc); return; }
  const t = e.target.closest("[data-loc]"); if (t) { liOpen(t.dataset.loc); return; }
  if (e.target === $("liBd2")) $("liBd2").classList.remove("show"); else if (e.target === $("liBd")) $("liBd").classList.remove("show");
});
document.addEventListener("keydown", e => { if (e.key !== "Escape") return;
  if ($("liBd2").classList.contains("show")) $("liBd2").classList.remove("show"); else $("liBd").classList.remove("show"); });
document.addEventListener("mousemove", e => {
  const tip = $("liTip"), t = e.target.closest(".li-wrap [data-tip], #liBd [data-tip], #liBd2 [data-tip]");
  document.querySelectorAll(".li-cx").forEach(cx => cx.setAttribute("opacity", 0));
  if (t && t.dataset.i != null) { const cx = t.ownerSVGElement.querySelector(".li-cx"), m = +t.getAttribute("x") + +t.getAttribute("width") / 2; cx.setAttribute("x1", m); cx.setAttribute("x2", m); cx.setAttribute("opacity", .35); }
  if (!t) { tip.style.opacity = 0; return; }
  tip.innerHTML = t.dataset.tip; tip.style.opacity = 1;
  tip.style.left = Math.max(8, Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8)) + "px"; tip.style.top = (e.clientY + 14) + "px";
});


window.LocIntel = {
  mount(el, o){
    clearTimeout(liTimer);
    host = el; opts = Object.assign({api: "/finance/api/locations", links: {sales: "/finance/#sales", typology: "/finance/#typology",
      movement: "/finance/#movement", fsales: "/finance/#fsales"}}, o || {});
    el.innerHTML = `<div class="li-wrap">${MARKUP}</div>`;
    bind();
    if (liData && liKey === key()) liRender(); else { liData = null; loadLocations(false); }
  },
  refresh(){ if (host && host.isConnected) loadLocations(true); }
};
})();
