"use strict";
// Trips tab: history list, trip page with map and consumption. All data is written with textContent / DOM
// APIs only (never as parsed HTML). Map images come from OpenStreetMap only when the server says so.
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
  function dayLabel(iso) {
    return new Date(iso).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  }
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

  // ---- consumption wording: say plainly when a number is not trustworthy -------------------------------------
  function consText(t) {
    if (t.kwh_per_100 !== null && t.kwh_per_100 !== undefined) {
      const pm = t.kwh_per_100_pm !== null && t.kwh_per_100_pm !== undefined ? " ±" + fmt(t.kwh_per_100_pm, 1) : "";
      return { big: (t.quality === "unobserved" ? "~" : "") + fmt(t.kwh_per_100, 1) + pm, unit: "kWh/100 km", note: "" };
    }
    if (t.quality === "regen") return { big: dash, unit: "", note: "net gain from regen (downhill)" };
    if (t.quality === "charged") return { big: dash, unit: "", note: "charged while the car was silent" };
    return { big: dash, unit: "", note: "too short to measure" };
  }

  // ---- list ---------------------------------------------------------------------------------------------------
  async function loadList() {
    const box = $("view-trips");
    if (!box.dataset.loaded) { clear(box); box.appendChild(el("div", "muted", "Loading trips…")); }
    let data;
    try { data = await getJSON("/api/trips"); } catch (e) {
      if (!box.dataset.loaded) { clear(box); box.appendChild(el("div", "stale", "Could not load trips.")); }
      window.UI.refreshLater("trips", 15000, "#/trips", loadList);
      return;
    }
    clear(box);
    box.dataset.loaded = "1";
    // a trip in progress refreshes itself; otherwise just keep the list reasonably current
    window.UI.refreshLater("trips", data.trips.some((t) => t.ongoing) ? 30000 : 120000, "#/trips", loadList);
    const tot = data.totals;
    const head = el("section", "hero tripshead");
    head.appendChild(el("div", "label", "All recorded driving"));
    const line = el("div", "tripstotal");
    line.appendChild(el("span", "bigish", fmt(tot.km, 0)));
    line.appendChild(el("span", "unit", " km in " + tot.trips + (tot.trips === 1 ? " trip" : " trips")));
    head.appendChild(line);
    const sub0 = el("div", "sub", tot.kwh_per_100 === null ? "Overall consumption: not enough driving yet"
      : "Overall " + fmt(tot.kwh_per_100, 1) + " kWh/100 km over " + fmt(tot.measured_km, 0) + " km");
    const hi0 = window.UI.info("Totals are more reliable than any single short trip, because battery rounding averages out across trips. A trip under about 3 km or 2% of battery shows 'too short to measure', since the battery percent is a whole number.", "About these figures");
    sub0.appendChild(document.createTextNode(" "));
    sub0.appendChild(hi0.btn);
    head.appendChild(sub0);
    head.appendChild(hi0.tip);
    box.appendChild(head);

    if (!data.trips.length) { box.appendChild(el("div", "muted pad", "No trips yet. Drive with the app running.")); return; }

    let lastDay = "";
    for (const t of data.trips) {
      const day = dayLabel(t.start);
      if (day !== lastDay) { box.appendChild(el("h2", null, day)); lastDay = day; }
      const card = t.observed ? el("a", "card") : el("div", "card gap");
      if (t.observed) card.setAttribute("href", "#/trip/" + t.id);
      const top = el("div", "cardtop");
      top.appendChild(el("span", "when", t.observed ? hhmm(t.start) + " – " + hhmm(t.end) : "Car offline, " + hhmm(t.start) + " – " + hhmm(t.end)));
      if (t.ongoing) top.appendChild(el("span", "badge live", "in progress"));
      else if (!t.observed) top.appendChild(el("span", "badge", "unrecorded drive"));
      else if (!t.has_route) top.appendChild(el("span", "badge", "no route"));
      card.appendChild(top);

      const mid = el("div", "cardmid");
      const km = el("div", "cell");
      km.appendChild(el("span", "v", fmt(t.km, 1)));
      km.appendChild(el("span", "u", " km"));
      mid.appendChild(km);
      const c = consText(t);
      const cons = el("div", "cell");
      cons.appendChild(el("span", c.unit ? "v" : "v dim", c.big));
      if (c.unit) cons.appendChild(el("span", "u", " " + c.unit));
      mid.appendChild(cons);
      card.appendChild(mid);

      const bits = [];
      if (t.observed) bits.push(dur(t.minutes));
      if (t.used_pct !== null) bits.push("battery " + fmt(t.soc_start, 0) + "→" + fmt(t.soc_end, 0) + "%");
      if (t.avg_kmh !== null) bits.push("avg " + fmt(t.avg_kmh, 0) + " km/h");
      card.appendChild(el("div", "note", bits.join(" · ") + (c.note ? (bits.length ? " · " : "") + c.note : "")));
      if (!t.observed) {
        const gi = window.UI.info("The car did not report during this drive, so there is no route. Distance and battery change are from before and after.", "About unrecorded drives");
        top.appendChild(gi.btn);
        card.appendChild(gi.tip);
      }
      box.appendChild(card);
    }
  }

  // ---- map ----------------------------------------------------------------------------------------------------
  function mercator(lat, lon, z) {
    const s = 256 * Math.pow(2, z);
    const x = (lon + 180) / 360 * s;
    const sin = Math.sin(lat * Math.PI / 180);
    const y = (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * s;
    return [x, y];
  }
  const SPEED_COLORS = [[30, "#16a34a"], [70, "#d97706"], [1e9, "#dc2626"]];
  function speedColor(kmh) {
    const v = kmh === null || kmh === undefined ? 0 : kmh;
    for (const [lim, col] of SPEED_COLORS) if (v < lim) return col;
    return "#dc2626";
  }

  function drawMap(box, pts, tiles) {
    clear(box);
    if (!pts || pts.length < 2) {
      box.appendChild(el("div", "maphint", "No route recorded for this trip (no GPS fixes)."));
      return;
    }
    const W = box.clientWidth || 360, H = 300, pad = 34;
    const lats = pts.map((p) => p.lat), lons = pts.map((p) => p.lon);
    const minLat = Math.min(...lats), maxLat = Math.max(...lats), minLon = Math.min(...lons), maxLon = Math.max(...lons);
    let z = 17;
    for (; z > 2; z--) {
      const a = mercator(maxLat, minLon, z), b = mercator(minLat, maxLon, z);
      if (b[0] - a[0] <= W - 2 * pad && b[1] - a[1] <= H - 2 * pad) break;
    }
    const a = mercator(maxLat, minLon, z), b = mercator(minLat, maxLon, z);
    const ox = (a[0] + b[0]) / 2 - W / 2, oy = (a[1] + b[1]) / 2 - H / 2;

    if (tiles) {
      const n = Math.pow(2, z);
      for (let x = Math.floor(ox / 256); x <= Math.floor((ox + W) / 256); x++) {
        for (let y = Math.floor(oy / 256); y <= Math.floor((oy + H) / 256); y++) {
          if (y < 0 || y >= n) continue;
          const img = new Image(256, 256);
          img.className = "maptile";
          img.alt = "";
          img.referrerPolicy = "no-referrer";
          img.style.left = (x * 256 - ox) + "px";
          img.style.top = (y * 256 - oy) + "px";
          img.src = "https://tile.openstreetmap.org/" + z + "/" + (((x % n) + n) % n) + "/" + y + ".png";
          box.appendChild(img);
        }
      }
    }

    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("class", "maplayer");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Route of this trip");
    const xy = pts.map((p) => { const m = mercator(p.lat, p.lon, z); return [m[0] - ox, m[1] - oy]; });

    const casing = document.createElementNS(ns, "polyline");
    casing.setAttribute("points", xy.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" "));
    casing.setAttribute("fill", "none"); casing.setAttribute("stroke", "#ffffff"); casing.setAttribute("stroke-width", "8");
    casing.setAttribute("stroke-linecap", "round"); casing.setAttribute("stroke-linejoin", "round");
    svg.appendChild(casing);
    for (let i = 1; i < xy.length; i++) {
      const seg = document.createElementNS(ns, "line");
      seg.setAttribute("x1", xy[i - 1][0].toFixed(1)); seg.setAttribute("y1", xy[i - 1][1].toFixed(1));
      seg.setAttribute("x2", xy[i][0].toFixed(1)); seg.setAttribute("y2", xy[i][1].toFixed(1));
      seg.setAttribute("stroke", speedColor(pts[i].kmh)); seg.setAttribute("stroke-width", "4.5");
      seg.setAttribute("stroke-linecap", "round");
      svg.appendChild(seg);
    }
    function marker(p, fill, letter) {
      const g = document.createElementNS(ns, "g");
      const c = document.createElementNS(ns, "circle");
      c.setAttribute("cx", p[0].toFixed(1)); c.setAttribute("cy", p[1].toFixed(1)); c.setAttribute("r", "11");
      c.setAttribute("fill", fill); c.setAttribute("stroke", "#ffffff"); c.setAttribute("stroke-width", "3");
      const t = document.createElementNS(ns, "text");
      t.setAttribute("x", p[0].toFixed(1)); t.setAttribute("y", (p[1] + 4.5).toFixed(1));
      t.setAttribute("text-anchor", "middle"); t.setAttribute("fill", "#ffffff");
      t.setAttribute("font-size", "12"); t.setAttribute("font-weight", "700");
      t.textContent = letter;
      g.appendChild(c); g.appendChild(t);
      return g;
    }
    svg.appendChild(marker(xy[0], "#0f766e", "S"));
    svg.appendChild(marker(xy[xy.length - 1], "#7c3aed", "E"));
    box.appendChild(svg);
    if (tiles) box.appendChild(el("div", "attrib", "© OpenStreetMap contributors"));
  }

  // ---- speed chart --------------------------------------------------------------------------------------------
  function drawSpeed(svg, series) {
    clear(svg);
    if (!series || series.length < 2) return;
    const tMax = Math.max(1, series[series.length - 1].s);
    const vMax = Math.max(40, ...series.map((p) => p.kmh));
    const ns = "http://www.w3.org/2000/svg";
    const pl = document.createElementNS(ns, "polyline");
    pl.setAttribute("points", series.map((p) => ((p.s / tMax) * 296 + 2).toFixed(1) + "," + (76 - (p.kmh / vMax) * 70).toFixed(1)).join(" "));
    pl.setAttribute("fill", "none"); pl.setAttribute("stroke", "currentColor"); pl.setAttribute("stroke-width", "2");
    pl.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(pl);
  }

  // ---- regen --------------------------------------------------------------------------------------------------
  const REGEN_TIP = "Regen is estimated from BYD's live battery power, a spot reading about every 30 s: positive means energy going back into the battery, negative means it is being drawn. Short bursts can fall between samples, so one trip's regen is rough (typically within about 35%; the \u00b1 shown is deliberately wider). Battery gain while driving is regen and is never counted as charging.";

  function drawPower(svg, series) {
    clear(svg);
    if (!series || series.length < 2) return;
    const tMax = Math.max(1, series[series.length - 1].s);
    const vMax = Math.max(20, ...series.map((p) => Math.abs(p.kw)));
    const ns = "http://www.w3.org/2000/svg";
    const zero = document.createElementNS(ns, "line");
    zero.setAttribute("x1", "0"); zero.setAttribute("x2", "300"); zero.setAttribute("y1", "40"); zero.setAttribute("y2", "40");
    zero.setAttribute("stroke", "currentColor"); zero.setAttribute("stroke-width", "1"); zero.setAttribute("opacity", "0.4");
    zero.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(zero);
    series.forEach((p, i) => {
      const next = i + 1 < series.length ? series[i + 1].s : p.s + (p.s - (series[i - 1] ? series[i - 1].s : 0));
      const w = Math.max(1.2, ((Math.min(next - p.s, 90)) / tMax) * 296 - 0.6);
      const h = Math.max(0.8, (Math.abs(p.kw) / vMax) * 37);
      const r = document.createElementNS(ns, "rect");
      r.setAttribute("x", ((p.s / tMax) * 296 + 2).toFixed(1));
      r.setAttribute("width", w.toFixed(1));
      r.setAttribute("height", h.toFixed(1));
      r.setAttribute("y", (p.kw >= 0 ? 40 - h : 40).toFixed(1));
      r.setAttribute("fill", p.kw >= 0 ? "#16a34a" : "#d97706");
      svg.appendChild(r);
    });
  }

  function drawRegenSection(box, t) {
    const head = el("h2", null, "Regen ");
    const hi = window.UI.info(REGEN_TIP, "About regen");
    head.appendChild(hi.btn);
    box.appendChild(head);
    box.appendChild(hi.tip);
    const g = t.regen;
    if (!g) {
      box.appendChild(el("div", "muted small", "Not enough power readings on this trip (needs 8 or more)."));
      return;
    }
    const grid = el("section", "tiles");
    grid.appendChild(stat("Recovered", fmt(g.regen_kwh, 2), "kWh",
      g.regen_pm === null || g.regen_pm === undefined ? "none seen in the samples" : "\u00b1" + fmt(g.regen_pm, 2) + " (rough)"));
    grid.appendChild(stat("Drawn", fmt(g.used_kwh, 2), "kWh", "\u00b1" + fmt(g.used_pm, 2)));
    if (g.regen_share_pct !== null && g.regen_share_pct !== undefined) {
      grid.appendChild(stat("Regen share", "~" + fmt(g.regen_share_pct, 0), "%", "of the energy drawn"));
    }
    if (g.check) {
      grid.appendChild(stat("Check vs battery %", g.check === "consistent" ? "Consistent" : "Differs", "",
        "net " + fmt(g.net_kwh, 2) + " vs " + fmt(g.soc_kwh, 2) + " kWh"));
    }
    box.appendChild(grid);
    if (t.power_series && t.power_series.length > 1) {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 300 80"); svg.setAttribute("preserveAspectRatio", "none");
      svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Battery power over the trip: up is regen, down is drawn");
      svg.style.color = getComputedStyle(document.documentElement).getPropertyValue("--muted");
      box.appendChild(svg);
      drawPower(svg, t.power_series);
      const leg = el("div", "legend");
      for (const [name, col] of [["regen (into the battery)", "#16a34a"], ["drawn from the battery", "#d97706"]]) {
        const sp = el("span");
        const sw = el("i", "sw");
        sw.style.background = col;
        sp.appendChild(sw);
        sp.appendChild(document.createTextNode(name));
        leg.appendChild(sp);
      }
      box.appendChild(leg);
    }
  }

  // ---- trip page ----------------------------------------------------------------------------------------------
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

  async function loadTrip(id) {
    const box = $("view-trip");
    if (box.dataset.loaded !== String(id)) { clear(box); box.appendChild(el("div", "muted", "Loading trip…")); }
    let t;
    try { t = await getJSON("/api/trips/" + encodeURIComponent(id)); } catch (e) {
      if (box.dataset.loaded === String(id)) { window.UI.refreshLater("trip", 15000, "#/trip/", () => loadTrip(id)); return; }
      clear(box);
      const back = el("a", "back", "← All trips"); back.setAttribute("href", "#/trips");
      box.appendChild(back);
      box.appendChild(el("div", "stale", "This trip was not found."));
      return;
    }
    clear(box);
    box.dataset.loaded = String(id);
    if (t.ongoing) window.UI.refreshLater("trip", 30000, "#/trip/", () => loadTrip(id));
    const back = el("a", "back", "← All trips");
    back.setAttribute("href", "#/trips");
    box.appendChild(back);

    box.appendChild(el("h1", "tripname", dayLabel(t.start) + ", " + hhmm(t.start) + " – " + hhmm(t.end)));
    if (t.ongoing) box.appendChild(el("div", "badge live", "in progress"));

    const map = el("div", "map");
    box.appendChild(map);
    drawMap(map, t.points, t.map_tiles);
    const leg = el("div", "legend");
    for (const [name, col] of [["under 30 km/h", "#16a34a"], ["30–70", "#d97706"], ["over 70", "#dc2626"]]) {
      const s = el("span");
      const sw = el("i", "sw");
      sw.style.background = col;
      s.appendChild(sw);
      s.appendChild(document.createTextNode(name));
      leg.appendChild(s);
    }
    box.appendChild(leg);

    const c = consText(t);
    const hero = el("section", "hero");
    hero.appendChild(el("div", "label", "Consumption"));
    const big = el("div", "big small");
    big.appendChild(el("span", null, c.big));
    if (c.unit) big.appendChild(el("span", "unit", " " + c.unit));
    hero.appendChild(big);
    let sub = c.note;
    let how = "";
    if (!sub) {
      how = t.method === "ticks" ? "Measured between battery ticks over the trip." : "Measured from start to end battery.";
      sub = "";
      if (t.byd_kwh_per_100_end !== null && t.byd_kwh_per_100_end !== undefined) {
        const diff = (t.kwh_per_100 / t.byd_kwh_per_100_end - 1) * 100;
        sub = "BYD showed " + fmt(t.byd_kwh_per_100_end, 1) + " at the end (" + (diff >= 0 ? "+" : "") + fmt(diff, 0) + "%)";
        how += " BYD's figure is its own last-50-km average at the end of the trip.";
      }
    }
    const subEl = el("div", "note", sub);
    if (how) {
      const ci = window.UI.info(how, "How this is measured");
      subEl.appendChild(document.createTextNode(" "));
      subEl.appendChild(ci.btn);
      hero.appendChild(subEl);
      hero.appendChild(ci.tip);
    } else {
      hero.appendChild(subEl);
    }
    box.appendChild(hero);

    const grid = el("section", "tiles");
    grid.appendChild(stat("Distance", fmt(t.km, 1), "km"));
    grid.appendChild(stat("Duration", dur(t.minutes), ""));
    grid.appendChild(stat("Average speed", fmt(t.avg_kmh, 0), "km/h", "including stops"));
    grid.appendChild(stat("Top speed seen", fmt(t.max_kmh, 0), "km/h", "30 s samples"));
    grid.appendChild(stat("Battery", fmt(t.soc_start, 0) + "→" + fmt(t.soc_end, 0), "%", t.used_pct === null ? "" : fmt(t.used_pct, 0) + "% used"));
    grid.appendChild(stat("Energy used", fmt(t.kwh, 1), "kWh", "from battery %"));
    box.appendChild(grid);

    drawRegenSection(box, t);

    const sph = el("h2", null, "Speed ");
    const spi = window.UI.info("Speed is sampled about every 30 s, so short bursts and stops can be missed.", "About this chart");
    sph.appendChild(spi.btn);
    box.appendChild(sph);
    box.appendChild(spi.tip);
    const sp = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    sp.setAttribute("viewBox", "0 0 300 80"); sp.setAttribute("preserveAspectRatio", "none");
    sp.setAttribute("role", "img"); sp.setAttribute("aria-label", "Speed over the trip");
    sp.style.color = getComputedStyle(document.documentElement).getPropertyValue("--band");
    box.appendChild(sp);
    drawSpeed(sp, t.speed_series);
  }

  // ---- router -------------------------------------------------------------------------------------------------
  let current = "";
  function route() {
    const h = location.hash || "#/";
    const m = h.match(/^#\/trip\/(\d{1,12})$/);
    const mc = h.match(/^#\/charge\/(\d{1,12})$/);
    const view = h === "#/trips" ? "trips" : h === "#/battery" ? "battery" : h === "#/charging" ? "charging"
      : h === "#/account" ? "account" : mc ? "charge" : m ? "trip" : "now";
    for (const v of ["now", "trips", "trip", "battery", "charging", "charge", "account"]) $("view-" + v).hidden = v !== view;
    $("tab-now").classList.toggle("on", view === "now");
    $("tab-trips").classList.toggle("on", view === "trips" || view === "trip");
    $("tab-charging").classList.toggle("on", view === "charging" || view === "charge");
    $("tab-battery").classList.toggle("on", view === "battery");
    const key = view + (m ? m[1] : mc ? mc[1] : "");
    if (key === current && view !== "now") return;
    current = key;
    window.scrollTo(0, 0);
    if (view === "account") window.loadAccount();
    else if (view === "charging") window.loadCharging();
    else if (view === "charge") window.loadCharge(mc[1]);
    else if (view === "battery") window.loadBattery();
    else if (view === "trips") loadList();
    else if (view === "trip") loadTrip(m[1]);
  }
  window.addEventListener("hashchange", route);
  route();
})();
