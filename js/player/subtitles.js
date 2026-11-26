// Subtitles shown by the browser: text cues (drawn by the player), ASS / SSA rendered by libass
// (WebAssembly) and PGS pictures rendered by libpgs. Tracks still being extracted on the server are
// served in part and reloaded regularly until they are complete.
import { get } from '../api.js';
import { h } from '../ui.js';
import { tr } from '../i18n.js';

const SUBS_REFRESH = 20000;
const SUBS_MAX_WAIT = 120000;

// The ASS engine is loaded once per page, the first time a track needs it.
let JassubClass;
async function jassubClass() {
  if (JassubClass === undefined) {
    try {
      if (typeof WebAssembly !== 'object' || typeof Worker !== 'function') throw new Error(tr('WebAssembly indisponible'));
      JassubClass = (await import('/vendor/jassub/jassub.es.js')).default;
    } catch (e) {
      console.warn('[noxa] moteur ASS indisponible, sous-titres simplifiés', e.message);
      JassubClass = null;
    }
  }
  return JassubClass;
}

const partialOf = (res) => {
  const v = res.headers.get('X-Noxa-Partial');
  return v === null ? null : Number(v);
};

// host: { video, subLayer, subTop, subsPending, session(), base(), seq(), destroyed() }
export function createSubtitles(host) {
  const { video, subLayer, subTop, subsPending } = host;
  const live = (seq) => seq === host.seq() && !host.destroyed();
  let cues = [], cueKey = '', cueMax = 0;
  let jassub = null, jassubKey = '', pgs = null, pgsKey = '';
  // ready up to `until` (ms) while the track is still being extracted, null once complete
  let until = null, refresh = null;

  function keepRefreshing(seq, fn) {
    clearTimeout(refresh);
    refresh = setTimeout(() => { if (live(seq)) fn(seq); }, SUBS_REFRESH);
  }

  // ------------------------------------------------------------ ASS / SSA (libass)
  function dropJassub() {
    if (jassub) { try { jassub.destroy(); } catch { /* already destroyed */ } }
    jassub = null;
    jassubKey = '';
  }
  const sync = () => { if (jassub) jassub.timeOffset = host.base() / 1000; };

  async function refreshAss(seq) {
    try {
      const res = await fetch(host.session().subtitle_ass, { credentials: 'same-origin' });
      if (!res.ok || seq !== host.seq() || !jassub) return;
      const content = await res.text();
      jassub.setTrack(content);
      until = partialOf(res);
    } catch { /* try again next round */ }
    if (until !== null) keepRefreshing(seq, refreshAss);
  }

  async function loadAss(seq) {
    const url = host.session().subtitle_ass;
    if (jassub && jassubKey === url) {
      sync();
      if (until !== null) keepRefreshing(seq, refreshAss);
      return true;
    }
    dropJassub();
    const J = await jassubClass();
    if (!J || !live(seq)) return false;
    try {
      const [res, fonts] = await Promise.all([fetch(url, { credentials: 'same-origin' }),
        get(`${url.replace(/\/ass$/, '')}/fonts`).catch(() => ({ fonts: [] }))]);
      if (!res.ok) throw new Error(`script ASS ${res.status}`);
      const content = await res.text();
      const partial = partialOf(res);
      if (!live(seq)) return false;
      const inst = new J({
        video, subContent: content, timeOffset: host.base() / 1000,
        workerUrl: '/vendor/jassub/jassub-worker.js', wasmUrl: '/vendor/jassub/jassub-worker.wasm',
        modernWasmUrl: '/vendor/jassub/jassub-worker-modern.wasm',
        availableFonts: { 'liberation sans': '/vendor/jassub/default.woff2' }, fallbackFont: 'liberation sans',
        fonts: fonts.fonts || [], useLocalFonts: false, prescaleHeightLimit: 1080, maxRenderHeight: 2160,
      });
      const ok = await new Promise((resolve) => {
        const t = setTimeout(() => resolve(false), 15000);
        inst.addEventListener('ready', () => { clearTimeout(t); resolve(true); }, { once: true });
        inst.addEventListener('error', (e) => { clearTimeout(t); console.warn('[noxa] libass', e.error || e.message); resolve(false); }, { once: true });
      });
      if (!ok || !live(seq)) { try { inst.destroy(); } catch { /* */ } return false; }
      jassub = inst;
      jassubKey = url;
      sync();
      until = partial;
      if (partial !== null) keepRefreshing(seq, refreshAss);
      return true;
    } catch (e) {
      console.warn('[noxa] sous-titres ASS', e.message);
      return false;
    }
  }

  // ------------------------------------------------------------ PGS (Blu-ray), pictures rendered by libpgs
  function dropPgs() {
    if (pgs) { try { pgs.dispose(); } catch { /* */ } if (pgs.ownCanvas) pgs.ownCanvas.remove(); }
    pgs = null;
    pgsKey = '';
  }
  async function fetchSup() {
    const res = await fetch(host.session().subtitle_pgs, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`PGS ${res.status}`);
    const buf = await res.arrayBuffer();
    return { buf, until: partialOf(res) };
  }
  async function refreshPgs(seq) {
    try {
      const data = await fetchSup();
      if (seq !== host.seq() || !pgs) return;
      await pgs.loadFromBuffer(data.buf);
      until = data.until;
    } catch { /* next round */ }
    if (until !== null) keepRefreshing(seq, refreshPgs);
  }
  async function loadPgs(seq) {
    const url = host.session().subtitle_pgs;
    if (pgs && pgsKey === url) {
      if (until !== null) keepRefreshing(seq, refreshPgs);
      return true;
    }
    dropPgs();
    try {
      const [{ PgsRenderer }, data] = await Promise.all([import('/vendor/libpgs/libpgs.js'), fetchSup()]);
      if (!live(seq)) return false;
      const canvas = h('canvas.pgs-layer');
      video.after(canvas);  // under the controls, above the picture
      const r = new PgsRenderer({ video, canvas, workerUrl: '/vendor/libpgs/libpgs.worker.js', aspectRatio: 'contain' });
      r.ownCanvas = canvas;
      pgs = r;
      pgsKey = url;
      await Promise.race([r.loadFromBuffer(data.buf), new Promise((res) => setTimeout(res, 20000))]);
      until = data.until;
      if (data.until !== null) keepRefreshing(seq, refreshPgs);
      return true;
    } catch (e) {
      console.warn('[noxa] sous-titres PGS', e.message);
      dropPgs();
      return false;
    }
  }

  // ------------------------------------------------------------ the track of the current stream
  // Subtitles are normally already ready (the player prepares them first); safety net for external tracks.
  async function load(seq) {
    const session = host.session();
    clearTimeout(refresh);
    until = null;
    subsPending.hidden = true;
    cues = [];
    cueKey = '';
    subLayer.replaceChildren();
    subTop.replaceChildren();
    if (!session.subtitle_pgs) dropPgs();
    if (session.subtitle_pgs) { await loadPgs(seq); dropJassub(); return; }
    if (session.subtitle_ass && await loadAss(seq)) return;
    if (!live(seq)) return;
    if (!session.subtitle_ass) dropJassub();
    if (!session.subtitle_overlay) return;
    const url = session.subtitle_overlay;
    return new Promise((resolve) => {
      let released = false;
      const release = () => { if (!released) { released = true; resolve(); } };
      const timer = setTimeout(release, SUBS_MAX_WAIT);
      const poll = async () => {
        if (!live(seq)) { clearTimeout(timer); release(); return; }
        try {
          const r = await get(url);
          if (!live(seq)) { clearTimeout(timer); release(); return; }
          if (r && r.pending) { setTimeout(poll, 800); return; }
          cues = r.cues || [];
          cueKey = '';
          cueMax = cues.reduce((m, c) => Math.max(m, c.e - c.s), 0);
          until = r.partial ? r.until : null;
          if (r.partial) keepRefreshing(seq, poll);  // the rest arrives during playback
        } catch (e) {
          console.warn('[noxa] sous-titres', e.message);
        }
        clearTimeout(timer);
        release();
      };
      poll();
    });
  }

  // Text cues at position p (ms): all the lines shown at the same time (dialogue + sign, two characters…)
  function paint(p) {
    if (!cues.length) return;
    let lo = 0, hi = cues.length - 1, found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (cues[mid].s <= p) { found = mid; lo = mid + 1; } else hi = mid - 1;
    }
    const active = [];
    for (let i = found; i >= 0 && p - cues[i].s <= cueMax && active.length < 8; i--) if (p <= cues[i].e) active.unshift(i);
    const key = active.join(',');
    if (key === cueKey) return;
    cueKey = key;
    const line = (i) => h('div', h('span.sub-line', cues[i].t));
    subLayer.replaceChildren(...active.filter((i) => !cues[i].top).map(line));
    subTop.replaceChildren(...active.filter((i) => cues[i].top).map(line));
  }

  function stop() {
    clearTimeout(refresh);
    dropJassub();
    dropPgs();
  }

  return {
    load, paint, sync, stop,
    // position (ms) up to which the track is ready while it is still being extracted, null once complete
    get until() { return until; },
  };
}
