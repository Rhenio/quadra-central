/* Quadra Central — Service Worker
 * Estratégia:
 *  - index.html: network-first (sempre tenta a versão nova; cache só como fallback offline)
 *  - ícones/manifest: cache-first (mudam raramente)
 *  - QUALQUER requisição cross-origin (Apps Script, odds, proxy CORS, Tennis Abstract,
 *    players.json via raw etc.): NÃO intercepta — passa direto pela rede.
 *    Isso garante que scores ao vivo e odds nunca fiquem presos em cache velho.
 */

const CACHE = "quadra-central-v1"; // ↑ incremente (v2, v3...) ao mudar o SW

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

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Só intercepta o que é do próprio site (GitHub Pages).
  // Apps Script, APIs de odds, Tennis Abstract etc. passam direto.
  if (url.origin !== self.location.origin) return;
  if (event.request.method !== "GET") return;

  // Navegação / index.html → network-first
  if (event.request.mode === "navigate" || url.pathname.endsWith("index.html")) {
    event.respondWith(
      fetch(event.request)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
          return resp;
        })
        .catch(() => caches.match(event.request).then((r) => r || caches.match("./index.html")))
    );
    return;
  }

  // Demais assets do site (ícones, manifest) → cache-first
  event.respondWith(
    caches.match(event.request).then((cached) => {
      return (
        cached ||
        fetch(event.request).then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
          return resp;
        })
      );
    })
  );
});
