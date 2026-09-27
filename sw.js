// MiniTradeIQ Service Worker — v4
//
// Two rules, both learned the hard way:
//
// 1. INSTALL MUST NEVER FAIL. If any pre-cached file is missing, a plain
//    cache.addAll() rejects and the whole worker refuses to install ("error
//    during installation"). Each file is cached on its own and failures are
//    ignored — a missing icon is not a reason to have no worker at all.
//
// 2. CACHE ONLY WHAT IS KNOWN TO BE STATIC. v3 kept a list of API paths to
//    skip and cached everything else — so every new endpoint (/calendar,
//    /paper-portfolio, /residual-income, /technicals) was silently cached
//    forever until someone remembered to add it. The list is now inverted:
//    only the files below are cached; anything else goes to the network,
//    untouched. New endpoints need no changes here.

const VERSION = "v4";
const SHELL_CACHE = `minitradeiq-shell-${VERSION}`;
const ASSET_CACHE = `minitradeiq-assets-${VERSION}`;
const STATIC_ASSETS = ["/manifest.json", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(ASSET_CACHE)
      .then((c) => Promise.allSettled(STATIC_ASSETS.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names
        .filter((n) => n !== SHELL_CACHE && n !== ASSET_CACHE)
        .map((n) => caches.delete(n))))            // drop v1–v3 caches
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // fonts, CDNs: browser's job

  // App shell: network first, cache only as an offline fallback
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(SHELL_CACHE).then((c) => c.put("/", copy));
          }
          return res;
        })
        .catch(() => caches.match("/").then((r) => r || Response.error()))
    );
    return;
  }

  // Known static files: cache first
  if (STATIC_ASSETS.includes(url.pathname)) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(ASSET_CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }))
    );
    return;
  }

  // Everything else — every API endpoint, current and future — is live.
});
