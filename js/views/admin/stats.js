// Admin page: statistics.
import { get } from '../../api.js';
import { locale, plural, tr } from '../../i18n.js';
import { clear, h, select, spinner, store } from '../../ui.js';

// --------------------------------------------------------------- statistiques
export async function statsPage(body, ctx) {
  const GB = 1e9;
  const size = (b) => (b >= GB ? tr('{n} Go', { n: (b / GB).toLocaleString(locale(), { maximumFractionDigits: 1 }) }) : tr('{n} Mo', { n: Math.max(0, Math.round(b / 1e6)) }));
  const hours = (s) => (s >= 3600 ? tr('{n} h', { n: (s / 3600).toLocaleString(locale(), { maximumFractionDigits: 1 }) }) : tr('{n} min', { n: Math.round(s / 60) }));
  const when = (ts) => new Date(ts * 1000).toLocaleDateString(locale(), { day: 'numeric', month: 'short' });
  const days = select([['7', tr('7 derniers jours')], ['30', tr('30 derniers jours')], ['90', tr('3 derniers mois')], ['365', tr('12 derniers mois')]],
    store('stats-days') || '30', { onchange: () => { store('stats-days', days.value); load(); } });
  const out = h('div', spinner());
  clear(body, h('div.actions-row.stats-head', days), out);
  async function load() {
    clear(out, spinner());
    let r;
    try { r = await get(`/api/admin/stats?days=${days.value}`); } catch (e) { clear(out, h('p.form-error', e.message)); return; }
    if (!ctx.isCurrent()) return;
    const t = r.totals;
    const tile = (label, value, sub) => h('div.stat-tile', h('div.stat-label', label), h('div.stat-value', value), sub ? h('div.stat-sub', sub) : null);
    const tModes = r.modes;
    const pct = (n) => (t.plays ? Math.round((n / t.plays) * 100) : 0);
    // a single series (hours per day): bars, value on hover, title that names it
    const max = Math.max(1, ...r.per_day.map((d) => d.seconds));
    const chart = h('div.stat-chart', { role: 'img', 'aria-label': tr('Heures de visionnage par jour') },
      r.per_day.map((d) => h('div.stat-col', { title: `${new Date(d.day).toLocaleDateString(locale(), { weekday: 'short', day: 'numeric', month: 'short' })} : ${hours(d.seconds)}` },
        h('span', { style: { height: `${d.seconds ? Math.max(3, (d.seconds / max) * 100) : 0}%` } }))));
    const table = (head, rows) => h('div.table-wrap', h('table.stat-table', h('thead', h('tr', head.map((x) => h('th', x)))), h('tbody', rows)));
    clear(out,
      h('div.stat-tiles',
        tile(tr('Temps de visionnage'), hours(t.seconds), plural(t.plays, '{n} lecture', '{n} lectures')),
        tile(tr('Données envoyées (estimation)'), size(t.bytes), tr('débit de la vidéo et du son')),
        tile(tr('Comptes actifs'), String(t.users)),
        tile(tr('Conversions'), `${pct(t.transcodes)} %`, tr('des lectures demandent une conversion par le processeur ou la carte graphique'))),
      h('section.panel', h('h2', tr('Heures de visionnage par jour')),
        t.plays ? chart : h('p.muted.small', tr('Aucune lecture sur cette période. Les statistiques commencent avec cette version de Noxa.')),
        t.plays ? h('div.stat-axis.muted.small', h('span', when(Date.parse(r.per_day[0].day) / 1000)), h('span', when(Date.parse(r.per_day[r.per_day.length - 1].day) / 1000))) : null),
      h('section.panel', h('h2', tr('Par compte')),
        r.users.length ? table([tr('Compte'), tr('Temps'), tr('Lectures'), tr('Titres'), tr('Données'), tr('Conversions'), tr('Dernière lecture')],
          r.users.map((u) => h('tr', h('td', h('strong', u.name)), h('td', hours(u.seconds)), h('td', String(u.plays)), h('td', String(u.titles)),
            h('td', size(u.bytes)), h('td', `${u.plays ? Math.round((u.transcodes / u.plays) * 100) : 0} %`), h('td', when(u.last)))))
          : h('p.muted.small', tr('Aucune lecture sur cette période.')),
        Object.keys(tModes).length ? h('p.muted.small', tr('Lecture directe {a} % · copie (remux) {b} % · conversion {c} %',
          { a: pct((tModes.direct || {}).plays || 0), b: pct((tModes.copy || {}).plays || 0), c: pct((tModes.transcode || {}).plays || 0) })) : null),
      h('section.panel', h('h2', tr('Les plus regardés')),
        r.top.length ? table([tr('Titre'), tr('Temps'), tr('Lectures'), tr('Spectateurs')],
          r.top.map((x) => h('tr', h('td', h('a', { href: `#/item/${x.id}` }, x.title), x.year ? h('span.muted', ` (${x.year})`) : null, x.show ? h('span.muted.small', ` · ${tr('série')}`) : null),
            h('td', hours(x.seconds)), h('td', String(x.plays)), h('td', String(x.viewers)))))
          : h('p.muted.small', tr('Aucune lecture sur cette période.'))),
      h('section.panel', h('h2', tr('Jamais regardés')),
        h('p.muted.small', tr('Films et séries de ce serveur que personne n’a commencés, ajoutés il y a plus de {d} jours : {n} titre(s), {size} au total. Les plus gros d’abord.',
          { d: r.never.after_days, n: r.never.count, size: size(r.never.size) })),
        r.never.items.length ? table([tr('Titre'), tr('Bibliothèque'), tr('Ajouté le'), tr('Taille')],
          r.never.items.map((x) => h('tr', h('td', h('a', { href: `#/item/${x.id}` }, x.title), x.year ? h('span.muted', ` (${x.year})`) : null),
            h('td', x.library), h('td', when(x.added_at)), h('td', size(x.size))))) : null));
  }
  await load();
}
