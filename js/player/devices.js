// The player and the account's other devices:
// - remote control: another device of the account (typically the phone) sees this playback and drives it;
// - casting to a TV: AirPlay (Safari, to an Apple TV) and Chromecast (Chrome / Edge). In both cases the TV
//   fetches the video itself, from a stream the server prepares for it.
import { post } from '../api.js';
import { deviceId, deviceName } from '../device.js';
import { clear, episodeCode, fmtTime, h, icon, toast } from '../ui.js';
import { tr } from '../i18n.js';
import { CHROMECAST_CAPS, castDeviceName, castLoad, loadCastSdk, watchAirplay } from '../cast.js';

// host: { video, loader, castLayer, castBtn, airplayBtn, offline, back, forward, speeds,
//         item(), session(), destroyed(), pos(), duration(), speed(), audio(), subtitle(),
//         togglePlay(), seekTo(ms), seekBy(ms), playNext(), setSpeed(v), setTracks(audio, subtitle), start(ms),
//         goBack(), dropHls(), airplay(on) }
export function createDevices(host) {
  const { video, loader, castLayer, castBtn, airplayBtn, offline } = host;
  const myDevice = deviceId();

  // ------------------------------------------------------------ remote control
  let remoteEs = null, remoteTimer = null, remoteLast = 0;
  // Current state, sent to the server for the account's remotes (at most every 0.8 s unless forced)
  function report(force = false) {
    const item = host.item(), session = host.session();
    if (offline || host.destroyed() || !item || !session) return;
    const now = Date.now();
    if (!force && now - remoteLast < 800) return;
    remoteLast = now;
    const audio = session.audio || [], subs = session.subtitles || [];
    const audioSel = host.audio(), subSel = host.subtitle();
    post('/api/remote/state', { device: myDevice, state: {
      item: String(item.id), title: item.type === 'episode' ? item.show : item.title,
      subtitle: item.type === 'episode' ? `${episodeCode(item)} · ${item.title}` : (item.year ? String(item.year) : ''),
      thumb: item.type === 'episode' ? (item.thumb || item.show_thumb) : (item.art || item.thumb),
      position: host.pos(), duration: host.duration(), playing: !video.paused, has_next: !!session.next,
      audio: audio.map((a) => ({ id: a.id, title: a.title })), subtitles: subs.map((t) => ({ id: t.id, title: t.title })),
      audio_selected: audioSel ? audioSel.id : null, subtitle_selected: subSel ? subSel.id : 0,
      volume: Math.round(video.volume * 100) / 100, muted: video.muted, speed: host.speed() } }).catch(() => {});
  }
  function command(c) {
    const v = c.value, session = host.session();
    switch (c.action) {
      case 'play': if (video.paused) host.togglePlay(); break;
      case 'pause': if (!video.paused) host.togglePlay(); break;
      case 'toggle': host.togglePlay(); break;
      case 'seek': if (typeof v === 'number') host.seekTo(Math.max(0, v)); break;
      case 'seekby': if (typeof v === 'number') host.seekBy(v); break;
      case 'next': if (session && session.next) host.playNext(); break;
      case 'volume': if (typeof v === 'number') { video.muted = false; video.volume = Math.min(1, Math.max(0, v)); } break;
      case 'mute': video.muted = !video.muted; break;
      case 'speed': if (host.speeds.includes(v)) host.setSpeed(v); break;
      case 'audio': {
        const a = (session.audio || []).find((x) => x.id === v);
        const cur = host.audio();
        if (a && (!cur || a.id !== cur.id)) { host.setTracks(a, undefined); host.start(host.pos()); }
        break;
      }
      case 'subtitle': {
        const s = v ? (session.subtitles || []).find((x) => x.id === v) || null : null;
        if (v && !s) break;
        host.setTracks(undefined, s);
        host.start(host.pos()); // (track chosen remotely: reload, without checking whether it could show without restarting)
        break;
      }
      case 'stop': host.goBack(); break;
      default:
    }
    toast(tr('Télécommande : {action}', { action: ({ play: tr('lecture'), pause: tr('pause'), toggle: tr('lecture / pause'),
      seek: tr('position'), seekby: tr('position'), next: tr('épisode suivant'), volume: tr('volume'), mute: tr('son'),
      speed: tr('vitesse'), audio: tr('langue audio'), subtitle: tr('sous-titres'), stop: tr('arrêt') })[c.action] || c.action }));
    setTimeout(() => report(true), 400);
  }

  // ------------------------------------------------------------ AirPlay
  // Safari sends the video address to the Apple TV; it is replaced by a native "for TV" stream
  let airplay = false;
  const stopAirplay = watchAirplay(video, (on) => { airplayBtn.hidden = !on || offline; }, (wireless) => {
    if (wireless === airplay) return;
    airplay = wireless;
    host.airplay(wireless);
    toast(wireless ? tr('Lecture sur l’Apple TV') : tr('Lecture de retour sur cet appareil'));
    host.start(host.pos());
  });
  airplayBtn.addEventListener('click', () => { if (video.webkitShowPlaybackTargetPicker) video.webkitShowPlaybackTargetPicker(); });

  // ------------------------------------------------------------ Chromecast
  // Google's SDK (Chrome, Edge) finds the devices; the server prepares a stream it can play
  let castFw = null, castStream = null, castRemote = null, castCtl = null, castTimer = null, castAt = 0;
  const castPos = () => (castRemote ? Math.round(castRemote.currentTime * 1000) : castAt);
  function paintCast() {
    const item = host.item();
    if (!castRemote || !castStream) { castLayer.hidden = true; return; }
    const d = host.duration();
    clear(castLayer, h('div.cast-box', icon('cast'),
      h('div', h('div.muted.small', tr('Diffusion sur {name}', { name: castDeviceName(castFw) || 'Chromecast' })),
        h('strong', item.type === 'episode' ? `${item.show} · ${episodeCode(item)}` : item.title),
        h('div.muted.small', `${fmtTime(castPos(), d >= 3600000)} / ${fmtTime(d, d >= 3600000)}`)),
      h('div.cast-ctl',
        h('button.ctl', { 'aria-label': tr('Reculer de {n} secondes', { n: host.back / 1000 }), onclick: () => castSeek(castPos() - host.back) }, icon('rewind')),
        h('button.ctl', { 'aria-label': tr('Lecture/Pause'), onclick: () => castCtl.playOrPause() }, icon(castRemote.isPaused ? 'play' : 'pause')),
        h('button.ctl', { 'aria-label': tr('Avancer de {n} secondes', { n: host.forward / 1000 }), onclick: () => castSeek(castPos() + host.forward) }, icon('forward'))),
      h('button.btn.small', { type: 'button', onclick: () => castFw.CastContext.getInstance().endCurrentSession(true) }, tr('Arrêter la diffusion'))));
    castLayer.hidden = false;
  }
  function castSeek(ms) {
    castRemote.currentTime = Math.max(0, Math.min(host.duration() / 1000, ms / 1000));
    castCtl.seek();
  }
  async function startCast() {
    const item = host.item(), session = host.session(), audioSel = host.audio();
    const at = host.pos();
    video.pause();
    loader.hidden = false;
    try {
      const s = await post('/api/play', { id: item.id, offset: Math.round(at), quality: 'original', caps: CHROMECAST_CAPS, cast: true,
        audio: audioSel ? audioSel.id : null, subtitle: 0 });
      castStream = s.stream;
      // playback here stops (the server only prepares one stream, the TV's)
      if (session && session.stream) post('/api/stop', { stream: session.stream }).catch(() => {});
      host.dropHls();
      await castLoad(castFw, { url: s.url, mode: s.mode, offset: s.offset, title: item.type === 'episode' ? item.show : item.title,
        subtitle: item.type === 'episode' ? `${episodeCode(item)} · ${item.title}` : '' });
      castRemote = new castFw.RemotePlayer();
      castCtl = new castFw.RemotePlayerController(castRemote);
      castCtl.addEventListener(castFw.RemotePlayerEventType.ANY_CHANGE, paintCast);
      clearInterval(castTimer);
      castTimer = setInterval(() => {  // progress of the account, like a playback here
        if (!castRemote || !castRemote.isConnected) return;
        castAt = castPos();
        paintCast();
        post('/api/progress', { id: item.id, position: castAt, duration: host.duration(), stream: castStream,
          state: castRemote.isPaused ? 'paused' : 'playing' }).catch(() => {});
      }, 5000);
      paintCast();
    } catch (e) {
      toast(e.message || tr('Diffusion impossible'), 'bad');
      castFw.CastContext.getInstance().endCurrentSession(true);
    } finally { loader.hidden = true; }
  }
  function endCast() {
    if (!castStream) return;
    const at = castPos() || castAt;
    post('/api/stop', { stream: castStream }).catch(() => {});
    castStream = null;
    castRemote = null;
    clearInterval(castTimer);
    castLayer.hidden = true;
    if (!host.destroyed()) { toast(tr('Lecture de retour sur cet appareil')); host.start(at); }
  }

  if (!offline) {
    loadCastSdk().then((fw) => {
      if (!fw || host.destroyed()) return;
      castFw = fw;
      const ctxCast = fw.CastContext.getInstance();
      const paintBtn = () => { castBtn.hidden = ctxCast.getCastState() === fw.CastState.NO_DEVICES_AVAILABLE; };
      ctxCast.addEventListener(fw.CastContextEventType.CAST_STATE_CHANGED, paintBtn);
      ctxCast.addEventListener(fw.CastContextEventType.SESSION_STATE_CHANGED, (e) => {
        if (e.sessionState === fw.SessionState.SESSION_ENDED) endCast();
      });
      paintBtn();
      castBtn.addEventListener('click', async () => {
        if (castStream) { ctxCast.endCurrentSession(true); return; }
        try {
          if (!ctxCast.getCurrentSession()) await ctxCast.requestSession();
          await startCast();
        } catch (e) { if (e !== 'cancel') toast(tr('Diffusion annulée')); }
      });
    });
    remoteEs = new EventSource(`/api/remote/events?device=${myDevice}&name=${encodeURIComponent(deviceName())}`);
    remoteEs.onmessage = (ev) => { try { command(JSON.parse(ev.data)); } catch { /* unreadable command */ } };
    remoteEs.onopen = () => report(true);
    remoteTimer = setInterval(() => report(true), 5000);
  }

  // Player closed: casting continues on the TV, the remotes no longer list this device
  function stop() {
    clearInterval(remoteTimer);
    stopAirplay();
    clearInterval(castTimer);
    if (remoteEs) { remoteEs.close(); post('/api/remote/state', { device: myDevice, state: null }, { keepalive: true }).catch(() => {}); }
  }

  return { report, stop };
}
