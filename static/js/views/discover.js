// « Découvrir » : catalogues Netflix, Prime Video, Disney+… et demandes via Seerr.
import { get, post } from '../api.js';
import { state } from '../app.js';
import { clear, h, icon, modal, row, select, spinner, toast } from '../ui.js';
import { tr, plural } from '../i18n.js';
import { dataCredit } from '../about.js';

export const STATUS_LABEL = {
  available: tr('Disponible'), partial: tr('En partie disponible'), processing: tr('En cours de téléchargement'),
  pending: tr('Demandé, en attente de validation'), blocked: tr('Refusé'), deleted: null,
};
const STATUS_SHORT = { available: tr('Disponible'), partial: tr('Partiel'), processing: tr('Bientôt'), pending: tr('Demandé'), blocked: tr('Refusé') };

export function tmdbImg(path, kind = 'poster') {
  return path ? `/api/discover/img?p=${encodeURIComponent(path)}&k=${kind}` : '';
}

const titleHref = (c) => (c.item_id ? `#/item/${c.item_id}` : `#/discover/title/${c.media_type}/${c.tmdb_id}`);

// Demande depuis une carte, sans quitter la liste : film tout de suite, série après le choix des saisons
async function quickRequest(c, btn, paint) {
  btn.disabled = true;
  try {
    let seasons = null;
    if (c.media_type === 'tv') {
      const t = await get(`/api/discover/title/tv/${c.tmdb_id}`);
      if (t.seasons.length > 1) {
        seasons = await chooseSeasons(t);
        if (!seasons) { btn.disabled = false; return; }
      }
    }
    await post('/api/discover/request', { type: c.media_type, tmdb_id: c.tmdb_id, seasons: Array.isArray(seasons) ? seasons : undefined });
    c.status = 'pending';
    toast(tr('« {title} » demandé', { title: c.title }), 'ok');
    paint();
  } catch (e) { toast(e.message, 'bad'); btn.disabled = false; }
}

const shortDate = (d) => { try { return new Date(`${d}T12:00:00`).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }); } catch { return d; } };

// opts.request : bouton « Demander » sur la carte (Seerr branché) ; opts.date : date de sortie à la place de l'année
export function discoverCard(c, opts = {}) {
  let badge = h('span', { hidden: true });
  const ask = h('button.dcard-ask', { type: 'button', title: tr('Demander'), 'aria-label': tr('Demander « {title} »', { title: c.title }) }, '+');
  const paint = () => {
    const st = STATUS_SHORT[c.status];
    const next = st ? h('span.dbadge', { class: `dbadge ${c.status}` }, c.status === 'available' ? icon('check') : null, st)
      : c.wished ? h('span.dbadge.wished', icon('bookmarked')) : h('span', { hidden: true });
    badge.replaceWith(next);
    badge = next;
    ask.hidden = !(opts.request && !c.item_id && !c.status);
  };
  ask.onclick = (e) => { e.preventDefault(); e.stopPropagation(); quickRequest(c, ask, paint); };
  const upcoming = opts.date && c.date && c.date > new Date().toISOString().slice(0, 10);
  const card = h('a.card.poster.dcard', { href: titleHref(c), title: c.title },
    h('div.card-img', c.poster ? h('img', { src: tmdbImg(c.poster), alt: '', loading: 'lazy' }) : h('div.noimg', c.title), badge, ask),
    h('div.card-title', c.title),
    h('div.card-sub', [upcoming ? shortDate(c.date) : c.year, c.media_type === 'tv' ? tr('Série') : tr('Film')].filter(Boolean).join(' · ')));
  paint();
  return card;
}

// Recherche : résultats pendant la frappe (sans remplir l'historique), Entrée pour valider
function searchBox(value) {
  const input = h('input', { type: 'search', value, placeholder: tr('Un film, une série…'), 'aria-label': tr('Rechercher un titre') });
  const target = () => { const q = input.value.trim(); return q ? `#/discover?q=${encodeURIComponent(q)}` : '#/discover'; };
  let timer = null;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!input.isConnected || target() === location.hash) return;
      // première lettre : une entrée d'historique (le bouton Retour ramène à Découvrir) ; ensuite on la remplace
      if (new URLSearchParams(location.hash.split('?')[1] || '').get('q') === null) location.hash = target();
      else location.replace(target());
    }, 450);
  });
  return h('form.discover-search', { onsubmit: (e) => { e.preventDefault(); clearTimeout(timer); if (target() !== location.hash) location.hash = target(); } },
    icon('search'), input);
}

const SORT_LABELS = () => [['popular', tr('Populaires')], ['trending', tr('Tendances')], ['upcoming', tr('À venir')],
  ['recent', tr('Les plus récents')], ['rating', tr('Les mieux notés')]];
const browseHref = (o) => `#/discover?${new URLSearchParams(Object.entries(o).filter(([, v]) => v).map(([k, v]) => [k, String(v)]))}`;
const SECTION_TITLES = () => ({ upcoming_movie: tr('Films à venir'), upcoming_tv: tr('Séries à venir'),
  popular_movie: tr('Films populaires'), popular_tv: tr('Séries populaires') });

// raccourcis en haut de Découvrir : les listes les plus utiles en un clic
function shortcuts(active) {
  const items = [[tr('Tendances'), { sort: 'trending', type: 'movie' }], [tr('Films à venir'), { sort: 'upcoming', type: 'movie' }],
    [tr('Séries à venir'), { sort: 'upcoming', type: 'tv' }], [tr('Films populaires'), { sort: 'popular', type: 'movie' }],
    [tr('Séries populaires'), { sort: 'popular', type: 'tv' }], [tr('Les mieux notés'), { sort: 'rating', type: 'movie' }]];
  return h('div.discover-chips', items.map(([label, o]) => h('a.chip-link', { href: browseHref(o),
    class: active && active.sort === o.sort && active.type === o.type ? 'chip-link active' : 'chip-link' }, label)));
}

export async function renderDiscover(main, ctx) {
  if (ctx.parts[1] === 'title') return renderTitle(main, ctx.parts[2], ctx.parts[3], ctx);
  const q = ctx.query.get('q') || '';
  const provider = ctx.query.get('provider');
  const head = h('div.page-head', h('h1', tr('Découvrir')), searchBox(q));
  if (q) {
    const r = await get(`/api/discover/search?q=${encodeURIComponent(q)}`);
    if (!ctx.isCurrent()) return;
    const box = head.querySelector('input');
    const typing = document.activeElement && document.activeElement.type === 'search';
    const by = (k) => r.results.filter((c) => c.media_type === k).sort((a, b) => (b.popularity || 0) - (a.popularity || 0));
    const group = (title, items) => (items.length ? h('section.block', h('h2', `${title} (${items.length})`),
      h('div.grid', items.map((c) => discoverCard(c, { request: r.seerr })))) : null);
    clear(main, head, h('p.muted', tr('Résultats pour « {q} »', { q })),
      r.results.length ? [group(tr('Films'), by('movie')), group(tr('Séries'), by('tv'))] : h('div.empty', tr('Aucun titre trouvé.')));
    if (typing) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); } // on continue de taper
    return;
  }
  if (provider || ctx.query.get('sort') || ctx.query.get('type') || ctx.query.get('genre')) return renderBrowse(main, head, Number(provider) || 0, ctx);
  const data = await get('/api/discover/home');
  if (!ctx.isCurrent()) return;
  const intro = data.seerr ? null : h('p.muted.small.discover-note', state.me.user.is_admin
    ? h('span', tr('Les demandes ne sont pas encore branchées : configure Seerr dans '), h('a', { href: '#/admin/requests' }, tr('Paramètres → Demandes')), '.')
    : tr('Les demandes ne sont pas encore activées sur ce serveur.'));
  const titles = SECTION_TITLES();
  const dcard = (c) => discoverCard(c, { request: data.seerr });
  clear(main, head, shortcuts(null), intro,
    row(tr('Tendances de la semaine'), data.trending, dcard, { href: browseHref({ sort: 'trending', type: 'movie' }) }),
    (data.sections || []).map((s) => row(titles[s.key] || s.key, s.items,
      (c) => discoverCard(c, { request: data.seerr, date: s.sort === 'upcoming' }), { href: browseHref({ sort: s.sort, type: s.type }) })),
    data.providers.map((p) => row(h('span.provider-title', p.logo ? h('img.provider-logo', { src: tmdbImg(p.logo, 'logo'), alt: '' }) : null,
      tr('Sur {name}', { name: p.name })), p.items, dcard, { href: `#/discover?provider=${p.id}` })),
    !data.providers.length && !data.trending.length ? h('div.empty', tr('Catalogue indisponible pour le moment.')) : null,
    dataCredit());
}

// Liste complète : films ou séries, tri (populaires, tendances, à venir…), genre et service, « Voir plus » en bas
async function renderBrowse(main, head, provider, ctx) {
  const type = ctx.query.get('type') === 'tv' ? 'tv' : 'movie';
  const sorts = SORT_LABELS();
  const sort = sorts.some(([v]) => v === ctx.query.get('sort')) ? ctx.query.get('sort') : 'popular';
  const genre = Number(ctx.query.get('genre')) || 0;
  const go = (k, v) => {
    const o = { provider: provider || '', type, sort, genre: genre || '' };
    o[k] = v;
    if (k === 'type') o.genre = '';                                   // les genres diffèrent entre films et séries
    if ((o.provider || o.genre) && o.sort === 'trending') o.sort = 'popular'; // tendances : sans filtre
    location.hash = browseHref(o);
  };
  const grid = h('div.grid');
  const more = h('div.center-row');
  const title = h('h2.browse-title');
  const tabs = h('div.tabs',
    h('a.tab', { href: '#', class: type === 'movie' ? 'tab active' : 'tab', onclick: (e) => { e.preventDefault(); go('type', 'movie'); } }, tr('Films')),
    h('a.tab', { href: '#', class: type === 'tv' ? 'tab active' : 'tab', onclick: (e) => { e.preventDefault(); go('type', 'tv'); } }, tr('Séries')));
  const sortSel = select(sorts, sort, { onchange: (e) => go('sort', e.target.value), 'aria-label': tr('Trier') });
  const genreSel = h('select', { 'aria-label': tr('Genre'), onchange: (e) => go('genre', e.target.value) }, h('option', { value: '' }, tr('Tous les genres')));
  const provSel = h('select', { 'aria-label': tr('Service'), onchange: (e) => go('provider', e.target.value) }, h('option', { value: '' }, tr('Tous les services')));
  clear(main, head, shortcuts(provider || genre ? null : { sort, type }),
    h('div.browse-head', title), h('div.filters-row', tabs, h('div.filters-selects', sortSel, genreSel, provSel)), grid, more);
  let page = 0, pages = 1, filled = false;
  const load = async () => {
    page += 1;
    more.replaceChildren(spinner());
    const r = await get(`/api/discover/browse?provider=${provider}&type=${type}&sort=${sort}&genre=${genre}&page=${page}`);
    if (!ctx.isCurrent()) return;
    pages = r.pages;
    if (!filled) {
      filled = true;
      for (const g of r.genres || []) genreSel.append(h('option', { value: g.id, selected: g.id === genre }, g.name));
      for (const p of r.providers || []) provSel.append(h('option', { value: p.id, selected: p.id === provider }, p.name));
      const label = (sorts.find(([v]) => v === sort) || sorts[0])[1];
      // (replaceChildren écrit « null » tel quel : pas de valeur vide dans la liste)
      title.replaceChildren(...[r.provider && r.provider.logo ? h('img.provider-logo.big', { src: tmdbImg(r.provider.logo, 'logo'), alt: '' }) : null,
        [r.provider ? r.provider.name : null, type === 'tv' ? tr('Séries') : tr('Films'), label].filter(Boolean).join(' · ')].filter(Boolean));
      document.title = `${tr('Découvrir')} · Noxa`;
    }
    grid.append(...r.results.map((c) => discoverCard(c, { request: r.seerr, date: sort === 'upcoming' })));
    more.replaceChildren(...(page < pages ? [h('button.btn', { onclick: load }, tr('Voir plus'))] : []));
    if (!grid.children.length) grid.replaceWith(h('div.empty', tr('Rien à afficher.')));
  };
  await load();
}

async function chooseSeasons(t) {
  const boxes = t.seasons.map((s) => h('label.check-row', h('input', { type: 'checkbox', checked: true, value: s.number }),
    h('span', s.name || tr('Saison {number}', { number: s.number })), h('span.muted.small', tr(' {p1} épisodes{p2}', { p1: s.episodes || '?', p2: s.year ? ` · ${s.year}` : '' }))));
  return modal(tr('Demander « {title} »', { title: t.title }), h('div', h('p.muted.small', tr('Saisons à demander :')), h('div.check-list', boxes)), [
    { label: tr('Annuler'), value: null },
    { label: tr('Demander'), primary: true, handler: () => {
      const picked = boxes.map((b) => b.querySelector('input')).filter((i) => i.checked).map((i) => Number(i.value));
      if (!picked.length) { toast(tr('Sélectionnez au moins une saison'), 'bad'); return false; }
      return picked.length === t.seasons.length ? 'all' : picked;
    } }]);
}

async function renderTitle(main, kind, tid, ctx) {
  const t = await get(`/api/discover/title/${kind}/${tid}`);
  if (!ctx.isCurrent()) return;
  document.title = `${t.title} · Noxa`;
  const rerender = () => renderTitle(main, kind, tid, ctx);
  const actions = h('div.detail-actions');
  if (t.item_id) actions.append(h('a.btn.primary', { href: `#/item/${t.item_id}` }, icon('play'), tr('Regarder')));
  else if (t.can_request) {
    actions.append(h('button.btn.primary', { onclick: async (e) => {
      let seasons = null;
      if (kind === 'tv' && t.seasons.length > 1) {
        seasons = await chooseSeasons(t);
        if (!seasons) return;
      }
      e.target.disabled = true;
      try {
        await post('/api/discover/request', { type: kind, tmdb_id: t.tmdb_id, seasons: Array.isArray(seasons) ? seasons : undefined });
        toast(tr('Demande envoyée'), 'ok');
        rerender();
      } catch (err) { toast(err.message, 'bad'); e.target.disabled = false; }
    } }, icon('playlist'), tr('Demander')));
  }
  const statusLine = t.status && STATUS_LABEL[t.status] && !t.item_id
    ? h('div.request-status', { class: `request-status ${t.status}` }, STATUS_LABEL[t.status]) : null;
  if (!t.item_id) {
    let on = t.wished;
    const wbtn = h('button.btn.ghost.icon-only', { 'aria-pressed': String(on) });
    const paint = () => {
      wbtn.replaceChildren(icon(on ? 'bookmarked' : 'bookmark'));
      wbtn.title = on ? tr('Retirer de « À regarder »') : tr('Ajouter à « À regarder »');
      wbtn.setAttribute('aria-label', wbtn.title);
      wbtn.setAttribute('aria-pressed', String(on));
      wbtn.classList.toggle('on', on);
    };
    wbtn.addEventListener('click', async () => {
      try {
        const r = await post('/api/wishlist', { type: kind, tmdb_id: t.tmdb_id, on: !on });
        on = !on;
        paint();
        if (on && r.requested) { toast(tr('Ajouté à « À regarder » et demandé'), 'ok'); rerender(); }
        else if (on && r.request_error) toast(tr('Ajouté à « À regarder » (demande impossible : {request_error})', { request_error: r.request_error }), 'bad');
        else toast(on ? tr('Ajouté à « À regarder »') : tr('Retiré de « À regarder »'), 'ok');
      } catch (err) { toast(err.message, 'bad'); }
    });
    paint();
    actions.append(wbtn);
  }
  if (t.trailer) {
    actions.append(h('button.btn.ghost', { onclick: () => {
      const frame = h('iframe.trailer-frame', { src: `https://www.youtube-nocookie.com/embed/${encodeURIComponent(t.trailer)}?autoplay=1&rel=0`,
        allow: 'autoplay; encrypted-media; picture-in-picture; fullscreen', allowfullscreen: true, title: tr('Bande-annonce') });
      modal(tr('Bande-annonce — {title}', { title: t.title }), h('div.trailer-box', frame), []);
    } }, icon('film'), tr('Bande-annonce')));
  }
  const meta = [t.year, t.media_type === 'tv' ? plural(t.seasons.length, '{n} saison', '{n} saisons') : null,
    t.runtime ? `${t.runtime} min` : null, t.rating ? `★ ${t.rating}` : null].filter(Boolean);
  clear(main,
    h('section.detail', { style: { '--backdrop': t.backdrop ? `url("${tmdbImg(t.backdrop, 'backdrop')}")` : 'none' } }, h('div.detail-backdrop'),
      h('div.detail-inner',
        h('div.detail-poster', t.poster ? h('img', { src: tmdbImg(t.poster), alt: '' }) : h('div.noimg', t.title)),
        h('div.detail-info',
          h('a.detail-kicker', { href: '#/discover' }, t.media_type === 'tv' ? tr('Série · Découvrir') : tr('Film · Découvrir')),
          h('h1.detail-title', t.title),
          h('div.meta-line', meta.map((m) => h('span', String(m)))),
          t.tagline ? h('p.tagline', t.tagline) : null,
          statusLine, actions,
          t.overview ? h('p.summary', t.overview) : null,
          t.genres.length ? h('div.chips', t.genres.map((g) => h('span.chip', g))) : null,
          t.streams.length ? h('div.streams', h('span.muted.small', tr('Disponible sur ')),
            t.streams.map((s) => h('a.stream', { href: `#/discover?provider=${s.id}`, title: s.name },
              s.logo ? h('img', { src: tmdbImg(s.logo, 'logo'), alt: s.name }) : s.name))) : null,
          t.cast.length ? h('p.muted.small', tr('Avec {p1}', { p1: t.cast.join(', ') })) : null))));
}
