// Admin pages built on the server settings: metadata, new content detection, subtitles, transcoding, downloads.
import { get, post, put } from '../../api.js';
import { refreshMe } from '../../app.js';
import { locale, tr } from '../../i18n.js';
import { clear, confirmDialog, field, h, select, toast, toggle } from '../../ui.js';

// --------------------------------------------------------------- server settings
// Each page reads the settings, then redraws itself after saving.
async function withSettings(body, ctx, build) {
  const s = await get('/api/admin/settings');
  if (!ctx.isCurrent()) return;
  const save = async (payload, msg) => {
    try { await put('/api/admin/settings', payload); toast(msg, 'ok'); withSettings(body, ctx, build); } catch (e) { toast(e.message, 'bad'); }
  };
  clear(body, ...[build(s, save, () => withSettings(body, ctx, build))].flat());
}

export const metadataPage = (body, ctx) => withSettings(body, ctx, (s, save) => {
  const key = h('input', { type: 'password', autocomplete: 'off',
    placeholder: s.tmdb_set ? tr('Clé enregistrée (laisser vide pour la garder)') : tr('Clé API (v3) ou jeton de lecture (v4)') });
  const lang = select([['fr-FR', tr('Français (France)')], ['fr-CA', tr('Français (Canada)')], ['en-US', tr('Anglais')], ['de-DE', tr('Allemand')],
    ['es-ES', tr('Espagnol')], ['it-IT', tr('Italien')]], s.language);
  return [
    h('section.panel', h('h2', tr('Clé TMDB et langue')),
      h('p.muted.small', tr('Affiches, résumés, distribution et titres d’épisodes proviennent de The Movie Database. Une clé gratuite s’obtient sur themoviedb.org (Paramètres → API). Sans clé, Noxa utilise les noms de fichiers, les fichiers .nfo et les images présentes dans les dossiers.')),
      h('div.status-box', { class: s.tmdb_set || s.tmdb_shared ? 'status-box ok' : 'status-box' }, s.tmdb_set ? tr('Clé TMDB configurée.')
        : s.tmdb_shared ? tr('Pas de clé ici : celle du serveur lié « {name} » est utilisée.', { name: s.tmdb_shared }) : tr('Aucune clé TMDB.')),
      h('div.grid2', field(tr('Clé TMDB'), key), field(tr('Langue des métadonnées'), lang)),
      h('div.actions-row',
        h('button.btn.primary', { onclick: () => save({ ...(key.value.trim() ? { tmdb_key: key.value.trim() } : {}), language: lang.value }, tr('Métadonnées enregistrées')) }, tr('Enregistrer')),
        s.tmdb_set ? h('button.btn', { onclick: () => save({ tmdb_key: '' }, tr('Clé supprimée')) }, tr('Supprimer la clé')) : null)),
    h('section.panel', h('h2', tr('Actualisation')),
      h('p.muted.small', tr('Les fiches incomplètes sont complétées automatiquement (tâche « Actualiser les métadonnées »). Un rechargement complet est utile après un changement de langue.')),
      h('div.actions-row',
        h('button.btn', { onclick: async () => {
          try { await post('/api/admin/scan', { refresh_metadata: true }); toast(tr('Actualisation de toutes les métadonnées lancée'), 'ok'); }
          catch (e) { toast(e.message, 'bad'); }
        } }, tr('Tout réactualiser maintenant')),
        h('a.btn.ghost', { href: '#/admin/tasks' }, tr('Planifier l’actualisation…')))),
  ];
});

export const detectionPage = (body, ctx) => withSettings(body, ctx, (s, save, reload) => {
  const watch = select([['0', tr('Désactivée')], ['30', tr('Toutes les 30 secondes')], ['60', tr('Toutes les minutes')], ['300', tr('Toutes les 5 minutes')],
    ['900', tr('Toutes les 15 minutes')]], String(s.watch_interval));
  const hookInput = h('input', { readonly: true, value: `${location.origin}${s.webhook_path}`, onfocus: (e) => e.target.select() });
  return [
    h('section.panel', h('h2', tr('Surveillance des dossiers')),
      h('p.muted.small', tr('Sur un partage réseau, les ajouts faits depuis une autre machine ne sont pas signalés au système. Noxa vérifie donc régulièrement les dossiers et analyse la bibliothèque concernée une fois la copie terminée.')),
      h('div.grid2', field(tr('Surveiller les dossiers'), watch, tr('Le scan complet quotidien se règle dans « Tâches planifiées ».'))),
      h('button.btn.primary', { onclick: () => save({ watch_interval: Number(watch.value) }, tr('Enregistré')) }, tr('Enregistrer')),
      s.watch_events.length ? h('details', h('summary.small', tr('Derniers déclenchements')),
        h('ul.small', s.watch_events.map((e) => h('li', `${new Date(e.t * 1000).toLocaleString(locale())} — ${e.text}`)))) : null),
    h('section.panel', h('h2', tr('Webhook Radarr / Sonarr')),
      h('p.muted.small', tr('Radarr et Sonarr signalent chaque import à Noxa : les nouveaux titres apparaissent immédiatement.')),
      h('div.copy-row', hookInput,
        h('button.btn.small', { onclick: async () => {
          try { await navigator.clipboard.writeText(hookInput.value); toast(tr('Adresse copiée'), 'ok'); }
          catch { hookInput.select(); }
        } }, tr('Copier')),
        h('button.btn.small.ghost', { onclick: async () => {
          if (!await confirmDialog(tr('Nouvelle adresse ?'), tr('L’ancienne adresse cessera de fonctionner : il faudra la remplacer dans Radarr et Sonarr.'), tr('Régénérer'))) return;
          await post('/api/admin/webhook/regenerate'); reload();
        } }, tr('Régénérer'))),
      h('p.muted.small', tr('Dans Radarr et Sonarr : Settings → Connect → + → Webhook. Renseigner cette adresse dans « URL » (méthode POST) et cocher « On Import », « On Upgrade », « On Rename » et « On Delete ». Si Radarr tourne sur une autre machine, utiliser l’adresse du serveur Noxa telle qu’elle est vue depuis Radarr.'))),
  ];
});

export const subtitlesPage = (body, ctx) => withSettings(body, ctx, (s, save) => {
  const prefetch = toggle(tr('Préparer les sous-titres des nouveaux fichiers à l’avance'), s.subs_prefetch);
  prefetch.querySelector('input').addEventListener('change', (e) => save({ subs_prefetch: e.target.checked }, tr('Enregistré')));
  const algo = select([['none', tr('ffmpeg (par défaut)')], ['dave750', 'Dave750'], ['nightmode', tr('Dialogues (nuit)')],
    ['rfc7845', 'RFC 7845'], ['ac4', 'AC-4']], s.downmix_algo);
  const boost = select([['1', tr('× 1 (aucun)')], ['1.5', tr('× 1,5')], ['2', tr('× 2 (par défaut)')], ['2.5', tr('× 2,5')], ['3', '× 3']], String(s.downmix_boost));
  const osPanel = h('section.panel', h('h2', 'OpenSubtitles'), h('p.muted.small', tr('Chargement…')));
  openSubtitlesPanel(osPanel);
  const wPanel = h('section.panel', h('h2', tr('Sous-titres générés (Whisper)')), h('p.muted.small', tr('Chargement…')));
  whisperPanel(wPanel);
  return [
    osPanel, wPanel,
    h('section.panel', h('h2', tr('Sous-titres intégrés')),
      h('p.muted.small', tr('Les sous-titres intégrés aux fichiers (MKV) ne peuvent être extraits qu’en lisant le fichier en entier. Noxa les prépare à l’avance, en dehors des lectures, pour qu’ils soient disponibles dès le début de la vidéo.')),
      prefetch,
      h('p.muted.small', s.subs_queue ? tr('{subs_queue} fichier(s) en attente de préparation.', { subs_queue: s.subs_queue }) : tr('Aucun fichier en attente.')),
      h('button.btn', { onclick: () => save({ subs_prefetch: true }, tr('Préparation lancée pour toute la médiathèque')) }, tr('Tout préparer maintenant'))),
    h('section.panel', h('h2', tr('Son 5.1 / 7.1 sur des enceintes stéréo')),
      h('p.muted.small', tr('« ffmpeg » : équilibre neutre. « Dave750 » : dialogues légèrement renforcés. « Dialogues (nuit) » : effets et musique fortement atténués. « RFC 7845 » et « AC-4 » : mixages normalisés. Le gain s’applique après le mixage ; un limiteur empêche toute saturation.')),
      h('div.grid2', field(tr('Mixage'), algo), field(tr('Gain après mixage'), boost)),
      h('button.btn.primary', { onclick: () => save({ downmix_algo: algo.value, downmix_boost: Number(boost.value) }, tr('Mixage enregistré (prochaines lectures)')) }, tr('Enregistrer'))),
  ];
});

// Subtitles downloaded from OpenSubtitles.com: account, languages, today's quota
async function openSubtitlesPanel(panel) {
  let v;
  try { v = await get('/api/admin/opensubtitles'); } catch (e) { clear(panel, h('h2', 'OpenSubtitles'), h('p.muted', e.message)); return; }
  const key = h('input', { type: 'password', autocomplete: 'off', placeholder: v.key_set ? tr('Clé enregistrée (laisser vide pour la garder)') : tr('Clé d’API (consumer)') });
  const user = h('input', { value: v.user, autocomplete: 'off', spellcheck: false });
  const pass = h('input', { type: 'password', autocomplete: 'new-password', placeholder: v.password_set ? tr('Enregistré (laisser vide pour le garder)') : '' });
  const chosen = new Set(v.langs);
  const chips = h('div.chips', v.all_langs.map((c) => {
    const b = h('button.chip', { type: 'button', 'aria-pressed': String(chosen.has(c)), onclick: () => {
      if (chosen.has(c)) chosen.delete(c); else if (chosen.size < 6) chosen.add(c);
      b.setAttribute('aria-pressed', String(chosen.has(c)));
    } }, v.lang_names[c] || c);
    return b;
  }));
  const hi = toggle(tr('Accepter les sous-titres pour sourds et malentendants (SDH) s’il n’y a qu’eux'), v.hi);
  const status = v.configured
    ? h('div.status-box.ok', v.remaining != null ? tr('Compte connecté : {n} téléchargement(s) restant(s) aujourd’hui.', { n: v.remaining }) : tr('Compte connecté.'))
    : h('div.status-box', tr('Pas encore configuré.'));
  const saveBtn = h('button.btn.primary', { onclick: async () => {
    saveBtn.disabled = true;
    try {
      const body = { user: user.value.trim(), langs: [...chosen], hi: hi.querySelector('input').checked };
      if (key.value.trim()) body.key = key.value.trim();
      if (pass.value) body.password = pass.value;
      const r = await put('/api/admin/opensubtitles', body);
      toast(r.account ? tr('Connexion réussie : {n} téléchargement(s) restant(s) aujourd’hui', { n: r.account.remaining ?? '?' }) : tr('Enregistré'), 'ok');
      await refreshMe();
      openSubtitlesPanel(panel);
    } catch (e) { toast(e.message, 'bad'); saveBtn.disabled = false; }
  } }, tr('Tester et enregistrer'));
  clear(panel, h('h2', 'OpenSubtitles'),
    h('p.muted.small', tr('Noxa télécharge lui-même les sous-titres manquants, sans Bazarr. Il faut un compte gratuit sur opensubtitles.com (20 sous-titres par jour) et une clé d’API, créée sur le site dans « API consumers ». Les sous-titres sont rangés dans le dossier de données de Noxa, jamais à côté des vidéos.')),
    status,
    h('div.grid2', field(tr('Clé d’API'), key), field(tr('Identifiant OpenSubtitles'), user)),
    h('div.grid2', field(tr('Mot de passe OpenSubtitles'), pass), h('div')),
    field(tr('Langues à télécharger automatiquement (6 au plus)'), chips,
      tr('Un film ou un épisode qui n’a aucun sous-titre complet dans l’une de ces langues en reçoit un, chaque nuit et après chaque ajout (tâche « Télécharger les sous-titres manquants »). Les sous-titres calés sur le fichier exact passent en premier ; les traductions automatiques sont écartées.')),
    hi,
    v.last ? h('p.muted.small', tr('Dernier passage : {summary}', { summary: v.last.summary || v.last.status })) : null,
    h('div.actions-row', saveBtn, h('a.btn', { href: '#/admin/tasks' }, tr('Tâches planifiées'))));
}

// Whisper: voice transcription (OpenAI-compatible service, or whisper.cpp on this server)
async function whisperPanel(panel) {
  let v;
  try { v = await get('/api/admin/whisper'); } catch (e) { clear(panel, h('h2', 'Whisper'), h('p.muted', e.message)); return; }
  const url = h('input', { value: v.url, placeholder: 'http://192.168.1.20:8000', spellcheck: false });
  const key = h('input', { type: 'password', autocomplete: 'off', placeholder: v.key_set ? tr('Clé enregistrée (laisser vide pour la garder)') : tr('Facultative') });
  const modelHttp = h('input', { value: v.model_http, spellcheck: false });
  const threads = h('input', { type: 'number', min: 1, max: 64, value: v.threads });
  const local = v.filter || v.cli;
  const save = async (patch, msg = tr('Enregistré')) => {
    try { await put('/api/admin/whisper', patch); toast(msg, 'ok'); await refreshMe(); whisperPanel(panel); } catch (e) { toast(e.message, 'bad'); }
  };
  const status = v.available
    ? h('div.status-box.ok', v.backend === 'http' ? tr('Prêt : service de transcription.') : tr('Prêt : whisper.cpp sur ce serveur (modèle {m}).', { m: v.model }))
    : h('div.status-box', v.note);
  const job = v.current ? h('p.small', tr('En cours : {title} ({p} %)', { title: v.current.title, p: Math.round((v.current.progress || 0) * 100) }),
    v.queue.length ? tr(' · {n} en attente', { n: v.queue.length }) : '') : null;
  const dl = v.download && !v.download.finished ? h('p.small', tr('Téléchargement du modèle {m} : {p} %', { m: v.download.model,
    p: Math.round((v.download.done / Math.max(1, v.download.total)) * 100) })) : (v.download && v.download.error ? h('p.form-error.small', v.download.error) : null);
  const modelRows = Object.entries(v.models).map(([m, info]) => h('div.os-row', h('div.os-info', h('strong', m),
    h('div.muted.small', tr('{n} Mo', { n: info.size_mb }), m === 'base' ? tr(' · bon compromis') : m === 'small' ? tr(' · plus juste, 3 fois plus lent') : '')),
    info.installed
      ? (v.model === m ? h('span.os-badge.good', tr('utilisé')) : h('button.btn.small', { onclick: () => save({ model: m }) }, tr('Utiliser')))
      : h('button.btn.small', { onclick: async () => {
        try { await post('/api/admin/whisper/model', { model: m }); toast(tr('Téléchargement lancé'), 'ok'); setTimeout(() => whisperPanel(panel), 1500); } catch (e) { toast(e.message, 'bad'); }
      } }, tr('Télécharger'))));
  clear(panel, h('h2', tr('Sous-titres générés (Whisper)')),
    h('p.muted.small', tr('Pour les vidéos sans aucun sous-titre dans la langue parlée, Whisper écrit les sous-titres à partir de la voix (dans cette langue : il ne traduit pas). C’est un calcul long ; il se fait une vidéo à la fois, en pause pendant les lectures.')),
    status, job, dl,
    h('h4.form-sub', tr('Service de transcription (recommandé)')),
    h('p.muted.small', tr('Un service compatible avec l’API d’OpenAI, par exemple Speaches (faster-whisper) lancé dans un conteneur à côté de Noxa, avec une carte graphique s’il y en a une. OpenAI convient aussi, mais l’audio des vidéos part alors sur Internet.')),
    h('div.grid2', field(tr('Adresse du service'), url), field(tr('Modèle'), modelHttp, tr('Ex. Systran/faster-whisper-small pour Speaches, whisper-1 pour OpenAI.'))),
    h('div.grid2', field(tr('Clé d’API'), key), h('div')),
    h('div.actions-row', h('button.btn.primary', { onclick: () => {
      const patch = { url: url.value.trim(), model_http: modelHttp.value.trim() || 'whisper-1' };
      if (key.value.trim()) patch.key = key.value.trim();
      save(patch);
    } }, tr('Enregistrer')),
    v.key_set ? h('button.btn', { onclick: () => save({ clear_key: true }, tr('Clé retirée')) }, tr('Retirer la clé')) : null),
    h('h4.form-sub', tr('Sur ce serveur (whisper.cpp)')),
    local
      ? [h('p.muted.small', v.filter ? tr('ffmpeg de ce serveur sait transcrire (filtre whisper). Choisissez un modèle : il est téléchargé dans le dossier de données.')
        : tr('whisper-cli est installé. Choisissez un modèle : il est téléchargé dans le dossier de données.')),
      h('div.os-results', modelRows), field(tr('Cœurs utilisés'), threads, tr('{n} cœur(s) sur ce serveur.', { n: v.cpus })),
      h('button.btn', { onclick: () => save({ threads: Number(threads.value) }) }, tr('Enregistrer'))]
      : h('p.muted.small', tr('Ni ffmpeg 8 avec le filtre whisper, ni whisper-cli n’est installé sur ce serveur (l’image Docker ne les contient pas) : utilisez un service de transcription.')),
    h('p.muted.small', tr('Automatique : tâche « Générer les sous-titres manquants (Whisper) », désactivée par défaut. À la main : fiche d’un film ou d’un épisode, menu ⋯.')));
}

export const transcodingPage = (body, ctx) => withSettings(body, ctx, (s, save) => {
  const maxStreams = h('input', { type: 'number', min: 1, max: 50, value: s.max_streams });
  const maxTranscodes = h('input', { type: 'number', min: 0, max: 16, value: s.max_transcodes });
  // hardware transcoding: engines offered depending on what the container sees
  const dev = s.hw_devices || { nvidia: false, dri: [] };
  const engines = [['none', tr('Aucune (processeur)')],
    ['nvenc', tr('NVIDIA NVENC{p1}', { p1: dev.nvidia ? '' : tr(' (carte non détectée)') })],
    ['vaapi', tr('Intel / AMD (VAAPI){p1}', { p1: dev.dri.length ? '' : tr(' (aucun /dev/dri détecté)') })]];
  const hwEngine = select(engines, s.hw_engine);
  const devices = [...new Set([...dev.dri, s.hw_device])];
  const hwDevice = select(devices.map((d) => [d, d]), s.hw_device);
  const hwDeviceField = field(tr('Périphérique VAAPI'), hwDevice);
  const paintHw = () => { hwDeviceField.hidden = hwEngine.value !== 'vaapi'; };
  hwEngine.addEventListener('change', paintHw);
  paintHw();
  const hwSave = h('button.btn.primary', { onclick: async () => {
    hwSave.disabled = true;
    hwSave.textContent = hwEngine.value === 'none' ? tr('Enregistrement…') : tr('Test de la carte…');
    await save({ hw_engine: hwEngine.value, hw_device: hwDevice.value },
      hwEngine.value === 'none' ? tr('Transcodage par le processeur') : tr('Test réussi : transcodage matériel activé (prochaines lectures)'));
    hwSave.disabled = false;
    hwSave.textContent = tr('Tester et enregistrer');
  } }, tr('Tester et enregistrer'));
  return [
    h('section.panel', h('h2', tr('Lectures simultanées')),
      h('p.muted.small', tr('Processeur : {cpus} cœur(s). Transcodage : {p2}. La lecture directe et le remux sont peu coûteux ; sans carte graphique, un transcodage 1080p (HEVC, 10 bits, sous-titres image, qualité réduite) occupe 1 à 2 cœurs.', { cpus: s.cpus, p2: s.hw_engine === 'none' ? tr('processeur') : tr('carte graphique') })),
      s.ffmpeg ? null : h('div.status-box.bad', tr('ffmpeg est introuvable sur le serveur (apt install ffmpeg).')),
      h('div.grid2', field(tr('Lectures simultanées (max.)'), maxStreams), field(tr('Transcodages simultanés (max.)'), maxTranscodes,
        tr('0 : transcodage désactivé (lecture directe et remux uniquement).'))),
      h('button.btn.primary', { onclick: () => save({ max_streams: Number(maxStreams.value), max_transcodes: Number(maxTranscodes.value) }, tr('Enregistré')) }, tr('Enregistrer'))),
    h('section.panel', h('h2', tr('Films HDR')),
      h('p.muted.small', tr('Quand un film HDR (HDR10, HLG, Dolby Vision avec base HDR10) doit être transcodé, ses couleurs sont converties pour un écran ordinaire ; sans cela, l’image paraît délavée et grise. La lecture directe d’un fichier HDR n’est pas touchée. La conversion se fait sur le processeur et coûte environ un cœur de plus en 1080p.')),
      s.tonemap_ok ? null : h('div.status-box.bad', tr('Le ffmpeg de ce serveur n’a pas le filtre zscale : la conversion HDR est impossible.')),
      field(tr('Conversion HDR → SDR'), select([['hable', tr('Hable (contraste naturel, par défaut)')], ['mobius', tr('Mobius (couleurs plus vives)')],
        ['reinhard', tr('Reinhard (plus doux)')], ['off', tr('Désactivée')]], s.tonemap, { onchange: (e) => save({ tonemap: e.target.value }, tr('Enregistré (prochaines lectures)')) }))),
    h('section.panel', h('h2', tr('Carte graphique')),
      h('p.muted.small', tr('La carte graphique décode et encode la vidéo à la place du processeur, ce qui permet beaucoup plus de transcodages simultanés. Un essai est effectué avant l’enregistrement ; si la carte refuse un fichier, le processeur prend le relais. L’accès d’un conteneur LXC à la carte est décrit dans le README.')),
      h('div.grid2', field(tr('Accélération matérielle'), hwEngine), hwDeviceField),
      hwSave),
  ];
});

export const downloadsPage = (body, ctx) => withSettings(body, ctx, (s, save) => {
  const downloadsToggle = toggle(tr('Autoriser les téléchargements'), s.downloads);
  downloadsToggle.querySelector('input').addEventListener('change', (e) => save({ downloads: e.target.checked }, tr('Enregistré')));
  const prio = select([['low', tr('Discrète : le moins de ressources possible, en pause pendant les lectures')],
    ['normal', tr('Normale : pleine vitesse quand personne ne regarde (conseillé)')],
    ['high', tr('Rapide : tout le processeur, même pendant les lectures')]], s.downloads_priority || 'normal',
  { onchange: (e) => save({ downloads_priority: e.target.value }, tr('Enregistré')) });
  return h('section.panel', h('h2', tr('Visionnage hors ligne')),
    h('p.muted.small', tr('Les utilisateurs peuvent télécharger films et épisodes dans l’application pour les regarder sans connexion. Le serveur prépare une copie à la qualité choisie, que l’appareil récupère pendant la préparation, puis la supprime une fois récupérée.')),
    downloadsToggle,
    field(tr('Vitesse de préparation'), prio,
      tr('Normale : en priorité basse pendant une lecture, et une conversion attend si un spectateur a besoin du processeur ; deux préparations à la fois sur un serveur de 8 cœurs ou plus. Rapide : deux à la fois, sans jamais attendre (les lectures peuvent en pâtir sur un petit serveur).')));
});
