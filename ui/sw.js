/* Morning Brief service worker.
 *
 * Deliberately conservative: this app talks to a server on your own machine
 * and every /api/ response is live data (now playing, markets, the briefing).
 * Caching those would show you yesterday's numbers, so only the shell is
 * cached, and even that is network-first so an edit to the HTML shows up on
 * the next load rather than being stuck behind a stale cache.
 */

const VERSION = 'morning-brief-v1';
const SHELL = ['/', '/dashboard', '/ui/manifest.webmanifest',
               '/ui/icon-192.png', '/ui/icon-512.png'];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(VERSION)
      .then(cache => cache.addAll(SHELL).catch(() => {}))  // a miss must not block install
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Live data: always go to the network, never serve a stale copy.
  if (url.pathname.startsWith('/api/')) return;

  // Shell: network first, falling back to cache when offline.
  event.respondWith(
    fetch(request)
      .then(response => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(VERSION).then(cache => cache.put(request, copy)).catch(() => {});
        }
        return response;
      })
      .catch(() => caches.match(request).then(hit => hit || caches.match('/dashboard')))
  );
});
