/* Quadra Central — Service Worker v2
 * CORREÇÃO da v1: a v1 aplicava cache-first a TODOS os arquivos same-origin,
 * o que congelava data/players.json e assets/ta.js na versão do primeiro acesso.
 * Agora:
 *  - cache-first: SOMENTE icons/ e manifest.json (imutáveis na prática)
 *  - network-first: todo o resto same-origin (index.html, data/*, assets/*)
 *    → sempre busca a versão nova; cache é apenas fallback offline
 *  - cross-origin (Apps Script, odds, Tennis Abstract): não intercepta
 */

const CACHE = "quadra-central-v2"; // ← v2 força a limpeza do cache velho da v1

const SHELL = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/icon-maskable-512.png",
  "./icons/apple-touch-icon.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Só ícones e manifest podem ser cache-first
function isImmutable(url) {
  return url.pathname.includes("/icons/") || url.pathname.endsWith("manifest.json");
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  if (url.origin !== self.location.origin) return; // APIs externas: rede direto
  if (event.request.method !== "GET") return;

  if (isImmutable(url)) {
    // cache-first
    event.respondWith(
      caches.match(event.request).then(
        (cached) =>
          cached ||
          fetch(event.request).then((resp) => {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put(event.request, copy));
            return resp;
          })
      )
    );
    return;
  }

  // network-first para tudo o mais (index, data/*.json, assets/*)
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy));
        return resp;
      })
      .catch(() =>
        caches.match(event.request).then((r) => r || caches.match("./index.html"))
      )
  );
});
