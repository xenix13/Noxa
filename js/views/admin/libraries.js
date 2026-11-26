// Admin pages: libraries and network shares (SMB).
import { del, get, post, put } from '../../api.js';
import { refreshMe } from '../../app.js';
import { plural, tr } from '../../i18n.js';
import { clear, confirmDialog, field, h, icon, modal, select, toast } from '../../ui.js';
import { since } from './util.js';

// -------------------------------------------------------------- libraries
async function pickFolder(start) {
  let current = start || '/';
  const list = h('div.folder-list');
  const pathEl = h('div.folder-path');
  const load = async (p) => {
    try {
      const r = await get(`/api/admin/browse?path=${encodeURIComponent(p)}`);
      current = r.path;
      pathEl.textContent = r.path;
      list.replaceChildren(
        r.parent ? h('button.folder', { onclick: () => load(r.parent) }, tr('⬑ Dossier parent')) : null,
        ...r.dirs.map((d) => h('button.folder', { onclick: () => load(`${r.path === '/' ? '' : r.path}/${d}`) }, `📁 ${d}`)),
        r.dirs.length ? null : h('p.muted.small', tr('Aucun sous-dossier.')));
    } catch (e) { toast(e.message, 'bad'); }
  };
  await load(current);
  return modal(tr('Choisir un dossier'), h('div', pathEl, list), [
    { label: tr('Annuler'), value: null },
    { label: tr('Choisir ce dossier'), primary: true, handler: () => current },
  ]);
}

async function libraryForm(lib) {
  const isNew = !lib || !lib.id;
  lib = lib || { name: '', kind: 'movie', paths: [] };
  const name = h('input', { value: lib.name, maxlength: 40, placeholder: tr('Films, Séries, Animes…') });
  const kind = select([['movie', tr('Films')], ['show', tr('Séries (et animes)')], ['photo', tr('Photos')], ['audiobook', tr('Livres audio')]], lib.kind);
  const paths = h('div.paths');
  const addPath = (value = '') => {
    const input = h('input', { value, placeholder: '/mnt/films' });
    const rowEl = h('div.path-row', input,
      h('button.btn.small', { type: 'button', onclick: async () => {
        const p = await pickFolder(input.value || '/mnt');
        if (p) input.value = p;
      } }, tr('Parcourir…')),
      h('button.icon-btn', { type: 'button', title: tr('Retirer'), onclick: () => rowEl.remove() }, icon('close')));
    paths.append(rowEl);
  };
  (lib.paths.length ? lib.paths : ['']).forEach(addPath);
  const err = h('p.form-error');
  const body = h('div.form',
    h('div.grid2', field(tr('Nom'), name), field(tr('Type de contenu'), kind)),
    field(tr('Dossiers'), paths, tr('Chemins sur le serveur Noxa (ex. /mnt/films). Chaque film ou série dans son propre sous-dossier ; pour des photos, chaque sous-dossier devient un album.')),
    h('button.btn.small.ghost', { type: 'button', onclick: () => addPath() }, tr('+ Ajouter un dossier')),
    isNew ? null : h('p.muted.small', tr('Changer le type vide la bibliothèque avant de la rescanner.')), err);
  return modal(isNew ? tr('Nouvelle bibliothèque') : tr('Modifier « {name} »', { name: lib.name }), body, [
    { label: tr('Annuler'), value: null },
    { label: isNew ? tr('Créer et scanner') : tr('Enregistrer et rescanner'), primary: true, handler: async () => {
      const payload = { name: name.value.trim(), kind: kind.value,
        paths: [...paths.querySelectorAll('input')].map((i) => i.value.trim()).filter(Boolean) };
      try {
        if (isNew) await post('/api/admin/libraries', payload);
        else await put(`/api/admin/libraries/${lib.id}`, payload);
        return true;
      } catch (e) { err.textContent = e.message; return false; }
    } },
  ]);
}

function scanBox(scan) {
  if (!scan.running && !scan.finished_at) return null;
  const pct = scan.total ? Math.round((scan.done / scan.total) * 100) : 0;
  return h('section.panel.scan',
    h('div.scan-head', h('strong', scan.running ? tr('Scan en cours : {phase}', { phase: scan.phase }) : tr('Dernier scan terminé {p1}', { p1: since(scan.finished_at) })),
      scan.running && scan.library ? h('span.muted', ` · ${scan.library}`) : null),
    scan.running ? h('div.bar', h('div.bar-fill', { style: { width: `${pct}%` } })) : null,
    scan.running && scan.total ? h('div.muted.small', `${scan.done} / ${scan.total}`) : null,
    !scan.running ? h('div.muted.small', tr('{added} ajout(s), {removed} suppression(s)', { added: scan.added, removed: scan.removed })) : null,
    scan.unrecognized.length ? h('details', h('summary', tr('{length} fichier(s) non reconnu(s) comme épisode', { length: scan.unrecognized.length })),
      h('ul.small', scan.unrecognized.map((f) => h('li', f))),
      h('p.muted.small', tr('Pour qu’ils soient reconnus, les noms doivent contenir S01E02 ou « Nom - 02 ».'))) : null,
    scan.errors.length ? h('details', { open: true }, h('summary', tr('{n} erreur(s)', { n: scan.errors.length })),
      h('ul.small', scan.errors.map((f) => h('li', f)))) : null);
}

export async function librariesTab(body, ctx) {
  const data = await get('/api/admin/libraries');
  if (!ctx.isCurrent()) return;
  const reload = () => librariesTab(body, ctx);
  const startScan = async (payload) => {
    try { await post('/api/admin/scan', payload); toast(tr('Scan lancé'), 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); }
  };
  const c = (l, k) => l.counts[k] || 0;
  clear(body,
    h('div.toolbar', h('p.muted', tr('Chaque bibliothèque apparaît comme un onglet. Les animes peuvent avoir leur propre bibliothèque de type « Séries ».')),
      h('div.actions-row',
        data.libraries.length ? h('button.btn', { onclick: () => startScan({}) }, tr('Tout rescanner')) : null,
        h('button.btn.primary', { onclick: async () => { if (await libraryForm(null)) { await refreshMe(); toast(tr('Bibliothèque créée, scan en cours'), 'ok'); reload(); } } }, tr('+ Ajouter')))),
    scanBox(data.scan),
    data.libraries.length ? h('div.user-list', data.libraries.map((l) => h('div.user-row',
      h('div.lib-icon', l.kind === 'movie' ? '🎬' : l.kind === 'photo' ? '🖼️' : l.kind === 'audiobook' ? '🎧' : '📺'),
      h('div.grow', h('strong', l.name),
        h('div.muted.small', l.kind === 'movie' ? plural(c(l, 'movie'), '{n} film', '{n} films')
          : l.kind === 'photo' ? tr('{p1} photos · {p2} albums', { p1: c(l, 'photo'), p2: c(l, 'album') })
            : l.kind === 'audiobook' ? plural(c(l, 'book'), '{n} livre', '{n} livres')
              : tr('{p1} séries · {p2} épisodes', { p1: c(l, 'show'), p2: c(l, 'episode') }),
          l.unmatched ? h('span', tr(' · {unmatched} non identifié(s)', { unmatched: l.unmatched })) : null,
          tr(' · scanné {p1}', { p1: since(l.scanned_at) })),
        h('div.small', l.paths.map((p, i) => h('code', { class: l.paths_ok[i] ? '' : 'bad-path', title: l.paths_ok[i] ? '' : tr('Dossier inaccessible') }, p))),
        (l.paths_writable || []).some(Boolean) ? h('div.small.warn-text', tr('Noxa peut écrire dans ce dossier : par précaution, montez-le en lecture seule (voir l’audit de sécurité).')) : null),
      h('button.btn.small', { onclick: () => startScan({ library: l.id }) }, tr('Scanner')),
      h('button.btn.small', { onclick: async () => { if (await libraryForm(l)) { await refreshMe(); reload(); } } }, tr('Modifier')),
      h('button.icon-btn', { title: tr('Supprimer'), onclick: async () => {
        if (!await confirmDialog(tr('Supprimer cette bibliothèque ?'), tr('« {name} » sera retirée de Noxa. Les fichiers ne sont pas modifiés.', { name: l.name }), tr('Supprimer'))) return;
        try { await del(`/api/admin/libraries/${l.id}`); await refreshMe(); reload(); } catch (e) { toast(e.message, 'bad'); }
      } }, icon('trash')))))
      : h('div.empty', h('p', tr('Aucune bibliothèque.')), h('p.muted.small', tr('Exemple : « Films » → /mnt/films, « Séries » → /mnt/series, « Animes » → /mnt/animes.'))));
  if (data.scan.running) {
    const t = setTimeout(async () => { if (ctx.isCurrent()) { await refreshMe().catch(() => {}); reload(); } }, 1500);
    ctx.setCleanup(() => clearTimeout(t));
  }
}

// ----------------------------------------------------------- partages SMB
export const SMB_VERSIONS = [['auto', tr('Automatique')], ['3.1.1', 'SMB 3.1.1'], ['3.0', 'SMB 3.0'], ['2.1', 'SMB 2.1'],
  ['2.0', 'SMB 2.0'], ['1.0', tr('SMB 1 (ancien NAS)')]];

const slug = (t) => t.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
  .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32) || 'partage';

async function shareForm(existing) {
  const isNew = !existing;
  const e = existing || { host: '', share: '', subdir: '', username: '', version: 'auto', name: '' };
  const host = h('input', { value: e.host, placeholder: '192.168.1.5' });
  const user = h('input', { value: e.username, autocomplete: 'off' });
  const pass = h('input', { type: 'password', autocomplete: 'new-password',
    placeholder: isNew ? '' : tr('Laisser vide pour garder le mot de passe actuel') });
  const domain = h('input', { placeholder: tr('WORKGROUP (facultatif)') });
  const version = select(SMB_VERSIONS, e.version || 'auto');
  const share = h('input', { value: e.share, placeholder: tr('Films') });
  const subdir = h('input', { value: e.subdir || '', placeholder: tr('facultatif, ex. Films/HD') });
  const name = h('input', { value: e.name, disabled: !isNew, placeholder: tr('films') });
  let nameTouched = !isNew;
  name.addEventListener('input', () => { nameTouched = true; });
  share.addEventListener('input', () => { if (!nameTouched) name.value = slug(share.value); });
  const found = h('div.share-list');
  const err = h('p.form-error');
  const creds = () => ({ host: host.value.trim(), username: user.value, password: pass.value, domain: domain.value.trim(),
    version: version.value, reuse: isNew ? undefined : e.name });
  const discover = h('button.btn', { type: 'button', onclick: async () => {
    err.textContent = '';
    discover.disabled = true;
    found.replaceChildren(h('p.muted.small', tr('Connexion au NAS…')));
    try {
      const r = await post('/api/admin/shares/discover', creds());
      found.replaceChildren(...(r.shares.length ? r.shares.map((x) => h('button.share-pick', { type: 'button',
        class: x.name === share.value ? 'share-pick on' : 'share-pick',
        onclick: (ev) => {
          share.value = x.name;
          if (!nameTouched) name.value = slug(x.name);
          found.querySelectorAll('.share-pick').forEach((b) => b.classList.remove('on'));
          ev.currentTarget.classList.add('on');
        } }, '🗂 ', h('strong', x.name), x.comment ? h('span.muted.small', ` ${x.comment}`) : null))
        : [h('p.muted.small', tr('Aucun partage visible avec ce compte.'))]));
    } catch (ex) { found.replaceChildren(); err.textContent = ex.message; } finally { discover.disabled = false; }
  } }, tr('Voir les partages du NAS'));
  const body = h('div.form',
    h('div.grid2', field(tr('Adresse du NAS'), host), field(tr('Identifiant'), user)),
    field(tr('Mot de passe'), pass),
    h('div.actions-row', discover), found,
    h('div.grid2', field(tr('Partage'), share), field(tr('Nom local'), name, tr('Monté dans /mnt/seance/<nom>'))),
    h('details.advanced', h('summary', tr('Options avancées')),
      h('div.grid2', field(tr('Sous-dossier du partage'), subdir), field(tr('Version SMB'), version)), field(tr('Domaine'), domain)),
    h('p.muted.small', tr('Le partage est monté en lecture seule : Noxa ne peut ni modifier ni supprimer les fichiers. Le mot de passe est stocké par le système (lisible par root uniquement), jamais dans la base de Noxa.')),
    err);
  return modal(isNew ? tr('Ajouter un partage réseau (SMB)') : tr('Modifier « {name} »', { name: e.name }), body, [
    { label: tr('Annuler'), value: null },
    { label: tr('Monter le partage'), primary: true, handler: async () => {
      err.textContent = tr('Montage en cours…');
      try {
        const r = await post('/api/admin/shares', { name: name.value.trim(), host: host.value.trim(), share: share.value.trim(),
          subdir: subdir.value.trim(), username: user.value, password: pass.value, domain: domain.value.trim(),
          version: version.value });
        return r.path;
      } catch (ex) { err.textContent = ex.message; return false; }
    } },
  ]);
}

export async function sharesTab(body, ctx) {
  const data = await get('/api/admin/shares');
  if (!ctx.isCurrent()) return;
  const reload = () => sharesTab(body, ctx);
  const offerLibrary = async (path, label) => {
    const ok = await libraryForm({ name: label.charAt(0).toUpperCase() + label.slice(1), kind: 'movie', paths: [path] });
    if (ok) { await refreshMe(); toast(tr('Bibliothèque créée, scan en cours'), 'ok'); }
    reload();
  };
  if (!data.available) {
    clear(body, h('section.panel', h('h2', tr('Partages réseau')), h('div.status-box.bad', data.error),
      h('p.muted.small', tr('Le service seance-mountd est absent ou arrêté. Relancer l’installation le réinstalle ; son état s’affiche avec systemctl status seance-mountd.'))));
    return;
  }
  const env = data.env;
  const warning = env.unprivileged ? h('section.panel.warn',
    h('h2', tr('Ce conteneur ne peut pas monter de partages')),
    h('p', tr('Linux interdit le montage SMB dans un conteneur LXC non privilégié. Les partages du NAS peuvent être listés, mais leur montage échouera. Deux solutions :')),
    h('ol',
      h('li', h('strong', tr('Conteneur privilégié')), tr(' (gestion des partages depuis Noxa) : sauvegarder le conteneur, le restaurer en mode privilégié (Proxmox : Sauvegarde → Restaurer, décocher « Non privilégié »), puis activer l’option '), h('code', 'pct set <ID> -features mount=cifs,nesting=1'), '.'),
      h('li', h('strong', tr('Garder le conteneur non privilégié')), tr(' : monter les partages sur l’hôte Proxmox et les transmettre au conteneur (procédure dans le README). Ils apparaissent alors dans l’explorateur de dossiers des bibliothèques.')))) : null;
  const tools = env.tools && (!env.tools['mount.cifs'] || !env.tools.smbclient)
    ? h('div.status-box.bad', tr('cifs-utils ou smbclient est manquant. Relancer l’installation de Noxa les installe.')) : null;
  clear(body,
    h('div.toolbar', h('p.muted', tr('Dossiers partagés d’un NAS (SMB/CIFS), montés depuis Noxa pour y créer des bibliothèques.')),
      h('button.btn.primary', { onclick: async () => {
        const path = await shareForm(null);
        if (path) {
          toast(tr('Partage monté'), 'ok');
          if (await confirmDialog(tr('Partage monté'), tr('Le partage est disponible dans {path}. Créer une bibliothèque avec ce dossier ?', { path }), tr('Créer une bibliothèque'), false)) {
            await offerLibrary(path, path.split('/').pop());
            return;
          }
          reload();
        }
      } }, tr('+ Ajouter un partage'))),
    warning, tools,
    data.shares.length ? h('div.user-list', data.shares.map((sh) => h('div.user-row',
      h('div.lib-icon', '🗄️'),
      h('div.grow', h('strong', sh.name), h('span.muted', ` //${sh.host}/${sh.share}${sh.subdir ? `/${sh.subdir}` : ''}`),
        h('div.small', h('code', sh.path),
          sh.mounted ? h('span.tag.ok', tr('Monté')) : h('span.tag.bad', tr('Non monté')),
          sh.username ? h('span.muted', tr(' · compte {username}', { username: sh.username })) : null,
          sh.libraries.length ? h('span.muted', tr(' · utilisé par {p1}', { p1: sh.libraries.join(', ') })) : null),
        sh.error ? h('div.form-error.small', sh.error) : null),
      sh.mounted && !sh.libraries.length ? h('button.btn.small', { onclick: () => offerLibrary(sh.path, sh.share) }, tr('Créer une bibliothèque')) : null,
      sh.mounted ? null : h('button.btn.small', { onclick: async () => {
        try { await post(`/api/admin/shares/${sh.name}/remount`); toast(tr('Partage remonté'), 'ok'); } catch (e) { toast(e.message, 'bad'); }
        reload();
      } }, tr('Remonter')),
      h('button.btn.small', { onclick: async () => { if (await shareForm(sh)) { toast(tr('Partage mis à jour'), 'ok'); reload(); } } }, tr('Modifier')),
      h('button.icon-btn', { title: tr('Démonter et retirer'), onclick: async () => {
        const used = sh.libraries.length ? tr(' Les bibliothèques {p1} ne trouveront plus leurs fichiers (leur contenu est conservé tant qu’elles ne sont pas rescannées avec le partage monté).', { p1: sh.libraries.join(', ') }) : '';
        if (!await confirmDialog(tr('Retirer ce partage ?'), tr('Le partage sera démonté et retiré de Noxa. Les fichiers du NAS ne sont pas modifiés.{used}', { used }), tr('Retirer'))) return;
        try { await del(`/api/admin/shares/${sh.name}`); toast(tr('Partage retiré'), 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); }
      } }, icon('trash')))))
      : h('div.empty', tr('Aucun partage réseau géré par Noxa.')));
}
