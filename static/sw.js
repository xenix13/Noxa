// Noxa: service worker. Makes the app installable and lets it open even when the server
// is unreachable ("Try again" message instead of a browser error page).
// Network first: after a server update, an old version is never used (the code, served at
// an address that changes with each version, comes straight from the cache).
// The cache is only used offline. API, videos and pictures do not go through here (browser HTTP cache).
// The server replaces the two lines below (version and list of the interface files).
const VERSION = 'dev';
const SHELL = ['/'];
const CACHE = `nexis-${VERSION}`;

self.addEventListener('install', (event) => {
  // cache: 'reload': the files come from the server, never from a copy kept by the browser
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL.map((u) => new Request(u, { cache: 'reload' }))))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith('nexis-') && k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET' || req.headers.has('range')) return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/') || url.pathname.startsWith('/vendor/')
      || url.pathname === '/sw.js') return;
  event.respondWith(networkFirst(req));
});

async function networkFirst(req) {
  const key = req.mode === 'navigate' ? '/' : req; // single-page app: every navigation is "/"
  const cache = await caches.open(CACHE);
  // code and style at an address specific to their version (/v/<hash>/): they never change, the copy is enough
  // (without this, each opening asks the server again for the forty or so interface files)
  if (new URL(req.url).pathname.startsWith('/v/')) {
    const hit = await cache.match(req);
    if (hit) return hit;
  }
  try {
    const res = await fetch(req);
    if (res.ok && res.type === 'basic') cache.put(key, res.clone());
    // server restarting (update): the proxy answers 502 / 503 / 504. The interface is opened
    // anyway, it shows "Noxa is restarting…" and reloads by itself.
    if (res.status >= 502 && res.status <= 504) return (await cache.match(key)) || res;
    return res;
  } catch (err) {
    const hit = await cache.match(key);
    if (hit) return hit;
    throw err;
  }
}

// Push notifications: the message arrives empty (nothing to encrypt on the server side); Noxa is asked what to show.
self.addEventListener('push', (event) => {
  event.waitUntil((async () => {
    let msgs = [];
    try {
      const r = await fetch('/api/push/pending', { credentials: 'same-origin', cache: 'no-store' });
      if (r.ok) msgs = (await r.json()).messages || [];
    } catch { /* server unreachable: generic message */ }
    const m = msgs[msgs.length - 1] || { title: 'Noxa', body: '', url: '#/', tag: 'nexis' };
    await self.registration.showNotification(m.title, { body: m.body, tag: m.tag, icon: '/icons/icon-192.png',
      badge: '/icons/maskable-192.png', data: { url: m.url || '#/' } });
  })());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = `/${(event.notification.data && event.notification.data.url) || '#/'}`;
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    const open = list.find((c) => new URL(c.url).origin === self.location.origin);
    if (open) return open.focus().then((c) => (c && c.navigate ? c.navigate(url) : c));
    return self.clients.openWindow(url);
  }));
});
