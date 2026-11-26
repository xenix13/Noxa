// Noxa demo: answers the interface's requests from recorded answers (demo/index.json), so that the web
// interface works on a static host (GitHub Pages), even in a sub-folder.
// - /api/… GET: the recorded answer, or the closest one (same picture at another size, same page without
//   its options); live connections (EventSource) stay open and silent;
// - /api/play: a direct stream of the title's short clip (demo/clips/<id>.mp4);
// - anything that would change something: an empty success, and the page is told that nothing was saved;
// - other absolute paths (/js/…, /vendor/…, /fonts/…): the same file in the site's folder.
const BASE = new URL('./', self.registration.scope).pathname;
let data = null;
const load = () => (data ||= fetch(`${BASE}demo/index.json`).then((r) => r.json()));

self.addEventListener('install', (e) => { self.skipWaiting(); e.waitUntil(load()); });
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url);
  if (u.origin !== location.origin) return;
  if (u.pathname.startsWith('/api/')) { e.respondWith(api(e.request, u)); return; }
  if (!u.pathname.startsWith(BASE)) e.respondWith(fetch(`${BASE}${u.pathname.slice(1)}${u.search}`));
});

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

async function recorded(f, type) {
  const r = await fetch(`${BASE}demo/r/${f}`);
  return new Response(r.body, { status: r.status, headers: { 'Content-Type': type } });
}

function silentStream() {
  // a live connection that never sends anything (and never closes, so the page does not reconnect)
  const body = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(': demo\n\n')); } });
  return new Response(body, { headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' } });
}

async function notify(what) {
  for (const c of await self.clients.matchAll()) c.postMessage({ demo: what });
}

async function api(req, u) {
  const d = await load();
  const path = u.pathname;
  if ((req.headers.get('Accept') || '').includes('text/event-stream') || /\/(events|feed|watch)$/.test(path)) return silentStream();
  if (req.method === 'GET') return get(d, u);
  if (path === '/api/play') return play(d, req);
  if (['/api/progress', '/api/stop', '/api/login', '/api/logout', '/api/remote/state'].includes(path)) return json({});
  // the administration is read-only: a clear message rather than a fake success
  if (path.startsWith('/api/admin/')) return json({ error: 'Demo: the administration is read-only.' }, 403);
  notify('not-saved');
  return json({});
}

async function get(d, u) {
  const key = u.pathname + u.search;
  const hit = d.answers[key];
  if (hit) return recorded(hit.f, hit.t);
  const keys = Object.keys(d.answers);
  if (u.pathname === '/api/img') {
    // same picture recorded at another size: the largest one
    const p = u.searchParams.get('p');
    const same = keys.filter((k) => k.startsWith('/api/img?') && new URL(k, location.origin).searchParams.get('p') === p);
    same.sort((a, b) => Number(new URL(b, location.origin).searchParams.get('w')) - Number(new URL(a, location.origin).searchParams.get('w')));
    if (same.length) return recorded(d.answers[same[0]].f, d.answers[same[0]].t);
  }
  if (/\/prepare$/.test(u.pathname)) return json({ ready: true });
  // bandwidth measure before playback: a small download, measured as fast (the clips are small)
  if (u.pathname === '/api/bandwidth') return new Response(new Uint8Array(256 * 1024), { headers: { 'Content-Type': 'application/octet-stream' } });
  if (/\/trickplay$/.test(u.pathname)) return json({ ready: false });
  if (u.pathname === '/api/together/time') return json({ now: Date.now() });
  // the same page with other options (sort, filters, page size): the recorded version
  const near = keys.find((k) => k.split('?')[0] === u.pathname);
  if (near) return recorded(d.answers[near].f, d.answers[near].t);
  return json({ error: 'Not available in the demo' }, 404);
}

async function play(d, req) {
  let body = {};
  try { body = await req.json(); } catch { /* */ }
  const id = Number(body.id);
  const itemKey = `/api/items/${id}`;
  if (!d.clips.includes(id) || !d.answers[itemKey]) return json({ error: 'No clip for this title in the demo' }, 404);
  // the browser refused the clip (it asks again for a converted stream): there is no conversion in the demo
  if (body.no_direct) return json({ error: 'This browser cannot play the demo clip (H.264).' }, 415);
  const item = await (await fetch(`${BASE}demo/r/${d.answers[itemKey].f}`)).json();
  return json({
    mode: 'direct', url: `${BASE}demo/clips/${id}.mp4`, offset: 0, duration: d.clip_seconds * 1000, stream: `demo-${id}`,
    reasons: [], markers: {}, next: null, audio: item.audio || [], subtitles: [], subtitle_selected: 0,
    subtitle_overlay: null, subtitle_ass: null, subtitle_pgs: null, auto_quality: null, source_kbps: 0,
  });
}
