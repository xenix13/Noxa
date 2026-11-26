import { get, post, setUnauthorizedHandler } from './api.js';
import { arrange, avatar, cancelImages, clear, episodeCode, errorBox, h, icon, img, spinner, toast } from './ui.js';
import { renderLogin, renderSetup } from './views/auth.js';
import { renderProfiles } from './views/profiles.js';
import { renderCollection, renderPlaylist, renderPlaylists, renderWatchlist } from './views/lists.js';
import { renderDiscover } from './views/discover.js';
import { renderHome } from './views/home.js';
import { renderLibrary } from './views/library.js';
import { renderItem } from './views/item.js';
import { renderSearch } from './views/search.js';
import { renderHistory } from './views/history.js';
import { renderSettingsArea } from './views/settings.js';
import { renderPlayer } from './player.js';
import { renderTogether } from './views/together.js';
import { renderPerson } from './views/person.js';
import { renderRemote } from './views/remote.js';
import { renderShared } from './views/photos.js';
import { renderBook } from './views/books.js';
import './remote.js'; // remote control, keyboard and gamepad
import { aboutDialog } from './about.js';
import { applyDevice, device } from './device.js';
import { createGroupDialog, currentGroups, isFreshFeed, onGroups, startGroupsFeed, stopGroupsFeed } from './groups.js';
import { canInstall, install, onInstallChange, setupInstall } from './install.js';
import { shortcutsHelp } from './keys.js';
import { forgetMe, list as listDownloads, pump, rememberMe, rememberedMe, supported as offlineSupported, syncProgress } from './offline.js';
import { renderDownloads } from './views/downloads.js';
import { followUpdate, resumeUpdate, updatePending } from './updater.js';
import { renderLive, renderLivePlayer } from './views/live.js';
import { lang, plural, tr, useLanguage } from './i18n.js';

export const state = { me: null, status: null };
const root = document.getElementById('app');
let cleanup = null;
let renderToken = 0;
// Going back: each visited page carries a number (history.state), and its scroll position is kept;
// coming back to a page already seen (Back button) reopens it where it was left
const scrolls = new Map();
let pageSeq = 0, pageNo = 0;
function enterPage() {
  if (pageNo) scrolls.set(pageNo, window.scrollY);
  let n = history.state && history.state.n;
  const back = Boolean(n) && n <= pageSeq && scrolls.has(n);
  if (!n || n > pageSeq) {
    n = ++pageSeq;
    try { history.replaceState({ ...(history.state || {}), n }, ''); } catch { /* historique indisponible */ }
  }
  pageNo = n;
  return back ? scrolls.get(n) : 0;
}
function restoreScroll(y, token) {
  if (!y) return;
  // the page is still growing (pictures, rows): try again while it is not tall enough
  let tries = 0;
  const go = () => {
    if (token !== renderToken) return;
    window.scrollTo(0, y);
    if (Math.abs(window.scrollY - y) > 4 && ++tries < 12) setTimeout(go, 120);
  };
  go();
}
// mini player: it keeps playing during navigation, in a corner of the screen (outside #app)
let mini = null;

function closeMini() {
  if (!mini) return;
  const m = mini;
  mini = null;
  m.stop();
  m.el.remove();
}

function minimize(m) {
  let dock = document.getElementById('mini-dock');
  if (!dock) {
    dock = h('div', { id: 'mini-dock' });
    document.body.append(dock);
  }
  closeMini();
  mini = m;
  cleanup = null; // the next page must not stop playback
  dock.append(m.el);
}

// gradient wallpapers (Settings → Appearance → Wallpaper)
export const GRADIENTS = {
  aurora: 'linear-gradient(135deg, #0f2027, #203a43 45%, #2c5364)',
  sunset: 'linear-gradient(135deg, #3a1c71, #d76d77 55%, #ffaf7b)',
  ocean: 'linear-gradient(135deg, #0b1d3a, #1a4f8b 55%, #2bc0e4)',
  forest: 'linear-gradient(135deg, #0b2312, #1f5130 55%, #6a9f58)',
  berry: 'linear-gradient(135deg, #2b0a3d, #6a1b5c 55%, #c2456f)',
  ember: 'linear-gradient(135deg, #1a0a05, #6b1d0b 55%, #e2582b)',
  steel: 'linear-gradient(135deg, #16181d, #343a46 55%, #6b7486)',
  mint: 'linear-gradient(135deg, #0d2b2b, #16645e 55%, #6fd3b6)',
};
const systemLight = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
let lastPrefs = null;
if (systemLight && systemLight.addEventListener) {
  systemLight.addEventListener('change', () => { if (lastPrefs && lastPrefs.theme === 'auto') applyPrefs(lastPrefs); });
}
let daily = null; // background of the day, asked for once a day
async function dailyWallpaper() {
  const day = new Date().toDateString();
  if (daily && daily.day === day) return daily.path;
  try { daily = { day, path: (await get('/api/me/wallpaper/daily')).path }; } catch { daily = { day, path: null }; }
  return daily.path;
}

// --- installed themes: only checked colours (#rrggbb), numbers and words from a fixed list,
// set as CSS variables; never CSS from elsewhere
const THEME_VARS = { bg: '--bg', surface: '--surface', surface2: '--surface-2', text: '--text', muted: '--muted', line: '--line',
  on_accent: '--ink-on-accent', accent2: '--accent-2' };
const OVERLAY_FX = ['scanlines', 'grain', 'vignette'];
// starry sky: small repeated dots ("stars" effect)
const STARS = ['radial-gradient(1px 1px at 20px 30px, #fff9 50%, transparent 51%)', 'radial-gradient(1px 1px at 120px 80px, #fff7 50%, transparent 51%)',
  'radial-gradient(1.5px 1.5px at 60px 150px, #fffa 50%, transparent 51%)', 'radial-gradient(1px 1px at 170px 200px, #fff6 50%, transparent 51%)',
  'radial-gradient(1.5px 1.5px at 230px 40px, #fff8 50%, transparent 51%)'];

export function customTheme(prefs) {
  if (!prefs || typeof prefs.theme !== 'string' || !prefs.theme.startsWith('t:')) return null;
  return (prefs.my_themes || []).find((t) => `t:${t.id}` === prefs.theme) || null;
}

export function paintTheme(t) {
  const r = document.documentElement;
  for (const v of Object.values(THEME_VARS)) r.style.removeProperty(v);
  for (const v of ['--radius', '--theme-bg', '--theme-bg-size']) r.style.removeProperty(v);
  r.style.removeProperty('color-scheme');
  delete r.dataset.fx;
  delete r.dataset.overlay;
  delete r.dataset.font;
  r.classList.remove('has-theme-bg');
  if (!t) return;
  for (const [k, v] of Object.entries(THEME_VARS)) if (t.colors[k]) r.style.setProperty(v, t.colors[k]);
  if (!t.colors.line) r.style.setProperty('--line', `color-mix(in srgb, ${t.colors.text} 10%, transparent)`);
  if (!t.colors.accent2) r.style.setProperty('--accent-2', 'var(--accent)');
  r.style.setProperty('--radius', `${t.radius}px`);
  r.style.setProperty('color-scheme', t.dark ? 'dark' : 'light');
  if (t.font && t.font !== 'default') r.dataset.font = t.font;
  const fx = t.effects || [];
  if (fx.length) r.dataset.fx = fx.join(' ');
  if (fx.some((f) => OVERLAY_FX.includes(f))) r.dataset.overlay = '1';
  const layers = [];
  if (fx.includes('stars')) layers.push(...STARS);
  if (t.background) layers.push(`linear-gradient(${t.background.angle}deg, ${t.background.stops.join(', ')})`);
  if (layers.length) {
    r.classList.add('has-theme-bg');
    r.style.setProperty('--theme-bg', layers.join(', '));
    r.style.setProperty('--theme-bg-size', [...(fx.includes('stars') ? STARS.map(() => '260px 260px') : []), ...(t.background ? ['100% 100%'] : [])].join(', '));
  }
}

export function applyPrefs(prefs) {
  const r = document.documentElement;
  lastPrefs = prefs;
  r.style.setProperty('--accent', prefs.accent);
  // installed theme (store, import, editor): its colours and effects; otherwise a built-in theme
  const custom = customTheme(prefs);
  paintTheme(custom);
  // automatic theme: light or dark like the device (and follows its change)
  r.dataset.theme = custom ? (custom.dark ? 'dark' : 'light')
    : prefs.theme === 'auto' ? (systemLight && systemLight.matches ? 'light' : 'dark') : prefs.theme;
  // interface style (layout): this device's when it has one, otherwise the profile's
  r.dataset.style = device().ui_style || prefs.ui_style || 'classic';
  r.dataset.text = prefs.text_size || 'm';
  r.dataset.density = prefs.density || 'normal';
  for (const [cls, on] of [['cards-no-titles', prefs.card_titles === false], ['cards-no-progress', prefs.card_progress === false],
    ['cards-no-watched', prefs.card_watched === false], ['cards-no-quality', prefs.card_quality === false],
    ['cards-no-new', prefs.card_new === false], ['cards-rating', !!prefs.card_rating]]) r.classList.toggle(cls, on);
  const kids = !!(state.me && state.me.user && state.me.user.kids);
  r.classList.toggle('kids', kids); // kids interface: big thumbnails, simplified menu
  r.dataset.poster = kids ? 'l' : prefs.poster_size;
  r.classList.toggle('no-spoilers', !!prefs.hide_spoilers);
  // profile wallpaper, faded behind the interface: a title's picture, a colour, a gradient, a different
  // title each day, or an uploaded picture
  const setWall = (css) => {
    r.classList.toggle('has-wallpaper', !!css);
    if (css) r.style.setProperty('--wallpaper', css); else r.style.removeProperty('--wallpaper');
  };
  const mode = prefs.wallpaper_mode || 'title';
  if (mode === 'title') setWall(prefs.wallpaper ? `url("${img(prefs.wallpaper, 960, 540)}")` : ''); // blurred: no need for more
  else if (mode === 'color') setWall(`linear-gradient(${prefs.wallpaper_color}, ${prefs.wallpaper_color})`);
  else if (mode === 'gradient') setWall(GRADIENTS[prefs.wallpaper_gradient] || GRADIENTS.aurora);
  else if (mode === 'upload') setWall(`url("/api/me/wallpaper/image?v=${prefs.wallpaper_version || state.wallVersion || 0}")`);
  else if (mode === 'daily') dailyWallpaper().then((p) => { if (lastPrefs === prefs) setWall(p ? `url("${img(p, 960, 540)}")` : ''); });
  else setWall('');
  // status bar of the phone / installed window in the theme's colour
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = getComputedStyle(r).getPropertyValue('--bg').trim() || '#0b0b0e';
}

export async function refreshMe() {
  state.me = await get('/api/me');
  state.offline = false;
  if (state.me.user.language !== lang && /^#\/(login|profiles)/.test(location.hash)) {
    history.replaceState(null, '', '#/'); // profile chosen: after the reload, land on the home page
  }
  if (useLanguage(state.me.user.language)) return new Promise(() => {}); // reload in the account's language
  applyPrefs(state.me.prefs);
  rememberMe(state.me); // to find one's offline downloads
  syncProgress(state.me.user.id); // positions saved offline
  pump(); // downloads to resume
  return state.me;
}

// Offline (server unreachable): switch to the downloads of the last person signed in
async function offlineMode(e, page, parts, token) {
  const me = rememberedMe();
  if (e.status !== 0 || !me || !offlineSupported() || !(await listDownloads(me.user.id)).length) return false;
  if (token !== renderToken) return true;
  state.me = me;
  state.offline = true;
  applyPrefs(me.prefs);
  const ctx = { query: new URLSearchParams(), parts, isCurrent: () => token === renderToken,
    setCleanup: (fn) => { cleanup = fn; }, offline: true };
  if (page === 'offline' && parts[1]) {
    document.body.classList.add('playing');
    document.title = 'Noxa';
    const stopPlayer = await renderPlayer(root, parts[1], ctx);
    if (token === renderToken) cleanup = stopPlayer; else stopPlayer();
    return true;
  }
  document.body.classList.remove('playing');
  document.title = tr('Téléchargements · Noxa');
  const main = h('main.content');
  clear(root, h('header.topbar.offline-bar', h('span.logo', h('span.logo-mark', { 'aria-hidden': 'true' }), 'Noxa'),
    h('div.grow'), h('button.btn.small', { onclick: () => location.reload() }, tr('Réessayer la connexion'))), main);
  await renderDownloads(main, ctx, { offline: true });
  return true;
}
// anti-spoiler: a click on a hidden overview shows it
document.addEventListener('click', (e) => {
  const sp = e.target.closest && e.target.closest('.no-spoilers .spoiler:not(.revealed)');
  if (sp) { sp.classList.add('revealed'); e.preventDefault(); }
});
window.addEventListener('online', () => {
  if (!state.offline) return;
  state.status = null;
  state.me = null;
  toast(tr('Connexion retrouvée'), 'ok');
  route();
});

// The proxy (nginx…) answers "Bad Gateway" while Noxa restarts: retry automatically
function restarting(token) {
  clear(root, h('div.center-page', h('div.restart-box', spinner(), h('h2', tr('Noxa redémarre…')),
    h('p.muted', tr('La page se rechargera automatiquement dès que le serveur répond.')))));
  const retry = async () => {
    if (token !== renderToken) return;
    try { await get('/api/status'); } catch { setTimeout(retry, 3000); return; }
    route();
  };
  setTimeout(retry, 3000);
}

function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [path, qs] = raw.split('?');
  return { parts: path.split('/').filter(Boolean), query: new URLSearchParams(qs || '') };
}

// icon shown depending on the style (sidebar, rounded); the text stays the link's label
function navLink(href, label, active, ico) {
  return h('a.nav-link', { href, class: active ? 'nav-link active' : 'nav-link', title: label },
    icon(ico || 'folder', 'nav-ico'), h('span.nav-label', label));
}
const LIB_ICONS = { movie: 'film', show: 'tv', photo: 'folder', audiobook: 'headphones', music: 'headphones' };

// Top bar links, in the order chosen by the user (Settings → Home and menu)
let startPending = true;
applyDevice(); // settings specific to this device (animations, remote control)

export function navItems(me) {
  const items = [{ key: 'home', href: '#/', label: tr('Accueil'), fixed: true, icon: 'home' },
    { key: 'watchlist', href: '#/watchlist', label: tr('À regarder'), icon: 'bookmark' }];
  const kids = me.user && me.user.kids;
  if (me.features && me.features.discover && !kids) items.push({ key: 'discover', href: '#/discover', label: tr('Découvrir'), icon: 'star' });
  for (const l of me.libraries) items.push({ key: `lib:${l.id}`, href: `#/library/${l.id}`, label: l.title, icon: LIB_ICONS[l.type] || 'folder' });
  if (me.features && me.features.live && !kids) items.push({ key: 'live', href: '#/live', label: tr('TV en direct'), icon: 'guide' });
  const byKey = new Map(items.map((i) => [i.key, i]));
  return arrange(items.map((i) => i.key), me.prefs.nav_order).map((k) => byKey.get(k));
}

function navLinks(me, active) {
  return navItems(me).filter((i) => i.fixed || !me.prefs.nav_hidden.includes(i.key))
    .map((i) => navLink(i.href, i.label, active === i.key, i.icon));
}

// after a change in Settings: the links are redrawn without reloading the page
// New episodes of followed shows: bell with the unread count, list on click (marked read when opened)
function notifBell(me) {
  const badge = h('span.notif-badge', { hidden: !me.notifications }, String(me.notifications || ''));
  const list = h('div.notif-menu.user-menu', { hidden: true, role: 'menu' });
  const btn = h('button.icon-btn.notif-btn', { type: 'button', 'aria-label': tr('Notifications'), title: tr('Nouveaux épisodes'),
    'aria-haspopup': 'true', onclick: async (e) => {
      e.stopPropagation();
      document.querySelectorAll('.user-menu').forEach((m) => { if (m !== list) m.hidden = true; });
      list.hidden = !list.hidden;
      if (list.hidden) return;
      list.replaceChildren(h('div.notif-head', tr('Nouveaux épisodes')), spinner());
      try {
        const r = await get('/api/notifications');
        list.replaceChildren(h('div.notif-head', tr('Nouveaux épisodes')),
          ...(r.items.length ? r.items.map((n) => h('a.notif-item', { href: `#/item/${n.show.id}`, class: n.unread ? 'notif-item unread' : 'notif-item' },
            n.show.thumb ? h('img', { src: img(n.show.thumb, 80, 120), alt: '', loading: 'lazy' }) : h('span.noimg'),
            h('span.notif-text', h('strong', n.show.title),
              h('span.muted', n.count > 1 ? tr('{count} nouveaux épisodes', { count: n.count }) : tr('Nouvel épisode : {p1} · {title}', { p1: episodeCode(n.episode), title: n.episode.title })))))
            : [h('p.muted.small.notif-empty', tr('Aucun nouvel épisode pour les séries suivies.'))]),
          h('p.muted.small.notif-foot', tr('Séries suivies : celles dont un épisode a été regardé, ou ajoutées à « À regarder ».')));
        if (r.unread) {
          await post('/api/notifications/seen');
          me.notifications = 0;
          badge.hidden = true;
        }
      } catch (err) { list.replaceChildren(h('p.form-error.small', err.message)); }
    } }, icon('bell'), badge);
  return h('div.user-wrap.notif-wrap', btn, list);
}

// Profile name or picture changed: the top bar follows without reloading the page
export function repaintUser() {
  const me = state.me;
  if (!me) return;
  const btn = document.querySelector('.user-btn .avatar');
  if (btn) btn.replaceWith(avatar(me.user, 34));
  const head = document.querySelector('.user-menu-head');
  if (head) head.replaceChildren(avatar(me.user, 36), h('div', h('strong', me.user.display_name), h('div.muted', `@${me.user.username}`)));
}

export function repaintNav() {
  const nav = document.querySelector('header.topbar .nav');
  if (nav && state.me) nav.replaceChildren(...navLinks(state.me, null));
}

function shell(active) {
  const me = state.me;
  const search = h('input.search-input', { type: 'search', placeholder: tr('Rechercher…'), 'aria-label': tr('Rechercher'),
    value: active === 'search' ? (parseHash().query.get('q') || '') : '' });
  let t;
  search.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => {
      const q = search.value.trim();
      if (q.length >= 2) location.hash = `#/search?q=${encodeURIComponent(q)}`;
    }, 350);
  });
  const menu = h('div.user-menu', { hidden: true },
    h('div.user-menu-head', avatar(me.user, 36), h('div', h('strong', me.user.display_name),
      h('div.muted', `@${me.user.username}`))),
    me.user.home ? h('a', { href: '#/profiles' }, tr('Changer de profil')) : null,
    h('a', { href: '#/playlists' }, tr('Mes playlists')),
    h('a', { href: '#/together' }, tr('Regarder ensemble')),
    h('a', { href: '#/remote' }, tr('Télécommande')),
    h('a', { href: '#/history' }, tr('Historique')),
    me.features && me.features.downloads && offlineSupported() ? h('a', { href: '#/downloads' }, tr('Téléchargements')) : null,
    h('a', { href: '#/settings' }, tr('Paramètres')),
    // install Noxa (native or web app, and the other devices); inside the app: the Apps page
    h('button.install-app', { hidden: !canInstall(), onclick: () => install() }, icon('install'), tr('Installer l’application')),
    h('a.apps-link', { href: '#/apps', hidden: canInstall() }, tr('Applications')),
    h('button', { onclick: () => aboutDialog(state.status && state.status.version) }, tr('À propos')),
    h('button', { onclick: logout }, tr('Se déconnecter')),
    state.status ? h('div.menu-version', `Noxa ${state.status.version}`) : null);
  const userBtn = h('button.user-btn', { 'aria-label': tr('Menu utilisateur'), 'aria-haspopup': 'true',
    onclick: (e) => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
      const first = !menu.hidden && menu.querySelector('a, button:not([hidden])');
      if (first) first.focus({ preventScroll: true });
    } }, avatar(me.user, 34));
  const header = h('header.topbar',
    // "back" button: the desktop app and the installed web app do not have the browser's
    h('button.icon-btn.topbar-back', { type: 'button', 'aria-label': tr('Retour'), title: tr('Retour'), hidden: active === 'home',
      onclick: () => { if (history.length > 1) history.back(); else location.hash = '#/'; } }, icon('back')),
    h('a.logo', { href: '#/', 'aria-label': tr('Noxa — accueil') }, h('span.logo-mark', { 'aria-hidden': 'true' }), 'Noxa'),
    h('nav.nav', navLinks(me, active)),
    h('div.topbar-right', h('span.together-slot', { dataset: { active } }),
      h('button.icon-btn.group-new', { type: 'button', title: tr('Créer un groupe « Regarder ensemble »'), 'aria-label': tr('Créer un groupe'),
        onclick: () => createGroupDialog(me) }, icon('group'), h('span.plus', '+')),
      me.update ? h('a.icon-btn.update-btn', { href: '#/admin/update', title: tr('Noxa {update} est disponible', { update: me.update }),
        'aria-label': tr('Mise à jour disponible : Noxa {update}', { update: me.update }) }, icon('upgrade')) : null,
      h('label.search', icon('search'), search), notifBell(me), h('div.user-wrap', userBtn, menu)));
  paintTogetherSlot(header.querySelector('.together-slot'));
  const main = h('main.content');
  const banner = me.warning ? h('div.banner-error', me.warning) : null;
  clear(root, header, banner, main);
  // transparent bar above the home picture, opaque as soon as the page scrolls
  const onScroll = () => header.classList.toggle('scrolled', window.scrollY > 24);
  onScroll();
  window.removeEventListener('scroll', window.__nexisScroll || (() => {}));
  window.__nexisScroll = onScroll;
  window.addEventListener('scroll', onScroll, { passive: true });
  return main;
}

// "Watch together" shortcut in the top bar, kept up to date by the live list
function paintTogetherSlot(slot, list = currentGroups()) {
  if (!slot) return;
  const mine = list.find((g) => g.member);
  let pill = null;
  if (mine) {
    pill = h('a.group-pill.mine', { href: `#/together/${mine.id}`, title: tr('Votre groupe : {name}', { name: mine.name }) }, icon('group'),
      mine.item ? mine.name : tr('{name} · salon', { name: mine.name }), h('span.group-count', String(mine.members.length)));
  } else if (list.length && slot.dataset.active !== 'together') {
    pill = h('a.group-pill', { href: '#/together', title: tr('Regarder ensemble') }, icon('group'),
      list.length === 1 ? tr('{owner} regarde', { owner: list[0].owner }) : plural(list.length, '{n} groupe', '{n} groupes'));
  }
  slot.replaceChildren(...(pill ? [pill] : []));
}
onGroups((list) => paintTogetherSlot(document.querySelector('.topbar .together-slot'), list));
// playback started in a group one belongs to, while browsing elsewhere: suggest joining it
const launched = new Map();
onGroups((list) => {
  const fresh = isFreshFeed(); // list reopened (back from the player…): what was missed is unknown, suggest nothing
  for (const g of list) {
    const was = launched.get(g.id);
    const now = g.member && g.item && !g.item.hidden ? g.item.id : null;
    launched.set(g.id, now);
    if (now && was === null && !fresh && !location.hash.includes(`group=${g.id}`) && !location.hash.startsWith(`#/together/${g.id}`)) {
      toast(tr('{name} : la lecture commence', { name: g.name }), 'ok', { label: tr('Rejoindre'), onClick: () => { location.hash = `#/play/${now}?group=${g.id}`; } });
    }
  }
});

async function logout() {
  closeMini();
  forgetMe(); // shared device: offline mode no longer shows this person's downloads
  stopGroupsFeed();
  try { await post('/api/logout'); } catch { /* ignore */ }
  state.me = null;
  location.hash = '#/login';
}

async function route() {
  const token = ++renderToken;
  if (cleanup) { try { cleanup(); } catch { /* ignore */ } cleanup = null; }
  cancelImages(root);
  const { parts, query } = parseHash();
  const page = parts[0] || '';
  const backTo = enterPage();
  window.scrollTo(0, 0);

  if (!state.status) {
    try { state.status = await get('/api/status'); } catch (e) {
      if (updatePending()) { followUpdate(); return; } // page reloaded during an update
      if ([502, 503, 504].includes(e.status)) { restarting(token); return; }
      if (await offlineMode(e, page, parts, token)) return;
      clear(root, h('div.center-page', errorBox(e, () => location.reload())));
      return;
    }
    if (await resumeUpdate(state.status)) return;
  }
  if (page === 'apps') {
    // apps to install: readable without being signed in (link from the sign-in page)
    document.body.classList.remove('playing');
    if (!state.me) {
      try { await get('/api/me', { quiet401: true }); await refreshMe(); } catch { /* not signed in */ }
    }
    if (token !== renderToken) return;
    const { renderApps } = await import('./views/apps.js');
    if (state.me) { await renderApps(shell('apps')); return; }
    const main = h('main.main.share-page');
    clear(root, h('header.share-head', h('a.logo', { href: '#/' }, h('span.logo-mark', { 'aria-hidden': 'true' }), 'Noxa')), main);
    await renderApps(main);
    return;
  }
  if (page === 'share' && parts[1]) {
    // photo album shared by link: viewable without an account
    document.body.classList.remove('playing');
    const main = h('main.main.share-page');
    clear(root, h('header.share-head', h('span.logo', h('span.logo-mark', { 'aria-hidden': 'true' }), 'Noxa')), main);
    const ctx = { query, parts, isCurrent: () => token === renderToken, setCleanup: (fn) => { cleanup = fn; } };
    try { await renderShared(main, parts[1], ctx); } catch (e) { clear(main, h('div.empty', e.message)); }
    return;
  }
  if (state.status.setup) {
    document.title = tr('Installation · Noxa');
    return renderSetup(root, () => { state.status = null; state.me = null; location.hash = '#/admin/libraries'; route(); });
  }
  if (page === 'login' || page === 'profiles') stopGroupsFeed(); // the list follows the signed-in person
  if (page === 'login') {
    document.title = tr('Connexion · Noxa');
    return renderLogin(root, async () => { await refreshMe(); location.hash = '#/profiles'; });
  }
  if (page === 'profiles') {
    document.title = tr('Qui regarde ? · Noxa');
    if (!state.me) {
      try { await refreshMe(); } catch { location.hash = '#/login'; return; }
    }
    document.body.classList.remove('playing');
    return renderProfiles(root, async () => { await refreshMe(); startPending = true; location.replace('#/'); });
  }
  if (!state.me) {
    try { await refreshMe(); } catch (e) {
      if (e.status === 401) { location.hash = '#/login'; return; }
      if (await offlineMode(e, page, parts, token)) return;
      clear(root, h('div.center-page', errorBox(e, route)));
      return;
    }
  }
  if (token !== renderToken) return;
  // start page chosen by the user (Settings → Home and menu), only when the app opens
  if (startPending) {
    startPending = false;
    const target = page === '' ? navItems(state.me).find((i) => i.key === state.me.prefs.start_page && i.key !== 'home') : null;
    if (target) { location.replace(target.href); return; }
  }
  startGroupsFeed({ paused: page === 'play' || page === 'tv' });

  const ctx = { query, parts, isCurrent: () => token === renderToken, setCleanup: (fn) => { cleanup = fn; },
    minimize, closeMini };
  if (page === 'play' && mini && mini.itemId === parts[1]) {
    // the mini player is enlarged: playback goes on without reloading
    document.body.classList.add('playing');
    const m = mini;
    mini = null;
    m.restore(root);
    cleanup = m.stop;
    return;
  }
  if (page === 'offline') ctx.offline = true; // download played from the device (also online: no network used)
  if (page === 'play' || page === 'tv' || page === 'offline') {
    closeMini(); // another video replaces the mini player's
    document.body.classList.add('playing');
    const stopPlayer = page === 'tv' ? await renderLivePlayer(root, parts[1], ctx) : await renderPlayer(root, parts[1], ctx);
    if (token === renderToken) cleanup = stopPlayer;
    else stopPlayer();
    return;
  }
  document.body.classList.remove('playing');
  const active = page === '' ? 'home' : page === 'library' ? `lib:${parts[1]}` : page;
  const main = shell(active);
  main.append(spinner());
  const titles = { '': tr('Accueil'), book: tr('Livre audio'), live: tr('TV en direct'), together: tr('Regarder ensemble'), watchlist: tr('À regarder'), discover: tr('Découvrir'), playlists: tr('Mes playlists'), search: tr('Recherche'), history: tr('Historique'), downloads: tr('Téléchargements'), settings: tr('Paramètres'), admin: tr('Paramètres') };
  document.title = `${titles[page] || 'Noxa'} · Noxa`;
  try {
    switch (page) {
      case '': await renderHome(main, ctx); break;
      case 'library': await renderLibrary(main, parts[1], ctx); break;
      case 'item': await renderItem(main, parts[1], ctx); break;
      case 'book': await renderBook(main, parts[1], ctx); break;
      case 'person': await renderPerson(main, decodeURIComponent(parts.slice(1).join('/')), ctx); break;
      case 'remote': await renderRemote(main, ctx); break;
      case 'search': await renderSearch(main, query.get('q') || '', ctx); break;
      case 'collection': await renderCollection(main, parts[1], ctx); break;
      case 'watchlist': await renderWatchlist(main, ctx); break;
      case 'discover': await renderDiscover(main, ctx); break;
      case 'live': await renderLive(main, ctx); break;
      case 'together': await renderTogether(main, parts[1], ctx); break;
      case 'downloads': await renderDownloads(main, ctx); break;
      case 'playlists': await renderPlaylists(main, ctx); break;
      case 'playlist': await renderPlaylist(main, parts[1], ctx); break;
      case 'history': await renderHistory(main, ctx); break;
      case 'settings': await renderSettingsArea(main, 'settings', parts[1], ctx); break;
      case 'admin':
        if (!state.me.user.is_admin) { location.hash = '#/'; return; }
        await renderSettingsArea(main, 'admin', parts[1], ctx); break;
      default: clear(main, h('div.empty', tr('Page introuvable.')));
    }
  } catch (e) {
    if (token === renderToken) clear(main, errorBox(e, route));
  }
  if (token === renderToken) restoreScroll(backTo, token);
}

setUnauthorizedHandler(() => {
  state.me = null;
  stopGroupsFeed();
  if (!location.hash.startsWith('#/login')) {
    toast(tr('Session expirée. Veuillez vous reconnecter.'));
    location.hash = '#/login';
  }
});

document.addEventListener('error', (e) => {
  if (e.target && e.target.tagName === 'IMG') e.target.classList.add('broken');
}, true);
document.addEventListener('click', () => {
  document.querySelectorAll('.user-menu, .notif-menu').forEach((m) => { m.hidden = true; });
});
// Page pictures: fade in once loaded (those already cached show right away).
function fadeImage(im) {
  if (im.complete || im.classList.contains('fade') || im.closest('.player')) return;
  im.classList.add('fade');
  const done = () => im.classList.add('in');
  im.addEventListener('load', done, { once: true });
  im.addEventListener('error', done, { once: true });
  setTimeout(done, 8000); // safety net: never a picture left invisible
}
new MutationObserver((records) => {
  for (const r of records) {
    for (const n of r.addedNodes) {
      if (n.nodeType !== 1) continue;
      if (n.tagName === 'IMG') fadeImage(n);
      else n.querySelectorAll('img').forEach(fadeImage);
    }
  }
}).observe(document.getElementById('app') || document.body, { childList: true, subtree: true });

setupInstall();
// shortcuts everywhere: "?" shows the help, "/" puts the cursor in the search
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || (e.target.closest && e.target.closest('input, select, textarea, [contenteditable]'))) return;
  if (document.querySelector('.modal-overlay')) return;
  if (e.key === '?') { e.preventDefault(); shortcutsHelp(); }
  else if (e.key === '/' && !location.hash.startsWith('#/play')) {
    const s = document.querySelector('.search-input');
    if (s) { e.preventDefault(); s.focus(); s.select(); }
  }
});
onInstallChange(() => {
  document.querySelectorAll('.install-app').forEach((b) => { b.hidden = !canInstall(); });
  document.querySelectorAll('.apps-link').forEach((b) => { b.hidden = canInstall(); });
});

window.addEventListener('hashchange', route);
route();
