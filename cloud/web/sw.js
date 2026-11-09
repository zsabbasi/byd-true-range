"use strict";
// Service worker: makes the page installable and lets the app shell open instantly. It NEVER caches /api/* (your data is always
// fetched live, so nothing about your car is ever stored by the worker). __VERSION__ is replaced by the server with a hash of the
// shell files, so a changed file produces a new cache and the old one is deleted.
const CACHE = "byd-range-__VERSION__";
const SHELL = ["/", "/app.css", "/ui.js", "/app.js", "/battery.js", "/charging.js", "/account.js", "/trips.js", "/pwa.js",
               "/icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil((async () => {
    const c = await caches.open(CACHE);
    for (const u of SHELL) {
      try { const r = await fetch(u, { cache: "no-store", credentials: "same-origin" }); if (r.ok) await c.put(u, r); } catch (err) { /* offline: skip */ }
    }
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith("byd-range-") && k !== CACHE) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;     // maps and everything else: the network, untouched
  if (url.pathname.startsWith("/api/")) return;                                // data: never cached
  if (!SHELL.includes(url.pathname)) return;
  e.respondWith((async () => {
    try {
      const r = await fetch(req);                                              // network first: always the newest shell when online
      if (r.ok) { const c = await caches.open(CACHE); c.put(url.pathname, r.clone()); }
      return r;
    } catch (err) {
      const hit = await caches.match(url.pathname);
      if (hit) return hit;
      throw err;
    }
  })());
});
