// Admin page: live TV sources.
import { del, get, post, put } from '../../api.js';
import { refreshMe } from '../../app.js';
import { tr } from '../../i18n.js';
import { clear, confirmDialog, field, h, icon, modal, select, toast, toggle } from '../../ui.js';
import { since } from './util.js';

// ------------------------------------------------------------ TV en direct
async function liveSourceForm(src) {
  const isNew = !src;
  src = src || { name: '', m3u_url: '', epg_url: '', user_agent: '', enabled: true };
  const name = h('input', { value: src.name, maxlength: 60, placeholder: tr('Fournisseur IPTV, box TV…') });
  const m3u = h('input', { value: src.m3u_url, placeholder: tr('http://…/playlist.m3u ou /srv/iptv/chaines.m3u') });
  const epg = h('input', { value: src.epg_url, placeholder: tr('facultatif : http://…/guide.xml(.gz)') });
  const ua = h('input', { value: src.user_agent, placeholder: tr('facultatif : VLC/3.0.20 LibVLC/3.0.20…') });
  const enabled = toggle(tr('Source active'), src.enabled);
  const err = h('p.form-error');
  return modal(isNew ? tr('Ajouter une source de chaînes') : tr('Modifier « {name} »', { name: src.name }), h('div.form',
    field(tr('Nom'), name),
    field(tr('Liste des chaînes (M3U / M3U8)'), m3u, tr('Adresse fournie par le fournisseur IPTV ou la box, ou chemin d’un fichier .m3u sur le serveur.')),
    field(tr('Guide des programmes (XMLTV)'), epg, tr('Facultatif si la liste indique déjà son guide (« url-tvg » dans l’en-tête). Fichiers .gz acceptés.')),
    h('details.advanced', h('summary', tr('Options avancées')),
      field('User-Agent', ua, tr('Certains fournisseurs n’acceptent qu’un lecteur précis (VLC, Kodi…).'))),
    isNew ? null : enabled, err), [
    { label: tr('Annuler'), value: null },
    { label: isNew ? tr('Ajouter et charger') : tr('Enregistrer'), primary: true, handler: async () => {
      err.textContent = isNew ? tr('Chargement de la liste…') : '';
      const payload = { name: name.value.trim(), m3u_url: m3u.value.trim(), epg_url: epg.value.trim(), user_agent: ua.value.trim(),
        enabled: enabled.querySelector('input').checked };
      try {
        const r = isNew ? await post('/api/admin/live/sources', payload) : await put(`/api/admin/live/sources/${src.id}`, payload);
        if (r.warning) toast(r.warning, 'bad');
        return true;
      } catch (e) { err.textContent = e.message; return false; }
    } },
  ]);
}

async function liveGroups(src) {
  const groups = await get(`/api/admin/live/sources/${src.id}/groups`);
  const checks = groups.map((g) => { const t = toggle(`${g.name || tr('Sans groupe')} (${g.count})`, !g.hidden); t.dataset.name = g.name; return t; });
  const all = (on) => checks.forEach((t) => { t.querySelector('input').checked = on; });
  return modal(tr('Groupes de « {name} »', { name: src.name }), h('div',
    h('p.muted.small', tr('Les groupes décochés sont masqués pour tout le monde (pratique pour une liste de plusieurs milliers de chaînes).')),
    h('div.actions-row', h('button.btn.small', { onclick: () => all(true) }, tr('Tout cocher')), h('button.btn.small', { onclick: () => all(false) }, tr('Tout décocher'))),
    h('div.checks.single.scroll-list', checks)), [
    { label: tr('Annuler'), value: null },
    { label: tr('Enregistrer'), primary: true, handler: async () => {
      await put(`/api/admin/live/sources/${src.id}/groups`, { hidden: checks.filter((t) => !t.querySelector('input').checked).map((t) => t.dataset.name) });
      return true;
    } },
  ]);
}

export async function liveTab(body, ctx) {
  const data = await get('/api/admin/live');
  if (!ctx.isCurrent()) return;
  const reload = async () => { await refreshMe().catch(() => {}); liveTab(body, ctx); };
  const st = data.settings;
  const maxS = h('input', { type: 'number', min: 1, max: 20, value: st.max_sessions });
  const vmode = select([['auto', tr('Automatique (H.264 recopié, le reste converti)')], ['copy', tr('Ne jamais convertir (le moins de processeur)')],
    ['transcode', tr('Toujours convertir en H.264 (le plus compatible)')]], st.video);
  const hide = toggle(tr('Masquer la TV en direct pour les profils avec une restriction d’âge'), st.hide_restricted);
  clear(body,
    h('section.panel', h('div.toolbar', h('div', h('h2', tr('Sources de chaînes')),
      h('p.muted.small', tr('Listes M3U (IPTV, box opérateur, tuner TNT réseau) et guides XMLTV. Les chaînes sont proposées à tous les utilisateurs dans l’onglet « TV en direct ». L’actualisation automatique se règle dans « Tâches planifiées ».'))),
    h('button.btn.primary', { onclick: async () => { if (await liveSourceForm(null)) { toast(tr('Source ajoutée'), 'ok'); reload(); } } }, tr('+ Ajouter'))),
    data.sources.length ? h('div.user-list', data.sources.map((s) => h('div.user-row', { class: s.enabled ? 'user-row' : 'user-row off' },
      h('div.lib-icon', '📡'),
      h('div.grow', h('strong', s.name),
        h('div.muted.small', tr('{channels} chaîne(s)', { channels: s.channels }), s.hidden ? tr(' dont {hidden} masquée(s)', { hidden: s.hidden }) : '', s.dead ? tr(' · {dead} hors service (masquée(s) automatiquement)', { dead: s.dead }) : '', tr(' · {programmes} programme(s) dans le guide', { programmes: s.programmes }),
          tr(' · actualisée {p1}', { p1: since(s.refreshed_at) })),
        h('div.small', h('code', s.m3u_url.length > 70 ? `${s.m3u_url.slice(0, 70)}…` : s.m3u_url)),
        s.error ? h('div.form-error.small', s.error) : null),
      h('button.btn.small', { onclick: async (e) => {
        e.currentTarget.disabled = true;
        try { const r = await post(`/api/admin/live/sources/${s.id}/refresh`); toast(tr('{channels} chaîne(s){p2}', { channels: r.channels, p2: r.programmes != null ? tr(', {programmes} programme(s)', { programmes: r.programmes }) : '' }), 'ok'); }
        catch (ex) { toast(ex.message, 'bad'); }
        reload();
      } }, tr('Actualiser')),
      h('button.btn.small', { onclick: async () => { if (await liveGroups(s)) { toast(tr('Groupes enregistrés'), 'ok'); reload(); } } }, tr('Groupes…')),
      h('button.btn.small', { onclick: async () => { if (await liveSourceForm(s)) { toast(tr('Source modifiée'), 'ok'); reload(); } } }, tr('Modifier')),
      h('button.icon-btn', { title: tr('Supprimer'), onclick: async () => {
        if (!await confirmDialog(tr('Supprimer cette source ?'), tr('Les chaînes de « {name} » et les favoris associés disparaîtront.', { name: s.name }), tr('Supprimer'))) return;
        await del(`/api/admin/live/sources/${s.id}`); reload();
      } }, icon('trash')))))
      : h('div.empty', tr('Aucune source configurée.'))),
    h('section.panel', h('h2', tr('Lecture')),
      h('p.muted.small', tr('Les navigateurs ne lisent pas directement les flux TV (MPEG-TS) : Noxa les reçoit et les convertit. Une chaîne regardée par plusieurs personnes n’est reçue qu’une fois. La plupart des fournisseurs limitent le nombre de connexions simultanées.')),
      h('div.grid2', field(tr('Chaînes regardées en même temps (max.)'), maxS), field(tr('Vidéo'), vmode)),
      hide,
      h('button.btn.primary', { onclick: async () => {
        try { await put('/api/admin/live/settings', { max_sessions: Number(maxS.value), video: vmode.value, hide_restricted: hide.querySelector('input').checked }); toast(tr('Enregistré'), 'ok'); reload(); }
        catch (e) { toast(e.message, 'bad'); }
      } }, tr('Enregistrer'))),
    data.sessions.length ? h('section.panel', h('h2', tr('Chaînes regardées en ce moment')), h('div.user-list', data.sessions.map((x) => h('div.user-row',
      h('div.grow', h('strong', x.channel), h('div.muted.small', tr('{p1} · {p2}{p3} · depuis {p4}', { p1: x.viewers.join(', ') || tr('personne'), p2: x.mode === 'transcode' ? tr('vidéo convertie') : tr('vidéo recopiée'), p3: x.codec ? ` (${x.codec})` : '', p4: since(x.since) })),
        x.error ? h('div.form-error.small', x.error) : null),
      h('button.btn.small.danger', { onclick: async () => { await del(`/api/admin/live/sessions/${x.sid}`); reload(); } }, tr('Arrêter')))))) : null);
}
