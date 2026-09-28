/* RHG Purchasing service worker.

   The shell - stylesheet, scripts, icon - is cached so the application opens at
   once and survives a dropped connection. Everything under /api/ is deliberately
   NOT cached: this reports financial figures, and a stale number presented as
   current is exactly the error the application exists to prevent. */
const CACHE = "rhg-purchasing-shell-v1";
const SHELL = ["brand.css", "select.css", "select.js", "icon.svg", "manifest.webmanifest"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE)
    .then(c => Promise.allSettled(SHELL.map(f => c.add(f))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // Built from parts so the reverse proxy's path rewriting cannot alter it:
  // it rewrites the literal "/api/", which would pin this to one prefix.
  if (url.pathname.includes("/" + "api" + "/")) return;   // never serve stale figures

  const name = url.pathname.split("/").pop();
  if (!SHELL.includes(name)) return;

  e.respondWith(caches.match(e.request).then(hit => {
    const net = fetch(e.request).then(res => {
      if (res.ok) caches.open(CACHE).then(c => c.put(e.request, res.clone()));
      return res;
    }).catch(() => hit);
    return hit || net;
  }));
});
