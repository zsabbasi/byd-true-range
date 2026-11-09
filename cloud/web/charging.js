"use strict";
// Charging tab: sessions, energy into the battery, estimated power, optional cost.
// All data is written with textContent / DOM APIs only (never as parsed HTML).
(function () {
  const $ = (id) => document.getElementById(id);
  const dash = "—";

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function fmt(v, d) { return v === null || v === undefined ? dash : Number(v).toFixed(d === undefined ? 0 : d); }
  function hhmm(iso) { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  function dayLabel(iso) { return new Date(iso).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }); }
  function dur(min) {
    if (min === null || min === undefined) return dash;
    if (min < 60) return Math.round(min) + " min";
    return Math.floor(min / 60) + " h " + String(Math.round(min % 60)).padStart(2, "0") + " min";
  }
  async function getJSON(url, ms = 10000) {         // never wait more than 10 s: a hung request must not stop a refresh loop
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
  // The +/- on a steady charge can be tiny, but BYD's power reading is not yet verified to that precision: never show
  // better than +/-0.1.
  function pm(v) { return fmt(Math.max(v === null || v === undefined ? 0 : v, 0.1), 1); }
  function money(v, cur) { return v === null || v === undefined ? null : fmt(v, 2) + (cur ? " " + cur : ""); }

  const ABOUT_ENERGY = "Energy into the battery is integrated from BYD's live battery power while it reports CHARGING (the power is steady, so this is accurate). With no power readings it falls back to battery % gained x your usable capacity (about \u00b10.2 kWh). Either way it excludes charger and cable losses, which we cannot see. Only a stopped car counts: battery gain while driving is regen, never charging.";
  const ABOUT_POWER = "Average power is the mean of BYD's live power readings while charging (without them it is derived from when the battery percent ticked up). AC or DC is only a guess from that power. The \u00b1 covers sampling only; on sessions of 5% or more the page compares the power-based energy with the battery % to test that BYD's power reading is accurate.";
  const NONE_YET = "A charge is recorded when BYD reports CHARGING while the car is stopped, or when the battery percent rises while the car is stopped. Battery gain while driving is regen, not charging.";

  // ---- list ---------------------------------------------------------------------------------------------------
  async function loadCharging() {
    const box = $("view-charging");
    if (!box.dataset.loaded) { clear(box); box.appendChild(el("div", "muted", "Loading…")); }
    let data;
    try { data = await getJSON("/api/charging"); } catch (e) {
      if (!box.dataset.loaded) { clear(box); box.appendChild(el("div", "stale", "Could not load charging sessions.")); }
      window.UI.refreshLater("charging", 15000, "#/charging", loadCharging);
      return;
    }
    clear(box);
    box.dataset.loaded = "1";
    // a charge in progress refreshes itself every 20 s; otherwise check once a minute in case one starts
    window.UI.refreshLater("charging", data.totals.ongoing ? 20000 : 60000, "#/charging", loadCharging);
    const tot = data.totals;
    const head = el("section", "hero tripshead");
    head.appendChild(el("div", "label", "Energy into the battery"));
    const line = el("div", "tripstotal");
    line.appendChild(el("span", "bigish", fmt(tot.energy_kwh, 0)));
    line.appendChild(el("span", "unit", " kWh in " + tot.sessions + (tot.sessions === 1 ? " session" : " sessions")));
    head.appendChild(line);
    const sub = el("div", "sub", tot.cost !== null && tot.cost !== undefined
      ? "Cost " + money(tot.cost, data.currency) + (tot.wall_kwh !== null ? " for " + fmt(tot.wall_kwh, 0) + " kWh from the wall" : "")
      : "");
    const tipText = ABOUT_ENERGY + (data.has_tariff ? " Cost uses your tariff and your charger loss (" + fmt(data.loss_pct, 0) + "%)." : " To see a cost, set tariff_per_kwh in settings.json. Nothing is assumed about prices.");
    const hi = window.UI.info(tipText, "About these figures");
    sub.appendChild(document.createTextNode(" "));
    sub.appendChild(hi.btn);
    head.appendChild(sub);
    head.appendChild(hi.tip);
    box.appendChild(head);

    if (!data.sessions.length) {
      const empty = el("div", "muted pad", "No charging recorded yet. ");
      const ei = window.UI.info(NONE_YET, "Why is this empty?");
      empty.appendChild(ei.btn);
      box.appendChild(empty);
      box.appendChild(ei.tip);
      return;
    }

    let lastDay = "";
    for (const s of data.sessions) {
      const day = dayLabel(s.start);
      if (day !== lastDay) { box.appendChild(el("h2", null, day)); lastDay = day; }
      const card = s.observed ? el("a", "card") : el("div", "card gap");
      if (s.observed) card.setAttribute("href", "#/charge/" + s.id);
      const top = el("div", "cardtop");
      top.appendChild(el("span", "when", s.observed ? hhmm(s.start) + " – " + hhmm(s.end) : "Car silent, " + hhmm(s.start) + " – " + hhmm(s.end)));
      const badges = el("span");
      if (s.ongoing) badges.appendChild(el("span", "badge live", "charging"));
      else if (!s.observed) badges.appendChild(el("span", "badge", "not watched"));
      else if (s.type_guess) badges.appendChild(el("span", "badge", s.type_guess + " (guess)"));
      top.appendChild(badges);
      card.appendChild(top);

      const mid = el("div", "cardmid");
      const gain = el("div", "cell");
      gain.appendChild(el("span", "v", "+" + fmt(s.gained_pct, 0)));
      gain.appendChild(el("span", "u", "%  " + fmt(s.soc_start, 0) + "→" + fmt(s.soc_end, 0)));
      mid.appendChild(gain);
      const en = el("div", "cell");
      en.appendChild(el("span", "v", fmt(s.energy_kwh, 1)));
      en.appendChild(el("span", "u", " kWh ±" + pm(s.energy_pm_kwh)));
      mid.appendChild(en);
      card.appendChild(mid);

      const bits = [];
      if (s.observed) bits.push(dur(s.minutes));
      if (s.avg_kw !== null) bits.push("avg " + fmt(s.avg_kw, 1) + " ±" + pm(s.avg_kw_pm) + " kW");
      if (s.cost !== null) bits.push(money(s.cost, data.currency));
      if (bits.length) card.appendChild(el("div", "note", bits.join(" · ")));
      if (!s.observed) {
        const gi = window.UI.info("The car did not report during this charge, so only the battery gain and energy are known: no duration, power or curve.", "About unwatched charges");
        top.appendChild(gi.btn);
        card.appendChild(gi.tip);
      }
      box.appendChild(card);
    }
  }

  // ---- detail -------------------------------------------------------------------------------------------------
  function stat(label, value, unit, sub) {
    const d = el("div", "tile");
    d.appendChild(el("div", "k", label));
    const v = el("div", "v");
    v.appendChild(el("span", null, value));
    if (unit) v.appendChild(el("span", "u", " " + unit));
    d.appendChild(v);
    if (sub) d.appendChild(el("div", "k", sub));
    return d;
  }

  function drawCurve(svg, curve, ticks) {
    clear(svg);
    if (!curve || curve.length < 2) return;
    const tMax = Math.max(1, curve[curve.length - 1].s);
    const lo = Math.max(0, Math.min(...curve.map((p) => p.soc)) - 3), hi = Math.min(100, Math.max(...curve.map((p) => p.soc)) + 3);
    const x = (s) => (s / tMax) * 296 + 2, y = (v) => 76 - ((v - lo) / Math.max(1, hi - lo)) * 70;
    const ns = "http://www.w3.org/2000/svg";
    const pl = document.createElementNS(ns, "polyline");
    pl.setAttribute("points", curve.map((p) => x(p.s).toFixed(1) + "," + y(p.soc).toFixed(1)).join(" "));
    pl.setAttribute("fill", "none"); pl.setAttribute("stroke", "currentColor"); pl.setAttribute("stroke-width", "2");
    pl.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(pl);
    for (const t of ticks || []) {
      const c = document.createElementNS(ns, "line");
      c.setAttribute("x1", x(t.s).toFixed(1)); c.setAttribute("x2", (x(t.s) + 0.01).toFixed(1));
      c.setAttribute("y1", y(t.soc).toFixed(1)); c.setAttribute("y2", y(t.soc).toFixed(1));
      c.setAttribute("stroke", "currentColor"); c.setAttribute("stroke-width", "5"); c.setAttribute("stroke-linecap", "round");
      c.setAttribute("vector-effect", "non-scaling-stroke"); c.setAttribute("opacity", "0.55");
      svg.appendChild(c);
    }
  }

  async function loadCharge(id) {
    const box = $("view-charge");
    if (box.dataset.loaded !== String(id)) { clear(box); box.appendChild(el("div", "muted", "Loading…")); }
    let s;
    try { s = await getJSON("/api/charging/" + encodeURIComponent(id)); } catch (e) {
      if (box.dataset.loaded === String(id)) { window.UI.refreshLater("charge", 15000, "#/charge/", () => loadCharge(id)); return; }
      clear(box);
      const b = el("a", "back", "← All charging"); b.setAttribute("href", "#/charging");
      box.appendChild(b);
      box.appendChild(el("div", "stale", "This session was not found."));
      return;
    }
    clear(box);
    box.dataset.loaded = String(id);
    if (s.ongoing) window.UI.refreshLater("charge", 20000, "#/charge/", () => loadCharge(id));
    const back = el("a", "back", "← All charging");
    back.setAttribute("href", "#/charging");
    box.appendChild(back);
    box.appendChild(el("h1", "tripname", dayLabel(s.start) + ", " + hhmm(s.start) + " – " + hhmm(s.end)));
    if (s.ongoing) box.appendChild(el("div", "badge live", "charging"));

    const hero = el("section", "hero");
    hero.appendChild(el("div", "label", "Energy into the battery"));
    const big = el("div", "big small");
    big.appendChild(el("span", null, fmt(s.energy_kwh, 1)));
    big.appendChild(el("span", "unit", " kWh ±" + pm(s.energy_pm_kwh)));
    hero.appendChild(big);
    const sub = el("div", "sub", (s.ongoing ? "so far: " : "") + "+" + fmt(s.gained_pct, 0) + "%  (" + fmt(s.soc_start, 0) + "→" + fmt(s.soc_end, 0) + "%) · by " + s.energy_method);
    const hi = window.UI.info(ABOUT_ENERGY, "About this figure");
    sub.appendChild(document.createTextNode(" "));
    sub.appendChild(hi.btn);
    hero.appendChild(sub);
    hero.appendChild(hi.tip);
    box.appendChild(hero);

    const cap = el("h2", null, "Battery during the session ");
    const ci = window.UI.info("The line joins the readings. Dots mark the moments the battery percent ticked up, which is where the true charge is exactly known.", "About this chart");
    cap.appendChild(ci.btn);
    box.appendChild(cap);
    box.appendChild(ci.tip);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 300 80"); svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Battery percentage during the charge");
    svg.style.color = getComputedStyle(document.documentElement).getPropertyValue("--band");
    box.appendChild(svg);
    drawCurve(svg, s.curve, s.ticks);

    const grid = el("section", "tiles");
    grid.appendChild(stat("Duration", dur(s.minutes), "", "about"));
    const kw = s.avg_kw === null ? dash : fmt(s.avg_kw, 1);
    const pwr = stat("Average power", kw, s.avg_kw === null ? "" : "kW", s.avg_kw === null ? "needs 2+ readings" : "±" + pm(s.avg_kw_pm) + " · " + s.type_guess + " (guess)");
    grid.appendChild(pwr);
    grid.appendChild(stat("Battery gained", "+" + fmt(s.gained_pct, 0), "%", s.energy_method === "BYD power" ? "= " + fmt(s.energy_soc_kwh, 1) + " kWh by battery %" : ""));
    if (s.check) grid.appendChild(stat("Check vs battery %", s.check === "consistent" ? "Consistent" : "Differs", "", fmt(s.energy_kwh, 1) + " vs " + fmt(s.energy_soc_kwh, 1) + " kWh"));
    if (s.cost !== null) grid.appendChild(stat("Cost", money(s.cost, s.currency) || dash, "", fmt(s.wall_kwh, 1) + " kWh from the wall"));
    box.appendChild(grid);
    const pi = window.UI.info(ABOUT_POWER + "", "About the power figures");
    const prow = el("div", "muted small", "About the power figures ");
    prow.appendChild(pi.btn);
    box.appendChild(prow);
    box.appendChild(pi.tip);
  }

  window.loadCharging = loadCharging;
  window.loadCharge = loadCharge;
})();
