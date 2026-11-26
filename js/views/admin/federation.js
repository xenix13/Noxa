// Admin page: linked servers.
import { adminTarget, del, get, post, put } from '../../api.js';
import { refreshMe } from '../../app.js';
import { plural, tr } from '../../i18n.js';
import { clear, confirmDialog, field, h, icon, modal, select, toast, toggle } from '../../ui.js';
import { since } from './util.js';

// ----------------------------------------------------------------- linked servers
async function inviteDialog(view) {
  const address = h('input', { value: view.address_manual || view.address, placeholder: view.address });
  const out = h('div');
  const err = h('p.form-error');
  const body = h('div.form',
    h('p.muted.small', tr('Créez l’invitation sur le serveur que l’autre peut joindre : en réseau local, n’importe lequel ; par Internet, celui dont le port {port} est ouvert (redirection sur la box ou reverse proxy TCP).', { port: view.port })),
    field(tr('Adresse de ce serveur, vue depuis l’autre'), address, tr('Adresse IP ou nom, suivi du port des serveurs liés (ex. 192.168.1.20:{port}).', { port: view.port })),
    out, err);
  return modal(tr('Inviter un serveur'), body, [
    { label: tr('Fermer'), value: null },
    { label: tr('Créer l’invitation'), primary: true, handler: async () => {
      err.textContent = '';
      try {
        const r = await post('/api/admin/federation/invite', { address: address.value.trim() });
        const text = h('textarea.invite-text', { readonly: true, rows: 4, onfocus: (e) => e.target.select() }, r.invite);
        clear(out,
          field(tr('Invitation (à coller sur l’autre serveur, dans Serveurs liés → Rejoindre)'), text,
            tr('Valable 15 minutes, une seule fois. Elle contient l’empreinte du certificat de ce serveur : l’autre vérifiera qu’il parle bien à celui-ci.')),
          h('div.actions-row', h('button.btn', { type: 'button', onclick: async () => {
            try { await navigator.clipboard.writeText(r.invite); toast(tr('Invitation copiée'), 'ok'); }
            catch { text.select(); toast(tr('Copie impossible : sélectionnez le texte'), 'bad'); }
          } }, tr('Copier'))));
      } catch (e) { err.textContent = e.message; }
      return false;
    } },
  ]);
}

async function joinDialog() {
  const invite = h('textarea', { rows: 4, placeholder: 'NEXIS1-…' });
  const address = h('input', { placeholder: tr('facultatif, ex. 192.168.1.30:8421') });
  const accounts = h('div');
  const err = h('p.form-error');
  let choices = null; // usernames present on both sides, once the invitation is checked
  invite.addEventListener('input', () => { choices = null; clear(accounts); });
  const body = h('div.form',
    field(tr('Invitation créée sur l’autre serveur'), invite),
    field(tr('Adresse de ce serveur, vue depuis l’autre'), address,
      tr('Laissez vide si l’autre ne peut pas joindre ce serveur (box sans port ouvert) : ce serveur lui ouvrira lui-même un tunnel chiffré.')),
    accounts, err);
  const showChoices = (preview) => {
    choices = preview.conflicts.map((c) => {
      const rename = h('input', { value: c.suggested, maxlength: 32, 'aria-label': tr('Nouvel identifiant ici') });
      const same = select([['split', tr('Personnes différentes')], ['merge', tr('Même personne : fusionner')]], 'split');
      const renameRow = h('div.small', tr('Le compte d’ici devient : '), rename);
      same.addEventListener('change', () => { renameRow.hidden = same.value === 'merge'; });
      return { c, same, rename, row: h('div.peer-lib.account-pick',
        h('div.grow', h('strong', c.username),
          h('div.muted.small', tr('Ici : {local} · sur « {server} » : {remote}', { local: c.display_name, server: preview.server, remote: c.remote_display_name || c.username })),
          renameRow), same) };
    });
    clear(accounts, choices.length ? h('div.status-box',
      h('p', tr('Ces identifiants existent sur les deux serveurs. S’il s’agit de la même personne, les comptes fusionnent avec le mot de passe et les réglages de « {server} » ; sinon, le compte d’ici est renommé et chacun garde le sien.', { server: preview.server })),
      h('div.peer-libs', choices.map((x) => x.row))) : null);
  };
  return modal(tr('Rejoindre un serveur'), body, [
    { label: tr('Annuler'), value: null },
    { label: tr('Lier les serveurs'), primary: true, handler: async () => {
      err.textContent = tr('Connexion à l’autre serveur…');
      try {
        if (!choices) {
          const preview = await post('/api/admin/federation/join/preview', { invite: invite.value.trim() });
          showChoices(preview);
          if (choices.length) { err.textContent = ''; return false; } // the administrator checks, then tries again
        }
        const picked = {};
        for (const x of choices) picked[x.c.username] = x.same.value === 'merge' ? { merge: true } : { rename: x.rename.value.trim() };
        return await post('/api/admin/federation/join', { invite: invite.value.trim(), address: address.value.trim(), accounts: picked });
      } catch (e) { err.textContent = e.message; return false; }
    } },
  ]);
}

const shortPrint = (fp) => (fp ? fp.slice(0, 32).toUpperCase().match(/.{4}/g).join(' ') : '—');

// Accept or not to be administered from a linked server (set on site: never from another server)
function remoteAdminPanel() {
  if (adminTarget()) return null;
  const box = h('section.panel', h('h2', tr('Administration depuis les serveurs liés')));
  get('/api/admin/servers').then((sv) => {
    const t = toggle(tr('Les administrateurs peuvent régler ce serveur depuis un serveur lié'), sv.remote_admin);
    t.querySelector('input').addEventListener('change', async (e) => {
      try { await put('/api/admin/servers', { remote_admin: e.target.checked }); toast(tr('Enregistré'), 'ok'); } catch (ex) { toast(ex.message, 'bad'); }
    });
    box.append(h('p.muted.small', tr('Un administrateur connecté à un autre serveur de la liste choisit ce serveur en haut des pages d’administration et règle tout comme s’il était connecté ici, avec son propre compte (le même sur tous les serveurs). Chaque action est notée dans le journal de ce serveur. Ce réglage ne se change que sur place.')), t);
  }).catch(() => {});
  return box;
}

export async function federationPage(body, ctx) {
  const render = (v) => {
    if (!ctx.isCurrent()) return;
    const name = h('input', { value: v.name, maxlength: 40 });
    const busy = h('input', { type: 'number', min: 0, max: 50, value: v.busy });
    const me = h('section.panel',
      h('h2', tr('Ce serveur')),
      h('div.grid2', field(tr('Nom affiché chez les serveurs liés'), name), h('div.field',
        h('span.field-label', tr('Empreinte du certificat')), h('code.small', shortPrint(v.fingerprint)),
        h('span.field-hint', v.listening ? tr('Port des serveurs liés : {port} (TLS)', { port: v.port }) : tr('Le port des serveurs liés s’ouvrira à la première invitation.')))),
      h('div.grid2', field(tr('Serveur occupé à partir de'), busy, tr('Lectures en cours ({n} en ce moment). Au-delà, un titre présent sur un serveur moins occupé est lu depuis celui-ci. 0 : toujours la meilleure copie.', { n: v.load })), h('div')),
      h('div.actions-row', h('button.btn', { onclick: async () => {
        try { render(await put('/api/admin/federation', { name: name.value.trim(), busy: Number(busy.value) })); toast(tr('Enregistré'), 'ok'); }
        catch (e) { toast(e.message, 'bad'); }
      } }, tr('Enregistrer'))));
    const state = (p) => (p.online === undefined || p.online === null ? h('span.tag', tr('En attente'))
      : p.online ? h('span.tag.ok', p.via === 'tunnel' ? tr('En ligne · par tunnel') : tr('En ligne · direct'))
        : h('span.tag.bad', tr('Injoignable')));
    const peers = v.peers.length ? h('div.user-list', v.peers.map((p) => h('div.user-row.peer-row',
      h('div.lib-icon', '🖧'),
      h('div.grow', h('strong', p.name), ' ', state(p),
        h('div.muted.small',
          p.address ? tr('Adresse {address}', { address: p.address }) : tr('Sans adresse : joint par le tunnel qu’il ouvre'),
          p.latency != null && p.online ? tr(' · {ms} ms', { ms: p.latency }) : '',
          p.version ? ` · Noxa ${p.version}` : '',
          p.online && p.load != null ? plural(p.load, ' · {n} lecture en cours', ' · {n} lectures en cours') : '',
          tr(' · vu {when}', { when: since(p.last_seen) })),
        p.online && p.shares && p.shares.length ? h('div.muted.small', tr('Prête : {list}', {
          list: p.shares.map((k) => (k === 'tmdb' ? tr('sa clé TMDB') : tr('son Seerr (demandes)'))).join(', ') })) : null,
        p.error && !p.online ? h('div.form-error.small', p.error) : null,
        p.libraries.length ? h('div.peer-libs', p.libraries.map((l) => {
          const options = [['', tr('Nouvelle bibliothèque')], ...v.local_libraries.filter((t) => t.kind === l.kind)
            .map((t) => [t.id, tr('Fusionner avec « {name} »', { name: t.name })])];
          const pick = select(options, l.merge_into || '', { 'aria-label': tr('Bibliothèque « {name} »', { name: l.name }),
            onchange: async () => {
              pick.disabled = true;
              try { render(await put(`/api/admin/federation/libraries/${l.id}`, { merge_into: pick.value || null })); await refreshMe(); toast(tr('Enregistré'), 'ok'); }
              catch (e) { toast(e.message, 'bad'); pick.disabled = false; }
            } });
          return h('div.peer-lib', h('div.grow', h('strong', l.name),
            h('div.muted.small', l.kind === 'show' ? plural(l.count, '{n} série', '{n} séries') : plural(l.count, '{n} film', '{n} films'),
              l.merge_into && l.merged ? tr(' · {n} en commun avec ce serveur', { n: l.merged }) : '')), pick);
        })) : null),
      h('div.peer-actions', h('button.btn.small', { onclick: async (e) => {
        e.currentTarget.disabled = true;
        try { render(await post(`/api/admin/federation/peers/${p.id}/sync`)); } catch (ex) { toast(ex.message, 'bad'); }
      } }, tr('Synchroniser')),
      h('button.btn.small', { onclick: async () => {
        const input = h('input', { value: p.address || '', placeholder: tr('vide : par tunnel uniquement') });
        const ok = await modal(tr('Adresse de « {name} »', { name: p.name }), h('div.form', field(tr('Adresse et port des serveurs liés'), input,
          tr('Comment ce serveur joint l’autre. Vide si l’autre n’est pas joignable : il passe alors par le tunnel qu’il ouvre.'))),
        [{ label: tr('Annuler'), value: null }, { label: tr('Enregistrer'), primary: true, value: true }]);
        if (!ok) return;
        try { render(await put(`/api/admin/federation/peers/${p.id}`, { address: input.value.trim() })); } catch (e) { toast(e.message, 'bad'); }
      } }, tr('Adresse')),
      h('button.icon-btn', { title: tr('Délier ce serveur'), onclick: async () => {
        if (!await confirmDialog(tr('Délier « {name} » ?', { name: p.name }),
          tr('Ses bibliothèques disparaissent de ce serveur (et les vôtres du sien). Les comptes restent sur les deux serveurs, mais ne sont plus synchronisés. Aucun fichier n’est touché.'), tr('Délier'))) return;
        try { render(await del(`/api/admin/federation/peers/${p.id}`)); toast(tr('Serveur délié'), 'ok'); } catch (e) { toast(e.message, 'bad'); }
      } }, icon('trash'))))))
      : h('div.empty', tr('Aucun serveur lié.'));
    const notices = v.notices && v.notices.length ? h('section.panel.warn', h('h2', tr('Comptes renommés')),
      h('ul.small', v.notices.map((n) => h('li', tr('« {username} » a été créé sur deux serveurs avant leur synchronisation : ce sont deux comptes, l’un s’appelle maintenant « {renamed} ».', n)))),
      h('div.actions-row', h('button.btn.small', { onclick: async () => { render(await del('/api/admin/federation/notices')); } }, tr('Compris')))) : null;
    clear(body, notices,
      h('div.toolbar', h('div.actions-row',
          h('button.btn', { onclick: async () => { const r = await joinDialog(); if (r) { toast(tr('Serveurs liés'), 'ok'); await refreshMe(); render(r); } } }, tr('Rejoindre un serveur')),
          h('button.btn.primary', { onclick: async () => { await inviteDialog(v); load(); } }, tr('+ Inviter un serveur')))),
      peers, me, remoteAdminPanel(),
      h('section.panel', h('h2', tr('Comment ça marche')), h('ul.small.muted',
        h('li', tr('Les deux serveurs se reconnaissent par leur certificat, échangé à l’invitation, et chiffrent tout ce qu’ils s’envoient (TLS). Pas besoin de VPN ni de nom de domaine, et cela marche aussi en HTTP sur le réseau local.')),
        h('li', tr('Un seul des deux doit pouvoir joindre l’autre : celui qui ne l’est pas ouvre lui-même un tunnel vers lui. Rien à ouvrir sur sa box.')),
        h('li', tr('Les comptes, mots de passe, profils et accès aux bibliothèques sont les mêmes sur les deux serveurs ; une modification faite sur l’un arrive sur l’autre en moins d’une minute.')),
        h('li', tr('Un identifiant présent sur les deux serveurs au moment de les lier n’est fusionné que si vous indiquez qu’il s’agit de la même personne ; sinon, l’un des comptes est renommé.')),
        h('li', tr('Une bibliothèque reçue s’ajoute comme une nouvelle, ou se fusionne avec l’une des vôtres : un titre présent sur les deux serveurs n’apparaît qu’une fois, et les saisons et épisodes de chacun sont réunis.')),
        h('li', tr('À la lecture d’un titre présent sur les deux serveurs, la copie est choisie toute seule : celle qui a la langue voulue si une seule l’a, puis celle d’un serveur qui n’est pas occupé, puis la meilleure qualité. Si un serveur ne répond pas, l’autre prend le relais, et les titres qu’il est seul à avoir sont masqués jusqu’à son retour.')),
        h('li', tr('Un serveur sans clé TMDB ou sans Seerr se sert de ceux d’un serveur lié : métadonnées, onglet Découvrir et demandes.')),
        h('li', tr('« Regarder ensemble » marche d’un serveur à l’autre : les groupes créés sur l’un apparaissent sur l’autre, et chacun regarde depuis le serveur auquel il est connecté.')),
        h('li', tr('Une lecture d’un titre de l’autre serveur passe par celui-ci : c’est l’autre serveur qui lit le fichier et le convertit si besoin. Le débit montant de sa connexion limite la qualité à distance.')),
        h('li', tr('À réserver à vos propres serveurs : partager des films avec un autre foyer sort du cadre de la copie privée.')))));
  };
  const load = async () => { const v = await get('/api/admin/federation'); render(v); };
  await load();
  const t = setInterval(() => { if (!document.querySelector('.modal-overlay')) load(); }, 15000);
  ctx.setCleanup(() => clearInterval(t));
}
