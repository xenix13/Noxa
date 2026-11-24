// Admin page: DLNA server.
import { get, put } from '../../api.js';
import { tr } from '../../i18n.js';
import { clear, field, h, toast, toggle } from '../../ui.js';

// ----------------------------------------------------------------- DLNA
export async function dlnaPage(body, ctx) {
  const render = (v) => {
    if (!ctx.isCurrent()) return;
    const on = toggle(tr('Activer le serveur DLNA'), v.enabled);
    const name = h('input', { value: v.custom_name, maxlength: 60, placeholder: v.name });
    const libs = v.libraries.map((l) => ({ l, box: h('input', { type: 'checkbox', checked: l.shared }) }));
    const status = !v.enabled ? h('div.status-box', tr('Le serveur DLNA est désactivé.'))
      : v.error ? h('div.status-box.bad', v.error)
        : h('div.status-box.ok', tr('Noxa est visible sur le réseau local sous le nom « {name} ».', { name: v.name }));
    clear(body,
      h('section.panel', h('h2', tr('Serveur DLNA')),
        h('p.muted.small', tr('Les téléviseurs, consoles et box du réseau local trouvent Noxa dans leur liste de sources (« Serveurs multimédia », « DLNA », « Partage »). Ils lisent les fichiers tels quels : sans conversion, un format que l’appareil ne connaît pas ne se lit pas (préférez alors l’application Noxa).')),
        status, on,
        field(tr('Nom affiché sur les appareils'), name),
        field(tr('Bibliothèques partagées'), h('div.check-list', libs.map(({ l, box }) => h('label.check-row', box, h('span', l.name)))),
          tr('Le DLNA ne connaît pas les comptes : tout appareil du réseau local voit ces bibliothèques, sans contrôle parental.')),
        h('div.actions-row', h('button.btn.primary', { onclick: async () => {
          try {
            render(await put('/api/admin/dlna', { enabled: on.querySelector('input').checked, name: name.value.trim(),
              libraries: libs.filter((x) => x.box.checked).map((x) => x.l.id) }));
            toast(tr('Enregistré'), 'ok');
          } catch (e) { toast(e.message, 'bad'); }
        } }, tr('Enregistrer')))),
      h('section.panel', h('h2', tr('Bon à savoir')), h('ul.small.muted',
        h('li', tr('Réservé au réseau local : les appareils doivent être sur le même réseau que le serveur, et les adresses extérieures sont refusées.')),
        h('li', tr('Avec Docker, le conteneur doit utiliser le réseau de l’hôte (network_mode: host) : l’annonce DLNA ne traverse pas le réseau interne de Docker.')),
        h('li', tr('Les titres d’un serveur lié ne sont pas partagés en DLNA, seulement ceux de ce serveur.')),
        v.address ? h('li', tr('Adresse de description : {address}', { address: v.address })) : null)));
  };
  render(await get('/api/admin/dlna'));
}
