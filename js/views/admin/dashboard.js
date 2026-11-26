// Admin page: overview and current streams.
import { adminTarget, del, get, post, put } from '../../api.js';
import { state } from '../../app.js';
import { locale, tr } from '../../i18n.js';
import { clear, field, fmtTime, h, toast, toggle } from '../../ui.js';

// ---------------------------------------------------------------- streams
const MODES = { direct: tr('Lecture directe'), copy: tr('Remux (vidéo copiée)'), transcode: tr('Transcodage') };

export async function dashboardPage(body, ctx) {
  const remote = adminTarget();
  const version = remote ? (await get('/api/admin/settings')).version : state.status.version;
  const latest = remote ? null : state.me.update;
  const versionCard = h('div.panel.scan', h('strong', `Noxa ${version}`), latest
    ? h('div.small', h('span.tag.accent', tr('Mise à jour')), tr(' Noxa {latest} est disponible · ', { latest }), h('a', { href: '#/admin/update' }, tr('Mettre à jour')))
    : h('div.muted.small', tr('À jour (vérification automatique toutes les 6 heures) · '), h('a', { href: '#/admin/update' }, tr('Détails'))));
  const alertsBox = h('div');
  const paintAlerts = (v) => {
    const c = v.conf;
    const sw = (k, label) => {
      const t = toggle(label, c[k]);
      t.querySelector('input').addEventListener('change', async (e) => {
        try { paintAlerts(await put('/api/admin/alerts', { [k]: e.target.checked })); } catch (ex) { toast(ex.message, 'bad'); }
      });
      return t;
    };
    const gbInput = h('input', { type: 'number', min: 1, max: 5000, value: c.disk_gb, onchange: async () => {
      try { paintAlerts(await put('/api/admin/alerts', { disk_gb: Number(gbInput.value) })); toast(tr('Enregistré'), 'ok'); } catch (ex) { toast(ex.message, 'bad'); }
    } });
    clear(alertsBox,
      v.alerts.length ? h('div.alert-list', v.alerts.map((a) => h('a.alert-row', { href: a.url },
        h('strong', a.title), h('div.small', a.body), h('div.muted.small', new Date(a.created_at * 1000).toLocaleString(locale(), { dateStyle: 'short', timeStyle: 'short' })))))
        : h('div.status-box.ok', tr('Aucune alerte : tout va bien.')),
      h('details.alert-conf', h('summary.small', tr('Alertes envoyées aux administrateurs')),
        h('p.muted.small', tr('Chaque alerte part en notification push sur les appareils où vous l’avez activée (Paramètres → Profil → Notifications), une fois, puis une fois par jour tant que le problème dure.')),
        sw('disk', tr('Disque presque plein (données de Noxa, sauvegardes)')),
        field(tr('Seuil d’espace libre (Go)'), gbInput),
        sw('folder', tr('Dossier de bibliothèque inaccessible (partage réseau démonté)')),
        sw('peer', tr('Serveur lié injoignable depuis 15 minutes')),
        sw('task', tr('Tâche planifiée en échec')),
        h('div.actions-row',
          h('button.btn.small', { onclick: async () => { try { paintAlerts(await post('/api/admin/alerts/check')); toast(tr('Vérification faite'), 'ok'); } catch (ex) { toast(ex.message, 'bad'); } } }, tr('Vérifier maintenant')),
          h('button.btn.small', { onclick: async () => {
            const r = await post('/api/admin/alerts/test');
            toast(r.sent ? tr('Alerte de test envoyée') : tr('Aucun appareil inscrit aux notifications pour ce compte'), r.sent ? 'ok' : 'bad');
          } }, tr('Envoyer une alerte de test')))));
  };
  const load = async () => {
    const [list, sys, alerts] = await Promise.all([get('/api/admin/streams'), get('/api/admin/system').catch(() => null),
      get('/api/admin/alerts').catch(() => null)]);
    if (!ctx.isCurrent()) return;
    if (alerts && !alertsBox.querySelector('details[open]')) paintAlerts(alerts);
    const gb = (mb) => tr('{n} Go', { n: (mb / 1024).toLocaleString(locale(), { maximumFractionDigits: 1, minimumFractionDigits: 1 }) });
    const swapHigh = sys && sys.swap_total && sys.swap_used > sys.swap_total * 0.25;
    const server = sys && sys.mem_total ? h('div.panel.scan', h('strong', tr('Serveur')), h('div.muted.small',
      tr('Mémoire libre {p1} sur {p2} · swap utilisé {p3} sur {p4} · ', { p1: gb(sys.mem_available), p2: gb(sys.mem_total), p3: gb(sys.swap_used), p4: gb(sys.swap_total || 0) }),
      tr('{ffmpeg} ffmpeg ({p2}) · charge {p3} pour {cpus} cœur(s)', { ffmpeg: sys.ffmpeg, p2: gb(sys.ffmpeg_mb), p3: Number(sys.load).toLocaleString(locale()), cpus: sys.cpus })),
      swapHigh ? h('div.form-error.small', tr('Utilisation élevée du swap : les lectures peuvent saccader. Réduire le nombre de transcodages simultanés ou ajouter de la mémoire.')) : null) : null;
    const sec = sys && sys.security;
    const secLine = (ok, label, hint) => h('li', { class: ok ? 'sec-ok' : 'sec-warn' }, h('strong', ok ? '✓ ' : '! '), label, hint ? h('span.muted', ` · ${hint}`) : null);
    const security = sec ? h('div.panel.scan', h('strong', tr('Sécurité de l’accès')), h('ul.sec-list.small',
      secLine(sec.cookie_secure, sec.cookie_secure ? tr('Cookies réservés au HTTPS') : tr('Cookies envoyés aussi en HTTP'),
        sec.cookie_secure ? null : tr('SEANCE_COOKIE_SECURE=1 si Noxa est exposé sur Internet')),
      secLine(!sec.via_proxy || sec.trust_proxy, sec.trust_proxy ? tr('Adresse des visiteurs lue depuis le proxy') : (sec.via_proxy ? tr('Proxy détecté mais non déclaré') : tr('Accès direct (sans proxy)')),
        sec.via_proxy && !sec.trust_proxy ? tr('SEANCE_TRUST_PROXY=1 pour distinguer les visiteurs') : null),
      secLine(sec.https || !sec.via_proxy, sec.https ? tr('Connexion chiffrée (HTTPS)') : (sec.via_proxy ? tr('Proxy sans HTTPS') : tr('Connexion locale en HTTP'))),
      secLine(sec.admins_2fa === sec.admins, tr('Double authentification : {admins_2fa} administrateur(s) sur {admins}', { admins_2fa: sec.admins_2fa, admins: sec.admins }),
        sec.admins_2fa === sec.admins ? null : tr('à activer dans Paramètres → Profil par chaque administrateur')),
      h('li.muted', tr('Adresse vue par le serveur pour cette session : {client_ip}', { client_ip: sec.client_ip })))) : null;
    clear(body, versionCard, h('section.panel', h('h2', tr('Alertes')), alertsBox), server, security, h('h3.sub', tr('Lectures en cours')), list.length ? h('div.user-list', list.map((s) => h('div.user-row',
      h('div.grow', h('strong', s.title), h('div.muted.small',
        tr(s.state === 'paused' ? '{user} · en pause à {time} · {mode}' : '{user} · en lecture à {time} · {mode}', { user: s.user, time: fmtTime(s.position_ms), mode: MODES[s.mode] || s.mode }),
        s.hw ? tr(' · carte graphique ({p1})', { p1: s.hw === 'nvenc' ? 'NVIDIA' : 'VAAPI' }) : '',
        s.source ? tr(' · source {source}', { source: s.source }) : '', s.rss_mb ? tr(' · ffmpeg {rss_mb} Mo', { rss_mb: s.rss_mb }) : '',
        s.peer ? tr(' · relayée depuis {peer}', { peer: s.peer }) : '',
        s.reasons && s.reasons.length ? ` (${s.reasons.join(', ')})` : '',
        s.speed ? h('span', { class: s.speed < 1.05 && s.mode === 'transcode' ? 'tag bad' : 'tag ok' }, tr('vitesse ×{speed}', { speed: s.speed })) : null),
        s.warnings && s.warnings.length ? h('details.small', h('summary', tr('Messages de ffmpeg')), h('pre.log', s.warnings.join('\n'))) : null),
      h('button.btn.small.danger', { onclick: async () => { await del(`/api/admin/streams/${s.id}`); load(); } }, tr('Arrêter')))))
      : h('div.empty', tr('Aucune lecture en cours.')));
  };
  await load();
  const t = setInterval(load, 10000);
  ctx.setCleanup(() => clearInterval(t));
}
