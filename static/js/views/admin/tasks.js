// Admin page: scheduled tasks.
import { get, post, put } from '../../api.js';
import { locale, plural, tr } from '../../i18n.js';
import { clear, field, h, icon, modal, select, toast, toggle } from '../../ui.js';
import { since } from './util.js';

// ---------------------------------------------------------- tâches planifiées
// lundi … dimanche, dans la langue de l'interface (le 1er janvier 2024 est un lundi)
const WEEKDAYS = [...Array(7)].map((_, i) => new Date(2024, 0, 1 + i).toLocaleDateString(locale(), { weekday: 'long' }));
const TASK_ICONS = { scan: 'search', metadata: 'film', chapters: 'chapters', intros: 'skip', subtitles: 'subs', livetv: 'tv', cleanup: 'trash' };
// catégories envoyées par le serveur (en français : ce sont aussi des clés de traduction)
const CATEGORIES = ['Bibliothèques', 'Lecture', 'TV en direct', 'Maintenance'];

function scheduleText(s) {
  if (s.mode === 'interval') return s.hours % 24 === 0 && s.hours > 24 ? tr('Tous les {p1} jours', { p1: s.hours / 24 }) : (s.hours === 1 ? tr('Toutes les heures') : tr('Toutes les {hours} h', { hours: s.hours }));
  if (s.mode === 'daily') return tr('Chaque jour à {time}', { time: s.time });
  if (s.mode === 'weekly') return tr('Chaque {p1} à {time}', { p1: WEEKDAYS[s.weekday ?? 6], time: s.time });
  return tr('Désactivée');
}

export function when(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  const tomorrow = new Date(Date.now() + 86400000);
  const time = d.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === today.toDateString()) return tr('aujourd’hui à {time}', { time });
  if (d.toDateString() === tomorrow.toDateString()) return tr('demain à {time}', { time });
  return tr('{day} à {time}', { day: d.toLocaleDateString(locale(), { weekday: 'long', day: 'numeric', month: 'short' }), time });
}

const duration = (sec) => (sec < 60 ? `${sec} s` : sec < 3600 ? `${Math.round(sec / 60)} min` : `${Math.floor(sec / 3600)} h ${String(Math.round((sec % 3600) / 60)).padStart(2, '0')}`);

async function taskSettings(task, libraries) {
  const sc = task.schedule;
  const mode = select([['off', tr('Désactivée (seulement à la main)')], ['interval', tr('À intervalle régulier')], ['daily', tr('Chaque jour')],
    ['weekly', tr('Chaque semaine')]], sc.mode);
  const hours = select([...[1, 2, 3, 6, 12, 24].map((n) => [n, plural(n, '{n} heure', '{n} heures')]),
    ...[2, 3, 7].map((n) => [n * 24, plural(n, '{n} jour', '{n} jours')])], String(sc.hours || 24));
  const time = h('input', { type: 'time', value: sc.time || '03:00' });
  const weekday = select(WEEKDAYS.map((d, i) => [i, d.charAt(0).toUpperCase() + d.slice(1)]), String(sc.weekday ?? 6));
  const fHours = field(tr('Toutes les'), hours);
  const fDay = field(tr('Jour'), weekday);
  const fTime = field(tr('Heure'), time);
  const sync = () => {
    fHours.hidden = mode.value !== 'interval';
    fDay.hidden = mode.value !== 'weekly';
    fTime.hidden = !['daily', 'weekly'].includes(mode.value);
  };
  mode.addEventListener('change', sync);
  sync();
  const inputs = {};
  const optionEls = task.option_defs.map((o) => {
    const v = task.options[o.key];
    if (o.type === 'bool') {
      const t = toggle(o.label, !!v);
      inputs[o.key] = () => t.querySelector('input').checked;
      return h('div', t, o.hint ? h('p.field-hint.toggle-hint', o.hint) : null);
    }
    if (o.type === 'int') {
      const i = h('input', { type: 'number', min: o.min, max: o.max, value: v });
      inputs[o.key] = () => Number(i.value);
      return field(o.label + (o.unit ? ` (${o.unit})` : ''), i, o.hint);
    }
    if (o.type === 'select') {
      const sel = select(o.choices, v);
      inputs[o.key] = () => sel.value;
      return field(o.label, sel, o.hint);
    }
    if (o.type === 'libraries') {
      const libs = libraries.filter((l) => !o.kind || l.kind === o.kind);
      const checks = libs.map((l) => { const t = toggle(l.name, (v || []).includes(l.id)); t.dataset.id = l.id; return t; });
      inputs[o.key] = () => checks.filter((t) => t.querySelector('input').checked).map((t) => t.dataset.id);
      return libs.length ? field(o.label, h('div.checks', checks), o.hint) : null;
    }
    return null;
  });
  const err = h('p.form-error');
  return modal(task.name, h('div.form',
    h('p.muted.small', task.description),
    h('h4.form-sub', tr('Planification')),
    h('div.sched-row', field(tr('Quand'), mode), fHours, fDay, fTime),
    optionEls.some(Boolean) ? h('h4.form-sub', tr('Options')) : null,
    optionEls, err), [
    { label: tr('Annuler'), value: null },
    { label: tr('Enregistrer'), primary: true, handler: async () => {
      const schedule = { mode: mode.value };
      if (mode.value === 'interval') schedule.hours = Number(hours.value);
      if (['daily', 'weekly'].includes(mode.value)) schedule.time = time.value || '03:00';
      if (mode.value === 'weekly') schedule.weekday = Number(weekday.value);
      const options = Object.fromEntries(Object.entries(inputs).map(([k, f]) => [k, f()]));
      try { await put(`/api/admin/tasks/${task.id}`, { schedule, options }); return true; } catch (e) { err.textContent = e.message; return false; }
    } },
  ]);
}

export async function tasksTab(body, ctx) {
  const data = await get('/api/admin/tasks');
  if (!ctx.isCurrent()) return;
  const reload = () => tasksTab(body, ctx);
  const act = async (path, msg) => { try { await post(path); if (msg) toast(msg, 'ok'); } catch (e) { toast(e.message, 'bad'); } reload(); };
  const row = (t) => {
    const last = t.last;
    const status = last ? { ok: ['ok', tr('Terminée')], error: ['bad', tr('Échec')], cancelled: ['', tr('Arrêtée')] }[last.status] || ['', last.status] : null;
    return h('div.task-row',
      h('div.task-icon', icon(TASK_ICONS[t.id] || 'clock')),
      h('div.grow',
        h('strong', t.name),
        h('p.muted.small.task-desc', t.description),
        h('div.task-meta',
          h('span', icon('clock'), ' ', scheduleText(t.schedule)),
          t.next && !t.running ? h('span', tr('Prochaine : {p1}', { p1: when(t.next) })) : null,
          last ? h('span', h('span', { class: status[0] }, status[1]), ` ${since(last.start)}`,
            last.end ? ` · ${duration(Math.max(0, last.end - last.start))}` : '', last.summary ? ` · ${last.summary}` : '') : h('span', tr('Jamais lancée')),
          t.queued ? h('span.tag', tr('En file')) : null),
        t.note ? h('p.small', { class: t.available ? 'small muted' : 'small form-error' }, t.note) : null,
        t.running ? h('div', h('div.bar', h('div.bar-fill', { style: { width: `${Math.round(t.running.progress * 100)}%` } })),
          h('div.task-running', t.running.paused ? '⏸ ' : '', t.running.message || tr('En cours…'),
            h('span.muted', ` · ${Math.round(t.running.progress * 100)} % · ${t.running.reason}`))) : null),
      h('div.task-actions',
        t.running || t.queued
          ? h('button.btn.small', { onclick: () => act(`/api/admin/tasks/${t.id}/cancel`, tr('Tâche arrêtée')) }, tr('Arrêter'))
          : h('button.btn.small.primary', { disabled: !t.available, onclick: () => act(`/api/admin/tasks/${t.id}/run`, tr('Tâche lancée')) }, icon('play'), tr('Lancer')),
        h('button.btn.small', { onclick: async () => { if (await taskSettings(t, data.libraries)) { toast(tr('Réglages enregistrés'), 'ok'); reload(); } } }, tr('Réglages'))));
  };
  clear(body,
    h('p.muted', tr('Travaux de fond du serveur. Une seule tâche s’exécute à la fois ; les tâches lourdes se mettent en pause pendant les lectures.')),
    CATEGORIES.map((cat) => {
      const list = data.tasks.filter((t) => t.category === cat);
      return list.length ? h('section.task-group', h('h3', tr(cat)), list.map(row)) : null;
    }));
  const busy = data.tasks.some((t) => t.running || t.queued);
  const timer = setTimeout(() => { if (ctx.isCurrent()) reload(); }, busy ? 2500 : 15000);
  ctx.setCleanup(() => clearTimeout(timer));
}
