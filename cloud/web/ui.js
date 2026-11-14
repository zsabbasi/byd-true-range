"use strict";
// Small shared UI helper: an (i) button that shows an explanation. Tap/click toggles it (phones have no hover);
// on a pointer device hovering shows it too. Text is only ever set with textContent, never as HTML.
(function () {
  let seq = 0;
  const hoverable = !!(window.matchMedia && window.matchMedia("(hover: hover)").matches);

  function closeAll(exceptBtn) {
    document.querySelectorAll(".tip").forEach((t) => { t.hidden = true; });
    document.querySelectorAll("button.info").forEach((b) => {
      if (b !== exceptBtn) { b.setAttribute("aria-expanded", "false"); b.dataset.pinned = ""; }
    });
  }

  function bind(btn, tip) {
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-controls", tip.id);
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const open = tip.hidden;
      closeAll();
      tip.hidden = !open;
      btn.dataset.pinned = open ? "1" : "";
      btn.setAttribute("aria-expanded", String(open));
    });
    if (hoverable) {
      btn.addEventListener("mouseenter", () => { tip.hidden = false; btn.setAttribute("aria-expanded", "true"); });
      btn.addEventListener("mouseleave", () => {
        if (!btn.dataset.pinned) { tip.hidden = true; btn.setAttribute("aria-expanded", "false"); }
      });
    }
  }

  // Build a button and its tip. The caller puts the button where it wants it and the tip after the row.
  function info(text, label) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "info";
    btn.textContent = "i";
    btn.setAttribute("aria-label", label || "More information");
    const tip = document.createElement("div");
    tip.className = "tip";
    tip.id = "tip" + (++seq);
    tip.setAttribute("role", "tooltip");
    tip.hidden = true;
    tip.textContent = text;
    bind(btn, tip);
    return { btn, tip };
  }

  // Buttons written in the page itself: <button class="info" data-tip="some-id">
  function wire(root) {
    (root || document).querySelectorAll("button.info[data-tip]").forEach((btn) => {
      const tip = document.getElementById(btn.getAttribute("data-tip"));
      if (tip && !btn.dataset.bound) { btn.dataset.bound = "1"; bind(btn, tip); }
    });
  }

  document.addEventListener("click", () => closeAll());
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeAll(); });
  // Re-run fn after ms while the page for hashPrefix is still open and visible. One timer per key; leaving the page stops it
  // (route() starts it again when the page is opened). Used so live pages (a charge in progress) refresh themselves.
  const timers = {};
  function refreshLater(key, ms, hashPrefix, fn) {
    clearTimeout(timers[key]);
    if (window.CAR && window.CAR.on) ms = Math.min(ms, 10000);      // car mode: every page stays live
    const run = () => {
      if (!location.hash.startsWith(hashPrefix)) return;
      if (document.hidden) { timers[key] = setTimeout(run, 5000); return; }
      fn();
    };
    timers[key] = setTimeout(run, ms);
  }

  window.UI = { info, wire, refreshLater };
  wire(document);
})();

// ?tips=open shows every explanation at once (handy for reading them all, or for a screenshot).
if (/[?&]tips=open\b/.test(location.search)) {
  setTimeout(() => document.querySelectorAll(".tip").forEach((t) => { if (t.textContent) t.hidden = false; }), 2500);
}
