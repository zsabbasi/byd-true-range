"use strict";
// Installable app support. Registers the service worker (only where the browser allows it: https or localhost) and offers an
// Install button when the browser says the app can be installed. No data is stored by any of this.
(function () {
  if ("serviceWorker" in navigator && window.isSecureContext) {
    document.documentElement.dataset.sw = "1";
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => { document.documentElement.dataset.sw = "failed"; });
  }
  let deferred = null;
  const btn = () => document.getElementById("installbtn");
  window.addEventListener("beforeinstallprompt", (ev) => {
    ev.preventDefault();
    deferred = ev;
    const b = btn();
    if (b) b.hidden = false;
  });
  window.addEventListener("appinstalled", () => { deferred = null; const b = btn(); if (b) b.hidden = true; });
  document.addEventListener("DOMContentLoaded", () => {
    const b = btn();
    if (!b) return;
    if (deferred) b.hidden = false;
    b.addEventListener("click", async () => {
      if (!deferred) return;
      deferred.prompt();
      try { await deferred.userChoice; } catch (e) { /* dismissed */ }
      deferred = null;
      b.hidden = true;
    });
  });
})();
