// ARIA-OS service worker — minimal: cache static shell, network-first for API.
const VERSION = "aria-vercel-v1";
const SHELL = ["/", "/index.html", "/manifest.json", "/mobile.css"];

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
  if (url.pathname.startsWith("/api/")) return;
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
