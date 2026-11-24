// Admin page: updates.
import { adminTarget, get, post, put } from '../../api.js';
import { refreshMe, state } from '../../app.js';
import { locale, tr } from '../../i18n.js';
import { clear, confirmDialog, field, h, select, toast } from '../../ui.js';
import { followUpdate } from '../../updater.js';

// --------------------------------------------------------------- mise à jour
// La vérification interroge GitHub (résultat gardé 30 min côté serveur) ; le bouton force une nouvelle vérification.
export async function updatePage(body) {
  // serveur lié choisi dans le sélecteur : sa version, pas celle de ce serveur
  const version = adminTarget() ? (await get('/api/admin/update')).current : state.status.version;
  const box = h('div');
  const checkBtn = h('button.btn', { onclick: () => load(true) }, tr('Rechercher une mise à jour'));
  const checked = h('span.muted.small');
  clear(body, h('section.panel#update', h('h2', `Noxa ${version}`), h('p.muted.small', tr('Version installée sur ce serveur.')), box,
    h('div.actions-row.update-check', checkBtn, checked)));
  const load = async (refresh = false) => {
    checkBtn.disabled = true;
    checkBtn.textContent = tr('Recherche en cours…');
    if (!box.childElementCount) box.replaceChildren(h('p.muted.small', tr('Recherche d’une nouvelle version…')));
    try {
      const u = await get(`/api/admin/update${refresh ? '?refresh=1' : ''}`);
      paint(u);
      checked.textContent = tr('Dernière vérification : {p1}', { p1: new Date().toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' }) });
      if (refresh) {
        toast(u.available ? tr('Noxa {latest} est disponible', { latest: u.latest }) : (u.check_error ? u.check_error : tr('Aucune mise à jour disponible')),
          u.check_error ? 'bad' : 'ok');
        await refreshMe().catch(() => {}); // indicateur de la barre du haut
      }
    } catch (e) {
      box.replaceChildren(h('p.form-error.small', e.message));
    } finally {
      checkBtn.disabled = false;
      checkBtn.textContent = tr('Rechercher une mise à jour');
    }
  };
  const paint = (u) => {
    const parts = [];
    if (u.channels && u.can_update) {
      const channel = select(Object.entries(u.channels).map(([k, label]) => [k, `${label} (${k})`]), u.branch || 'main');
      channel.addEventListener('change', async () => {
        const to = channel.value;
        const msg = to === 'main'
          ? tr('Le serveur suivra les versions stables. La version stable actuelle pourra être plus ancienne que celle installée : la réinstaller ramène le serveur sur le canal stable.')
          : tr('Le serveur suivra les versions de test, publiées avant validation. Elles peuvent contenir des défauts.');
        if (!await confirmDialog(tr('Changer de canal ?'), msg, tr('Changer de canal'), false)) { channel.value = u.branch || 'main'; return; }
        try { await put('/api/admin/update/channel', { branch: to }); toast(tr('Canal enregistré'), 'ok'); load(true); }
        catch (e) { toast(e.message, 'bad'); channel.value = u.branch || 'main'; }
      });
      parts.push(field(tr('Canal de mise à jour'), channel, tr('Stable : versions validées. Test : nouveautés en cours de validation.')));
    }
    if (u.check_error) parts.push(h('div.status-box.bad', tr('Vérification impossible : {check_error}', { check_error: u.check_error })));
    else if (u.available) parts.push(h('div.status-box.ok', tr('Nouvelle version disponible : Noxa {latest}', { latest: u.latest })));
    else if (u.latest && u.latest !== version) parts.push(h('div.status-box', tr('Version publiée sur ce canal : Noxa {latest} (source : {source}).', { latest: u.latest, source: u.source })));
    else parts.push(h('div.status-box.ok', tr('Noxa est à jour (source : {source}).', { source: u.source })));
    if (u.available && u.changes && u.changes.length) {
      parts.push(h('details', h('summary.small', tr('Derniers changements')),
        h('ul.small', u.changes.map((c) => h('li', c.title)))));
    }
    if (u.running) {
      parts.push(h('p.small', tr('Mise à jour en cours : {p1} ({p2} %).', { p1: u.step || tr('installation'), p2: u.percent || 0 })),
        h('button.btn.primary', { onclick: () => followUpdate(version) }, tr('Afficher la progression')));
    }
    if (!u.can_update) {
      parts.push(h('p.form-error.small', u.update_error || tr('Mise à jour automatique indisponible sur cette installation.')),
        h('p.muted.small', tr('Mise à jour manuelle : relancer la commande d’installation sur le serveur (voir le README).')));
    } else if (!u.running) {
      parts.push(h('button.btn', { class: u.available ? 'btn primary' : 'btn', onclick: () => runUpdate(version) },
        u.latest && u.latest !== version ? tr('Installer Noxa {latest}', { latest: u.latest }) : tr('Réinstaller la version actuelle')));
    }
    box.replaceChildren(...parts);
  };
  load();
}

async function runUpdate(version) {
  if (!await confirmDialog(tr('Mettre à jour Noxa ?'), tr('Le serveur télécharge la nouvelle version puis redémarre (une à deux minutes). Les lectures en cours seront interrompues.'), tr('Mettre à jour'), false)) return;
  try { await post('/api/admin/update'); } catch (e) { toast(e.message, 'bad'); return; }
  const remote = adminTarget();
  if (remote) { toast(tr('Mise à jour lancée sur « {name} » : il redémarre dans une à deux minutes', { name: remote.name }), 'ok'); return; }
  followUpdate(version);
}
