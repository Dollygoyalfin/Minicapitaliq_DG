// MiniTradeIQ Service Worker  —  v3 (network-first for the app shell)
//
// v1/v2 cached "/" cache-first, so a deployed index.html was invisible until
// the cache expired — new tabs simply did not appear while ?v=999 (a different
// cache key) showed them fine. For an app that changes often, staleness is a
// far worse failure than a slow first paint, so the HTML is now fetched from
// the network first and the cache is only a fallback for genuine offline use.

const SHELL_CACHE = "minitradeiq-shell-v3";
const ASSET_CACHE = "minitradeiq-assets-v3";

const ASSETS = ["/manifest.json", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(ASSET_CACHE)
      .then((c) => c.addAll(ASSETS).catch(() => null))
      .then(() => self.skipWaiting())          // take over immediately
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((n) => n !== SHELL_CACHE && n !== ASSET_CACHE)
             .map((n) => caches.delete(n))     // purge every older version
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // API calls: always live, never cached.
  const API_PATHS = ["/dcf", "/valuation", "/financials", "/convergence",
                     "/quality", "/baserates", "/ideas", "/events",
                     "/ai-verdict", "/screener", "/ipos", "/commodities",
                     "/reverse-dcf"];
  if (API_PATHS.some((p) => url.pathname.startsWith(p))) return;

  // App shell (HTML): NETWORK FIRST. Cache only as an offline fallback.
  const isHTML = req.mode === "navigate" ||
                 (req.headers.get("accept") || "").includes("text/html");
  if (isHTML) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(SHELL_CACHE).then((c) => c.put("/", copy));
          return res;
        })
        .catch(() => caches.match("/").then((r) => r || Response.error()))
    );
    return;
  }

  // Static assets: cache-first is fine, they rarely change.
  event.respondWith(
    caches.match(req).then((cached) => cached || fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(ASSET_CACHE).then((c) => c.put(req, copy));
      return res;
    }).catch(() => cached))
  );
});
