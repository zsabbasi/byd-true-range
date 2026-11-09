"use strict";
// All data is written with textContent / DOM APIs only (never as parsed HTML).
const $ = (id) => document.getElementById(id);
const dash = "—";
const FAST_MS = 5000, SLOW_MS = 20000;

function num(v, d = 0) { return v === null || v === undefined ? dash : Number(v).toFixed(d); }
function ago(s) {
  if (s === null || s === undefined) return "";
  if (s < 90) return "updated " + Math.round(s) + " s ago";
  if (s < 5400) return "updated " + Math.round(s / 60) + " min ago";
  return "updated " + (s / 3600).toFixed(1) + " h ago";
}
function level(cls, el) { el.classList.remove("ok", "warn", "bad"); if (cls) el.classList.add(cls); }

function signInBanner(s) {
  const banner = $("stale");
  const rejected = s.poller_state === "auth-failed";
  if (s.signed_in === false || rejected) {
    banner.hidden = false;
    banner.textContent = "";
    const a = document.createElement("a");
    a.href = "#/account";
    a.textContent = "Open Account";
    banner.appendChild(document.createTextNode((rejected ? "BYD rejected the stored sign-in. " : "Not signed in to BYD yet. ")));
    banner.appendChild(a);
    banner.appendChild(document.createTextNode(rejected ? " to sign in again." : " to sign in."));
    return true;
  }
  return false;
}

function batteryBanner(s) {
  const b = $("batnote");
  if (!b) return;
  b.textContent = "";
  if (s.battery_set === false) {
    b.hidden = false;
    b.appendChild(document.createTextNode("Battery size not set: assuming " + s.battery_kwh + " kWh. "));
    const a = document.createElement("a");
    a.href = "#/account";
    a.textContent = "Set it in Account";
    b.appendChild(a);
  } else { b.hidden = true; }
}

function render(s) {
  // A shared server: until someone has signed in, the only thing to show is the sign-in page (no tabs, no empty dashboard).
  document.body.classList.toggle("anon", !!(s.multi_user && s.signed_in === false));
  if (s.multi_user && s.signed_in === false && (location.hash === "" || location.hash === "#/")) location.hash = "#/account";
  batteryBanner(s);
  if (!s.has_data) {
    $("state").textContent = s.signed_in === false ? "Not signed in" : "No data yet";
    $("age").textContent = "";
    level("warn", $("dot"));
    $("range").textContent = dash;
    $("band").textContent = s.signed_in === false ? "" : "Waiting for the first reading from the car.";
    if (!signInBanner(s)) $("stale").hidden = true;
    return;
  }
  const r = s.reading, g = s.range;

  // Freshness is judged by the CAR own timestamp: BYD can hand back a cached snapshot, so "we polled a
  // second ago" proves nothing. Say it in words as well as colour.
  const age = r.data_age_s !== null && r.data_age_s !== undefined ? r.data_age_s : r.age_s;
  const stale = age !== null && age > 180;
  const hero = document.querySelector(".hero");
  const banner = $("stale");
  if (stale) {
    $("state").textContent = "No fresh data from the car";
    banner.hidden = false;
    banner.textContent = "The car is not answering (asleep, off or no signal). Showing its last report, " +
      ago(age).replace("updated ", "").replace(" ago", "") + " old.";
    hero.classList.add("old");
  } else {
    $("state").textContent = (r.is_driving ? "Driving" : "Parked") + (r.charging_state === "CHARGING" ? " \u00b7 charging" : "");
    banner.hidden = true;
    hero.classList.remove("old");
  }
  $("age").textContent = "car data " + ago(age).replace("updated ", "");
  level(stale ? "bad" : "ok", $("dot"));

  // hero
  const bar = $("barwrap");
  if (g.km === null) {
    $("range").textContent = dash;
    $("band").textContent = "Not enough data yet";
    $("basis").textContent = "A true range appears after about 25 km of driving.";
    $("trust").textContent = "Trust " + dash;
    level(null, $("trust"));
    bar.style.visibility = "hidden";
  } else {
    $("range").textContent = num(g.km);
    $("band").textContent = num(g.low_km) + "\u2013" + num(g.high_km) + " km";
    $("trust").textContent = "Trust " + g.trust_pct + "%";
    level(g.trust_pct >= 65 ? "ok" : g.trust_pct >= 40 ? "warn" : "bad", $("trust"));
    let basis;
    if (g.policy === "immature") {
      basis = "Using BYD's measured consumption on your " + num(g.assumed_usable_kwh, 1) + " kWh battery. The app switches to your own driving once it has recorded "
        + num(g.own_needed_km) + " km of it (" + num(g.own_observed_km, 0) + " km so far).";
    } else if (g.policy === "missing-own") {
      basis = "Your own driving data is missing right now, so this uses BYD's measured consumption on your "
        + num(g.assumed_usable_kwh, 1) + " kWh battery.";
    } else if (g.policy === "new-car") {
      basis = "New car: BYD's own estimate, until there is real driving data. Expect it to read high.";
    } else if (g.basis === "combined") {
      basis = "Combined: your driving " + num(g.own_km) + " km (weight " + num(g.own_weight_pct) + "%) and BYD's measured consumption on your "
        + num(g.assumed_usable_kwh, 1) + " kWh battery " + num(g.byd_based_km) + " km.";
      if (Math.abs(g.disagree_pct) >= 12) basis += " These disagree by " + num(Math.abs(g.disagree_pct)) + "%, so the band is wider.";
      if (g.vs_byd_pct !== null) basis += " " + (g.vs_byd_pct >= 0 ? "+" : "") + g.vs_byd_pct + "% vs BYD's own range.";
    } else if (g.status === "provisional") {
      basis = "Provisional: from BYD's last-50-km consumption and your battery size (" + num(g.assumed_usable_kwh, 1) + " kWh). It tightens as you drive.";
    } else {
      basis = "From your last " + num(g.window_km) + " km (" + num(g.soc_drop_pct) + "% battery used)";
      if (g.vs_byd_pct !== null) basis += ", " + (g.vs_byd_pct >= 0 ? "+" : "") + g.vs_byd_pct + "% vs BYD";
      basis += ".";
    }
    if (g.upside_capped) basis += " The top of the band is limited by the best consumption BYD has recorded on your car (" + num(g.best_kwh_per_100, 1) + " kWh/100 km).";
    $("basis").textContent = basis;
    drawBar(g, r.byd_range_km);
    bar.style.visibility = "visible";
  }

  // tiles
  $("soc").textContent = num(r.soc, 0);
  $("byd").textContent = num(r.byd_range_km);
  $("speed").textContent = num(r.speed_kmh);
  $("cons").textContent = num(g.consumption_kwh_per_100km, 1);
  $("cons2").textContent = r.byd_consumption ? "BYD: " + num(r.byd_consumption, 1) + " (last 50 km)" : "";
}

function drawBar(g, byd) {
  const hi = Math.max(g.high_km, byd || 0) * 1.1 || 1;
  const pct = (v) => Math.max(0, Math.min(100, (v / hi) * 100));
  const band = $("bandbar");
  band.style.left = pct(g.low_km) + "%";
  band.style.width = Math.max(1, pct(g.high_km) - pct(g.low_km)) + "%";
  $("marker").style.left = pct(g.km) + "%";
  const t = $("bydtick");
  if (byd) { t.style.left = pct(byd) + "%"; t.style.display = "block"; } else { t.style.display = "none"; }
  $("barwrap").setAttribute("aria-label", "Range band " + g.low_km + " to " + g.high_km + " km, estimate " + g.km + " km" + (byd ? ", BYD says " + byd + " km" : ""));
}

function renderSeries(rows) {
  const svg = $("spark");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  if (!rows.length) { $("sparknote").textContent = "No readings in the last 24 h."; return; }
  const t0 = Date.parse(rows[0].ts), t1 = Date.parse(rows[rows.length - 1].ts);
  const span = Math.max(1, t1 - t0);
  const pts = rows.map((p) => [((Date.parse(p.ts) - t0) / span) * 296 + 2, 76 - (p.soc / 100) * 72]);
  const ns = "http://www.w3.org/2000/svg";
  svg.style.color = getComputedStyle(document.documentElement).getPropertyValue("--band");
  if (rows.length === 1) {   // a single reading: show a dot (a round-capped zero-length line stays round when stretched)
    const dot = document.createElementNS(ns, "line");
    dot.setAttribute("x1", "150"); dot.setAttribute("x2", "150.01");
    dot.setAttribute("y1", pts[0][1].toFixed(1)); dot.setAttribute("y2", pts[0][1].toFixed(1));
    dot.setAttribute("stroke", "currentColor"); dot.setAttribute("stroke-width", "9");
    dot.setAttribute("stroke-linecap", "round"); dot.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(dot);
    $("sparknote").textContent = "1 reading so far, battery " + rows[0].soc + "%";
    return;
  }
  const line = document.createElementNS(ns, "polyline");
  line.setAttribute("points", pts.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" "));
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "currentColor");
  line.setAttribute("stroke-width", "2");
  line.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(line);
  $("sparknote").textContent = rows.length + " readings, battery " + rows[0].soc + "% to " + rows[rows.length - 1].soc + "%";
}

// A phone on a changing network can leave a request hanging forever, which silently stops a refresh loop. Never wait
// more than 10 s for anything.
async function getJSON(url, ms = 10000) {
  const ctl = new AbortController();
  const to = setTimeout(() => ctl.abort(), ms);
  try {
    const res = await fetch(url, { cache: "no-store", credentials: "same-origin", signal: ctl.signal });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return await res.json();
  } finally {
    clearTimeout(to);
  }
}

let timer = null;
function onNow() { const h = location.hash; return h === "" || h === "#" || h === "#/"; }   // other tabs must not wake the car

let lastOk = 0, lastAge = null;

// A short pulse on the status dot every time fresh data arrives, so you can SEE that it is refreshing.
function pulse() {
  const d = $("dot");
  d.classList.remove("pulse");
  void d.offsetWidth;                // restart the animation
  d.classList.add("pulse");
}

async function tick() {
  let next = SLOW_MS;
  if (!onNow()) { timer = setTimeout(tick, 3000); return; }
  if (document.hidden) {           // don't keep the car awake from a background tab / pocket
    timer = setTimeout(tick, 5000);
    return;
  }
  try {
    const s = await getJSON("/api/status");
    render(s);
    lastOk = Date.now();
    lastAge = s.has_data ? (s.reading.data_age_s !== null && s.reading.data_age_s !== undefined ? s.reading.data_age_s : s.reading.age_s) : null;
    pulse();
    if (s.has_data && s.reading.is_driving) next = FAST_MS;
  } catch (e) {
    next = 5000;                     // after a failure retry soon, not after a long wait
    $("state").textContent = "Connection lost, retrying\u2026";
    level("bad", $("dot"));
  } finally {
    timer = setTimeout(tick, next);  // ALWAYS schedule the next refresh, whatever happened above
  }
}

// "car data 41 s ago" keeps counting between refreshes instead of freezing at the last fetched value.
setInterval(() => {
  if (!onNow() || document.hidden || lastAge === null || !lastOk) return;
  $("age").textContent = "car data " + ago(lastAge + (Date.now() - lastOk) / 1000).replace("updated ", "");
}, 1000);

// Watchdog: visible, on this tab, and nothing has worked for a minute -> say so and try again right away.
setInterval(() => {
  if (!onNow() || document.hidden || !lastOk) return;
  if (Date.now() - lastOk > 60000) { $("state").textContent = "No update for over a minute"; level("bad", $("dot")); clearTimeout(timer); tick(); }
}, 15000);
async function seriesTick() {
  try { renderSeries(await getJSON("/api/series")); } catch (e) { /* keep last chart */ }
  setTimeout(seriesTick, 60000);
}
tick();
seriesTick();
const kick = () => { if (onNow() && !document.hidden) { clearTimeout(timer); tick(); } };
window.addEventListener("hashchange", () => { if (onNow()) { clearTimeout(timer); tick(); } });
document.addEventListener("visibilitychange", kick);   // screen on again: refresh at once
window.addEventListener("pageshow", kick);              // restored from the back/forward cache (iOS)
window.addEventListener("focus", kick);
window.addEventListener("online", kick);                // the network came back

// ---- keep the screen on (optional) ----------------------------------------------------------------------------------
// A page cannot run while the phone's screen is off, so it only catches up when you wake it. This asks the browser to keep
// the screen awake while the page is open. It needs HTTPS (the ngrok link) or localhost, and it uses battery, so it is a
// switch, remembered per phone.
(function () {
  const btn = $("wakebtn");
  if (!btn || !("wakeLock" in navigator) || !window.isSecureContext) return;
  let sentinel = null;
  const get = () => { try { return localStorage.getItem("wake") === "1"; } catch (e) { return false; } };
  const put = (v) => { try { localStorage.setItem("wake", v ? "1" : "0"); } catch (e) { /* private mode */ } };
  const label = () => { btn.textContent = sentinel ? "Screen stays on" : "Keep screen on"; btn.setAttribute("aria-pressed", String(!!sentinel)); };
  async function on() {
    try {
      sentinel = await navigator.wakeLock.request("screen");
      sentinel.addEventListener("release", () => { sentinel = null; label(); });
      put(true);
    } catch (e) { sentinel = null; }
    label();
  }
  async function off() {
    try { if (sentinel) await sentinel.release(); } catch (e) { /* already released */ }
    sentinel = null;
    put(false);
    label();
  }
  btn.hidden = false;
  btn.addEventListener("click", () => (sentinel ? off() : on()));
  label();
  if (get()) on();                                        // a browser may refuse without a tap; the button is still there
  document.addEventListener("visibilitychange", () => { if (!document.hidden && get() && !sentinel) on(); });
})();
