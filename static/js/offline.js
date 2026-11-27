// Downloads to watch offline.
// - The server prepares an MP4 (H.264 / AAC, playable everywhere); it is fetched in 16 MB chunks, four at a
//   time, which allows resuming after a network cut or after the app was closed. Outside Safari, the file
//   is a fragmented MP4: its chunks are fetched while the server is still preparing it.
// - The chunks, the poster and the subtitles go into the browser's storage (Cache API); the record and
//   the progress into IndexedDB. Nothing leaves the device.
// - Offline, playback positions are kept, then sent back to the server when the network returns.
import { del, get, post } from './api.js';
import { browserCaps } from './caps.js';
import { img } from './ui.js';
import { tr, locale } from './i18n.js';

const DB_NAME = 'nexis-offline';
const MEDIA = 'nexis-offline-media';
const CHUNK = 16 * 1024 * 1024;
const OLD_CHUNK = 8 * 1024 * 1024; // downloads started before the switch to 16 MB
const PARALLEL = 4;
// Safari (and any iPhone / iPad browser): plain MP4, fetched once ready
const progressiveOk = () => !/iphone|ipad|ipod/i.test(navigator.userAgent) && !(/safari/i.test(navigator.userAgent) && !/chrome|chromium|crios|android|fxios|edg/i.test(navigator.userAgent));
// what THIS device can play (the kept file is only played on it): the server only converts the rest
export const deviceCaps = () => { const c = browserCaps(); return { video: c.video, audio: c.audio, containers: ['mp4'] }; };

let dbPromise = null;
function db() {
  dbPromise = dbPromise || new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore('downloads', { keyPath: 'key' });
      req.result.createObjectStore('progress', { keyPath: 'key' });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbPromise;
}
async function tx(store, mode, fn) {
  const d = await db();
  return new Promise((resolve, reject) => {
    const t = d.transaction(store, mode);
    const out = fn(t.objectStore(store));
    t.oncomplete = () => resolve(out && 'result' in out ? out.result : out);
    t.onerror = () => reject(t.error);
  });
}
const all = (store) => tx(store, 'readonly', (s) => s.getAll());
const put = (store, value) => tx(store, 'readwrite', (s) => s.put(value));
const remove = (store, key) => tx(store, 'readwrite', (s) => s.delete(key));
const one = (store, key) => tx(store, 'readonly', (s) => s.get(key));

export const supported = () => 'indexedDB' in window && 'caches' in window && window.isSecureContext;

// ------------------------------------------------------------------ who is watching (also offline)
const ME_KEY = 'nexis.offline.me';
export function rememberMe(me) {
  try { localStorage.setItem(ME_KEY, JSON.stringify({ user: me.user, prefs: me.prefs, libraries: [], features: {} })); } catch { /* ignore */ }
}
export function forgetMe() {
  try { localStorage.removeItem(ME_KEY); } catch { /* ignore */ }
}
export function rememberedMe() {
  try { return JSON.parse(localStorage.getItem(ME_KEY) || 'null'); } catch { return null; }
}

// ------------------------------------------------------------------ subscription (pages that show the state)
const listeners = new Set();
export function onDownloads(fn) { listeners.add(fn); return () => listeners.delete(fn); }
const notify = () => listeners.forEach((fn) => { try { fn(); } catch (e) { console.warn(e); } });

// ------------------------------------------------------------------ installed app: keep going in the background
// Android freezes an app that is left: while a download runs, the Noxa app declares itself
// "at work" ("Noxa is downloading…" notification with the progress) and the transfer goes on, screen off
// included. Computer: no sleep during a download.
const busy = new Map(); // downloads in progress (key -> record)
let toldActive = false;
let tellTimer = null;
const nativeDownloads = () => (window.NoxaAndroid && window.NoxaAndroid.downloads ? (j) => window.NoxaAndroid.downloads(j)
  : window.noxaNative && window.noxaNative.downloads ? (j) => window.noxaNative.downloads(j) : null);
const NET_WAIT = 30 * 60 * 1000; // network lost: the app stays awake 30 min to try again
const isBusy = (rec) => ['queued', 'preparing', 'downloading'].includes(rec.state)
  || (rec.state === 'paused' && Date.now() - (rec.pausedAt || 0) < NET_WAIT);
function track(rec, gone = false) {
  if (!gone && isBusy(rec)) busy.set(rec.key, rec); else busy.delete(rec.key);
  tellNative();
}
function tellNative() {
  const send = nativeDownloads();
  if (!send) return;
  const active = busy.size > 0;
  if (!active && !toldActive) return; // nothing running, nothing announced
  if (active === toldActive && tellTimer) return; // next send already planned (at most one per second)
  const now = () => {
    tellTimer = null;
    const list = [...busy.values()];
    toldActive = list.length > 0;
    const cur = list.find((d) => d.state === 'downloading') || list.find((d) => d.state === 'preparing') || list[0];
    const it = cur ? cur.item : null;
    const pct = !cur ? 0 : cur.state === 'downloading' ? (cur.chunks ? cur.got / cur.chunks : 0) : cur.state === 'preparing' ? (cur.prep || 0) : 0;
    try {
      send(JSON.stringify({ active: list.length,
        title: it ? (it.type === 'episode' ? `${it.show || ''} · ${it.title || ''}` : it.title || '') : '',
        state: cur ? cur.state : '', percent: Math.round(pct * 100),
        text: cur ? (cur.state === 'preparing' ? tr('Préparation sur le serveur…') : cur.state === 'downloading' ? tr('Téléchargement…')
          : cur.state === 'paused' ? tr('En attente du réseau…') : tr('En attente')) : '' }));
    } catch { /* ancienne application */ }
  };
  if (active !== toldActive) { clearTimeout(tellTimer); now(); return; } // start or end: right away
  tellTimer = setTimeout(now, 1000);
}

const keyOf = (uid, rk) => `${uid}:${rk}`;
const mediaUrl = (key, part) => `/offline/${key.replace(':', '/')}/${part}`;

export async function list(uid) {
  if (!supported()) return [];
  return (await all('downloads')).filter((d) => d.uid === uid)
    .sort((a, b) => (a.item.show || a.item.title).localeCompare(b.item.show || b.item.title, locale())
      || (a.item.season || 0) - (b.item.season || 0) || (a.item.episode || 0) - (b.item.episode || 0));
}
export const find = (uid, rk) => (supported() ? one('downloads', keyOf(uid, rk)) : Promise.resolve(null));

// ------------------------------------------------------------------ requesting a download
// opts: { quality, audio (id), subs: [ids of the text subtitles to keep], burn (id of a picture subtitle) }
export async function download(uid, item, opts) {
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {}); // no eviction by the browser
  const key = keyOf(uid, item.id);
  const existing = await one('downloads', key);
  if (existing && existing.state !== 'error') return existing;
  const subs = (item.subtitles || []).filter((s) => s.overlay && (opts.subs || []).includes(s.id));
  const rec = {
    key, uid, rk: String(item.id), item: { ...item, subtitles: subs.map((s) => ({ ...s, external: true })),
      audio: (item.audio || []).filter((a) => a.id === opts.audio).slice(0, 1) },
    opts, state: 'queued', job: null, size: 0, got: 0, chunks: 0, prep: 0, error: null, savedAt: Date.now(),
  };
  await put('downloads', rec);
  notify();
  pump();
  return rec;
}

// ------------------------------------------------------------------ queue: one preparation / one transfer at a time
let running = false;
// Network cut (unstable Wi-Fi, app sent to the background by the system…): retried automatically,
// more and more spaced out (10 s, 20 s, 40 s, then every minute), and right away when the connection returns or
// Noxa comes back to the foreground. No need to reopen the Downloads page anymore.
let resumeTimer = null;
let resumeDelay = 0;
function scheduleResume() {
  clearTimeout(resumeTimer);
  resumeDelay = Math.min(resumeDelay ? resumeDelay * 2 : 10000, 60000);
  resumeTimer = setTimeout(() => { resumeTimer = null; pump(); }, resumeDelay);
}
function resumeNow() {
  if (!resumeTimer) return; // nothing waiting
  clearTimeout(resumeTimer);
  resumeTimer = null;
  resumeDelay = 0;
  pump();
}
window.addEventListener('online', resumeNow);
document.addEventListener('visibilitychange', () => { if (!document.hidden) resumeNow(); });

// does the server answer? (checked before declaring the connection lost)
async function reachable() {
  try {
    const r = await fetch('/api/status', { cache: 'no-store', headers: { 'X-Noxa': '1' }, signal: AbortSignal.timeout(10000) });
    return r.status < 500;
  } catch { return false; }
}
// passing error (network, chunk cut, server or proxy briefly unavailable): to retry, not an error
const transient = (e) => !navigator.onLine || e instanceof TypeError || e.name === 'AbortError' || e.name === 'TimeoutError'
  || e.status === 0 || e.status >= 500 || e.incomplete || /^HTTP 5\d\d$/.test(e.message);
// small retries in place: up to ~30 s waiting for the server to answer again
async function waitNetwork() {
  for (const ms of [1000, 2000, 4000, 8000, 15000]) {
    await new Promise((ok) => setTimeout(ok, ms));
    if (await reachable()) return true;
  }
  return false;
}
const retries = new Map(); // key -> { got, n }: retries without progress (avoids going round in circles)

export async function pump() {
  if (running || !supported()) return;
  if (!navigator.onLine) { if ((await all('downloads')).some((d) => d.state === 'paused')) scheduleResume(); return; }
  running = true;
  let stalled = false;
  try {
    await runSmart();
    for (;;) {
      const pending = (await all('downloads')).filter((d) => ['queued', 'preparing', 'downloading', 'paused'].includes(d.state));
      const next = pending[0];
      if (!next) break;
      // the next two: preparation requested right away, the server moves on during this transfer
      for (const later of pending.slice(1, 3)) if (!later.job) requestJob(later).catch(() => {});
      const ok = await work(next);
      if (!ok) { stalled = true; break; } // network cut: new automatic try
      resumeDelay = 0;
    }
  } finally {
    running = false;
    if (stalled) scheduleResume();
    // queue finished, or network missing for more than 30 min: the app can go back to sleep
    for (const [k, d] of busy) if (!isBusy(d) || (!stalled && d.state === 'paused')) busy.delete(k);
    tellNative();
  }
}

async function save(rec, patch) {
  Object.assign(rec, patch);
  await put('downloads', rec);
  track(rec);
  notify();
}

async function requestJob(rec) {
  const job = await post('/api/downloads', { id: rec.rk, quality: rec.opts.quality, audio: rec.opts.audio ?? null,
    burn: rec.opts.burn ?? null, caps: deviceCaps(), progressive: progressiveOk() });
  await save(rec, { job: job.id, gen: job.gen, state: rec.state === 'queued' ? 'preparing' : rec.state, got: 0, chunks: 0, chunk: CHUNK });
  return job;
}

// chunks [rec.got, upto): four at a time; rec.got moves over those received in a row (exact resume)
async function transfer(rec, upto, cache) {
  const size = rec.chunk || (rec.got ? OLD_CHUNK : CHUNK);
  if (!rec.chunk) await save(rec, { chunk: size });
  const done = new Set();
  let next = rec.got;
  let failure = null;
  const fetchChunk = async (n) => {
    const start = n * size;
    const end = Math.min(rec.size || Infinity, start + size) - 1;
    const res = await fetch(`/api/downloads/${rec.job}/file`, { headers: { Range: `bytes=${start}-${end}`, 'X-Noxa': '1' },
      credentials: 'same-origin' });
    if (res.status === 404) { const e = new Error('gone'); e.gone = true; throw e; }
    if (res.status !== 206 && res.status !== 200) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    if (blob.size !== end - start + 1) { const e = new Error('morceau incomplet'); e.incomplete = true; throw e; } // connection cut
    await cache.put(mediaUrl(rec.key, `chunk-${n}`), new Response(blob));
    done.add(n);
    let got = rec.got;
    while (done.has(got)) got += 1;
    if (got !== rec.got) await save(rec, { got });
  };
  const lane = async () => {
    while (!failure && next < upto) {
      const n = next++;
      try { await fetchChunk(n); } catch (e) { failure = failure || e; }
    }
  };
  await Promise.all(Array.from({ length: Math.min(PARALLEL, Math.max(1, upto - rec.got)) }, lane));
  if (failure) throw failure;
}

async function resetChunks(rec, cache, patch) {
  for (let n = 0; n < Math.max(rec.chunks || 0, rec.got || 0) + PARALLEL; n++) await cache.delete(mediaUrl(rec.key, `chunk-${n}`));
  await save(rec, { got: 0, chunks: 0, chunk: CHUNK, ...patch });
}

async function work(rec) {
  const cache = await caches.open(MEDIA);
  try {
    if (rec.unstable) await save(rec, { unstable: false }); // left over from a cut (app closed meanwhile)
    if (!rec.posterSaved) {
      const art = rec.item.type === 'episode' ? (rec.item.thumb || rec.item.show_thumb) : rec.item.thumb;
      if (art) {
        const res = await fetch(img(art, 400, rec.item.type === 'episode' ? 225 : 600));
        if (res.ok) await cache.put(mediaUrl(rec.key, 'poster'), res);
      }
      for (const s of rec.item.subtitles) {  // text subtitles: the lines, already converted by the server
        let r = await get(`/api/items/${rec.rk}/subtitles/${s.id}`);
        for (let i = 0; i < 90 && r && (r.pending || r.partial); i++) {  // still being extracted on the server
          await new Promise((ok) => setTimeout(ok, 2000));
          r = await get(`/api/items/${rec.rk}/subtitles/${s.id}`);
        }
        await cache.put(mediaUrl(rec.key, `subs-${s.id}`), new Response(JSON.stringify({ cues: r.cues || [] }),
          { headers: { 'Content-Type': 'application/json' } }));
      }
      await save(rec, { posterSaved: true });
    }
    // 1. preparation on the server; the parts already written are fetched meanwhile (fragmented MP4)
    for (;;) {
      let job;
      try {
        job = rec.job ? await get(`/api/downloads/${rec.job}`) : null;
      } catch (e) {
        if (e.status !== 404) throw e;
        job = null; // server restarted: ask again
      }
      if (!job) {
        if (rec.got) await resetChunks(rec, cache, {});
        job = await requestJob(rec);
      }
      if (job.state === 'error') { await save(rec, { state: 'error', error: job.error }); return true; }
      if (job.gen !== undefined && rec.gen !== undefined && job.gen !== rec.gen) {
        await resetChunks(rec, cache, { gen: job.gen }); // new try on the server: start again from zero
      }
      if (job.state === 'ready') {
        if (!rec.chunk) await save(rec, { chunk: rec.got ? OLD_CHUNK : CHUNK });
        if (!rec.chunks || rec.size !== job.size) await save(rec, { size: job.size, chunks: Math.ceil(job.size / rec.chunk) });
        break;
      }
      await save(rec, { state: 'preparing', prep: job.progress || 0, waiting: !!job.waiting, convert: job.mode === 'transcode', gen: job.gen });
      if (job.progressive && job.ready) {
        const whole = Math.floor(job.ready / (rec.chunk || CHUNK)); // chunks fully written
        if (whole > rec.got) {
          try { await transfer(rec, whole, cache); } catch (e) {
            if (!e.gone && !/HTTP 409/.test(e.message)) throw e; // 409: caught up with the preparation, wait
          }
          continue;
        }
      }
      await new Promise((r) => setTimeout(r, 2500));
      const fresh = await one('downloads', rec.key);
      if (!fresh) return true; // deleted meanwhile
    }
    // 2. transfer of what is left (resumes where it stopped)
    await save(rec, { state: 'downloading' });
    try {
      await transfer(rec, rec.chunks, cache);
    } catch (e) {
      if (e.gone) { await save(rec, { job: null, state: 'queued', got: 0 }); return true; }
      throw e;
    }
    if (!(await one('downloads', rec.key))) return true; // deleted meanwhile
    del(`/api/downloads/${rec.job}`).catch(() => {}); // the server can erase its copy
    await save(rec, { state: 'done', job: null, doneAt: Date.now() });
    return true;
  } catch (e) {
    if (e.name === 'QuotaExceededError') {
      const app = window.noxaNative || window.NoxaAndroid; // app: no permission to give, the device is full
      await save(rec, { state: 'error', error: app ? tr('Plus de place sur l’appareil. Supprimez un téléchargement ou libérez de l’espace.')
        : tr('Espace de stockage épuisé. Autorisez plus d’espace ou supprimez un téléchargement.') });
      return true;
    }
    if (transient(e)) {
      // the server answers again (short cut): resume right away, where it was
      const r = retries.get(rec.key);
      const n = r && r.got === rec.got ? r.n + 1 : 0;
      retries.set(rec.key, { got: rec.got, n });
      if (n < 6) {
        await save(rec, { unstable: true }); // shown: "Unstable connection: trying again…"
        const back = await waitNetwork();
        await save(rec, { unstable: false });
        if (back) return true;
      }
      retries.delete(rec.key);
      await save(rec, { state: 'paused', pausedAt: rec.state === 'paused' ? rec.pausedAt : Date.now() });
      return false;
    }
    await save(rec, { state: 'error', error: e.message });
    return true;
  }
}

export async function removeDownload(key) {
  const rec = await one('downloads', key);
  if (!rec) return;
  await remove('downloads', key);
  track(rec, true);
  if (rec.job) del(`/api/downloads/${rec.job}`).catch(() => {});
  const cache = await caches.open(MEDIA);
  const prefix = mediaUrl(key, '');
  for (const req of await cache.keys()) {
    if (new URL(req.url).pathname.startsWith(prefix)) await cache.delete(req);
  }
  notify();
}

// several at once (selection on the Downloads page): the storage is only scanned once
export async function removeMany(keys) {
  const wanted = new Set(keys);
  const prefixes = [];
  for (const key of wanted) {
    const rec = await one('downloads', key);
    if (!rec) continue;
    await remove('downloads', key);
    track(rec, true);
    if (rec.job) del(`/api/downloads/${rec.job}`).catch(() => {});
    prefixes.push(mediaUrl(key, ''));
  }
  const cache = await caches.open(MEDIA);
  for (const req of await cache.keys()) {
    const path = new URL(req.url).pathname;
    if (prefixes.some((p) => path.startsWith(p))) await cache.delete(req);
  }
  notify();
  return prefixes.length;
}

export async function retry(key) {
  const rec = await one('downloads', key);
  if (!rec) return;
  await save(rec, { state: 'queued', error: null, job: null, got: 0 });
  pump();
}

// ------------------------------------------------------------------ lecture hors ligne
export async function playable(uid, rk) {
  const rec = await one('downloads', keyOf(uid, rk));
  if (!rec || rec.state !== 'done') return null;
  const cache = await caches.open(MEDIA);
  const parts = [];
  for (let n = 0; n < rec.chunks; n++) {
    const res = await cache.match(mediaUrl(rec.key, `chunk-${n}`));
    if (!res) return null; // erased by the browser (not enough space)
    parts.push(await res.blob());
  }
  const urls = [];
  const blobUrl = (b) => { const u = URL.createObjectURL(b); urls.push(u); return u; };
  const subUrls = {};
  for (const s of rec.item.subtitles) {
    const res = await cache.match(mediaUrl(rec.key, `subs-${s.id}`));
    if (res) subUrls[s.id] = blobUrl(await res.blob());
  }
  const posterRes = await cache.match(mediaUrl(rec.key, 'poster'));
  return {
    rec,
    item: rec.item,
    url: blobUrl(new Blob(parts, { type: 'video/mp4' })),
    subUrls,
    poster: posterRes ? blobUrl(await posterRes.blob()) : null,
    release: () => urls.forEach((u) => URL.revokeObjectURL(u)),
  };
}

export async function posterUrl(rec) {
  const res = await (await caches.open(MEDIA)).match(mediaUrl(rec.key, 'poster'));
  return res ? URL.createObjectURL(await res.blob()) : null;
}

// Next episode… among the downloads
export async function nextDownloaded(uid, item) {
  if (item.type !== 'episode') return null;
  const eps = (await list(uid)).filter((d) => d.state === 'done' && d.item.show_id === item.show_id);
  const after = eps.filter((d) => (d.item.season - item.season || d.item.episode - item.episode) > 0);
  return after.length ? after[0].item : null;
}

// ------------------------------------------------------------------ smart downloads (device setting)
// A downloaded episode watched to the end: the next episode is downloaded (same languages, same quality), and,
// when asked, the watched episode is deleted. Done when the network returns if it was watched offline.
const SMART_KEY = 'nexis.dl.smart';
const SMART_QUEUE = 'nexis.dl.smartQueue';
const readLS = (k, d) => { try { return JSON.parse(localStorage.getItem(k) || 'null') ?? d; } catch { return d; } };
const writeLS = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* stockage indisponible */ } };
export const smartPrefs = () => ({ next: true, clean: false, ...readLS(SMART_KEY, {}) });
export const setSmart = (patch) => writeLS(SMART_KEY, { ...smartPrefs(), ...patch });

function matchTracks(rec, full) {
  const prev = (rec.item.audio || [])[0];
  const audio = (prev && (full.audio || []).find((a) => a.lang === prev.lang)) || null;
  const langs = new Set((rec.item.subtitles || []).map((t) => t.lang));
  const subs = (full.subtitles || []).filter((t) => t.overlay && langs.has(t.lang)).map((t) => t.id);
  return { audio: audio ? audio.id : null, subs, burn: null };
}

async function smartAfter(uid, item) {
  const s = smartPrefs();
  if (item.type !== 'episode' || (!s.next && !s.clean)) return;
  const q = readLS(SMART_QUEUE, []);
  if (!q.some((x) => x.key === keyOf(uid, item.id))) q.push({ key: keyOf(uid, item.id), uid });
  writeLS(SMART_QUEUE, q);
  if (navigator.onLine) pump();
}

let smartRunning = false;
async function runSmart() {
  const q = readLS(SMART_QUEUE, []);
  if (smartRunning || !q.length || !navigator.onLine) return;
  smartRunning = true;
  const left = [];
  try {
    writeLS(SMART_QUEUE, []);
    for (const x of q) {
      const rec = await one('downloads', x.key);
      if (!rec) continue;
      const s = smartPrefs();
      try {
        const nxt = rec.item.next;
        if (s.next && nxt && !nxt.watched && !(await one('downloads', keyOf(x.uid, nxt.id)))) {
          const full = await get(`/api/items/${nxt.id}`);
          await download(x.uid, full, { ...matchTracks(rec, full), quality: rec.opts.quality, smart: true });
        }
        if (s.clean) await removeDownload(x.key);
      } catch { left.push(x); } // network cut meanwhile: will try again
    }
  } finally {
    if (left.length) writeLS(SMART_QUEUE, [...readLS(SMART_QUEUE, []), ...left]);
    smartRunning = false;
  }
}

// ------------------------------------------------------------------ offline playback positions
export async function saveProgress(uid, item, position, duration, ended) {
  await put('progress', { key: keyOf(uid, item.id), id: String(item.id), uid, position, duration, ended: !!ended, at: Math.floor(Date.now() / 1000) });
  const rec = await one('downloads', keyOf(uid, item.id));
  if (rec) await put('downloads', { ...rec, item: { ...rec.item, position: ended ? 0 : position, watched: rec.item.watched || !!ended } });
  if (rec && ended) smartAfter(uid, rec.item);
  syncProgress(uid);
}

let syncing = false;
export async function syncProgress(uid) {
  if (syncing || !navigator.onLine || !supported()) return;
  syncing = true;
  try {
    const pending = (await all('progress')).filter((p) => p.uid === uid);
    if (!pending.length) return;
    await post('/api/progress/offline', { items: pending });
    await Promise.all(pending.map((p) => remove('progress', p.key)));
  } catch { /* will try again */ } finally {
    syncing = false;
  }
}

// Space the BROWSER grants Noxa (this is not the phone's free space) and persistent storage
export async function usage() {
  try {
    const [est, persisted] = await Promise.all([navigator.storage.estimate(), navigator.storage.persisted ? navigator.storage.persisted() : false]);
    return { ...est, persisted };
  } catch { return null; }
}

// Asks the browser to keep the downloads (and, on Firefox, to lift the 10 GB limit)
export async function askPersist() {
  try { return navigator.storage && navigator.storage.persist ? await navigator.storage.persist() : false; } catch { return false; }
}
