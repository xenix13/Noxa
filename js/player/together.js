// "Watch together" in the player: the group shares one playback position (see noxa/syncplay.py).
// Each member plays their own stream and aligns it on that position: a small gap is caught up by playing
// barely faster or slower, a big one by a jump. Pauses, seeks and reloads put the whole group on hold until
// everyone has enough video buffered.
import { get, post, put } from '../api.js';
import { state } from '../app.js';
import { avatar, fmtTime, h, toast } from '../ui.js';
import { tr } from '../i18n.js';

// clock offset with the server, kept from one episode to the next
const clock = { offset: 0, at: 0 };
const CLOCK_TTL = 10 * 60 * 1000;

async function syncClock() {
  if (Date.now() - clock.at < CLOCK_TTL) return; // measured recently (previous episode): nothing to redo
  // offset between the server's clock and ours: the measurement with the shortest round trip is kept
  let best = null;
  for (let i = 0; i < 3; i++) {
    const t0 = Date.now();
    try {
      const r = await get('/api/together/time');
      const t1 = Date.now();
      if (!best || t1 - t0 < best.rtt) best = { rtt: t1 - t0, offset: r.now - (t0 + t1) / 2 };
    } catch { break; }
  }
  if (best) Object.assign(clock, { offset: best.offset, at: Date.now() });
}
// each event carries the server time at sending: it cannot be ahead of our corrected
// clock, so it refines the measurement without an extra request
function refineClock(serverMs) {
  if (serverMs) clock.offset = Math.max(clock.offset, serverMs - Date.now());
}

// host: { video, banner, button, count, menu, openMenu(kind), item(), session(), switching(), seq(), pos(),
//         seekTo(ms, fromGroup), report(state, force), waitBuffer(seconds, maxMs, seq), finish(), paintPlay(),
//         prerollSeek(mode) }
export function createTogether(host) {
  const { video } = host;
  let group = null;
  let quietUntil = 0;   // right after an order from the group (play, seek): a short loading is normal
  let readyPending = false;
  let lastDrift = 0;

  const serverNow = () => Date.now() + (group ? clock.offset : 0);
  const target = (st) => st.position + (st.playing && !st.waiting ? Math.max(0, serverNow() - st.ref) : 0);

  function send(action, extra = {}) {
    if (!group) return Promise.resolve();
    return post(`/api/together/${group.id}/cmd`, { action, ...extra }).catch((e) => {
      if (e.status === 403 && ['play', 'pause', 'seek'].includes(action)) {
        toast(e.message, 'bad');
        apply(group.state); // back to the group's position
      } else if (e.status === 404) leave(tr('Le groupe est terminé'));
    });
  }

  // Joins the group before the first stream: -> the group's state, or null (the player plays alone)
  async function join(id) {
    try {
      const g = await post(`/api/together/${id}/join`);
      group = { id, name: g.name, state: g, members: g.members, hostOnly: g.host_only, isHost: g.is_host, es: null };
      await syncClock();
      return g;
    } catch (e) {
      toast(e.message, 'bad');
      group = null;
      return null;
    }
  }

  function listen() {
    const es = new EventSource(`/api/together/${group.id}/events`);
    group.es = es;
    es.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (!group) return;
      refineClock(m.server_now);
      if (m.type === 'state') {
        if (group.state && m.seq && group.state.seq && m.seq < group.state.seq) return;
        const prev = group.state;
        group.state = m;
        notice(m, prev);
        apply(m);
      } else if (m.type === 'members') {
        group.members = m.members;
        group.hostOnly = m.host_only;
        group.name = m.name;
        group.isHost = m.members.some((x) => x.host && x.id === state.me.user.id);
        paint();
        if (!host.menu.hidden && host.menu.dataset.kind === 'group') { host.menu.hidden = true; host.openMenu('group'); }
      } else if (m.type === 'notice') {
        toast(m.text);
      } else if (m.type === 'closed') {
        leave(tr('Le groupe est terminé'));
      }
    };
    es.onerror = () => { if (es.readyState === EventSource.CLOSED && group) leave(tr('Connexion au groupe perdue')); };
  }

  function leave(msg) {
    if (!group) return;
    if (group.es) group.es.close();
    group = null;
    video.playbackRate = 1;
    host.banner.hidden = true;
    paint();
    if (msg) toast(msg);
  }

  function notice(m, prev) {
    if (!m.by || m.by === state.me.user.display_name) return;
    if (m.action === 'pause') toast(tr('{by} a mis en pause', { by: m.by }));
    else if (m.action === 'play') toast(tr('{by} a repris la lecture', { by: m.by }));
    else if (m.action === 'seek') toast(tr(prev && m.position < target(prev) ? '{by} a reculé jusqu’à {time}' : '{by} a avancé jusqu’à {time}', { by: m.by, time: fmtTime(m.position) }));
    else if (m.action === 'item' && prev && prev.item_id !== m.item_id) toast(tr('{by} lance une autre vidéo', { by: m.by }));
    if (m.notice) toast(m.notice);
  }

  // Applies the group's state to this player
  function apply(st) {
    const session = host.session(), item = host.item();
    if (!group || !st || !session) return;
    if (st.item_id && st.item_id !== String(item.id)) {
      host.finish();
      host.report('stopped', true);
      location.replace(`#/play/${st.item_id}?group=${group.id}`);
      return;
    }
    if (host.switching()) return; // the video is still loading: the state will be applied once it is ready
    const at = target(st);
    const meWaited = st.waiting && (st.waiting_ids || []).includes(state.me.user.id);
    host.banner.hidden = !st.waiting;
    if (st.waiting) host.banner.textContent = st.waiting_for.length ? tr('En attente de {p1}…', { p1: st.waiting_for.join(', ') }) : tr('Synchronisation…');
    if (!st.playing || st.waiting) {
      if (!video.paused) video.pause();
      video.playbackRate = 1;
      quietUntil = Date.now() + 4000;
      if (Math.abs(host.pos() - at) > 400) host.seekTo(at, true);
      if (meWaited && !readyPending) {
        // reload at the new position, then tell the group
        readyPending = true;
        const seq = host.seq();
        host.waitBuffer(host.prerollSeek(session.mode), 30000, seq).then(() => {
          readyPending = false;
          if (group && seq === host.seq()) send('ready');
        });
      }
    } else {
      quietUntil = Date.now() + 4000;
      if (Math.abs(host.pos() - at) > 1500) host.seekTo(at, true);
      if (video.paused) video.play().catch(() => { host.paintPlay(); });
    }
    host.paintPlay();
  }

  // Called at each time update while playing in a group
  function drift(p) {
    const st = group.state;
    if (!st || !st.playing || st.waiting || video.paused || video.seeking || host.switching()) return;
    const now = Date.now();
    if (now - lastDrift < 1000) return;
    lastDrift = now;
    const diff = p - target(st);
    if (Math.abs(diff) > 2000) { video.playbackRate = 1; host.seekTo(target(st), true); return; }
    // small gap: play a little faster or slower (the audio keeps its pitch), without a visible jump;
    // ±8 % catches up 1 s in about twelve seconds
    video.playbackRate = Math.abs(diff) < 150 ? 1 : Math.min(1.08, Math.max(0.92, 1 - diff / 2500));
  }

  function paint() {
    host.button.classList.toggle('on', !!group);
    host.count.hidden = !group;
    if (group) host.count.textContent = String((group.members || []).filter((m) => m.online !== false).length || 1);
  }

  // Content of the player's "Watch together" menu; opt(label, active, onPick) makes one entry
  function menu(opt) {
    const item = host.item();
    if (!group) {
      return h('div.menu-col.group-col', h('h4', tr('Regarder ensemble')),
        h('p.menu-info', tr('Chaque membre suit la même lecture sur son appareil ; pauses et sauts sont synchronisés.')),
        opt(tr('Créer un groupe avec cette vidéo'), false, async () => {
          try {
            const g = await post('/api/together', { item_id: item.id, position: host.pos() });
            host.finish();
            host.report('stopped', true);
            location.replace(`#/play/${item.id}?group=${g.id}`);
          } catch (e) { toast(e.message, 'bad'); }
        }),
        opt(tr('Voir les groupes en cours'), false, () => { location.hash = '#/together'; }));
    }
    const status = (m) => (m.online === false ? tr('hors ligne') : (m.ready ? tr('prêt') : tr('chargement…')));
    return h('div.menu-col.group-col', h('h4', group.name),
      h('div.group-members', (group.members || []).map((m) => h('div.group-member', avatar({ display_name: m.name, avatar_color: m.color, avatar: m.avatar }, 26),
        h('span.grow', m.name, m.host ? h('span.tag.accent', tr('Hôte')) : null), h('span.muted.small', status(m))))),
      group.hostOnly ? h('p.menu-info', tr('Seul l’hôte contrôle la lecture.')) : null,
      opt(tr('Copier le lien d’invitation'), false, async () => {
        const link = `${location.origin}/#/together/${group.id}`;
        try { await navigator.clipboard.writeText(link); toast(tr('Lien d’invitation copié'), 'ok'); }
        catch { toast(link); }
      }),
      group.isHost ? opt(group.hostOnly ? tr('Tout le monde peut contrôler') : tr('Seul l’hôte contrôle la lecture'), false, async () => {
        try { await put(`/api/together/${group.id}`, { host_only: !group.hostOnly }); } catch (e) { toast(e.message, 'bad'); }
      }) : null,
      opt(tr('Quitter le groupe'), false, async () => {
        const id = group.id;
        leave();
        await post(`/api/together/${id}/leave`).catch(() => {});
        history.replaceState(null, '', `#/play/${item.id}`);
      }));
  }

  // Player closed: stay in the group when chaining to another episode of the group
  function close() {
    if (!group) return;
    if (group.es) group.es.close();
    if (!location.hash.includes(`group=${group.id}`)) post(`/api/together/${group.id}/leave`, {}, { keepalive: true }).catch(() => {});
  }

  return {
    join, listen, send, apply, drift, paint, menu, close, target,
    get group() { return group; },
    quiet: () => Date.now() < quietUntil,
  };
}
