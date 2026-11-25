// Web player: HLS stream proxied through Noxa, custom controls, resume,
// per-user track preferences, "skip intro" banner and next-episode countdown.
import { applySubStyle } from './substyle.js';
import { deviceOverrides } from './device.js';
import { get, post, put } from './api.js';
import { SIGNS_HINT, fixForced, menuSubs, pickAudio, pickSubtitle, trackPayload, withLibLangs } from './tracks.js';
import { state } from './app.js';
import { browserCaps } from './caps.js';
import { moveFocus, remoteMode } from './remote.js';
import { nextDownloaded, playable, saveProgress } from './offline.js';
import { chapterStrip, chapterTitle } from './chapters.js';
import { clear, episodeCode, errorBox, fmtTime, h, icon, img, loadHls, spinner, store, toast } from './ui.js';
import { tr } from './i18n.js';
import { createSubtitles } from './player/subtitles.js';
import { createTogether } from './player/together.js';
import { createDevices } from './player/devices.js';
import { measureBandwidth, saveBandwidth } from './bandwidth.js';
import { actionFor } from './keys.js';
import { AIRPLAY_CAPS } from './cast.js';

const REPORT_EVERY = 10000;
const HIDE_AFTER = 3000;
// Seconds of video to have buffered before starting / resuming after a stall.
// An on-the-fly conversion produces barely faster than real time: get ahead.
const PREROLL = { direct: 5, copy: 12, transcode: 20 };
const PREROLL_SEEK = { direct: 3, copy: 6, transcode: 10 }; // after a seek: shorter buffer
const REBUFFER = { direct: 6, copy: 15, transcode: 25 };
const PREPARE_MAX = 10 * 60 * 1000;


// Playback speed and sleep timer: kept from one episode to the next (the player is recreated)
const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2];
let speed = 1;
let speedChosen = false; // speed chosen in the player: kept from one episode to the next, otherwise the profile's
const sleep = { at: 0, endOfItem: false }; // at: instant of the pause (ms); endOfItem: at the end of the video

export async function renderPlayer(root, rk, ctx) {
  const hlsReady = loadHls(); // in parallel with loading the title page
  const prefs = { ...state.me.prefs, ...deviceOverrides() }; // device settings take priority
  const BACK = (Number(prefs.skip_back) || 10) * 1000, FWD = (Number(prefs.skip_forward) || 30) * 1000; // profile's skips
  const isAdmin = state.me.user.is_admin;
  const video = h('video.video', { playsinline: true, preload: 'auto', 'x-webkit-airplay': 'allow' });
  const subLayer = h('div.sub-layer', { style: { fontSize: `${(prefs.sub_size / 100) * 2.4}vmin` } });
  const subTop = h('div.sub-layer.sub-top', { style: { fontSize: `${(prefs.sub_size / 100) * 2.4}vmin` } });
  const subsPending = h('div.subs-pending', { hidden: true }, tr('Sous-titres en préparation…'));
  const loader = h('div.player-loader', spinner());
  const errorLayer = h('div.player-error', { hidden: true });
  const titleEl = h('div.player-title');
  const timeEl = h('span.time');
  const played = h('div.tl-played');
  const buffered = h('div.tl-buffered');
  const introMark = h('div.tl-intro', { hidden: true });
  const recapMark = h('div.tl-intro.tl-recap', { hidden: true });
  const previewImg = h('div.tl-preview-img', { hidden: true });
  const previewChapter = h('div.tl-preview-chapter');
  const previewTime = h('div.tl-preview-time');
  const hoverTip = h('div.tl-tip', { hidden: true }, previewImg, previewChapter, previewTime);
  const chapterMarks = h('div.tl-chapters');
  const timeline = h('div.timeline', { role: 'slider', 'aria-label': tr('Position'), tabindex: 0 },
    h('div.tl-track', buffered, recapMark, introMark, chapterMarks, played, h('div.tl-thumb')), hoverTip);
  const playBtn = h('button.ctl', { 'aria-label': tr('Lecture/Pause') }, icon('play'));
  const muteBtn = h('button.ctl', { 'aria-label': tr('Couper le son') }, icon('volume'));
  const volume = h('input.volume', { type: 'range', min: 0, max: 1, step: 0.05, 'aria-label': tr('Volume') });
  const nextBtn = h('button.ctl', { 'aria-label': tr('Épisode suivant'), hidden: true }, icon('next'));
  const subsBtn = h('button.ctl', { 'aria-label': tr('Audio et sous-titres') }, icon('subs'));
  const chaptersBtn = h('button.ctl', { 'aria-label': tr('Chapitres'), title: tr('Chapitres (C)'), hidden: true }, icon('chapters'));
  const groupCount = h('span.ctl-badge', { hidden: true });
  const groupBtn = h('button.ctl', { 'aria-label': tr('Regarder ensemble'), title: tr('Regarder ensemble') }, icon('group'), groupCount);
  const cogBtn = h('button.ctl', { 'aria-label': tr('Qualité') }, icon('cog'));
  const fullBtn = h('button.ctl', { 'aria-label': tr('Plein écran') }, icon('full'));
  const castBtn = h('button.ctl', { 'aria-label': tr('Diffuser sur un Chromecast'), title: tr('Diffuser sur un Chromecast'), hidden: true }, icon('cast'));
  const airplayBtn = h('button.ctl', { 'aria-label': tr('Diffuser avec AirPlay'), title: tr('Diffuser avec AirPlay'), hidden: true }, icon('airplay'));
  const castLayer = h('div.cast-layer', { hidden: true });
  const menu = h('div.player-menu', { hidden: true });
  const chapterPanel = h('div.chapter-panel', { hidden: true });
  const groupBanner = h('div.group-banner', { hidden: true });
  const skipLabel = h('span', tr('Passer l’intro'));
  const skipBtn = h('button.skip-btn', { hidden: true }, icon('skip'), skipLabel);
  let skipping = null; // marker the "Skip" button jumps over (intro or recap)
  const nextCard = h('div.next-card', { hidden: true });
  const backBtn = h('button.ctl.back', { 'aria-label': tr('Retour') }, icon('back'));
  const miniBtn = h('button.ctl', { 'aria-label': tr('Réduire le lecteur'), title: tr('Réduire (i) : continuer à naviguer') }, icon('pip'));
  // controls of the mini player (corner of the screen)
  const miniPlay = h('button.ctl', { 'aria-label': tr('Lecture/Pause') }, icon('pause'));
  const miniBar = h('div.mini-bar',
    h('button.ctl', { 'aria-label': tr('Agrandir le lecteur'), title: tr('Agrandir') }, icon('full')), miniPlay,
    h('button.ctl', { 'aria-label': tr('Fermer le lecteur'), title: tr('Arrêter la lecture') }, icon('close')));

  const controls = h('div.controls',
    timeline,
    h('div.ctl-row',
      playBtn,
      h('button.ctl', { 'aria-label': tr('Reculer de {n} secondes', { n: BACK / 1000 }), onclick: () => seekBy(-BACK) }, icon('rewind')),
      h('button.ctl', { 'aria-label': tr('Avancer de {n} secondes', { n: FWD / 1000 }), onclick: () => seekBy(FWD) }, icon('forward')),
      h('div.vol', muteBtn, volume), timeEl, h('div.grow'), nextBtn, groupBtn, chaptersBtn, subsBtn, castBtn, airplayBtn, cogBtn, fullBtn));
  const top = h('div.player-top', backBtn, titleEl, h('div.grow'), miniBtn);
  const player = h('div.player', video, subLayer, subTop, subsPending, loader, errorLayer, castLayer, top, groupBanner, skipBtn, nextCard,
    menu, chapterPanel, controls, miniBar);
  applySubStyle(player, prefs);
  clear(root, player);

  // ------------------------------------------------------------ state
  let item, session = null, hls = null, base = 0;
  let audioSel = null, subSel = null, quality = prefs.quality;
  let lastReport = 0, introSkipped = false, recapSkipped = false, nextTimer = null, nextDismissed = false, nextAtEnd = false, destroyed = false;
  let idleTimer = null, scrubbing = false, startedAt = 0;
  let switching = true, finished = false; // no progress reports while a stream is (re)starting or after the end
  let rebuffering = false, stalls = [], lowerHinted = false, everPlayed = false;
  let bandwidth = null; // measured bandwidth (kbit/s), for automatic quality
  let convSpeed = -1;   // speed of the server's conversion (x real time), -1: unknown
  let titleKbps = 0;    // bandwidth kept for this title only (server conversion too slow)
  let slowHinted = false;
  let airplay = false; // the video goes to an Apple TV: native stream, playable without the browser session

  // download played offline (#/offline/…): file kept on the device, nothing goes through the server
  const offline = !!ctx.offline;
  let local = null;
  // "Watch together": position shared by the group (see noxa/syncplay.py)
  const groupId = offline ? null : ctx.query.get('group');

  // mini player: it survives navigation, so it no longer depends on the page that opened it
  let mini = false, detached = false;
  const current = () => !destroyed && (detached || ctx.isCurrent());
  const duration = () => (session && session.duration) || (item && item.duration) || 0;
  const pos = () => base + Math.round((video.currentTime || 0) * 1000);
  const subs = createSubtitles({ video, subLayer, subTop, subsPending, session: () => session, base: () => base,
    seq: () => startSeq, destroyed: () => destroyed });
  const together = createTogether({ video, banner: groupBanner, button: groupBtn, count: groupCount, menu,
    openMenu: (kind) => openMenu(kind), item: () => item, session: () => session, switching: () => switching, seq: () => startSeq,
    pos, seekTo: (ms, fromGroup) => seekTo(ms, fromGroup), report: (st, force) => report(st, force),
    waitBuffer: (sec, maxMs, seq) => waitBuffer(sec, maxMs, seq), finish: () => { finished = true; }, paintPlay: () => paintPlay(),
    prerollSeek: (mode) => PREROLL_SEEK[mode] ?? 6 });

  // ------------------------------------------------------------ boot
  try {
    if (offline) {
      local = await playable(state.me.user.id, rk);
      if (!local) throw new Error(tr('Ce téléchargement n’est plus sur cet appareil.'));
      item = local.item;
    } else {
      item = await get(`/api/items/${rk}`);
    }
  } catch (e) {
    clear(root, h('div.center-page', errorBox(e, () => history.back())));
    return () => {};
  }
  if (destroyed || !current()) return () => {};
  if (!['movie', 'episode', 'extra'].includes(item.type)) { location.replace(`#/item/${rk}`); return () => {}; }
  document.title = `${item.type === 'episode' ? `${item.show} ${episodeCode(item)}` : item.title} · Noxa`;
  titleEl.append(h('strong', item.type === 'episode' ? item.show : item.title),
    item.type === 'episode' ? h('span', `${episodeCode(item)} · ${item.title}`) : (item.year ? h('span', String(item.year)) : null));
  const poster = item.type === 'episode' ? (item.show_art || item.art) : item.art;
  if (offline) video.poster = local.poster || '';
  else if (poster) video.poster = img(poster, 1280, 720);
  if (offline) { cogBtn.hidden = true; groupBtn.hidden = true; miniBtn.hidden = true; }

  const lp = withLibLangs(prefs, item);
  audioSel = pickAudio(item.audio || [], lp, item.track_prefs);
  subSel = pickSubtitle(item.subtitles || [], audioSel, lp, item.track_prefs);
  // choices made on the title page just before starting
  const qa = ctx.query.get('a'), qs = ctx.query.get('s');
  if (qa !== null) audioSel = (item.audio || []).find((a) => String(a.id) === qa) || audioSel;
  if (qs !== null) subSel = qs === '0' ? null : ((item.subtitles || []).find((x) => String(x.id) === qs) || subSel);
  subSel = fixForced(subSel, audioSel, item.subtitles || []);
  const chapters = (item.chapters || []).filter((c) => c.end > c.start);
  let trick = null;
  const paintChapters = () => {
    const d = duration();
    chapterMarks.replaceChildren(...(d ? chapters.filter((c) => c.start > 0).map((c) =>
      h('span.tl-chapter', { style: { left: `${(c.start / d) * 100}%` } })) : []));
  };
  chaptersBtn.hidden = chapters.length < 2;
  paintChapters();
  const loadTrick = async (again) => {
    try {
      const r = await get(`/api/items/${item.id}/trickplay`);
      if (r.ready) trick = r;
      else if (again && !destroyed) setTimeout(() => loadTrick(false), 60000);
    } catch { /* no preview */ }
  };
  if (!offline) loadTrick(true);
  const rememberTracks = () => offline ? null : put(`/api/items/${item.id}/tracks`, trackPayload(audioSel, subSel)).catch(() => {});
  let startAt = 0;
  if (ctx.query.get('t')) startAt = Number(ctx.query.get('t')) || 0;
  else if (ctx.query.get('resume') && item.position && !item.watched) startAt = item.position;
  if (groupId) {
    const g = await together.join(groupId);
    if (destroyed || !current()) return () => {};
    if (g) {
      if (g.item_id && g.item_id !== String(item.id)) { location.replace(`#/play/${g.item_id}?group=${groupId}`); return () => {}; }
      if (g.item_id) startAt = together.target(g);
      together.listen();
    }
  }
  together.paint();

  const savedVol = Number(store('seance.volume'));
  const keepVol = prefs.remember_volume !== false; // volume and mute kept (profile setting)
  video.volume = keepVol && Number.isFinite(savedVol) && store('seance.volume') !== null ? savedVol : 1;
  video.muted = keepVol && store('seance.muted') === '1';
  if (!speedChosen) speed = Number(prefs.default_speed) || 1;
  volume.value = video.volume;

  // ------------------------------------------------------------ streaming
  // The server can take a while to produce the first segment after a jump (CPU transcode,
  // burned-in ASS subtitles…): be much more patient than hls.js defaults.
  const slow = (firstByte, total, retries) => ({ default: {
    maxTimeToFirstByteMs: firstByte, maxLoadTimeMs: total,
    timeoutRetry: { maxNumRetry: retries, retryDelayMs: 1000, maxRetryDelayMs: 4000 },
    errorRetry: { maxNumRetry: retries, retryDelayMs: 1000, maxRetryDelayMs: 8000 },
  } });
  // HLS stream: complete VOD playlist, original timestamps -> currentTime = real position.
  const HLS_CONFIG = {
    maxBufferLength: 60, maxMaxBufferLength: 120, backBufferLength: 90,
    manifestLoadPolicy: slow(60000, 90000, 2),
    playlistLoadPolicy: slow(60000, 90000, 4),
    fragLoadPolicy: slow(60000, 120000, 6),
  };
  let startSeq = 0, autoRetries = 0, forceHls = false;

  async function start(offset) {
    const seq = ++startSeq;
    switching = true;
    finished = false;
    loader.hidden = false;
    errorLayer.hidden = true;
    const previous = session && session.stream;
    // in a group, reloading (language, burnt-in subtitles, quality) pauses the others until we are ready
    if (together.group && previous && !destroyed) together.send('buffering', { position: Math.round(offset), reason: 'reload' });
    if (hls) { hls.destroy(); hls = null; }
    if (video.getAttribute('src')) { video.removeAttribute('src'); video.load(); }
    let s;
    if (offline) {
      s = { mode: 'direct', url: local.url, offset: Math.round(offset), duration: item.duration, stream: null, reasons: [],
        markers: item.markers || {}, next: await nextDownloaded(state.me.user.id, item), audio: item.audio,
        subtitles: item.subtitles, subtitle_overlay: subSel ? local.subUrls[subSel.id] || null : null };
    } else try {
      if (quality === 'auto' && bandwidth === null) bandwidth = await measureBandwidth();
      if (destroyed || seq !== startSeq) return;
      convSpeed = -1;
      const kbps = titleKbps && (!bandwidth || titleKbps < bandwidth) ? titleKbps : bandwidth;
      s = await post('/api/play', { id: item.id, offset: Math.round(offset), quality, bandwidth: kbps || undefined, capped: (kbps && kbps === titleKbps && kbps !== bandwidth) || undefined,
        no_direct: forceHls || undefined,
        caps: airplay ? AIRPLAY_CAPS : browserCaps(), cast: airplay || undefined,
        playlist: ctx.query.get('playlist') || undefined, shuffle: ctx.query.get('shuffle') || undefined,
        cold: (isAdmin && ctx.query.get('cold') === '1') || undefined, // trying the copy on cold storage
        audio: audioSel ? audioSel.id : null, subtitle: subSel ? subSel.id : 0, replace: previous || undefined });
    } catch (e) { if (seq === startSeq) showError(e.message, true); return; }
    if (destroyed || seq !== startSeq) {
      // a newer seek (or leaving the player) superseded this one
      post('/api/stop', { stream: s.stream }).catch(() => {});
      return;
    }
    session = s;
    base = 0;
    introSkipped = false;
    recapSkipped = false;
    nextBtn.hidden = !session.next;
    updateIntroMark();
    // playback only starts once the subtitles are ready (no gap at the start of the episode)
    const subsReady = subs.load(seq);
    let autoplayed = false;
    const autoplay = async () => {
      if (autoplayed) return;
      autoplayed = true;
      await subsReady;
      if (seq !== startSeq || destroyed) return;
      // let the buffer fill before starting: no stall in the first seconds
      await waitBuffer((everPlayed ? PREROLL_SEEK : PREROLL)[session.mode] ?? 12, 90000, seq);
      if (seq !== startSeq || destroyed) return;
      if (together.group) {
        // in a group: say we are ready, playback starts when everyone is
        loader.hidden = true;
        switching = false;
        together.send('ready');
        together.apply(together.group.state);
        return;
      }
      video.play().catch(() => { loader.hidden = true; paintPlay(); });
    };
    await hlsReady;
    if (seq !== startSeq || destroyed) return;
    if (session.mode === 'direct') {
      // the browser plays the file as is
      video.src = session.url;
      video.addEventListener('loadedmetadata', () => {
        if (session.offset) video.currentTime = session.offset / 1000;
        autoplay();
      }, { once: true });
      video.addEventListener('error', () => {
        if (seq === startSeq && session.mode === 'direct' && !destroyed) {
          // format refused by this browser: switch to a converted stream
          forceHls = true;
          start(Math.max(session.offset, pos()));
        }
      }, { once: true });
    } else if (window.Hls && window.Hls.isSupported() && !airplay) {
      hls = new window.Hls({ ...HLS_CONFIG, startPosition: session.offset / 1000 });
      hls.on(window.Hls.Events.ERROR, (_, data) => {
        if (!data.fatal || seq !== startSeq) return;
        if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) { hls.recoverMediaError(); return; }
        console.warn('[noxa] erreur HLS', data.type, data.details, data.response && data.response.code);
        const at = pos() || session.offset;
        if (autoRetries < 2) {
          autoRetries += 1;
          toast(tr('Flux interrompu, reconnexion…'));
          start(at);
          return;
        }
        const code = data.response && data.response.code ? ` ${data.response.code}` : '';
        showError(tr('Le flux a été interrompu ({details}{code}).', { details: data.details, code }), true, at);
      });
      hls.on(window.Hls.Events.MANIFEST_PARSED, autoplay);
      hls.on(window.Hls.Events.FRAG_BUFFERED, () => { autoRetries = 0; });
      hls.loadSource(session.url);
      hls.attachMedia(video);
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = session.url;  // Safari : HLS natif
      video.addEventListener('loadedmetadata', () => {
        if (session.offset) video.currentTime = session.offset / 1000;
        autoplay();
      }, { once: true });
    } else {
      showError(tr('Ce navigateur ne lit pas le HLS et le lecteur hls.js est absent du serveur. Réinstaller Noxa corrige le problème.'));
      return;
    }
    startedAt = Date.now();
    setupMediaSession();
  }


  // ---------------------------------------------------------- video buffer
  const aheadSec = () => {
    const b = video.buffered, t = video.currentTime;
    for (let i = 0; i < b.length; i++) {
      if (t >= b.start(i) - 0.3 && t <= b.end(i)) return b.end(i) - t;
      // very start of a stream: the first range can start a little after the position
      if (b.start(i) >= t && b.start(i) - t < 1.5) return b.end(i) - t;
    }
    return 0;
  };
  const nearEnd = () => { const d = duration(); return d > 0 && pos() + aheadSec() * 1000 >= d - 5000; };
  function waitBuffer(target, maxMs, seq) {
    return new Promise((resolve) => {
      const t0 = Date.now();
      const tick = () => {
        if (destroyed || seq !== startSeq) { resolve(false); return; }
        const enough = aheadSec() >= target || nearEnd()
          || (session && session.mode === 'direct' && video.readyState >= 4 && aheadSec() >= Math.min(target, 2));
        if (enough || Date.now() - t0 > maxMs) { resolve(true); return; }
        setTimeout(tick, 250);
      };
      tick();
    });
  }

  // Stall during playback: pause long enough to build a real buffer again,
  // rather than restarting at the first second received and stalling again right away.
  let stallTimer = null;
  function onStall() {
    if (switching || video.seeking || rebuffering || finished || destroyed || !session || video.paused) return;
    if (together.group) {
      // in a group, a stall puts everyone on hold: only report it when it lasts
      if (together.quiet() || stallTimer) return;
      stallTimer = setTimeout(() => {
        stallTimer = null;
        if (!together.group || video.paused || video.readyState >= 3 || destroyed) return;
        rebufferNow();
      }, 2000);
      return;
    }
    rebufferNow();
  }
  function rebufferNow() {
    rebuffering = true;
    const seq = startSeq;
    const now = Date.now();
    stalls = stalls.filter((t) => now - t < 180000);
    stalls.push(now);
    video.pause();
    paintPlay();
    if (together.group) together.send('buffering');
    waitBuffer(REBUFFER[session.mode] ?? 15, 45000, seq).then(() => {
      if (!rebuffering) return; // the user took over meanwhile
      rebuffering = false;
      if (seq !== startSeq || destroyed) return;
      if (together.group) { paintPlay(); together.send('ready'); return; } // the group starts again when everyone is ready
      video.play().catch(() => paintPlay());
    });
    if (stalls.length >= 2 && quality === 'auto') {
      stepDown(); // automatic quality: step down one notch by itself (connection, or conversion too slow)
    } else if (stalls.length >= 2 && ['original', '1080p'].includes(quality) && !lowerHinted) {
      lowerHinted = true;
      suggestLowerQuality(session.mode === 'transcode');
    }
  }

  // Automatic quality and playback stuttering despite the measurement: one notch lower, and the measurement kept for
  // the next playbacks is lowered
  function stepDown() {
    if (!session) return;
    if (session.mode === 'transcode' && convSpeed > 0 && convSpeed < 1) {
      // it is the server that does not convert fast enough, not the connection: the bandwidth measurement stays valid
      // for the next playbacks; only this title goes down to 720p, no lower
      stalls = [];
      const speed = `×${convSpeed.toFixed(1).replace('.', ',')}`;
      if (['original', '1080p'].includes(session.auto_quality)) {
        titleKbps = Math.round((4000 + 192) * 1.35);
        toast(tr('Conversion trop lente sur le serveur ({speed}) : qualité réduite pour ce titre', { speed }));
        start(pos());
      } else if (!slowHinted) {
        slowHinted = true;
        toast(tr('Le serveur ne convertit pas cette vidéo assez vite ({speed})', { speed }));
      }
      return;
    }
    if (session.auto_quality === '480p') return;
    stalls = [];
    // bandwidth lowered, below the file's: the server picks the converted quality that fits
    const rate = ({ '1080p': 8000, '720p': 4000 })[session.auto_quality] || session.source_kbps || 8000;
    bandwidth = Math.max(300, Math.round(Math.min(bandwidth || rate * 1.3, rate * 1.2) * 0.6));
    saveBandwidth(bandwidth);
    toast(tr('Connexion trop lente : qualité réduite'));
    start(pos());
  }

  function suggestLowerQuality(cpu) {
    const box = h('div.player-hint',
      h('span', cpu ? tr('Le serveur ne convertit pas cette vidéo assez vite.')
        : tr('La connexion est trop lente pour le débit de ce fichier.')),
      h('button.btn.primary.small', { onclick: () => { box.remove(); quality = '720p'; start(pos()); } }, tr('Passer en 720p')),
      h('button.icon-btn', { 'aria-label': tr('Fermer'), onclick: () => box.remove() }, icon('close')));
    player.append(box);
    setTimeout(() => box.remove(), 20000);
  }

  // Before the very first playback: all the subtitle tracks are extracted from the file
  // (a single read, at full speed, with no video stream competing on the network share).
  async function prepare(at = 0) {
    if (offline) return; // download: subtitles already on the device
    const t0 = Date.now();
    let hint = null;
    while (!destroyed && current()) {
      let r;
      // ready as soon as the subtitles cover the first minutes to watch: the rest arrives during playback
      try { r = await get(`/api/items/${item.id}/prepare?wait=10&at=${Math.round(at)}`); } catch (e) {
        console.warn('[noxa] préparation', e.message);
        break;
      }
      if (r.ready) break;
      if (!hint && Date.now() - t0 > 4000) { hint = h('div.loader-hint', tr('Préparation de la lecture…')); loader.append(hint); }
      if (Date.now() - t0 > PREPARE_MAX) break;
    }
    if (hint) hint.remove();
  }

  function showError(msg, retry = false, at = null) {
    loader.hidden = true;
    errorLayer.hidden = false;
    clear(errorLayer, h('div.error-box', h('p', msg),
      h('div.actions-row',
        retry ? h('button.btn.primary', { onclick: () => { autoRetries = 0; start(at ?? pos()); } }, tr('Réessayer')) : null,
        h('button.btn', { onclick: goBack }, tr('Retour')))));
  }

  function seekTo(ms, remote = false) {
    const d = duration();
    ms = Math.max(0, d ? Math.min(ms, d - 3000) : ms);
    if (together.group && !remote) together.send('seek', { position: Math.round(ms) });
    // the playlist covers the whole video: the player fetches the right segments, the server restarts
    // ffmpeg at the right place if needed: no need to recreate the stream anymore
    video.currentTime = ms / 1000;
  }
  const seekBy = (delta) => seekTo(pos() + delta);

  // ------------------------------------------------------------ progress
  function report(st, force = false, extra = {}) {
    if (!session || !item || destroyed || switching || finished) return;
    const now = Date.now();
    if (!force && now - lastReport < REPORT_EVERY) return;
    lastReport = now;
    const p = pos();
    if (p < 1000 && st !== 'stopped' && !extra.ended) return;
    if (offline) { saveProgress(state.me.user.id, item, p, duration(), extra.ended); return; }
    const sent = session.stream;
    post('/api/progress', { id: item.id, position: p, duration: duration(), stream: session.stream, state: st, ...extra },
      { keepalive: st === 'stopped' }).then((r) => {
        if (r && r.screen) screenTime(r.screen);
        if (r && typeof r.speed === 'number' && session && session.stream === sent) convSpeed = r.speed;
      }).catch(() => {});
  }

  // screen time (profile limited by an administrator): warned a few minutes before, stop at the limit
  let screenWarned = false;
  function screenTime(sc) {
    if (sc.blocked) {
      video.pause();
      report('paused', true);
      if (session && session.stream) post('/api/stop', { stream: session.stream }).catch(() => {});
      showError(sc.reason === 'bedtime'
        ? tr('C’est l’heure d’aller dormir : la lecture reprendra demain matin.')
        : tr('Temps d’écran du jour écoulé : à demain !'));
      return;
    }
    if (sc.left != null && sc.left <= 5 && !screenWarned) {
      screenWarned = true;
      toast(tr('Plus que {n} min de temps d’écran aujourd’hui', { n: Math.max(1, sc.left) }));
    }
  }

  // ------------------------------------------------------------ UI updates
  function paintPlay() {
    playBtn.replaceChildren(icon(video.paused && !rebuffering ? 'play' : 'pause'));
    miniPlay.replaceChildren(icon(video.paused && !rebuffering ? 'play' : 'pause'));
    player.classList.toggle('paused', video.paused && !rebuffering);
    if (rebuffering) loader.hidden = false;
  }

  function paintTime() {
    const d = duration(), p = pos();
    timeEl.textContent = `${fmtTime(p, d >= 3600000)} / ${fmtTime(d, d >= 3600000)}`;
    if (!scrubbing && d) played.style.width = `${Math.min(100, (p / d) * 100)}%`;
    const b = video.buffered;
    if (b.length && d) {
      const end = base + b.end(b.length - 1) * 1000;
      const startB = base + b.start(0) * 1000;
      buffered.style.left = `${(startB / d) * 100}%`;
      buffered.style.width = `${Math.max(0, ((end - startB) / d) * 100)}%`;
    }
    timeline.setAttribute('aria-valuenow', String(Math.round(p / 1000)));
  }

  function updateIntroMark() {
    const d = duration();
    for (const [mark, kind] of [[introMark, 'intro'], [recapMark, 'recap']]) {
      const m = session && session.markers && session.markers[kind];
      mark.hidden = !(m && d);
      if (m && d) {
        mark.style.left = `${(m.start / d) * 100}%`;
        mark.style.width = `${((m.end - m.start) / d) * 100}%`;
      }
    }
  }

  function paintMarkers(p) {
    if (switching) return;
    const m = (session && session.markers) || {};
    const intro = m.intro, recap = m.recap;
    const inIntro = intro && p >= intro.start && p < intro.end - 1000;
    const inRecap = !inIntro && recap && p >= recap.start && p < recap.end - 1000;
    if (inIntro && prefs.auto_skip_intro && !introSkipped) {
      introSkipped = true;
      toast(tr('Intro passée'));
      seekTo(intro.end);
      return;
    }
    if (inRecap && prefs.auto_skip_recap && !recapSkipped) {
      recapSkipped = true;
      toast(tr('Récap passé'));
      seekTo(recap.end);
      return;
    }
    skipping = inIntro ? intro : inRecap ? recap : null;
    if (skipping) skipLabel.textContent = inIntro ? tr('Passer l’intro') : tr('Passer le récap');
    skipBtn.hidden = !skipping;
    // next episode countdown
    const next = session && session.next;
    const d = duration();
    if (!next || nextDismissed || !d) return;
    const creditsAt = m.credits ? m.credits.start : d - 25000;
    // during the credits: a suggestion, without a countdown (the credits can be watched)
    if (p >= creditsAt && nextCard.hidden && !video.paused && !nextAtEnd) showNextCard(next, false);
    if (p < creditsAt - 2000 && !nextCard.hidden && !nextAtEnd) hideNextCard();
  }

  // countdown: at the end of the video, the next one starts after the chosen delay (Settings), unless cancelled
  function showNextCard(next, countdown) {
    let left = countdown ? Math.max(0, Number(prefs.next_delay ?? 5)) : null;
    const counter = h('span.next-count');
    const paint = () => { counter.textContent = left === null ? '' : tr('dans {left} s', { left }); };
    paint();
    const cancel = h('button.btn.ghost.small', { onclick: () => {
      if (left !== null) { left = null; clearInterval(nextTimer); paint(); cancel.textContent = tr('Fermer'); return; }
      nextDismissed = !nextAtEnd;
      hideNextCard();
    } }, countdown ? tr('Annuler') : tr('Masquer'));
    clear(nextCard,
      next.thumb ? h('img', { src: img(next.type === 'episode' ? next.thumb : next.art, 320, 180), alt: '' }) : null,
      h('div.next-body', h('div.muted.small', next.type === 'episode' ? tr('Épisode suivant ') : tr('À suivre '), counter),
        h('strong', next.type === 'episode' ? `${episodeCode(next)} · ${next.title}` : next.title),
        h('div.actions-row',
          h('button.btn.primary.small', { onclick: playNext }, icon('play'), tr('Lire maintenant')), cancel)));
    nextCard.classList.toggle('end', !!countdown);
    nextCard.hidden = false;
    clearInterval(nextTimer);
    if (left === 0) { playNext(); return; }
    if (left !== null) {
      nextTimer = setInterval(() => {
        left -= 1;
        paint();
        if (left <= 0) { clearInterval(nextTimer); playNext(); }
      }, 1000);
    }
  }
  function hideNextCard() { nextCard.hidden = true; nextCard.classList.remove('end'); clearInterval(nextTimer); }

  function playNext() {
    if (!session || !session.next) return;
    clearInterval(nextTimer);
    report('stopped', true, { ended: true });
    finished = true;
    if (together.group) {
      together.send('item', { item_id: session.next.id, position: 0 });
      location.replace(`#/play/${session.next.id}?group=${together.group.id}`);
      return;
    }
    if (offline) { location.replace(`#/offline/${session.next.id}`); return; }
    const keep = ctx.query.get('playlist') ? `?playlist=${ctx.query.get('playlist')}` : ctx.query.get('shuffle') ? `?shuffle=${ctx.query.get('shuffle')}` : '';
    location.replace(`#/play/${session.next.id}${keep}`);
  }

  // ------------------------------------------------------------ mini player
  function minimize() {
    if (mini || destroyed || !ctx.minimize) return;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    mini = true;
    detached = true;
    menu.hidden = true;
    chapterPanel.hidden = true;
    player.classList.add('mini');
    player.classList.remove('idle');
    ctx.minimize({ itemId: String(item.id), el: player, stop: stopAll, restore });
    goBack();
  }
  function restore(target) {
    mini = false;
    player.classList.remove('mini');
    clear(target, player);
    wake();
  }
  const expand = () => { location.hash = `#/play/${item.id}${together.group ? `?group=${together.group.id}` : ''}`; };
  miniBtn.addEventListener('click', minimize);
  miniBar.children[0].addEventListener('click', (e) => { e.stopPropagation(); expand(); });
  miniPlay.addEventListener('click', (e) => { e.stopPropagation(); togglePlay(); });
  miniBar.children[2].addEventListener('click', (e) => { e.stopPropagation(); if (ctx.closeMini) ctx.closeMini(); });
  miniBar.addEventListener('click', () => { if (mini) expand(); });

  function goBack() {
    if (history.length > 1) history.back();
    else location.hash = `#/item/${item ? (item.type === 'episode' ? item.show_id : item.id) : ''}`;
  }

  // ------------------------------------------------------------ menus
  function openMenu(kind) {
    if (!menu.hidden && menu.dataset.kind === kind) { menu.hidden = true; return; }
    menu.dataset.kind = kind;
    const opt = (label, active, onPick, hint) => h('button.menu-opt', { class: active ? 'menu-opt on' : 'menu-opt',
      onclick: () => { menu.hidden = true; onPick(); } }, h('span', label), hint ? h('span.muted.small', hint) : null,
    active ? icon('check') : null);
    const restart = () => start(pos());
    // subtitles shown by the browser (text, ASS, PGS): the track changes without restarting the video
    const burned = (x) => !!x && !x.overlay && !(x.pgs && browserCaps().pgs);
    const pickSub = (x) => {
      const before = subSel;
      subSel = x;
      rememberTracks();
      if (!session || burned(before) || burned(x)) { restart(); return; }
      const u = x ? `/api/items/${item.id}/subtitles/${x.id}` : null;
      Object.assign(session, { subtitle_selected: x ? x.id : 0, subtitle_overlay: x && x.overlay ? u : null,
        subtitle_ass: x && x.ass ? `${u}/ass` : null, subtitle_pgs: x && x.pgs && !x.overlay ? `${u}/sup` : null });
      subs.load(startSeq);
    };
    chapterPanel.hidden = true;
    if (kind === 'group') {
      clear(menu, together.menu(opt));
    } else if (kind === 'tracks') {
      const audio = session ? session.audio : item.audio;
      const subs = session ? session.subtitles : item.subtitles;
      clear(menu,
        h('div.menu-col', h('h4', tr('Audio')), audio.length ? audio.map((a) => opt(a.title, audioSel && a.id === audioSel.id,
          () => { audioSel = a; subSel = fixForced(subSel, a, subs); rememberTracks(); restart(); })) : h('p.muted.small', tr('Piste unique'))),
        h('div.menu-col', h('h4', tr('Sous-titres')),
          opt(tr('Aucun'), !subSel, () => pickSub(null)),
          menuSubs(subs, audioSel).map((s) => opt(s.title, subSel && s.id === subSel.id, () => pickSub(s),
            s.forced ? SIGNS_HINT : (s.overlay || s.pgs ? null : tr('incrusté'))))));
    } else {
      const autoLabel = session.auto_quality ? tr('Automatique ({q})', { q: session.auto_quality === 'original' ? tr('originale') : session.auto_quality }) : tr('Automatique');
      const q = [['auto', autoLabel], ['original', tr('Originale')], ['1080p', '1080p'], ['720p', '720p'], ['480p', '480p']];
      const modeLabel = { direct: tr('Lecture directe'), copy: tr('Remux (vidéo copiée)'), transcode: tr('Transcodage') }[session.mode] || '';
      const cols = [h('div.menu-col', h('h4', tr('Qualité')), q.map(([v, l]) => opt(l, quality === v, () => { quality = v; if (v === 'auto') bandwidth = null; restart(); })),
        h('p.menu-info', h('strong', modeLabel), session.reasons && session.reasons.length ? ` — ${session.reasons.join(', ')}` : '',
          session.mode === 'transcode' ? h('br') : null, session.mode === 'transcode' ? tr('Le processeur du serveur travaille pour cette lecture.') : null))];
      if (!together.group) {
        cols.push(h('div.menu-col', h('h4', tr('Vitesse')),
          SPEEDS.map((v) => opt(v === 1 ? tr('Normale') : `×${String(v).replace('.', ',')}`, speed === v, () => setSpeed(v)))));
      }
      const left = sleep.at ? Math.max(1, Math.round((sleep.at - Date.now()) / 60000)) : 0;
      cols.push(h('div.menu-col', h('h4', tr('Minuterie')),
        opt(tr('Désactivée'), !sleep.at && !sleep.endOfItem, () => setSleep(0)),
        opt(item.type === 'episode' ? tr('Fin de l’épisode') : tr('Fin du film'), sleep.endOfItem, () => setSleep(-1)),
        [15, 30, 45, 60, 90].map((m) => opt(tr('{n} min', { n: m }), false, () => setSleep(m))),
        sleep.at ? h('p.menu-info', tr('Pause dans {n} min', { n: left })) : null));
      if (isAdmin && item.type === 'episode') {
        const NAMES = { intro: [tr('Intro'), tr('Placez la lecture à la fin de l’intro.')], recap: [tr('Récap'), tr('Placez la lecture à la fin du récap.')] };
        const setMarker = async (kind, which) => {
          const cur = (session.markers && session.markers[kind]) || {};
          const draft = { start: cur.start ?? 0, end: cur.end ?? 0, [which]: pos() };
          if (draft.end <= draft.start) {
            session.markers = { ...session.markers, [kind]: { ...draft, end: draft.start + 1 } };
            toast(which === 'start' ? `${tr('Début enregistré.')} ${NAMES[kind][1]}` : tr('La fin doit être après le début'), which === 'start' ? 'info' : 'bad');
            return;
          }
          try {
            await put(`/api/admin/markers/${item.id}`, { kind, start: draft.start, end: draft.end });
            session.markers = { ...session.markers, [kind]: { ...draft, source: 'manual' } };
            updateIntroMark();
            toast(`${NAMES[kind][0]} : ${fmtTime(draft.start)} → ${fmtTime(draft.end)}`, 'ok');
          } catch (e) { toast(e.message, 'bad'); }
        };
        const clearMarker = async (kind) => {
          await put(`/api/admin/markers/${item.id}`, { kind, clear: true });
          session.markers = { ...session.markers, [kind]: undefined };
          updateIntroMark();
        };
        cols.push(h('div.menu-col', h('h4', tr('Intro (admin)')),
          opt(tr('Début de l’intro ici'), false, () => setMarker('intro', 'start')),
          opt(tr('Fin de l’intro ici'), false, () => setMarker('intro', 'end')),
          opt(tr('Effacer le marqueur'), false, () => clearMarker('intro')),
          h('h4', tr('Récap (admin)')),
          opt(tr('Début du récap ici'), false, () => setMarker('recap', 'start')),
          opt(tr('Fin du récap ici'), false, () => setMarker('recap', 'end')),
          opt(tr('Effacer le marqueur'), false, () => clearMarker('recap'))));
      }
      clear(menu, cols);
    }
    menu.hidden = false;
  }

  // ------------------------------------------------------------ chapters
  function openChapters() {
    if (!chapterPanel.hidden) { chapterPanel.hidden = true; return; }
    menu.hidden = true;
    const p = pos();
    const cur = chapters.findIndex((c) => p >= c.start && p < c.end);
    const strip = chapterStrip(chapters, { current: cur, onPick: (c) => { chapterPanel.hidden = true; seekTo(c.start + 1); } });
    clear(chapterPanel,
      h('div.chapter-panel-head', h('h3', tr('Sélection du chapitre')),
        cur >= 0 ? h('span.muted.small', tr('En cours : {p1}', { p1: chapterTitle(chapters[cur], cur) })) : null,
        h('div.grow'),
        h('button.icon-btn', { 'aria-label': tr('Fermer'), onclick: () => { chapterPanel.hidden = true; } }, icon('close'))),
      strip);
    chapterPanel.hidden = false;
    const on = strip.querySelector('.chapter-card.on') || strip.firstElementChild;
    if (on) requestAnimationFrame(() => { on.scrollIntoView({ block: 'nearest', inline: 'center' }); on.focus({ preventScroll: true }); });
  }

  function setupMediaSession() {
    if (!('mediaSession' in navigator) || !window.MediaMetadata) return;
    const art = item.type === 'episode' ? (item.show_thumb || item.thumb) : item.thumb;
    navigator.mediaSession.metadata = new window.MediaMetadata({
      title: item.type === 'episode' ? `${episodeCode(item)} · ${item.title}` : item.title,
      artist: item.type === 'episode' ? item.show : String(item.year || ''),
      artwork: art ? [{ src: img(art, 300, 450), sizes: '300x450', type: 'image/jpeg' }] : [],
    });
    navigator.mediaSession.setActionHandler('seekbackward', () => seekBy(-BACK));
    navigator.mediaSession.setActionHandler('seekforward', () => seekBy(FWD));
    if (session.next) navigator.mediaSession.setActionHandler('nexttrack', playNext);
  }

  // ------------------------------------------------------------ events
  const togglePlay = () => {
    const group = together.group;
    if (group) {
      if (group.state && group.state.waiting && group.state.playing) { together.send('pause', { position: pos() }); return; }
      if (video.paused) { together.send('play', { position: pos() }); rebuffering = false; video.play().catch(() => {}); }
      else { together.send('pause', { position: pos() }); video.pause(); }
      return;
    }
    if (rebuffering) { rebuffering = false; paintPlay(); return; } // stays paused: the automatic resume is cancelled
    if (video.paused) video.play().catch(() => {}); else video.pause();
  };
  const toggleFull = () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    // fullscreen on the whole page, not on the player: it is rebuilt at each episode, and
    // fullscreen would stop at each move to the next episode
    else document.documentElement.requestFullscreen().catch(() => player.requestFullscreen().catch(() => {}));
  };
  const wake = () => {
    player.classList.remove('idle');
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { if (!video.paused && menu.hidden && chapterPanel.hidden) player.classList.add('idle'); }, HIDE_AFTER);
  };

  playBtn.addEventListener('click', togglePlay);
  fullBtn.addEventListener('click', toggleFull);
  backBtn.addEventListener('click', goBack);
  nextBtn.addEventListener('click', playNext);
  subsBtn.addEventListener('click', (e) => { e.stopPropagation(); openMenu('tracks'); });
  chaptersBtn.addEventListener('click', (e) => { e.stopPropagation(); openChapters(); });
  groupBtn.addEventListener('click', (e) => { e.stopPropagation(); openMenu('group'); });
  cogBtn.addEventListener('click', (e) => { e.stopPropagation(); openMenu('settings'); });
  skipBtn.addEventListener('click', () => {
    if (!skipping) return;
    if (skipping === session.markers.intro) introSkipped = true; else recapSkipped = true;
    skipBtn.hidden = true;
    seekTo(skipping.end);
  });
  muteBtn.addEventListener('click', () => { video.muted = !video.muted; });
  volume.addEventListener('input', () => { video.volume = Number(volume.value); video.muted = video.volume === 0; });
  video.addEventListener('volumechange', () => {
    muteBtn.replaceChildren(icon(video.muted || video.volume === 0 ? 'mute' : 'volume'));
    volume.value = video.muted ? 0 : video.volume;
    store('seance.volume', String(video.volume));
    store('seance.muted', video.muted ? '1' : '0');
  });
  video.addEventListener('click', () => {
    if (!menu.hidden || !chapterPanel.hidden) { menu.hidden = true; chapterPanel.hidden = true; return; }
    togglePlay();
  });
  video.addEventListener('dblclick', toggleFull);
  video.addEventListener('play', () => { paintPlay(); wake(); devices.report(); });
  video.addEventListener('pause', () => { paintPlay(); wake(); if (!rebuffering) report('paused', true); devices.report(); });
  video.addEventListener('seeked', () => devices.report());

  // ------------------------------------------------------------ the account's other devices (remote control, casting)
  const devices = createDevices({ video, loader, castLayer, castBtn, airplayBtn, offline, back: BACK, forward: FWD, speeds: SPEEDS,
    item: () => item, session: () => session, destroyed: () => destroyed, pos, duration, speed: () => speed,
    audio: () => audioSel, subtitle: () => subSel, togglePlay, seekTo: (ms) => seekTo(ms), seekBy, playNext,
    setSpeed: (v) => setSpeed(v), start: (ms) => start(ms), goBack, dropHls: () => { if (hls) { hls.destroy(); hls = null; } },
    setTracks: (a, sub) => {
      if (a !== undefined) { audioSel = a; subSel = fixForced(subSel, a, (session && session.subtitles) || []); }
      if (sub !== undefined) subSel = sub;
      rememberTracks();
    },
    airplay: (on) => { airplay = on; } });
  video.addEventListener('waiting', () => { loader.hidden = false; onStall(); });
  video.addEventListener('playing', () => { loader.hidden = true; switching = false; everPlayed = true; subs.sync(); });
  video.addEventListener('canplay', () => { if (!rebuffering && !switching) loader.hidden = true; });
  video.addEventListener('timeupdate', () => {
    const p = pos();
    if (together.group) together.drift(p);
    paintTime();
    subs.paint(p);
    paintMarkers(p);
    // seek beyond what is already extracted: say so rather than show a screen without subtitles
    subsPending.hidden = subs.until === null || p <= subs.until;
    if (!video.paused) report('playing');
  });
  video.addEventListener('progress', paintTime);
  // text subtitles aligned on each frame shown (timeupdate only comes every ~250 ms)
  if ('requestVideoFrameCallback' in HTMLVideoElement.prototype) {
    const onFrame = (_now, meta) => {
      if (destroyed) return;
      subs.paint(base + Math.round((meta && meta.mediaTime != null ? meta.mediaTime : video.currentTime) * 1000));
      video.requestVideoFrameCallback(onFrame);
    };
    video.requestVideoFrameCallback(onFrame);
  }
  // chosen speed: set again after each load (a new stream resets it to 1), not in a group (the
  // synchronisation uses the speed to catch up gaps)
  const applySpeed = () => { if (!together.group) { video.defaultPlaybackRate = speed; video.playbackRate = speed; } };
  video.addEventListener('loadedmetadata', applySpeed);
  const setSpeed = (v) => { speed = v; speedChosen = true; applySpeed(); toast(tr('Vitesse ×{v}', { v: String(v).replace('.', ',') })); };
  const stepSpeed = (dir) => {
    if (together.group) { toast(tr('Vitesse fixe en groupe')); return; }
    const i = SPEEDS.indexOf(speed);
    setSpeed(SPEEDS[Math.min(SPEEDS.length - 1, Math.max(0, (i < 0 ? 1 : i) + dir))]);
  };
  // sleep timer: checked every second, playback is paused at the set time
  const sleepTimer = setInterval(() => {
    if (sleep.at && Date.now() >= sleep.at) {
      sleep.at = 0;
      if (!video.paused) togglePlay();
      toast(tr('Minuterie : lecture mise en pause'));
    }
  }, 1000);
  const setSleep = (min) => {
    sleep.at = min > 0 ? Date.now() + min * 60000 : 0;
    sleep.endOfItem = min === -1;
    toast(min === 0 ? tr('Minuterie désactivée') : min === -1 ? tr('Arrêt à la fin de cette vidéo')
      : tr('Pause dans {n} min', { n: min }));
  };
  video.addEventListener('ended', () => {
    // the transcoded stream can be a little shorter than the file: treat as finished
    report('stopped', true, { ended: true });
    finished = true;
    paintPlay();
    player.classList.remove('idle');
    if (sleep.endOfItem) { // "end of the video" sleep timer: no next episode
      sleep.endOfItem = false;
      toast(tr('Minuterie : fin de la lecture'));
      return;
    }
    if (session.next) {
      nextAtEnd = true;
      showNextCard(session.next, prefs.autoplay_next && !mini); // mini player: suggest without interrupting navigation
    }
  });
  document.addEventListener('fullscreenchange', onFullscreen);
  function onFullscreen() { fullBtn.replaceChildren(icon(document.fullscreenElement ? 'exitfull' : 'full')); }

  // timeline: hover preview + click/drag to seek
  const ratioAt = (e) => {
    const r = timeline.getBoundingClientRect();
    return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  };
  timeline.addEventListener('pointermove', (e) => {
    const ratio = ratioAt(e);
    const t = ratio * duration();
    hoverTip.hidden = false;
    previewTime.textContent = fmtTime(t);
    const ch = chapters.find((c) => t >= c.start && t < c.end);
    previewChapter.textContent = ch && (ch.title || chapters.length > 1) ? (ch.title || tr('Chapitre {p1}', { p1: chapters.indexOf(ch) + 1 })) : '';
    if (trick) {
      // thumbnail taken from the preview sheet (grid cols x rows)
      const n = Math.min(trick.count - 1, Math.floor(t / 1000 / trick.interval));
      const perSheet = trick.cols * trick.rows;
      const sheet = Math.floor(n / perSheet) + 1;
      const k = n % perSheet;
      previewImg.hidden = false;
      previewImg.style.width = `${trick.w}px`;
      previewImg.style.height = `${trick.h}px`;
      previewImg.style.backgroundImage = `url("/api/items/${item.id}/trickplay/${sheet}.jpg")`;
      previewImg.style.backgroundPosition = `-${(k % trick.cols) * trick.w}px -${Math.floor(k / trick.cols) * trick.h}px`;
      previewImg.style.backgroundSize = `${trick.w * trick.cols}px ${trick.h * trick.rows}px`;
    }
    const r = timeline.getBoundingClientRect();
    const half = (trick ? trick.w : 60) / 2 + 4;
    const x = Math.min(r.width - half, Math.max(half, ratio * r.width));
    hoverTip.style.left = `${x}px`;
    if (scrubbing) played.style.width = `${ratio * 100}%`;
  });
  timeline.addEventListener('pointerleave', () => { if (!scrubbing) hoverTip.hidden = true; });
  timeline.addEventListener('pointerdown', (e) => {
    scrubbing = true;
    timeline.setPointerCapture(e.pointerId);
    played.style.width = `${ratioAt(e) * 100}%`;
  });
  timeline.addEventListener('pointerup', (e) => {
    if (!scrubbing) return;
    scrubbing = false;
    hoverTip.hidden = true;
    seekTo(ratioAt(e) * duration());
  });
  timeline.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') e.stopPropagation();
    if (e.key === 'ArrowLeft') seekBy(-BACK);
    if (e.key === 'ArrowRight') seekBy(BACK);
  });

  player.addEventListener('pointermove', wake);
  player.addEventListener('click', (e) => { if (!menu.contains(e.target) && e.target !== video) wake(); });
  const onKey = (e) => {
    if (mini || (e.target.closest && e.target.closest('input, select, textarea'))) return;
    wake();
    if (!chapterPanel.hidden && ['ArrowLeft', 'ArrowRight', 'Enter', ' '].includes(e.key)) {
      // in the chapter selection, the arrows move from one thumbnail to the next
      const cards = [...chapterPanel.querySelectorAll('.chapter-card')];
      let i = cards.indexOf(document.activeElement);
      if (e.key === 'Enter' || e.key === ' ') { if (i >= 0) { e.preventDefault(); cards[i].click(); } return; }
      e.preventDefault();
      if (i < 0) i = Math.max(0, cards.findIndex((c) => c.classList.contains('on')));
      else i = Math.min(cards.length - 1, Math.max(0, i + (e.key === 'ArrowRight' ? 1 : -1)));
      cards[i].focus();
      cards[i].scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
      return;
    }
    // remote: arrows = from one button to the next when in the controls (except the time bar)
    const inControls = player.contains(document.activeElement) && document.activeElement !== player
      && !document.activeElement.classList.contains('video');
    const arrow = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key);
    if (arrow && inControls && !(document.activeElement === timeline && ['ArrowLeft', 'ArrowRight'].includes(e.key))) {
      e.preventDefault();
      moveFocus({ ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right' }[e.key]);
      return;
    }
    if (!inControls && remoteMode() && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      e.preventDefault();
      playBtn.focus(); // like on a TV: up / down show the controls
      return;
    }
    // configurable keys (Settings → This device); media keys and Escape stay fixed
    const action = actionFor(e.key);
    if (action && !(e.key === 'Enter') && !e.ctrlKey && !e.metaKey && !e.altKey) {
      const run = { play: togglePlay, back: () => seekBy(-BACK), forward: () => seekBy(BACK),
        volup: () => { video.volume = Math.min(1, video.volume + 0.1); }, voldown: () => { video.volume = Math.max(0, video.volume - 0.1); },
        full: toggleFull, mute: () => { video.muted = !video.muted; }, skip: () => { if (!skipBtn.hidden) skipBtn.click(); },
        next: () => { if (session && session.next) playNext(); }, chapters: () => { if (chapters.length > 1) openChapters(); },
        mini: minimize, slower: () => stepSpeed(-1), faster: () => stepSpeed(1) }[action];
      if (run) { e.preventDefault(); run(); return; }
    }
    switch (e.key) {
      case 'MediaPlayPause': e.preventDefault(); togglePlay(); break;
      case 'Enter': if (!inControls) { e.preventDefault(); togglePlay(); } break;
      case 'MediaPlay': if (video.paused) togglePlay(); break;
      case 'MediaPause': if (!video.paused) togglePlay(); break;
      case 'MediaFastForward': seekBy(30000); break;
      case 'MediaRewind': seekBy(-10000); break;
      case 'MediaTrackNext': if (session && session.next) playNext(); break;
      case 'Escape': case 'BrowserBack': case 'GoBack':
        if (!chapterPanel.hidden) chapterPanel.hidden = true;
        else if (!menu.hidden) menu.hidden = true;
        else if (inControls && remoteMode()) { document.activeElement.blur(); player.classList.add('idle'); } // hide the controls
        else if (!document.fullscreenElement) goBack();
        break;
      default:
    }
  };
  document.addEventListener('keydown', onKey);
  const onHide = () => report(document.visibilityState === 'hidden' ? 'paused' : 'playing', true);
  document.addEventListener('visibilitychange', onHide);
  const onUnload = () => report('stopped', true);
  window.addEventListener('pagehide', onUnload);

  await prepare(startAt);
  if (destroyed || !current()) return () => {};
  await start(startAt);
  wake();

  function stopAll() {
    if (destroyed) return;
    if (Date.now() - startedAt > 3000 && !switching) report('stopped', true);
    if (session && !offline) post('/api/stop', { stream: session.stream }, { keepalive: true }).catch(() => {});
    destroyed = true;
    if (local) setTimeout(local.release, 0); // frees the file kept in working memory
    together.close();
    clearInterval(nextTimer);
    clearInterval(sleepTimer);
    devices.stop(); // casting continues on the TV when the player is left
    // sleep timer: it follows the chain of episodes, not a return to browsing
    if (!/^#\/(play|offline)\//.test(location.hash)) Object.assign(sleep, { at: 0, endOfItem: false });
    clearTimeout(idleTimer);
    document.removeEventListener('keydown', onKey);
    document.removeEventListener('visibilitychange', onHide);
    document.removeEventListener('fullscreenchange', onFullscreen);
    window.removeEventListener('pagehide', onUnload);
    subs.stop();
    if (hls) hls.destroy();
    video.removeAttribute('src');
    video.load();
    if (document.fullscreenElement && !location.hash.startsWith('#/play/')) document.exitFullscreen().catch(() => {});
  }
  return stopAll;
}
