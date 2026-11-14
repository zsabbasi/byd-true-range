"use strict";
// Car mode: when the app runs on a car's screen it refreshes everything often, keeps the screen awake and uses a larger layout.
// It is detected automatically and can be switched on or off by hand (footer button, or ?car=1 / ?car=0 / ?car=auto once). Nothing here
// talks to the server and nothing is sent anywhere: it only decides how often THIS page asks the server, and how big it draws.
(function () {
  const KEY = "carmode";

  // The one head unit seen so far (an Atto 3): a desktop-style Linux user agent on a platform that is really ARM ("Linux armv81"), with
  // a touch screen. A phone using "request desktop site" looks the same, which is why there is a manual override.
  function detect() {
    try {
      const ua = navigator.userAgent || "", plat = navigator.platform || "";
      return /X11; Linux/.test(ua) && !/Android/.test(ua) && /^Linux (arm|aarch)/i.test(plat) && (navigator.maxTouchPoints || 0) > 0;
    } catch (e) { return false; }
  }
  function stored() {
    try { const v = localStorage.getItem(KEY); return v === "on" || v === "off" ? v : "auto"; } catch (e) { return "auto"; }
  }
  function store(v) { try { if (v === "auto") localStorage.removeItem(KEY); else localStorage.setItem(KEY, v); } catch (e) { /* private mode */ } }

  // ?car=1 / ?car=0 / ?car=auto sets the mode once and is remembered.
  try {
    const q = new URLSearchParams(location.search).get("car");
    if (q === "1" || q === "on") store("on");
    else if (q === "0" || q === "off") store("off");
    else if (q === "auto") store("auto");
  } catch (e) { /* no URLSearchParams */ }

  const CAR = { detected: detect(), mode: stored() };
  CAR.on = CAR.mode === "on" || (CAR.mode === "auto" && CAR.detected);
  // ms(normal, car): the normal refresh interval, or the (shorter) one used in the car.
  CAR.ms = (normal, car) => (CAR.on ? car : normal);
  window.CAR = CAR;
  if (CAR.on) document.documentElement.classList.add("car");

  // Keep the screen awake while the page is visible (it needs https or localhost; the browser may refuse, which is fine).
  let sentinel = null;
  async function awake() {
    if (!CAR.on || document.hidden || !("wakeLock" in navigator) || !window.isSecureContext) return;
    try {
      sentinel = await navigator.wakeLock.request("screen");
      document.documentElement.dataset.awake = "1";
      sentinel.addEventListener("release", () => { sentinel = null; document.documentElement.dataset.awake = ""; });
    } catch (e) { document.documentElement.dataset.awake = ""; }
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !sentinel) awake(); });

  function label() {
    return "Car mode: " + CAR.mode + (CAR.mode === "auto" ? (CAR.on ? " (on)" : " (off)") : "");
  }
  document.addEventListener("DOMContentLoaded", () => {
    awake();
    const btn = document.getElementById("carbtn");
    if (!btn) return;
    btn.textContent = label();
    btn.setAttribute("title", "Car mode refreshes every page often, keeps the screen on and draws larger. Auto turns it on when this looks like a car screen. Tap to change; the page reloads.");
    btn.addEventListener("click", () => {
      const next = { auto: "on", on: "off", off: "auto" }[CAR.mode];
      store(next);
      location.reload();
    });
  });
})();
