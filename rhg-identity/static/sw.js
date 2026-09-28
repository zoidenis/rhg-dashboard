/* RHG Control Towers service worker.
   The shell (stylesheet, icon, manifest) is cached so the app opens instantly
   and survives a dropped connection. Everything under /api/ is deliberately
   NOT cached: this application reports financial figures, and a stale number
   shown as current is exactly the error it exists to prevent. */
const CACHE = "rhg-shell-v1";
const SHELL = ["/app.css", "/icon.svg", "/manifest.webmanifest"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/internal/")) return;

  // Shell assets: serve from cache, refresh in the background.
  if (SHELL.includes(url.pathname)) {
    e.respondWith(caches.match(e.request).then(hit => {
      const net = fetch(e.request).then(res => {
        if (res.ok) caches.open(CACHE).then(c => c.put(e.request, res.clone()));
        return res;
      }).catch(() => hit);
      return hit || net;
    }));
  }
});
