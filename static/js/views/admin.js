import { tr } from '../i18n.js';

// Administration pages, grouped by topic in the Settings menu.
// keys: words that let the menu search find it.
const ADMIN_DEFS = [
  { title: tr('Serveur'), items: [
    { id: 'dashboard', label: tr('Vue d’ensemble'), icon: 'dashboard', lead: tr('Version, état du serveur et lectures en cours.'),
      keys: tr('tableau de bord lectures en cours streams mémoire swap charge processeur version arrêter') },
    { id: 'stats', label: tr('Statistiques'), icon: 'list', lead: tr('Qui regarde quoi, combien de temps, débit utilisé, et les titres jamais regardés.'),
      keys: tr('statistiques activité heures visionnage débit données comptes titres populaires jamais regardés place disque') },
    { id: 'users', label: tr('Utilisateurs'), icon: 'group', lead: tr('Comptes, profils du foyer, contrôle parental et accès aux bibliothèques.'),
      keys: tr('compte profil enfant pin âge restriction administrateur désactiver mot de passe foyer langue language') },
    { id: 'update', label: tr('Mise à jour'), icon: 'upgrade', lead: tr('Version installée et mises à jour de Noxa.'),
      keys: tr('version mettre à jour github nouveautés changements réinstaller') },
    { id: 'backups', label: tr('Sauvegardes'), icon: 'backup', lead: tr('Sauvegarde automatique de la base et des clés, et restauration.'),
      keys: tr('sauvegarde backup restaurer restauration archive base de données migration nouveau serveur') },
    { id: 'federation', label: tr('Serveurs liés'), icon: 'link', lead: tr('Relier vos serveurs Noxa : comptes communs, bibliothèques de chacun visibles et lisibles depuis l’autre.'),
      keys: tr('fédération cluster lier jumeler invitation second serveur réplication tunnel') },
    { id: 'tasks', label: tr('Tâches planifiées'), icon: 'clock', lead: tr('Travaux de fond : scans, métadonnées, intros, sous-titres, nettoyage.'),
      keys: tr('planification scan automatique nettoyage cache intro générique chapitres horaire') },
  ] },
  { title: tr('Médiathèque'), items: [
    { id: 'libraries', label: tr('Bibliothèques'), icon: 'folder', lead: tr('Dossiers de films et de séries, et leur analyse.'),
      keys: tr('dossier films séries animes scanner rescanner ajouter chemin') },
    { id: 'shares', label: tr('Partages réseau'), icon: 'storage', lead: tr('Montage des dossiers partagés d’un NAS (SMB) pour y créer des bibliothèques.'),
      keys: tr('nas smb cifs samba montage monter partage réseau') },
    { id: 'cold', label: tr('Stockage froid'), icon: 'backup', lead: tr('Copies des titres sur un autre stockage (disque, NAS, cloud) : lus depuis là si le fichier quitte la bibliothèque.'),
      keys: tr('stockage froid archive archiver libérer place disque usb nas sftp webdav s3 backblaze hetzner storage box nextcloud rclone strm') },
    { id: 'metadata', label: tr('Métadonnées'), icon: 'film', lead: tr('Affiches, résumés et titres d’épisodes (TMDB).'),
      keys: tr('tmdb clé api affiches résumés langue actualiser images') },
    { id: 'detection', label: tr('Ajout des nouveautés'), icon: 'eye', lead: tr('Détection automatique des fichiers ajoutés et notifications de Radarr / Sonarr.'),
      keys: tr('surveillance dossiers webhook radarr sonarr nouveaux fichiers détection') },
    { id: 'requests', label: tr('Demandes'), icon: 'star', lead: tr('Connexion à Seerr pour permettre aux utilisateurs de demander des films et des séries.'),
      keys: tr('seerr overseerr jellyseerr demander radarr sonarr découvrir') },
  ] },
  { title: tr('Lecture'), items: [
    { id: 'transcoding', label: tr('Transcodage'), icon: 'bolt', lead: tr('Lectures simultanées et carte graphique.'),
      keys: tr('processeur cpu gpu carte graphique nvidia nvenc vaapi intel amd limites lectures simultanées matériel') },
    { id: 'subtitles', label: tr('Sous-titres et son'), icon: 'subs', lead: tr('Sous-titres manquants (OpenSubtitles), sous-titres intégrés et mixage du son 5.1 / 7.1.'),
      keys: tr('sous-titres opensubtitles bazarr télécharger manquants préparer audio son 5.1 7.1 stéréo mixage downmix volume dialogues') },
    { id: 'dlna', label: tr('DLNA'), icon: 'tv', lead: tr('Téléviseurs, consoles et box du réseau local : Noxa apparaît comme serveur multimédia.'),
      keys: tr('dlna upnp téléviseur smart tv samsung lg sony console xbox ps5 box réseau local serveur multimédia') },
    { id: 'downloads', label: tr('Téléchargements'), icon: 'install', lead: tr('Visionnage hors ligne dans l’application.'),
      keys: tr('hors ligne télécharger offline téléphone') },
    { id: 'live', label: tr('TV en direct'), icon: 'tv', lead: tr('Listes de chaînes IPTV (M3U) et guide des programmes.'),
      keys: tr('iptv m3u xmltv chaînes guide epg télévision box') },
  ] },
];
// Menu pages: a page can gather several topics, as sub-tabs (each keeps its #/admin/<id> address).
export function settingsPage(defs, ids, opts = {}) {
  const first = defs[ids[0]];
  return { id: ids[0], label: opts.label || first.label, icon: opts.icon || first.icon, lead: opts.lead || first.lead,
    keys: ids.map((i) => `${defs[i].label} ${defs[i].lead} ${defs[i].keys}`).join(' '),
    tabs: ids.map((i) => ({ id: i, label: (opts.names || {})[i] || defs[i].label, keys: `${defs[i].label} ${defs[i].lead} ${defs[i].keys}` })) };
}
const A = Object.fromEntries(ADMIN_DEFS.flatMap((g) => g.items).map((it) => [it.id, it]));
export const ADMIN_GROUPS = [
  { title: tr('Serveur'), items: [
    settingsPage(A, ['dashboard', 'stats'], { lead: tr('Version, état du serveur, lectures en cours et statistiques.'), names: { dashboard: tr('État du serveur') } }),
    settingsPage(A, ['users']),
    settingsPage(A, ['update', 'backups', 'tasks'], { label: tr('Maintenance'), lead: tr('Mises à jour de Noxa, sauvegardes et travaux planifiés.') }),
    settingsPage(A, ['federation']),
  ] },
  { title: tr('Médiathèque'), items: [
    settingsPage(A, ['libraries', 'shares', 'detection'], { lead: tr('Dossiers de films et de séries, partages réseau du NAS et ajout automatique des nouveautés.') }),
    settingsPage(A, ['metadata', 'requests'], { label: tr('Métadonnées et demandes'), lead: tr('Affiches et résumés (TMDB), et demandes de films et de séries (Seerr).'), names: { requests: tr('Demandes (Seerr)') } }),
    settingsPage(A, ['cold']),
  ] },
  { title: tr('Lecture et diffusion'), items: [
    settingsPage(A, ['transcoding', 'subtitles'], { label: tr('Conversion et son'), lead: tr('Lectures simultanées, carte graphique, sous-titres manquants et mixage du son.') }),
    settingsPage(A, ['dlna', 'downloads'], { label: tr('Diffusion et hors ligne'), icon: 'install', lead: tr('Téléviseurs et consoles du réseau local (DLNA), et visionnage hors ligne.'), names: { downloads: tr('Téléchargements hors ligne') } }),
    settingsPage(A, ['live']),
  ] },
];
// anciennes adresses (favoris, liens) -> nouvelles pages
export const ADMIN_ALIASES = { settings: 'metadata', streams: 'dashboard' };

// Each page lives in views/admin/ and is loaded the first time it is opened: the admin code is not downloaded by
// people who never open these settings.
const PAGES = { // page id -> [module in views/admin/, function]
  cold: ['cold', 'coldPage'],
  stats: ['stats', 'statsPage'],
  dashboard: ['dashboard', 'dashboardPage'],
  backups: ['backups', 'backupsPage'],
  users: ['users', 'usersTab'],
  update: ['update', 'updatePage'],
  tasks: ['tasks', 'tasksTab'],
  libraries: ['libraries', 'librariesTab'],
  federation: ['federation', 'federationPage'],
  dlna: ['dlna', 'dlnaPage'],
  shares: ['libraries', 'sharesTab'],
  metadata: ['server-settings', 'metadataPage'],
  detection: ['server-settings', 'detectionPage'],
  requests: ['requests', 'requestsTab'],
  transcoding: ['server-settings', 'transcodingPage'],
  subtitles: ['server-settings', 'subtitlesPage'],
  downloads: ['server-settings', 'downloadsPage'],
  live: ['live', 'liveTab'],
};

async function page(file) {
  return import(`./admin/${file}.js`);
}

export async function renderAdminSection(id, body, ctx) {
  const [file, fn] = PAGES[id];
  await (await page(file))[fn](body, ctx);
}

// opened from a title's menu and from bulk selection
export async function archiveDialog(ids, label) {
  return (await page('cold')).archiveDialog(ids, label);
}

export async function coldCopiesDialog(item) {
  return (await page('cold')).coldCopiesDialog(item);
}
