// Admin page: requests (Seerr).
import { get, post, put } from '../../api.js';
import { refreshMe } from '../../app.js';
import { tr } from '../../i18n.js';
import { clear, field, h, toast, toggle } from '../../ui.js';

// --------------------------------------------------------------- demandes
export async function requestsTab(body, ctx) {
  const cfg = await get('/api/admin/requests');
  if (!ctx.isCurrent()) return;
  const reload = () => requestsTab(body, ctx);
  const url = h('input', { value: cfg.seerr_url, placeholder: 'http://192.168.1.20:5055', autocomplete: 'off' });
  const key = h('input', { type: 'password', autocomplete: 'off',
    placeholder: cfg.seerr_key_set ? tr('Clé enregistrée (laisser vide pour la garder)') : tr('Paramètres → Général → Clé d’API') });
  const auto = toggle(tr('Demander automatiquement les titres ajoutés à « À regarder » qui ne sont pas sur le serveur'), cfg.auto_request);
  auto.querySelector('input').addEventListener('change', async (e) => {
    try { await put('/api/admin/requests', { auto_request: e.target.checked }); toast(tr('Enregistré'), 'ok'); } catch (err) { toast(err.message, 'bad'); }
  });
  const status = h('div.status-box', { class: cfg.enabled ? 'status-box ok' : 'status-box' },
    cfg.shared_by ? tr('Pas de Seerr ici : les demandes passent par celui du serveur lié « {name} ».', { name: cfg.shared_by })
      : cfg.enabled ? tr('Seerr est configuré : les utilisateurs peuvent demander des films et des séries.') : tr('Seerr n’est pas encore configuré.'));
  clear(body,
    h('section.panel', h('h2', tr('Seerr')),
      h('p.muted.small', tr('Seerr reçoit les demandes faites depuis l’onglet Découvrir et les transmet à Radarr / Sonarr. Une demande est faite au nom du compte Seerr portant le même nom que le compte Noxa, sinon au nom du propriétaire de la clé d’API.')),
      status,
      h('div.grid2', field(tr('Adresse de Seerr'), url), field(tr('Clé d’API de Seerr'), key)),
      h('div.actions-row',
        h('button.btn.primary', { onclick: async () => {
          try {
            await put('/api/admin/requests', { seerr_url: url.value.trim(), ...(key.value.trim() ? { seerr_key: key.value.trim() } : {}) });
            const t = await post('/api/admin/requests/test');
            toast(tr('Connecté à {title} (serveur : {media_server})', { title: t.title, media_server: t.media_server }), 'ok');
            await refreshMe();
            reload();
          } catch (e) { toast(e.message, 'bad'); reload(); }
        } }, tr('Enregistrer et tester')),
        cfg.enabled && !cfg.shared_by ? h('button.btn', { onclick: async () => {
          try { const t = await post('/api/admin/requests/test'); toast(tr('Connecté à {title} (serveur : {media_server})', { title: t.title, media_server: t.media_server }), 'ok'); }
          catch (e) { toast(e.message, 'bad'); }
        } }, tr('Tester')) : null,
        cfg.enabled && !cfg.shared_by ? h('button.btn.ghost', { onclick: async () => {
          await put('/api/admin/requests', { seerr_url: '', seerr_key: '' }); await refreshMe(); reload();
        } }, tr('Déconnecter')) : null),
      auto,
      h('p.muted.small', tr('Catalogues affichés pour le pays : {region} (celui des métadonnées).', { region: cfg.region }))));
}
