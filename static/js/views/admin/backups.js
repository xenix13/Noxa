// Admin page: backups and restore.
import { adminUrl, del, get, post, put } from '../../api.js';
import { locale, tr } from '../../i18n.js';
import { clear, confirmDialog, field, h, icon, modal, select, spinner, toast, toggle } from '../../ui.js';

// --------------------------------------------------------------- sauvegardes
export async function backupsPage(body, ctx) {
  const size = (b) => (b >= 1e9 ? tr('{n} Go', { n: (b / 1e9).toLocaleString(locale(), { maximumFractionDigits: 1 }) })
    : b >= 1e6 ? tr('{n} Mo', { n: (b / 1e6).toLocaleString(locale(), { maximumFractionDigits: 1 }) })
      : tr('{n} Ko', { n: Math.max(1, Math.round(b / 1e3)) }));
  const when = (ts) => new Date(ts * 1000).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' });
  const reload = async () => render(await get('/api/admin/backups'));
  // after a restore: Noxa restarts, the page reloads once it is back
  const restarted = (r) => {
    if (!r.restarting) { toast(tr('Restauration prête : redémarrez Noxa pour l’appliquer'), 'ok'); return; }
    clear(body, h('section.panel', h('h2', tr('Restauration en cours')), h('p.muted', tr('Noxa redémarre avec la sauvegarde. La page se rechargera toute seule.'))));
    const started = Date.now();
    const wait = async () => {
      await new Promise((res) => setTimeout(res, 2500));
      try { await get('/api/status'); location.reload(); } catch { if (Date.now() - started < 180000) wait(); }
    };
    wait();
  };
  const askPassphrase = async (title) => {
    const input = h('input', { type: 'password', autocomplete: 'off' });
    const ok = await modal(title, h('div.form', h('p.muted.small', tr('Cette archive est chiffrée : indiquez la phrase secrète choisie au moment de la sauvegarde (laisser vide pour essayer celle enregistrée ici).')),
      field(tr('Phrase secrète'), input)), [{ label: tr('Annuler'), value: false }, { label: tr('Restaurer'), primary: true, value: true }]);
    return ok ? input.value : null;
  };
  const confirmRestore = (b) => confirmDialog(tr('Restaurer cette sauvegarde ?'),
    `${b && b.server && !b.mine ? tr('Elle vient du serveur « {name} » : ce serveur prendra son identité (comptes, réglages, liaisons). À faire seulement pour remplacer « {name} », par exemple sur une nouvelle machine.', { name: b.server }) + ' ' : ''}${
      tr('Comptes, historique, réglages et bibliothèques reviennent à leur état de la sauvegarde. Ce qui a changé depuis est perdu (l’état actuel reste gardé dans le dossier de données, sous « before-restore »). Noxa redémarre.')}`, tr('Restaurer'));

  const render = (v) => {
    if (!ctx.isCurrent()) return;
    const sched = v.schedule || { mode: 'off' };
    const mode = select([['off', tr('Désactivée')], ['daily', tr('Chaque jour')], ['weekly', tr('Chaque semaine (dimanche)')]],
      sched.mode === 'interval' ? 'daily' : sched.mode);
    const time = h('input', { type: 'time', value: sched.time || '04:30' });
    const keep = h('input', { type: 'number', min: 1, max: 90, value: v.keep });
    const dir = h('input', { value: v.custom_dir ? v.dir : '', placeholder: v.default_dir, spellcheck: false });
    const saveConf = async (extra = {}) => {
      const schedule = mode.value === 'off' ? { mode: 'off' } : { mode: mode.value, time: time.value || '04:30', ...(mode.value === 'weekly' ? { weekday: 6 } : {}) };
      try { render(await put('/api/admin/backups/settings', { schedule, keep: Number(keep.value), dir: dir.value.trim(), ...extra })); toast(tr('Enregistré'), 'ok'); } catch (e) { toast(e.message, 'bad'); }
    };
    const setOnly = async (patch, msg = tr('Enregistré')) => {
      try { render(await put('/api/admin/backups/settings', patch)); toast(msg, 'ok'); } catch (e) { toast(e.message, 'bad'); }
    };
    const nowBtn = h('button.btn.primary', { onclick: async () => {
      nowBtn.disabled = true;
      nowBtn.textContent = tr('Sauvegarde en cours…');
      try { render(await post('/api/admin/backups')); toast(tr('Sauvegarde terminée : copies en cours vers les autres destinations'), 'ok'); } catch (e) { toast(e.message, 'bad'); nowBtn.disabled = false; }
    } }, tr('Sauvegarder maintenant'));
    // upload an archive (to keep it), or restore directly from a file on the device
    const pick = (restore) => {
      const input = h('input', { type: 'file', accept: '.gz,.enc,application/gzip,application/octet-stream', hidden: true, onchange: async () => {
        const f = input.files[0];
        if (!f) return;
        let pass = '';
        if (restore) {
          if (!await confirmRestore(null)) return;
          if (/\.enc$/i.test(f.name)) { pass = await askPassphrase(tr('Archive chiffrée')); if (pass === null) return; }
        }
        toast(tr('Envoi de la sauvegarde…'));
        try {
          const headers = { 'X-Noxa': '1', 'Content-Type': 'application/octet-stream' };
          if (pass) headers['X-Backup-Passphrase'] = pass;
          const r = await fetch(adminUrl(restore ? '/api/admin/backups/restore-file' : '/api/admin/backups/upload'), { method: 'POST', body: f, credentials: 'same-origin', headers });
          const j = await r.json();
          if (!r.ok) throw new Error(j.error || r.statusText);
          if (restore) { restarted(j); return; }
          render(j);
          toast(tr('Sauvegarde ajoutée : vous pouvez la restaurer'), 'ok');
        } catch (e) { toast(e.message, 'bad'); }
      } });
      document.body.append(input);
      input.click();
      setTimeout(() => input.remove(), 60000);
    };
    const restorePath = async () => {
      const path = h('input', { placeholder: '/backups/noxa-salon_20261107-043000-auto.tar.gz', spellcheck: false });
      const pass = h('input', { type: 'password', autocomplete: 'off', placeholder: tr('Seulement pour une archive chiffrée (.enc)') });
      const ok = await modal(tr('Restaurer depuis un fichier du serveur'), h('div.form',
        h('p.muted.small', tr('Un fichier déjà présent sur le serveur : disque externe, NAS monté, dossier partagé avec le conteneur…')),
        field(tr('Chemin du fichier'), path), field(tr('Phrase secrète'), pass)),
      [{ label: tr('Annuler'), value: false }, { label: tr('Restaurer'), primary: true, value: true }]);
      if (!ok || !path.value.trim()) return;
      if (!await confirmRestore(null)) return;
      try { restarted(await post('/api/admin/backups/restore-path', { path: path.value.trim(), passphrase: pass.value || undefined })); } catch (e) { toast(e.message, 'bad'); }
    };
    const last = v.last;
    const row = (b) => h('div.backup-row',
      h('div.backup-info', h('strong', when(b.created)),
        h('div.muted.small', [b.version ? `Noxa ${b.version}` : null, size(b.size),
          b.uploaded ? tr('envoyée') : b.name.includes('-restauree') ? tr('restaurée') : b.auto ? tr('automatique') : tr('manuelle')].filter(Boolean).join(' · '),
        b.broken ? h('span.bad', ` · ${tr('illisible')}`) : null), h('code.backup-name', b.name)),
      h('div.actions-row',
        h('a.btn.small', { href: adminUrl(`/api/admin/backups/${b.name}`), download: b.name }, icon('download'), tr('Télécharger')),
        h('button.btn.small', { disabled: !!b.broken, onclick: async () => {
          if (!await confirmRestore(b)) return;
          try { restarted(await post(`/api/admin/backups/${b.name}/restore`)); } catch (e) { toast(e.message, 'bad'); }
        } }, tr('Restaurer')),
        h('button.icon-btn', { title: tr('Supprimer'), 'aria-label': tr('Supprimer'), onclick: async () => {
          if (!await confirmDialog(tr('Supprimer cette sauvegarde ?'), when(b.created), tr('Supprimer'))) return;
          try { render(await del(`/api/admin/backups/${b.name}`)); } catch (e) { toast(e.message, 'bad'); }
        } }, icon('trash'))));
    // backups of this server, then copies of each linked server (grouped by origin server)
    const mine = v.backups.filter((b) => b.mine);
    const others = {};
    for (const b of v.backups.filter((x) => !x.mine)) (others[b.server || b.slug || '?'] ||= []).push(b);
    const copyLine = (st) => (st ? h('span', { class: st.ok ? 'muted' : 'bad' }, st.ok ? tr('dernière copie {date}', { date: when(st.at) }) : tr('échec le {date} : {error}', { date: when(st.at), error: st.error || '?' })) : h('span.muted', tr('pas encore de copie')));
    clear(body,
      v.restored ? h('div.status-box.ok', tr('Sauvegarde du {date} restaurée (Noxa {version}).', { date: when(v.restored.created), version: v.restored.version || '?' })) : null,
      h('section.panel', h('h2', tr('Sauvegarde automatique')),
        h('p.muted.small', tr('L’archive contient la base (comptes, historique, notes, playlists, réglages, bibliothèques et métadonnées), les clés des serveurs liés et des notifications, les images de profil et les sous-titres téléchargés. Les vidéos et les photos n’y sont pas. Elle contient les mots de passe (chiffrés) : gardez-la en lieu sûr.')),
        h('div.grid2', field(tr('Fréquence'), mode), field(tr('Heure'), time)),
        h('div.grid2', field(tr('Sauvegardes automatiques gardées'), keep, tr('Pour ce serveur, et pour chaque serveur lié dont il garde les copies.')),
          field(tr('Dossier des sauvegardes'), dir, tr('De préférence sur un autre disque ou un NAS (dossier monté dans le conteneur avec Docker). Vide : dans le dossier de données.'))),
        h('p.muted.small', [v.free != null ? tr('Espace libre : {n}', { n: size(v.free) }) : null,
          last ? tr('Dernière sauvegarde automatique : {date} ({status})', { date: when(last.start), status: last.status === 'ok' ? tr('réussie') : last.summary || tr('échec') }) : null].filter(Boolean).join(' · ')),
        h('button.btn', { onclick: () => saveConf() }, tr('Enregistrer'))),
      v.peers.length ? (() => {
        const to = toggle(tr('Recopier chaque sauvegarde de ce serveur sur les serveurs liés'), v.to_peers);
        to.querySelector('input').addEventListener('change', (e) => setOnly({ to_peers: e.target.checked }));
        const from = toggle(tr('Garder ici les sauvegardes envoyées par les serveurs liés'), v.from_peers);
        from.querySelector('input').addEventListener('change', (e) => setOnly({ from_peers: e.target.checked }));
        return h('section.panel', h('h2', tr('Recopie entre les serveurs liés')),
          h('p.muted.small', tr('Chaque serveur garde aussi les sauvegardes des autres : si l’un tombe en panne, sa dernière sauvegarde est sur un autre. Les noms commencent par le nom du serveur d’origine (noxa-<serveur>_date), et chaque serveur garde celles des autres à part.')),
          to, from,
          h('ul.small.copy-status', v.peers.map((p) => h('li', h('strong', p.name), ' · ', copyLine(p.last)))));
      })() : null,
      remotesPanel(v),
      h('section.panel', h('h2', tr('Chiffrement des copies hors du serveur')),
        h('p.muted.small', tr('Avec une phrase secrète, les copies envoyées sur S3, Google Drive ou OneDrive sont chiffrées (AES-256) : le service ne peut pas les lire. Notez la phrase ailleurs : sans elle, ces copies ne se restaurent pas. Les copies entre vos serveurs liés et celles du dossier ne sont pas chiffrées.')),
        h('div.status-box', { class: v.passphrase_set ? 'status-box ok' : 'status-box' }, v.passphrase_set ? tr('Phrase secrète enregistrée : les copies externes sont chiffrées.') : tr('Pas de phrase secrète : les copies externes ne sont pas chiffrées.')),
        (() => {
          const pw = h('input', { type: 'password', autocomplete: 'new-password', placeholder: tr('10 caractères au moins') });
          return h('div.actions-row', pw,
            h('button.btn', { onclick: () => { if (pw.value) setOnly({ passphrase: pw.value }, tr('Phrase secrète enregistrée')); } }, v.passphrase_set ? tr('Changer') : tr('Enregistrer')),
            v.passphrase_set ? h('button.btn', { onclick: () => setOnly({ clear_passphrase: true }, tr('Chiffrement désactivé')) }, tr('Retirer')) : null);
        })()),
      h('section.panel', h('h2', tr('Sauvegardes de ce serveur')),
        h('div.actions-row', nowBtn, h('button.btn', { onclick: () => pick(true) }, tr('Restaurer depuis un fichier…')),
          h('button.btn', { onclick: restorePath }, tr('Restaurer depuis un fichier du serveur…')),
          h('button.btn', { onclick: () => pick(false) }, tr('Ajouter un fichier à la liste…'))),
        h('p.muted.small', tr('Pour passer sur un nouveau serveur, ou remplacer un serveur en panne : installez Noxa, créez le compte administrateur, puis « Restaurer depuis un fichier… » avec sa sauvegarde (téléchargée depuis un autre serveur lié ou un stockage externe).')),
        mine.length ? h('div.backup-list', mine.map(row)) : h('p.muted', tr('Aucune sauvegarde pour l’instant.'))),
      ...Object.entries(others).map(([server, list]) => h('section.panel', h('h2', tr('Copies de « {name} »', { name: server })),
        h('p.muted.small', tr('Sauvegardes reçues de ce serveur lié. Pour le reconstruire, téléchargez la plus récente et restaurez-la sur sa nouvelle installation.')),
        h('div.backup-list', list.map(row)))));
  };

  // stockages externes : S3, Google Drive, OneDrive
  function remotesPanel() {
    const box = h('section.panel', h('h2', tr('Stockages externes')), spinner());
    const kinds = { s3: 'S3', gdrive: 'Google Drive', onedrive: 'OneDrive' };
    const load = async () => {
      let r;
      try { r = await get('/api/admin/backups/remotes'); } catch (e) { clear(box, h('h2', tr('Stockages externes')), h('p.muted', e.message)); return; }
      const card = (s) => {
        const files = h('div');
        const on = toggle(tr('Envoyer chaque sauvegarde'), s.enabled);
        on.querySelector('input').addEventListener('change', async (e) => { await put(`/api/admin/backups/remotes/${s.id}`, { enabled: e.target.checked }); load(); });
        const showFiles = async () => {
          files.replaceChildren(spinner());
          try {
            const f = await get(`/api/admin/backups/remotes/${s.id}/files`);
            files.replaceChildren(f.files.length ? h('div.backup-list', f.files.map((x) => h('div.backup-row',
              h('div.backup-info', h('strong', x.name), h('div.muted.small', [size(x.size), x.encrypted ? tr('chiffrée') : null, x.server].filter(Boolean).join(' · '))),
              h('button.btn.small', { onclick: async () => {
                if (!await confirmRestore(null)) return;
                let pass = '';
                if (x.encrypted) { pass = await askPassphrase(tr('Archive chiffrée')); if (pass === null) return; }
                toast(tr('Téléchargement de la sauvegarde…'));
                try { restarted(await post(`/api/admin/backups/remotes/${s.id}/restore`, { id: x.id, passphrase: pass || undefined })); } catch (e) { toast(e.message, 'bad'); }
              } }, tr('Restaurer'))))) : h('p.muted.small', tr('Aucune sauvegarde sur ce stockage.')));
          } catch (e) { files.replaceChildren(h('p.form-error.small', e.message)); }
        };
        const link = async () => {
          try {
            const c = await post(`/api/admin/backups/remotes/${s.id}/link`);
            const status = h('p.muted.small', tr('Cette page se met à jour toute seule une fois l’accès accordé.'));
            files.replaceChildren(h('p', tr('Sur {url}, saisissez ce code :', { url: c.verification_url })), h('div.trakt-code', c.user_code),
              h('div.actions-row', h('a.btn.primary', { href: c.verification_url, target: '_blank', rel: 'noopener' }, tr('Ouvrir la page'))), status);
            const poll = async () => {
              const p = await post(`/api/admin/backups/remotes/${s.id}/poll`);
              if (p.status === 'pending') { setTimeout(poll, (c.interval || 5) * 1000); return; }
              toast(p.status === 'linked' ? tr('Compte relié') : tr('Liaison annulée ou expirée : recommencez'), p.status === 'linked' ? 'ok' : 'bad');
              load();
            };
            setTimeout(poll, (c.interval || 5) * 1000);
          } catch (e) { toast(e.message, 'bad'); }
        };
        return h('div.remote-card',
          h('div.remote-head', h('strong', s.name), h('span.os-badge', kinds[s.kind]),
            s.kind === 's3' ? h('span.muted.small', `${s.conf.bucket}${s.conf.prefix ? `/${s.conf.prefix}` : ''}`) : null,
            !s.linked ? h('span.os-badge', tr('à relier')) : null),
          h('p.small', s.last ? h('span', { class: s.last.ok ? 'muted' : 'bad' }, s.last.ok ? tr('dernière copie {date}', { date: when(s.last.at) }) : tr('échec le {date} : {error}', { date: when(s.last.at), error: s.last.error || '?' })) : h('span.muted', tr('pas encore de copie'))),
          on,
          h('div.actions-row',
            !s.linked ? h('button.btn.primary', { onclick: link }, tr('Relier le compte')) : null,
            s.linked ? h('button.btn.small', { onclick: async (e) => {
              e.target.disabled = true;
              try { await post(`/api/admin/backups/remotes/${s.id}/push`); toast(tr('Sauvegarde envoyée'), 'ok'); load(); } catch (ex) { toast(ex.message, 'bad'); e.target.disabled = false; }
            } }, tr('Envoyer maintenant')) : null,
            s.linked ? h('button.btn.small', { onclick: showFiles }, tr('Voir les sauvegardes')) : null,
            h('button.btn.small', { onclick: () => remoteForm(s.kind, s).then((ok) => ok && load()) }, tr('Modifier')),
            h('button.icon-btn', { title: tr('Supprimer'), onclick: async () => {
              if (!await confirmDialog(tr('Retirer ce stockage ?'), tr('Les sauvegardes déjà envoyées y restent.'), tr('Retirer'))) return;
              await del(`/api/admin/backups/remotes/${s.id}`);
              load();
            } }, icon('trash'))),
          files);
      };
      clear(box, h('h2', tr('Stockages externes')),
        h('p.muted.small', tr('Chaque sauvegarde est aussi envoyée sur ces stockages, hors de chez vous : un compartiment S3 (AWS, Backblaze B2, Wasabi, Scaleway, OVH, MinIO…), Google Drive ou OneDrive. Chacun garde le nombre de copies automatiques choisi.')),
        r.remotes.length ? h('div.remote-list', r.remotes.map(card)) : null,
        h('div.actions-row', Object.entries(kinds).map(([k, label]) => h('button.btn', { onclick: () => remoteForm(k).then((ok) => ok && load()) }, `+ ${label}`))));
    };
    load();
    return box;
  }

  async function remoteForm(kind, existing = null) {
    const c = (existing && existing.conf) || {};
    const has = (k) => (c.secrets_set || []).includes(k);
    const inp = (k, opts = {}) => h('input', { value: c[k] || opts.value || '', spellcheck: false, autocomplete: 'off', ...opts, type: opts.type || 'text' });
    const name = h('input', { value: existing ? existing.name : '', placeholder: { s3: 'S3', gdrive: 'Google Drive', onedrive: 'OneDrive' }[kind] });
    const keepIn = h('input', { type: 'number', min: 1, max: 90, value: existing ? existing.keep : 7 });
    let fields, help;
    if (kind === 's3') {
      fields = { endpoint: inp('endpoint', { placeholder: 'https://s3.eu-west-3.amazonaws.com' }), region: inp('region', { placeholder: 'eu-west-3' }),
        bucket: inp('bucket'), prefix: inp('prefix', { value: existing ? '' : 'nexis' }), access_key: inp('access_key'),
        secret_key: inp('secret_key', { type: 'password', value: '', placeholder: has('secret_key') ? tr('Enregistrée (laisser vide pour la garder)') : '' }) };
      help = tr('Adresse du service (vide pour AWS), région, compartiment existant, et une clé d’accès qui peut y lire, écrire et supprimer. Exemples d’adresse : https://s3.eu-central-003.backblazeb2.com, https://s3.fr-par.scw.cloud, https://s3.gra.io.cloud.ovh.net.');
    } else if (kind === 'gdrive') {
      fields = { client_id: inp('client_id'), client_secret: inp('client_secret', { type: 'password', value: '', placeholder: has('client_secret') ? tr('Enregistré (laisser vide pour le garder)') : '' }),
        folder: inp('folder', { value: existing ? '' : 'Noxa' }) };
      help = tr('Une fois : console.cloud.google.com → nouveau projet → activer « Google Drive API » → Identifiants → ID client OAuth de type « TV et appareils à saisie limitée » (écran de consentement en mode test, avec votre adresse comme testeur). Copiez ici l’ID client et le code secret, puis reliez le compte avec le code affiché. Noxa ne voit que les fichiers qu’il a créés.');
    } else {
      fields = { client_id: inp('client_id', { placeholder: '00000000-0000-0000-0000-000000000000' }) };
      help = tr('Une fois : portal.azure.com → Inscriptions d’applications → Nouvelle inscription (comptes personnels Microsoft et professionnels), puis dans « Authentification » : « Autoriser les flux clients publics » = Oui. Copiez ici l’ID d’application (client), puis reliez le compte avec le code affiché. Noxa n’écrit que dans son dossier d’application (Applications/Noxa).');
    }
    const labels = { endpoint: tr('Adresse du service'), region: tr('Région'), bucket: tr('Compartiment (bucket)'), prefix: tr('Dossier dans le compartiment'),
      access_key: tr('Clé d’accès'), secret_key: tr('Clé secrète'), client_id: tr('ID client'), client_secret: tr('Code secret du client'), folder: tr('Dossier dans Google Drive') };
    const err = h('p.form-error');
    return modal(existing ? tr('Modifier {name}', { name: existing.name }) : { s3: tr('Ajouter un stockage S3'), gdrive: tr('Ajouter Google Drive'), onedrive: tr('Ajouter OneDrive') }[kind],
      h('div.form', h('p.muted.small', help), field(tr('Nom'), name),
        h('div.grid2', ...Object.entries(fields).map(([k, el]) => field(labels[k], el))),
        field(tr('Copies automatiques gardées'), keepIn), err),
      [{ label: tr('Annuler'), value: false }, { label: existing ? tr('Enregistrer') : tr('Ajouter'), primary: true, handler: async () => {
        const conf = Object.fromEntries(Object.entries(fields).map(([k, el]) => [k, el.value.trim()]));
        try {
          if (existing) await put(`/api/admin/backups/remotes/${existing.id}`, { name: name.value.trim(), keep: Number(keepIn.value), conf });
          else await post('/api/admin/backups/remotes', { kind, name: name.value.trim(), keep: Number(keepIn.value), conf });
          return true;
        } catch (e) { err.textContent = e.message; return false; }
      } }]);
  }

  await reload();
}
