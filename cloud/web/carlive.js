"use strict";
// Live from this screen (car mode only). Speed, heading and position come from the head unit's OWN GPS, computed and drawn in this page,
// nothing is sent anywhere. Power and the tyre pressures are BYD's readings from its cloud (a spot value every ~30 s while driving) and are
// always shown with their age, never as if they were live. No motion sensors are used: the unit's accelerometer gives a fixed value, and an
// accelerometer cannot give speed anyway (integrating it drifts by metres per second within seconds), so speed is GPS. All text goes in with textContent.
(function () {
  const G = 9.80665;
  const FIX_MAX_AGE_MS = 5000;     // a GPS fix older than this is "no fix"
  const STATUS_OLD_S = 90;         // BYD values older than this are shown dim

  // ---------------------------------------------------------------------------------------------------- pure helpers
  const rad = (d) => d * Math.PI / 180;
  function haversine(a, b) {                                   // metres between two {lat, lon}
    const R = 6371000, dLat = rad(b.lat - a.lat), dLon = rad(b.lon - a.lon);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function bearing(a, b) {                                     // degrees clockwise from north, a to b
    const y = Math.sin(rad(b.lon - a.lon)) * Math.cos(rad(b.lat));
    const x = Math.cos(rad(a.lat)) * Math.sin(rad(b.lat)) - Math.sin(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.cos(rad(b.lon - a.lon));
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  }
  function compass(deg) { return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round((((deg % 360) + 360) % 360) / 45) % 8]; }
  const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);

  // GPS: speed and heading from the browser when it gives them, else worked out from two fixes.
  function speedOf(fix, nowMs) {
    if (!fix || nowMs - fix.t > FIX_MAX_AGE_MS) return null;
    const v = fix.speed !== null ? fix.speed * 3.6 : fix.derivedKmh;
    return v === null || v === undefined ? null : (v < 1.5 ? 0 : v);
  }
  function headingOf(fix, nowMs) {
    if (!fix || nowMs - fix.t > FIX_MAX_AGE_MS) return null;
    if (fix.heading !== null && (fix.speed === null || fix.speed > 1)) return fix.heading;
    return fix.derivedHeading === undefined ? null : fix.derivedHeading;
  }
  function makeFix(pos, prev) {
    const c = pos.coords;
    const fix = { t: pos.timestamp || Date.now(), lat: c.latitude, lon: c.longitude, acc: num(c.accuracy), speed: num(c.speed), heading: num(c.heading), alt: num(c.altitude) };
    if (prev && fix.t - prev.t > 200 && fix.t - prev.t < 5000 && (fix.acc || 99) < 30 && (prev.acc || 99) < 30) {
      const d = haversine(prev, fix);
      fix.derivedKmh = d / ((fix.t - prev.t) / 1000) * 3.6;
      if (d > 5) fix.derivedHeading = bearing(prev, fix);
    }
    return fix;
  }

  window.CARLIVE = { haversine, bearing, compass, makeFix, speedOf, headingOf, constants: { G } };

  // ---------------------------------------------------------------------------------------------------- the card
  const CAR = window.CAR;
  const card = document.getElementById("live");
  if (!CAR || !CAR.on || !card) return;

  const onNow = () => { const h = location.hash; return h === "" || h === "#" || h === "#/"; };
  const state = { fix: null, prev: null, status: null, statusAt: 0, showPos: false, gps: "idle" };
  const refs = {};

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function tile(id, label) {
    const t = el("div", "tile");
    t.appendChild(el("div", "k", label));
    const v = el("div", "v", "—");
    t.appendChild(v);
    const s = el("div", "k", "");
    t.appendChild(s);
    refs[id] = v;
    refs[id + "Sub"] = s;
    return t;
  }
  function set(node, text) { if (node && node.textContent !== text) node.textContent = text; }

  function build() {
    // Car layout, built to fit a landscape screen with no scrolling: the GPS speed sits on the right of the range heading, the battery,
    // BYD range and consumption tiles join the live tiles in one row, and the power chart fills what is left.
    const hero = document.querySelector("#view-now > .hero[aria-live]");
    const spd = el("div", "spdbox live");
    spd.appendChild(el("div", "label", "Speed (GPS)"));
    const row = el("div", "row2");
    refs.speed = el("span", "spd", "—");
    row.appendChild(refs.speed);
    row.appendChild(el("span", "unit", " km/h"));
    spd.appendChild(row);
    refs.speedSub = el("div", "sub", "Starting GPS…");
    spd.appendChild(refs.speedSub);
    refs.bydSpeed = el("div", "note", "");
    spd.appendChild(refs.bydSpeed);
    refs.gpsBtn = el("button", "pill", "Start GPS");
    refs.gpsBtn.type = "button";
    refs.gpsBtn.hidden = true;
    refs.gpsBtn.addEventListener("click", () => startGps(true));
    spd.appendChild(refs.gpsBtn);
    if (hero) hero.appendChild(spd); else card.appendChild(spd);

    const tiles = el("div", "tiles livetiles");
    const origTiles = document.querySelector("#view-now > .tiles");
    for (const id of ["soc", "byd", "cons"]) {                     // the same tiles app.js already fills in, moved into this row
      const n = document.getElementById(id), t = n && n.closest(".tile");
      if (t) tiles.appendChild(t);
    }
    if (origTiles) origTiles.hidden = true;                        // what is left of it is BYD's speed, replaced by the GPS speed
    tiles.appendChild(tile("tyres", "Tyres (BYD)"));
    refs.tyres.classList.add("pre");                               // two lines: front pair, rear pair
    const ht = tile("heading", "Heading");
    ht.classList.add("tappable");
    ht.setAttribute("role", "button");
    ht.addEventListener("click", () => { state.showPos = !state.showPos; });      // tap to show or hide the position (hidden by default)
    tiles.appendChild(ht);
    card.appendChild(tiles);

    const pt = document.getElementById("ptsec");                   // the power chart, same style as the Trips page
    if (pt) {
      const h = pt.querySelector("h2");
      refs.power = el("span", "pnow", "");
      if (h) { h.appendChild(refs.power); if (window.UI) {
        const hi = window.UI.info("Speed and heading come from this screen's own GPS, so they are live (tap Heading to show the position, hidden by default). Power and tyre pressures are not available to a web page: they come from BYD's cloud, a spot reading about every 30 seconds while driving, so they carry their age and dim when old. Nothing here is sent anywhere.", "How this screen works");
        h.appendChild(hi.btn); pt.appendChild(hi.tip);
      } }
      card.appendChild(pt);
    } else { refs.power = el("div", "sub", ""); card.appendChild(refs.power); }
  }

  // ---------------------------------------------------------------------------------------------------- sensors
  let watchId = null, running = false, timer = null;
  function startGps(userAsked) {
    if (!("geolocation" in navigator) || watchId !== null) return;
    const begin = () => {
      state.gps = "waiting";
      refs.gpsBtn.hidden = true;
      watchId = navigator.geolocation.watchPosition((pos) => { state.prev = state.fix; state.fix = makeFix(pos, state.prev); state.gps = "ok"; },
        (e) => { state.gps = e.code === 1 ? "denied" : "error"; watchId = null; refs.gpsBtn.hidden = false; },
        { enableHighAccuracy: true, maximumAge: 0, timeout: 20000 });
    };
    if (userAsked || !navigator.permissions) { begin(); return; }
    navigator.permissions.query({ name: "geolocation" }).then((p) => {
      if (p.state === "granted") begin(); else { state.gps = "needs-permission"; refs.gpsBtn.hidden = false; }
    }, begin);
  }
  function stopGps() { if (watchId !== null) { navigator.geolocation.clearWatch(watchId); watchId = null; } }

  function start() {
    if (running) return;
    running = true;
    startGps(false);
    timer = setInterval(render, 250);
  }
  function stop() {
    if (!running) return;
    running = false;
    stopGps();
    clearInterval(timer);
  }
  function sync() {
    const show = onNow();
    card.hidden = !show;
    document.documentElement.classList.toggle("carnow", show);      // the no-scroll layout applies on the Now page only
    if (show && !document.hidden) start(); else stop();
  }

  // ---------------------------------------------------------------------------------------------------- drawing
  const f0 = (v) => (v === null || v === undefined ? "—" : String(Math.round(v)));
  const f1 = (v) => (v === null || v === undefined ? "—" : v.toFixed(1));

  function render() {
    const now = Date.now();
    const fix = state.fix;
    const sp = speedOf(fix, now);
    set(refs.speed, sp === null ? "—" : f0(sp));
    const how = fix && fix.speed === null ? "from positions" : "GPS";
    set(refs.speedSub, sp !== null ? how + (fix.acc !== null ? ", accuracy ±" + f0(fix.acc) + " m" : "")
      : ({ idle: "GPS not started", waiting: "Waiting for a GPS fix…", denied: "Location is blocked for this app", "needs-permission": "Tap to allow location", error: "No GPS fix" }[state.gps] || "No GPS fix"));
    const st = state.status;
    const bs = st && st.has_data ? st.reading : null;
    set(refs.bydSpeed, bs && bs.speed_kmh !== null && bs.speed_kmh !== undefined
      ? "BYD last said " + f0(bs.speed_kmh) + " km/h, " + f0(bs.data_age_s !== null && bs.data_age_s !== undefined ? bs.data_age_s : bs.age_s) + " s ago" : "");

    const h = headingOf(fix, now);
    set(refs.heading, h === null ? "—" : f0(h) + "° " + compass(h));
    set(refs.headingSub, state.showPos ? "no position yet" : (fix ? "tap to show position" : ""));

    if (state.showPos && fix) set(refs.headingSub, fix.lat.toFixed(4) + ", " + fix.lon.toFixed(4));      // four decimals: about 10 m, not more

    drawBydParts(st, now);
  }

  function drawBydParts(st, now) {
    const p = st && st.has_data ? st.power : null;
    const age = p && p.age_s !== null && p.age_s !== undefined ? p.age_s + (now - state.statusAt) / 1000 : null;
    const old = age !== null && age > STATUS_OLD_S;
    const kw = p ? p.kw : null;
    if (kw === null || kw === undefined) {
      set(refs.power, "—");
    } else {
      const label = kw > 0.05 ? "+" + kw.toFixed(1) + " kW" : kw < -0.05 ? "−" + Math.abs(kw).toFixed(1) + " kW" : "0 kW";
      set(refs.power, " " + label + (age !== null ? " · " + Math.round(age) + " s ago" + (old ? " (old)" : "") : ""));
    }
    refs.power.classList.toggle("dimmed", old);

    const t = st && st.has_data ? st.tyres : null;
    if (!t) { set(refs.tyres, "—"); set(refs.tyresSub, st && st.has_data ? "no pressures recorded yet" : ""); refs.tyres.classList.remove("warnv"); return; }
    set(refs.tyres, f1(t.lf) + "  " + f1(t.rf) + "\n" + f1(t.lr) + "  " + f1(t.rr));
    const ta = t.age_s !== null && t.age_s !== undefined ? t.age_s + (now - state.statusAt) / 1000 : null;
    set(refs.tyresSub, (t.unit || "") + " (front left, right / rear left, right)" + (ta !== null ? ", " + (ta < 3600 ? Math.round(ta / 60) + " min" : (ta / 3600).toFixed(1) + " h") + " ago" : "")
      + (t.spread !== null && t.spread !== undefined && t.spread >= 3 ? ", spread " + f1(t.spread) : ""));
    refs.tyres.classList.toggle("warnv", t.spread !== null && t.spread !== undefined && t.spread >= 3);
  }

  // app.js hands every status answer here; nothing else is fetched.
  window.CARLIVE.onStatus = (s) => { state.status = s; state.statusAt = Date.now(); };

  build();
  window.addEventListener("hashchange", sync);
  document.addEventListener("visibilitychange", sync);
  window.addEventListener("pagehide", stop);
  sync();
})();
