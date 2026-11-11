const CACHE_NAME = "tpms-v1.2";
const CORE_ASSETS = [
  "./",
  "./index.html",
  "./manifest.json",
  "./assets/icon-192.png",
  "./assets/icon-512.png"
];

const OPTIONAL_ASSETS = [
  "./assets/lc150_2015.svg",
  "./assets/sedan.svg",
  "./assets/motocross.svg",
  "./assets/trailer.svg"
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      await cache.addAll(CORE_ASSETS);
      for (const file of OPTIONAL_ASSETS) {
        try {
          await cache.add(file);
        } catch (err) {
          console.warn("Optional asset skipped:", file);
        }
      }
    })
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

self.addEventListener("fetch", (e) => {
  e.respondWith(
    caches.match(e.request).then((cached) => cached || fetch(e.request))
  );
});
