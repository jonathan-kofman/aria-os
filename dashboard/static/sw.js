// ARIA-OS service worker — minimal: cache static shell, network-first for API.
// Lets the PWA work briefly offline and load instantly on repeat visits.

const VERSION = "aria-v1";
const SHELL = ["/", "/static/index.html", "/static/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION).then((c) => c.addAll(SHELL).catch(() => null))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // API requests: always go to network (no caching of run state)
  if (url.pathname.startsWith("/api/")) return;
  // Static shell: cache-first
  event.respondWith(
    caches.match(event.request).then((hit) => {
      if (hit) return hit;
      return fetch(event.request).then((res) => {
        const clone = res.clone();
        caches.open(VERSION).then((c) => c.put(event.request, clone)).catch(() => {});
        return res;
      }).catch(() => caches.match("/"));
    })
  );
});
