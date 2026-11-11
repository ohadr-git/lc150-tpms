const CACHE_NAME = "tpms-v1.0";
const ASSETS = [
  "./",
  "./index.html",
  "./manifest.json",
  "./assets/lc150_2015.svg",
  "./assets/sedan.svg",
  "./assets/motocross.svg",
  "./assets/trailer.svg"
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Cache-First Strategy: Never fail if offline
self.addEventListener("fetch", (e) => {
  e.respondWith(
    caches.match(e.request).then((cached) => cached || fetch(e.request))
  );
});
