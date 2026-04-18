// ARIA-OS service worker.
//
// Strategy:
//   * /api/*           — bypass SW entirely (always live)
//   * HTML / manifest  — network-first, fall back to cache (so a new deploy is
//                        picked up on the very next page load instead of being
//                        masked by a stale cache).
//   * Hashed assets    — cache-first (Vite fingerprints the filename, so these
//                        never change once published).
//
// Bump VERSION whenever this file is edited so old caches get evicted on the
// next "activate".
const VERSION = "aria-vercel-v3";
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

const isHtmlShell = (req, url) =>
  req.mode === "navigate" ||
  url.pathname === "/" ||
  url.pathname.endsWith(".html") ||
  url.pathname === "/manifest.json" ||
  url.pathname === "/mobile.css";

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) return;

  // HTML / shell → network-first so deploys roll out immediately.
  if (isHtmlShell(event.request, url)) {
    event.respondWith(
      fetch(event.request)
        .then((res) => {
          const clone = res.clone();
          caches.open(VERSION).then((c) => c.put(event.request, clone)).catch(() => {});
          return res;
        })
        .catch(() => caches.match(event.request).then((hit) => hit || caches.match("/")))
    );
    return;
  }

  // Hashed assets → cache-first.
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
