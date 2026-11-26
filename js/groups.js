// "Watch together": live group list (one connection per tab, shared by all the pages)
// and creating a group in advance (waiting room).
import { post } from './api.js';
import { field, h, modal, toggle } from './ui.js';
import { tr } from './i18n.js';

let source = null;
let groups = [];
const listeners = new Set();

export function currentGroups() { return groups; }

// fn(groups) called right away then at each change; returns what unsubscribes
export function onGroups(fn) {
  listeners.add(fn);
  fn(groups);
  return () => listeners.delete(fn);
}

// paused: player in fullscreen, nobody sees the list; the connection is given back to the video segments
// (presence in a group then goes through the group's own stream)
export function startGroupsFeed({ paused = false } = {}) {
  wanted = true;
  // member of a group: the list stays open, it is what keeps us present (trailer, preview…)
  feedPaused = paused && !groups.some((g) => g.member);
  if (feedPaused) closeFeed();
  else if (!document.hidden) openFeed();
}

function closeFeed() {
  if (source) source.close();
  source = null;
  reopened = true;
}

let reopened = true; // the first send after (re)opening: what changed meanwhile is unknown
export function isFreshFeed() { return reopened; }

function openFeed() {
  if (source || typeof EventSource === 'undefined') return;
  source = new EventSource('/api/together/feed');
  source.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type !== 'groups') return;
    groups = msg.groups;
    for (const fn of [...listeners]) { try { fn(groups); } catch (err) { console.warn(err); } }
    reopened = false;
  };
  // network cut: the browser reconnects by itself; refusal from the server (too many connections…): it gives up,
  // on retente plus tard
  const es = source;
  source.onerror = () => {
    if (es.readyState !== EventSource.CLOSED || source !== es) return;
    source = null;
    setTimeout(() => { if (wanted && !feedPaused && !document.hidden) openFeed(); }, 10000);
  };
}

// Tab in the background: the connection is released (browsers limit their number per server),
// unless one belongs to a group (stay present in the room while doing something else).
let wanted = false;
let feedPaused = false;
let hiddenTimer = null;
document.addEventListener('visibilitychange', () => {
  clearTimeout(hiddenTimer);
  if (!wanted) return;
  if (document.hidden) {
    hiddenTimer = setTimeout(() => {
      if (document.hidden && !groups.some((g) => g.member)) closeFeed();
    }, 30000);
  } else if (!feedPaused) {
    openFeed();
  }
});

export function stopGroupsFeed() {
  wanted = false;
  closeFeed();
  groups = [];
  for (const fn of [...listeners]) { try { fn(groups); } catch { /* ignore */ } }
}

// Groups where a title can be started: those one is a member of and can control
export function launchableGroups() {
  return groups.filter((g) => g.member && (g.is_host || !g.host_only));
}

export async function createGroupDialog(me) {
  const name = h('input', { placeholder: tr('Chez {display_name}', { display_name: me.user.display_name }), maxlength: 60 });
  const hostOnly = toggle(tr('Seul l’hôte contrôle la lecture (pause, avance, épisode suivant)'), false);
  const err = h('p.form-error');
  const g = await modal(tr('Créer un groupe'), h('div.form',
    h('p.muted.small', tr('Le groupe est créé tout de suite et son lien peut être partagé. Les membres attendent dans le salon ; le titre lancé ensuite démarre en même temps pour tous.')),
    field(tr('Nom du groupe'), name), hostOnly, err), [
    { label: tr('Annuler'), value: null },
    { label: tr('Créer le groupe'), primary: true, handler: async () => {
      try {
        return await post('/api/together', { name: name.value.trim() || undefined, host_only: hostOnly.querySelector('input').checked });
      } catch (e) { err.textContent = e.message; return false; }
    } },
  ]);
  if (g) location.hash = `#/together/${g.id}`;
}
