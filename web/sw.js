const CACHE_NAME = "tpms-v1";
const ASSETS = [
  "index.html",
  "manifest.json",
  "assets/lc150_2015.svg",
  "assets/sedan.svg",
  "assets/motocross.svg",
  "assets/trailer.svg"
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (e) => {
  e.respondWith(caches.match(e.request).then((res) => res || fetch(e.request)));
});
