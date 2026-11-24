// Admin page: cold storage, freeing up space, compression, and the archive dialogs.
import { del, get, post, put } from '../../api.js';
import { locale, plural, tr } from '../../i18n.js';
import { clear, confirmDialog, field, h, icon, modal, select, store, toast, toggle } from '../../ui.js';
import { SMB_VERSIONS } from './libraries.js';
import { since } from './util.js';

// --------------------------------------------------------------- stockage froid
const COLD_KINDS = [
  ['local', tr('Dossier de cette machine'), tr('Disque USB, NAS déjà monté par le système, volume Docker…')],
  ['smb', tr('Partage SMB (NAS)'), tr('Monté en écriture par Noxa (installation par script ou paquet).')],
  ['sftp', tr('SFTP'), tr('Hetzner Storage Box, NAS, serveur SSH.')],
  ['webdav', tr('WebDAV'), tr('Nextcloud, Synology, Hetzner Storage Box…')],
  ['s3', tr('S3 et compatibles'), tr('Backblaze B2, Wasabi, Scaleway, OVH, AWS, MinIO.')],
  ['rclone', tr('rclone (autres services)'), tr('Un « remote » déjà configuré avec rclone config : pCloud, Mega, Google Drive…')],
  ['peer', tr('Serveur lié'), tr('Un serveur de la fédération qui réserve de la place pour vos archives (réglage de son administrateur).')],
];
const coldSize = (b) => (b >= 1e12 ? tr('{n} To', { n: (b / 1e12).toLocaleString(locale(), { maximumFractionDigits: 2 }) })
  : b >= 1e9 ? tr('{n} Go', { n: (b / 1e9).toLocaleString(locale(), { maximumFractionDigits: 1 }) }) : tr('{n} Mo', { n: Math.max(0, Math.round(b / 1e6)) }));

function coldSummary(s) {
  const c = s.conf;
  if (s.kind === 'local') return c.path;
  if (s.kind === 'smb') return `//${c.host}/${c.share}${c.subdir ? `/${c.subdir}` : ''}`;
  if (s.kind === 'sftp') return `${c.username}@${c.host}:${c.port}/${c.path}`;
  if (s.kind === 'webdav') return c.url;
  if (s.kind === 's3') return `${c.bucket}/${c.prefix || ''} · ${c.endpoint || 'AWS'}`;
  if (s.kind === 'peer') return tr('serveur lié n° {n}', { n: c.peer });
  return c.remote;
}

async function coldForm(existing, available, peers = []) {
  const kind = select(COLD_KINDS.map(([k, l]) => [k, l + (available[k] ? ` — ${tr('indisponible ici')}` : '')]), existing ? existing.kind : 'local',
    { disabled: !!existing });
  const c = existing ? existing.conf : {};
  const keep = existing ? tr('Laisser vide pour garder l’actuel') : '';
  const inputs = {};
  const inp = (k, attrs = {}) => { inputs[k] = h('input', { value: c[k] ?? attrs.value ?? '', autocomplete: 'off', ...attrs }); return inputs[k]; };
  const name = h('input', { value: existing ? existing.name : '', placeholder: tr('ex. Disque USB, Storage Box') });
  const fieldsFor = (k) => {
    if (k === 'local') return [field(tr('Dossier'), inp('path', { placeholder: '/mnt/archive' }), tr('Chemin sur cette machine (dans Docker : un volume du conteneur). Noxa doit pouvoir y écrire.'))];
    if (k === 'smb') return [h('div.grid2', field(tr('Adresse du NAS'), inp('host', { placeholder: '192.168.1.5' })), field(tr('Partage'), inp('share', { placeholder: 'archive' }))),
      h('div.grid2', field(tr('Sous-dossier'), inp('subdir', { placeholder: tr('facultatif') })), field(tr('Version SMB'), (inputs.version = select(SMB_VERSIONS, c.version || 'auto')))),
      h('div.grid2', field(tr('Identifiant'), inp('username')), field(tr('Mot de passe'), inp('password', { type: 'password', value: '', placeholder: keep })))];
    if (k === 'sftp') return [h('div.grid2', field(tr('Serveur'), inp('host', { placeholder: 'u123456.your-storagebox.de' })), field(tr('Port'), inp('port', { type: 'number', value: 22, min: 1, max: 65535 }))),
      h('div.grid2', field(tr('Identifiant'), inp('username')), field(tr('Mot de passe'), inp('password', { type: 'password', value: '', placeholder: keep }))),
      field(tr('Dossier'), inp('path', { placeholder: 'nexis' }), tr('Relatif au dossier personnel, ou absolu (/data/nexis). Créé au besoin.'))];
    if (k === 'webdav') return [field(tr('Adresse WebDAV'), inp('url', { placeholder: 'https://cloud.exemple.fr/remote.php/dav/files/moi/Noxa' })),
      h('div.grid2', field(tr('Identifiant'), inp('username')), field(tr('Mot de passe'), inp('password', { type: 'password', value: '', placeholder: keep })))];
    if (k === 's3') return [field(tr('Adresse (vide pour AWS)'), inp('endpoint', { placeholder: 'https://s3.eu-central-003.backblazeb2.com' })),
      h('div.grid2', field(tr('Région'), inp('region', { placeholder: 'eu-central-003' })), field(tr('Seau (bucket)'), inp('bucket'))),
      h('div.grid2', field(tr('Clé d’accès'), inp('access_key')), field(tr('Clé secrète'), inp('secret_key', { type: 'password', value: '', placeholder: keep }))),
      field(tr('Préfixe'), inp('prefix', { value: 'nexis-archive' }))];
    if (k === 'peer') {
      inputs.peer = select((peers || []).map((p) => [p.id, p.name]), c.peer || (peers[0] || {}).id);
      return [field(tr('Serveur'), inputs.peer, tr('Son administrateur doit réserver de la place pour les archives (Paramètres → Stockage froid de son serveur).'))];
    }
    return [field(tr('Remote rclone et dossier'), inp('remote', { placeholder: 'pcloud:Noxa' }), tr('Nom donné dans rclone config, suivi de « : » et du dossier.')),
      field(tr('Fichier de configuration'), inp('config', { placeholder: tr('facultatif, ex. /home/seance/.config/rclone/rclone.conf') }))];
  };
  const box = h('div.form');
  const note = h('p.muted.small');
  const err = h('p.form-error');
  const paint = () => {
    for (const k of Object.keys(inputs)) delete inputs[k];
    const k = kind.value;
    note.textContent = available[k] || COLD_KINDS.find((x) => x[0] === k)[2];
    note.className = available[k] ? 'form-error small' : 'muted small';
    box.replaceChildren(...fieldsFor(k));
  };
  kind.addEventListener('change', paint);
  paint();
  const conf = () => {
    const out = {};
    for (const [k, el] of Object.entries(inputs)) out[k] = k === 'port' || k === 'peer' ? Number(el.value) : el.value.trim();
    return out;
  };
  // SFTP : la clé du serveur est montrée une fois, à confirmer (elle sera vérifiée ensuite à chaque connexion)
  const confirmKey = async (fp) => {
    const pretty = fp.match(/../g).join(':');
    return confirmDialog(tr('Clé du serveur SFTP'), tr('Empreinte MD5 de la clé du serveur : {fp}. Vérifiez-la si vous le pouvez (chez votre hébergeur, ou avec ssh-keygen -l -E md5), puis confirmez : Noxa refusera ensuite tout serveur qui présente une autre clé.', { fp: pretty }), tr('Confirmer la clé'), false);
  };
  return modal(existing ? tr('Modifier le stockage') : tr('Ajouter un stockage froid'), h('div.form',
    existing ? null : field(tr('Type'), kind), note, field(tr('Nom'), name), box, err), [
    { label: tr('Annuler'), value: false },
    { label: existing ? tr('Enregistrer') : tr('Ajouter'), primary: true, handler: async () => {
      err.textContent = '';
      const k = existing ? existing.kind : kind.value;
      const body = { kind: k, name: name.value.trim(), conf: conf() };
      try {
        for (let round = 0; round < 2; round += 1) {
          const r = existing ? await put(`/api/admin/cold/stores/${existing.id}`, body) : await post('/api/admin/cold/stores', body);
          if (r.ok) return true;
          if (r.fingerprint && await confirmKey(r.fingerprint)) { body.conf.hostkey = r.fingerprint; continue; }
          return false;
        }
      } catch (e) { err.textContent = e.message; }
      return false;
    } },
  ]);
}

export async function coldPage(body, ctx) {
  let data, freeData, extra, comp;
  try {
    [data, freeData, extra, comp] = await Promise.all([get('/api/admin/cold'), get('/api/admin/cold/free'), get('/api/admin/cold/extra'), get('/api/admin/cold/compress')]);
  } catch (e) { clear(body, h('p.form-error', e.message)); return; }
  if (!ctx.isCurrent()) return;
  const reload = () => coldPage(body, ctx);
  const stores = data.stores;
  const byId = Object.fromEntries(stores.map((s) => [s.id, s]));
  const free = (s) => {
    const el = h('span.muted');
    get(`/api/admin/cold/stores/${s.id}/free`).then((r) => { if (r.free != null) el.textContent = tr(' · {n} libres', { n: coldSize(r.free) }); }).catch(() => {});
    return el;
  };
  const storeRow = (s) => h('div.user-row',
    h('div.lib-icon', icon(s.kind === 's3' || s.kind === 'webdav' || s.kind === 'rclone' ? 'backup' : 'storage')),
    h('div.grow', h('strong', s.name), h('span.muted', ` · ${COLD_KINDS.find((k) => k[0] === s.kind)[1]}`),
      h('div.small', h('code', coldSummary(s))),
      h('div.small.muted', plural(s.copies, '{n} titre archivé', '{n} titres archivés'), s.bytes ? ` · ${coldSize(s.bytes)}` : '', free(s))),
    toggle(tr('Utilisé'), s.enabled, { onchange: async (e) => {
      try { await put(`/api/admin/cold/stores/${s.id}`, { enabled: e.target.checked }); } catch (x) { toast(x.message, 'bad'); }
    } }),
    s.kind === 'smb' ? null : h('button.btn.small', { onclick: async () => { if (await coldForm(s, data.available, extra.peers)) { toast(tr('Stockage mis à jour'), 'ok'); reload(); } } }, tr('Modifier')),
    h('button.icon-btn', { title: tr('Retirer'), onclick: async () => {
      if (!await confirmDialog(tr('Retirer ce stockage ?'), tr('Noxa oublie ce stockage et ses copies. Les fichiers déjà copiés restent sur le stockage.'), tr('Retirer'))) return;
      try { await del(`/api/admin/cold/stores/${s.id}`); toast(tr('Stockage retiré'), 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); }
    } }, icon('trash')));
  const running = data.jobs.filter((j) => j.state === 'running');
  const errors = data.jobs.filter((j) => j.state === 'error');
  const jobRow = (j) => h('div.user-row',
    h('div.grow', h('strong', j.title), h('span.muted', ` → ${(byId[j.store_id] || {}).name || '?'}`),
      j.state === 'running' && j.phase === 'compress' ? h('div.dl-bar.cold-bar', h('span.films', { style: { width: `${Math.round((j.pct || 0) * 100)}%` } })) : null,
      j.state === 'running' && j.phase === 'compress' ? h('div.small.muted', tr('Compression… {p} %', { p: Math.round((j.pct || 0) * 100) }),
        j.paused ? ` · ${tr('en pause pendant une lecture convertie')}` : '', j.hw ? ` · ${tr('carte graphique')}` : ` · ${tr('processeur')}`) : null,
      j.state === 'running' && j.phase !== 'compress' && j.total ? h('div.dl-bar.cold-bar', h('span.films', { style: { width: `${Math.round((j.done / j.total) * 100)}%` } })) : null,
      j.state === 'running' && j.phase !== 'compress' ? h('div.small.muted', j.total ? tr('{a} sur {b}', { a: coldSize(j.done || 0), b: coldSize(j.total) }) : tr('Préparation…')) : null,
      j.kind === 'compress' && j.state !== 'running' ? h('div.small.muted', tr('Compression d’une archive')) : null,
      j.error ? h('div.form-error.small', j.error) : null),
    j.state === 'error' ? h('button.btn.small', { onclick: async () => { await post(`/api/admin/cold/jobs/${j.id}/retry`); reload(); } }, tr('Réessayer')) : null,
    h('button.icon-btn', { title: tr('Annuler'), onclick: async () => { await del(`/api/admin/cold/jobs/${j.id}`); reload(); } }, icon('close')));
  const rate = h('input', { type: 'number', min: 0, step: 1, value: data.settings.cold_rate || 0 });
  const ratePlay = h('input', { type: 'number', min: 0, step: 1, value: data.settings.cold_rate_play });
  // la page est longue : en sous-onglets (retenu sur cet appareil)
  const COLD_TABS = [['stores', tr('Stockages')], ['free', tr('Libérer de la place')], ['compress', tr('Compression')], ['cache', tr('Cache et serveurs liés')]];
  let tab = store('cold-tab') || 'stores';
  if (!COLD_TABS.some(([k]) => k === tab)) tab = 'stores';
  const jobsPanel = running.length || data.queued || errors.length ? h('section.panel', h('h2', tr('Archivage en cours')),
    data.queued ? h('p.muted.small', plural(data.queued, '{n} titre en attente', '{n} titres en attente')) : null,
    h('div.user-list', [...running, ...errors].map(jobRow)),
    data.jobs.filter((j) => j.state === 'queued').length ? h('details', h('summary.small', tr('File d’attente')),
      h('div.user-list', data.jobs.filter((j) => j.state === 'queued').map(jobRow))) : null) : null;
  const sections = {
    stores: () => [
      h('div.toolbar', h('p.muted', tr('Le titre garde son affiche, son résumé et son historique ; si son fichier quitte la bibliothèque, Noxa le lit depuis la copie (quelques secondes de plus au démarrage). Pour archiver : menu ⋯ d’un titre, ou sélection multiple d’une bibliothèque.')),
        h('button.btn.primary', { onclick: async () => { if (await coldForm(null, data.available, extra.peers)) { toast(tr('Stockage ajouté'), 'ok'); reload(); } } }, tr('+ Ajouter un stockage'))),
      stores.length ? h('div.user-list', stores.map(storeRow)) : h('div.empty', tr('Aucun stockage froid. Ajoutez un disque, un NAS ou un stockage en ligne.')),
      jobsPanel,
      h('section.panel', h('h2', tr('Débit des copies')),
        h('p.muted.small', tr('Les copies se font une à la fois, en tâche de fond. Pendant qu’une vidéo est regardée, leur débit est réduit pour ne pas la gêner. 0 : sans limite.')),
        h('div.grid2', field(tr('Débit maximal (Mo/s)'), rate), field(tr('Pendant une lecture (Mo/s)'), ratePlay)),
        h('div.actions-row', h('button.btn', { onclick: async () => {
          try { await put('/api/admin/cold/settings', { cold_rate: Number(rate.value), cold_rate_play: Number(ratePlay.value) }); toast(tr('Enregistré'), 'ok'); } catch (e) { toast(e.message, 'bad'); }
        } }, tr('Enregistrer'))))],
    free: () => [freePanel(freeData, stores, reload), freeLists(freeData, reload)],
    compress: () => [compressPanel(comp, reload), jobsPanel],
    cache: () => [cachePanel(extra, reload)],
  };
  const tabs = h('div.tabs.cold-tabs', { role: 'tablist' }, COLD_TABS.map(([k, label]) => h('button.tab', { type: 'button', role: 'tab',
    class: k === tab ? 'tab active' : 'tab', 'aria-selected': String(k === tab), onclick: () => { store('cold-tab', k); reload(); } },
  label, k === 'free' && freeData.groups.some((g) => g.state === 'proposed') ? h('span.count-badge', freeData.groups.filter((g) => g.state === 'proposed').length) : null)));
  clear(body, tabs, ...sections[tab]());
  // suivi de la copie en cours (pas sur les onglets à formulaires : la saisie serait perdue)
  if ((running.length || data.queued) && (tab === 'stores' || tab === 'compress')) {
    setTimeout(() => { if (ctx.isCurrent() && body.isConnected && (store('cold-tab') || 'stores') === tab) reload(); }, 2500);
  }
}

// libérer de la place : règle, Radarr / Sonarr, suppression par bibliothèque
function freePanel(free, stores, reload) {
  const c = free.conf;
  const on = toggle(tr('Libérer de la place automatiquement'), c.enabled);
  const days = h('input', { type: 'number', min: 7, max: 3650, value: c.days });
  const mode = select([['review', tr('Me proposer une liste à valider')], ['auto', tr('Archiver et libérer d’office')]], c.mode);
  const store = select([['', tr('Premier stockage actif')], ...stores.map((s) => [s.id, s.name])], c.store || '');
  const delay = h('input', { type: 'number', min: 0, max: 90, value: c.delay_days });
  const minFree = h('input', { type: 'number', min: 0, value: c.min_free_gb });
  const keepList = toggle(tr('Garder les titres d’une liste « À regarder »'), c.keep_watchlist);
  const keepRated = toggle(tr('Garder les titres notés 4 ou 5 étoiles'), c.keep_rated);
  const keepStarted = toggle(tr('Garder les titres commencés'), c.keep_started);
  const methods = {};
  const libRows = free.libraries.map((l) => {
    const chosen = select([['arr', l.kind === 'movie' ? tr('par Radarr') : tr('par Sonarr')], ['write', tr('par Noxa (bibliothèque en écriture)')],
      ['manual', tr('à la main (liste des fichiers)')]], l.method);
    methods[l.id] = chosen;
    const use = h('input', { type: 'checkbox', checked: !c.libraries.length || c.libraries.includes(l.id) });
    return { id: l.id, use, row: h('div.cold-lib', h('label.check', use, h('span', l.name)), chosen) };
  });
  const arrFields = (which, label) => {
    const url = h('input', { value: c[`${which}_url`] || '', placeholder: which === 'radarr' ? 'http://192.168.1.20:7878' : 'http://192.168.1.20:8989' });
    const key = h('input', { type: 'password', autocomplete: 'off', placeholder: c.secrets_set.includes(`${which}_key`) ? tr('Laisser vide pour garder l’actuel') : tr('Settings → General → API Key') });
    const test = h('button.btn.small', { type: 'button', onclick: async () => {
      try { const r = await post('/api/admin/cold/free/check', { which, url: url.value.trim(), key: key.value.trim() }); toast(tr('{app} {v} répond', { app: r.app, v: r.version || '' }), 'ok'); } catch (e) { toast(e.message, 'bad'); }
    } }, tr('Tester'));
    return { url, key, el: h('div.grid2', field(tr('Adresse de {app}', { app: label }), url), field(tr('Clé d’API de {app}', { app: label }), h('div.actions-row', key, test))) };
  };
  const radarr = arrFields('radarr', 'Radarr');
  const sonarr = arrFields('sonarr', 'Sonarr');
  const preview = h('div');
  const save = async () => {
    const libs = libRows.filter((x) => x.use.checked).map((x) => x.id);
    const body = { enabled: on.querySelector('input').checked, days: Number(days.value), mode: mode.value, store: store.value ? Number(store.value) : null,
      delay_days: Number(delay.value), min_free_gb: Number(minFree.value), keep_watchlist: keepList.querySelector('input').checked,
      keep_rated: keepRated.querySelector('input').checked, keep_started: keepStarted.querySelector('input').checked,
      libraries: libs.length === libRows.length ? [] : libs, methods: Object.fromEntries(Object.entries(methods).map(([k, el]) => [k, el.value])),
      radarr_url: radarr.url.value.trim(), sonarr_url: sonarr.url.value.trim() };
    if (radarr.key.value.trim()) body.radarr_key = radarr.key.value.trim();
    if (sonarr.key.value.trim()) body.sonarr_key = sonarr.key.value.trim();
    await put('/api/admin/cold/free', body);
  };
  return h('section.panel', h('h2', tr('Libérer de la place')),
    h('p.muted.small', tr('Les films et les séries que personne n’a regardés depuis le nombre de jours choisi sont copiés sur le stockage froid, puis leur fichier quitte la bibliothèque après un délai. Ils restent dans Noxa et se lisent depuis la copie. Un titre regardé pendant le délai est gardé.')),
    on,
    h('div.grid2', field(tr('Non regardés depuis (jours)'), days), field(tr('Fonctionnement'), mode)),
    h('div.grid2', field(tr('Stockage froid'), store), field(tr('Délai avant suppression (jours)'), delay)),
    field(tr('Seulement si la place libre de la bibliothèque passe sous (Go)'), minFree, tr('0 : sans condition de place')),
    keepList, keepRated, keepStarted,
    h('h3.install-sub', tr('Bibliothèques et suppression des fichiers')),
    h('p.muted.small', tr('Les bibliothèques restent en lecture seule pour Noxa : le fichier est supprimé par Radarr ou Sonarr (le titre n’y est plus surveillé, sinon ils le retéléchargeraient), par Noxa si la bibliothèque est montée en écriture, ou par vous à partir d’une liste.')),
    h('div.cold-libs', libRows.map((x) => x.row)),
    radarr.el, sonarr.el,
    h('div.actions-row',
      h('button.btn.primary', { onclick: async () => { try { await save(); toast(tr('Enregistré'), 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); } } }, tr('Enregistrer')),
      h('button.btn', { onclick: async () => {
        try {
          await save();
          const r = await get('/api/admin/cold/free/preview');
          clear(preview, h('p.small', tr('Nouveaux titres concernés avec ces réglages (en plus de ceux déjà listés) : {n}, {size}.', { n: r.count, size: coldSize(r.size) })),
            r.items.length ? h('div.table-wrap', h('table.stat-table', h('tbody', r.items.slice(0, 50).map((x) => h('tr',
              h('td', h('a', { href: `#/item/${x.root}` }, x.title), x.year ? h('span.muted', ` (${x.year})`) : null), h('td', x.library),
              h('td', x.last ? tr('vu {when}', { when: since(x.last) }) : tr('jamais vu')), h('td', coldSize(x.size))))))) : null);
        } catch (e) { toast(e.message, 'bad'); }
      } }, tr('Aperçu')),
      h('button.btn', { onclick: async () => {
        try { await save(); const r = await post('/api/admin/cold/free/run'); toast(tr('{n} nouveau(x) · {f} libéré(s)', { n: r.new, f: r.freed }), 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); }
      } }, tr('Lancer maintenant'))),
    preview);
}

// ce qui est prévu : à valider, en cours d'archivage, suppression prévue, à effacer à la main ; titres gardés
function freeLists(free, reload) {
  const by = (st) => free.groups.filter((g) => g.state === st);
  const act = (label, fn, extra = '') => h(`button.btn.small${extra}`, { onclick: async () => { try { await fn(); reload(); } catch (e) { toast(e.message, 'bad'); } } }, label);
  const row = (g, extras) => h('div.user-row',
    h('div.grow', h('a', { href: `#/item/${g.root}` }, h('strong', g.title)), h('span.muted', ` · ${coldSize(g.size)}`),
      g.files.length > 1 ? h('span.muted.small', ` · ${plural(g.files.length, '{n} fichier', '{n} fichiers')}`) : null,
      g.delete_after ? h('div.small.muted', tr('Supprimé de la bibliothèque le {d}', { d: new Date(g.delete_after * 1000).toLocaleDateString(locale(), { day: 'numeric', month: 'long' }) })) : null,
      g.errors.map((e) => h('div.form-error.small', e)),
      g.state === 'manual' ? h('details', h('summary.small', tr('Fichiers à supprimer')), h('div.small', g.files.map((f) => h('div', h('code', f.path))))) : null),
    ...extras(g));
  const keepBtn = (g) => act(tr('Garder sur le disque'), () => post('/api/admin/cold/free/keep', { root: g.root }));
  const skipBtn = (g) => act(tr('Pas cette fois'), () => post('/api/admin/cold/free/cancel', { root: g.root }), '.ghost');
  const proposed = by('proposed');
  const panels = [];
  if (proposed.length) {
    const size = proposed.reduce((n, g) => n + g.size, 0);
    panels.push(h('section.panel', h('h2', tr('À valider')),
      h('p.muted.small', tr('{n} titre(s) non regardés, {size} à libérer.', { n: proposed.length, size: coldSize(size) })),
      h('div.actions-row', act(tr('Tout valider'), () => post('/api/admin/cold/free/approve', { roots: proposed.map((g) => g.root) }), '.primary')),
      h('div.user-list', proposed.map((g) => row(g, (x) => [act(tr('Valider'), () => post('/api/admin/cold/free/approve', { roots: [x.root] })), keepBtn(x), skipBtn(x)])))));
  }
  const approved = by('approved');
  if (approved.length) panels.push(h('section.panel', h('h2', tr('Copie sur le stockage froid')), h('div.user-list', approved.map((g) => row(g, (x) => [keepBtn(x), skipBtn(x)])))));
  const pending = by('pending');
  if (pending.length) panels.push(h('section.panel', h('h2', tr('Suppression prévue')), h('p.muted.small', tr('Copie vérifiée. Le fichier quittera la bibliothèque à la date indiquée, sauf si le titre est regardé d’ici là.')),
    h('div.user-list', pending.map((g) => row(g, (x) => [keepBtn(x), skipBtn(x)])))));
  const manual = by('manual');
  if (manual.length) panels.push(h('section.panel', h('h2', tr('À supprimer à la main')), h('p.muted.small', tr('Copie vérifiée : vous pouvez effacer ces fichiers de la bibliothèque. Noxa s’en aperçoit au prochain scan.')),
    h('div.user-list', manual.map((g) => row(g, (x) => [keepBtn(x)])))));
  if (free.freed.n) panels.push(h('p.muted.small', tr('Déjà libéré : {n} fichier(s), {size}. Ces titres se lisent depuis le stockage froid.', { n: free.freed.n, size: coldSize(free.freed.bytes) })));
  if (free.kept.length) panels.push(h('details.panel', h('summary', tr('Toujours gardés sur le disque ({n})', { n: free.kept.length })),
    h('div.user-list', free.kept.map((k) => h('div.user-row', h('div.grow', h('a', { href: `#/item/${k.item_id}` }, k.title)),
      act(tr('Ne plus garder'), () => post('/api/admin/cold/free/keep', { root: k.item_id, keep: false }), '.ghost'))))));
  return h('div', ...panels);
}

// compression des archives (HEVC) : carte graphique recommandée
function compressPanel(c, reload) {
  const on = toggle(tr('Compresser les fichiers archivés (HEVC)'), c.enabled);
  const quality = select([['high', tr('Très bonne qualité (moins de gain)')], ['balanced', tr('Équilibrée')], ['small', tr('Petite taille')]], c.quality);
  const height = select([['0', tr('Garder la définition')], ['2160', tr('4K au plus')], ['1080', tr('1080p au plus')], ['720', tr('720p au plus')]], String(c.height));
  const engine = { nvenc: tr('NVIDIA (NVENC)'), vaapi: tr('Intel ou AMD (VAAPI)') }[c.hw];
  const saved = c.done.n ? Math.round((1 - c.done.after / Math.max(1, c.done.before)) * 100) : 0;
  return h('section.panel', h('h2', tr('Compression des archives'), ' ', h('span.tag.accent', tr('Carte graphique recommandée'))),
    h('p.muted.small', tr('Avant d’être copiée, la vidéo est réencodée en HEVC : souvent 30 à 60 % de place en moins sur le stockage froid, pour une qualité très proche. Le son, les sous-titres, les chapitres et les polices sont gardés tels quels. Les vidéos déjà en HEVC, AV1 ou VP9, et les vidéos HDR, sont copiées sans changement ; si le gain est trop faible, l’original est gardé. Les fichiers de la bibliothèque ne sont jamais modifiés.')),
    c.hw ? h('div.status-box.ok', tr('Carte graphique utilisée : {engine}.', { engine }))
      : h('div.status-box.bad', tr('Aucune carte graphique configurée (Paramètres → Transcodage) : la compression se fait par le processeur, très lentement sur une petite machine (une à plusieurs heures par film), et se met en pause pendant les lectures converties.')),
    on, h('div.grid2', field(tr('Qualité'), quality), field(tr('Définition'), height)),
    c.done.n ? h('p.small', tr('{n} archive(s) compressée(s) : {a} avant, {b} après (−{p} %).', { n: c.done.n, a: coldSize(c.done.before), b: coldSize(c.done.after), p: saved })) : null,
    h('div.actions-row',
      h('button.btn', { onclick: async () => {
        try { await put('/api/admin/cold/settings', { compress: on.querySelector('input').checked, compress_quality: quality.value, compress_height: Number(height.value) }); toast(tr('Enregistré'), 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); }
      } }, tr('Enregistrer')),
      c.todo.count ? h('button.btn', { onclick: async () => {
        if (!await confirmDialog(tr('Compresser les archives déjà faites ?'), tr('{n} archive(s), {size}, seront relues, compressées puis remplacées sur leur stockage, une à la fois.', { n: c.todo.count, size: coldSize(c.todo.bytes) }), tr('Compresser'), false)) return;
        try { const r = await post('/api/admin/cold/compress', {}); toast(plural(r.queued, '{n} archive à compresser', '{n} archives à compresser'), 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); }
      } }, tr('Compresser les archives déjà faites ({n})', { n: c.todo.count })) : null));
}

// titres ramenés en local, et place réservée aux archives des serveurs liés
function cachePanel(extra, reload) {
  const c = extra.cache;
  const gb = h('input', { type: 'number', min: 0, step: 1, value: c.gb });
  const keep = h('input', { type: 'number', min: 0, step: 1, value: c.keep_free_gb });
  const next = h('input', { type: 'number', min: 0, max: 20, value: c.next });
  const host = h('input', { type: 'number', min: 0, step: 1, value: extra.host_gb });
  const saveBtn = (body) => async () => { try { await put('/api/admin/cold/extra', body()); toast(tr('Enregistré'), 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); } };
  return h('div',
    h('section.panel', h('h2', tr('Titres ramenés en local')),
      h('p.muted.small', tr('Un titre lu depuis un stockage en ligne est ramené sur ce serveur, avec les épisodes qui suivent : les lectures suivantes démarrent tout de suite. Les plus anciens repartent quand la place réservée est pleine (la copie froide, elle, reste).')),
      h('p.small', tr('{n} titre(s) ramené(s), {size}', { n: c.count, size: coldSize(c.bytes) })),
      h('div.grid2', field(tr('Place réservée (Go)'), gb, tr('0 : ne rien ramener')), field(tr('Toujours garder libres sur le disque (Go)'), keep)),
      field(tr('Épisodes suivants à ramener'), next),
      h('div.actions-row', h('button.btn', { onclick: saveBtn(() => ({ cache_gb: Number(gb.value), keep_free_gb: Number(keep.value), next: Number(next.value) })) }, tr('Enregistrer')),
        c.count ? h('button.btn.ghost', { onclick: saveBtn(() => ({ clear_cache: true })) }, tr('Vider')) : null)),
    extra.peers.length ? h('section.panel', h('h2', tr('Archives des serveurs liés')),
      h('p.muted.small', tr('Place que ce serveur réserve aux archives des autres serveurs de la fédération (dans son dossier de données). 0 : il n’en accueille pas.')),
      field(tr('Place réservée (Go)'), host),
      extra.hosted.length ? h('div.small', extra.hosted.map((x) => h('div', `${x.peer} : ${coldSize(x.bytes)}`))) : null,
      h('div.actions-row', h('button.btn', { onclick: saveBtn(() => ({ host_gb: Number(host.value) })) }, tr('Enregistrer')))) : null);
}

// archiver des titres (fiche, sélection multiple) : choix du stockage
export async function archiveDialog(ids, label) {
  let data;
  try { data = await get('/api/admin/cold'); } catch (e) { toast(e.message, 'bad'); return; }
  const stores = data.stores.filter((s) => s.enabled);
  if (!stores.length) {
    if (await confirmDialog(tr('Aucun stockage froid'), tr('Ajoutez d’abord un stockage froid (disque, NAS, stockage en ligne) dans l’administration.'), tr('Ouvrir le stockage froid'), false)) location.hash = '#/admin/cold';
    return;
  }
  const pick = select(stores.map((s) => [s.id, `${s.name} — ${coldSummary(s)}`]), stores[0].id);
  const ok = await modal(tr('Archiver sur un stockage froid'), h('div.form',
    h('p', label), field(tr('Stockage'), pick),
    h('p.muted.small', tr('Le fichier est copié puis vérifié, en tâche de fond. Il reste aussi dans la bibliothèque : rien n’y est supprimé.'))),
  [{ label: tr('Annuler'), value: false }, { label: tr('Archiver'), primary: true, value: true }]);
  if (!ok) return;
  try {
    const r = await post('/api/admin/cold/archive', { ids, store: Number(pick.value) });
    toast(r.queued ? plural(r.queued, '{n} fichier à archiver', '{n} fichiers à archiver') : tr('Déjà archivé sur ce stockage'), 'ok');
  } catch (e) { toast(e.message, 'bad'); }
}

// copies froides d'un titre (fiche, menu ⋯)
export async function coldCopiesDialog(item) {
  let r;
  try { r = await get(`/api/admin/cold/items/${item.id}`); } catch (e) { toast(e.message, 'bad'); return; }
  const list = h('div.user-list', r.copies.map((c) => h('div.user-row',
    h('div.grow', h('strong', c.store), h('div.small', h('code', c.name)),
      h('div.small.muted', `${coldSize(c.size)} · ${tr('vérifiée {when}', { when: since(c.verified_at) })}`)),
    h('button.icon-btn', { title: tr('Supprimer cette copie'), onclick: async () => {
      if (!await confirmDialog(tr('Supprimer cette copie ?'), tr('Le fichier est effacé du stockage « {s} ».', { s: c.store }), tr('Supprimer'))) return;
      try { await del(`/api/admin/cold/items/${item.id}/${c.store_id}`); toast(tr('Copie supprimée'), 'ok'); document.querySelector('.modal-overlay')?.remove(); } catch (e) { toast(e.message, 'bad'); }
    } }, icon('trash')))));
  const jobs = r.jobs.map((j) => h('p.small', j.state === 'error' ? h('span.form-error', `${tr('Échec')} : ${j.error}`) : tr('Archivage en attente ou en cours')));
  modal(tr('Copies sur le stockage froid'), h('div.form', r.copies.length ? list : h('p.muted', tr('Aucune copie pour l’instant.')), ...jobs),
    [{ label: tr('Fermer'), value: false }]);
}
