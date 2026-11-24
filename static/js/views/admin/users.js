// Admin page: users and household profiles.
import { del, get, post, put } from '../../api.js';
import { state } from '../../app.js';
import { LANGUAGES, locale, tr } from '../../i18n.js';
import { avatar, clear, confirmDialog, field, h, icon, modal, select, toast, toggle } from '../../ui.js';
import { since } from './util.js';

// ------------------------------------------------------------------ users
const AGE_LIMITS = [['', tr('Aucune restriction')], ['7', tr('Jeunes enfants (7 ans max.)')], ['10', tr('Enfant (10 ans max.)')],
  ['12', tr('12 ans max.')], ['16', tr('16 ans max.')]];

async function userForm(user, libraries, defaultLang = 'fr', others = []) {
  const isNew = !user;
  user = user || { username: '', display_name: '', is_admin: false, all_libraries: true, libraries: [], disabled: false,
    managed: false, home: true, max_age: null, allow_unrated: true, has_pin: false, kids: false };
  const username = h('input', { value: user.username, disabled: !isNew, autocomplete: 'off' });
  const display = h('input', { value: user.display_name, maxlength: 50 });
  const pass = h('input', { type: 'password', autocomplete: 'new-password',
    placeholder: isNew ? tr('8 caractères minimum') : tr('Laisser vide pour ne pas changer') });
  const managed = toggle(tr('Profil géré : sans mot de passe, accessible depuis « Changer de profil » (adapté aux enfants)'), user.managed);
  const home = toggle(tr('Membre du foyer (affiché dans « Qui regarde ? »)'), user.home);
  const pin = h('input', { type: 'password', inputmode: 'numeric', maxlength: 8, autocomplete: 'off',
    placeholder: user.has_pin ? tr('Code défini (laisser vide pour le garder)') : tr('4 à 8 chiffres (facultatif)') });
  const clearPin = user.has_pin ? toggle(tr('Retirer le code PIN'), false) : null;
  const age = select(AGE_LIMITS, user.max_age == null ? '' : String(user.max_age));
  const unrated = toggle(tr('Autoriser les contenus sans classification'), user.allow_unrated);
  const kids = toggle(tr('Interface enfant : grandes vignettes, menu simplifié'), user.kids);
  // temps d'écran et limites du compte
  const minutes = select([['', tr('Illimité')], ['30', tr('30 min')], ['45', tr('45 min')], ['60', tr('1 h')], ['90', tr('1 h 30')],
    ['120', tr('2 h')], ['180', tr('3 h')], ['240', tr('4 h')], ['360', tr('6 h')]],
  user.daily_minutes ? String(user.daily_minutes) : '');
  if (user.daily_minutes && !minutes.querySelector(`option[value="${user.daily_minutes}"]`)) {
    minutes.append(h('option', { value: String(user.daily_minutes), selected: true }, tr('{n} min', { n: user.daily_minutes })));
  }
  const [bedFrom, bedTo] = (user.bedtime || '-').split('-');
  const bedOn = toggle(tr('Heure du coucher : pas de lecture la nuit'), !!user.bedtime);
  const bedStart = h('input', { type: 'time', value: bedFrom || '21:00' });
  const bedEnd = h('input', { type: 'time', value: bedTo || '07:00' });
  const bedFields = h('div.grid2', field(tr('À partir de'), bedStart), field(tr('Jusqu’à'), bedEnd));
  const streams = h('input', { type: 'number', min: 1, max: 20, value: user.max_streams || '', placeholder: tr('Illimité') });
  const remote = select([['', tr('Illimité')], ['2000', '2 Mbit/s'], ['4000', '4 Mbit/s'], ['8000', '8 Mbit/s'], ['12000', '12 Mbit/s'],
    ['20000', '20 Mbit/s'], ['40000', '40 Mbit/s']], user.remote_kbps ? String(user.remote_kbps) : '');
  if (user.remote_kbps && !remote.querySelector(`option[value="${user.remote_kbps}"]`)) {
    remote.append(h('option', { value: String(user.remote_kbps), selected: true }, `${(user.remote_kbps / 1000).toLocaleString(locale())} Mbit/s`));
  }
  const admin = toggle(tr('Administrateur'), user.is_admin);
  const copyFrom = select([['', tr('Ne rien copier')], ...others.filter((o) => !user.id || o.id !== user.id).map((o) => [o.id, o.display_name])], '');
  const disabled = toggle(tr('Compte désactivé'), user.disabled);
  const all = toggle(tr('Accès à toutes les bibliothèques (y compris les futures)'), user.all_libraries);
  const libChecks = libraries.map((l) => {
    const t = toggle(l.name, user.libraries.includes(l.id));
    t.dataset.id = l.id;
    return t;
  });
  const libBox = h('div.checks', libChecks);
  const passField = field(isNew ? tr('Mot de passe') : tr('Nouveau mot de passe'), pass);
  const sync = () => {
    libBox.hidden = all.querySelector('input').checked;
    const isManaged = managed.querySelector('input').checked;
    passField.hidden = isManaged;
    admin.hidden = isManaged;
    unrated.hidden = !age.value;
    bedFields.hidden = !bedOn.querySelector('input').checked;
  };
  [all, managed, bedOn].forEach((t) => t.querySelector('input').addEventListener('change', sync));
  age.addEventListener('change', sync);
  sync();
  const err = h('p.form-error');
  // téléphone perdu : l'administrateur désactive la double authentification du compte
  const twoFactor = !isNew && user.two_factor ? h('div.twofa-admin',
    h('span.small', tr('Double authentification activée.')),
    h('button.btn.small', { type: 'button', onclick: async (e) => {
      if (!await confirmDialog(tr('Réinitialiser la double authentification ?'),
        tr('{display_name} pourra se connecter avec son seul mot de passe, puis la réactiver dans Paramètres → Profil.', { display_name: user.display_name }), tr('Réinitialiser'))) return;
      try {
        await post(`/api/admin/users/${user.id}/2fa/reset`);
        user.two_factor = false;
        e.target.closest('.twofa-admin').replaceChildren(h('span.small', tr('Double authentification désactivée.')));
        toast(tr('Double authentification réinitialisée'), 'ok');
      } catch (ex) { toast(ex.message, 'bad'); }
    } }, tr('Réinitialiser'))) : null;
  const language = select(LANGUAGES.map((l) => [l.id, l.name]), (user && user.language) || defaultLang);
  const body = h('div.form',
    h('div.grid2', field(tr('Identifiant'), username), field(tr('Nom affiché'), display)),
    field(tr('Langue de l’interface'), language, tr('Chaque utilisateur peut la changer dans Paramètres → Profil.')),
    managed, passField, twoFactor,
    h('h4.form-sub', tr('Profil et contrôle parental')),
    home,
    h('div.grid2', field(tr('Code PIN du profil'), pin), field(tr('Restriction d’âge'), age, tr('D’après la classification (TMDB ou .nfo).'))),
    clearPin, unrated, kids,
    h('h4.form-sub', tr('Temps d’écran et limites')),
    h('div.grid2', field(tr('Temps d’écran par jour'), minutes, user.screen_used != null ? tr('{n} min regardées aujourd’hui.', { n: user.screen_used }) : null),
      field(tr('Lectures simultanées (max.)'), streams, tr('Sur tous les appareils du compte.'))),
    bedOn, bedFields,
    field(tr('Débit maximum hors de la maison'), remote, tr('Depuis l’extérieur, la qualité est réduite pour tenir dans ce débit. À la maison, rien ne change.')),
    others.length ? h('h4.form-sub', tr('Réglages')) : null,
    others.length ? field(tr('Copier les réglages de…'), copyFrom, tr('Apparence, langues, lecture, accueil et menu de ce compte remplacent ceux-ci (pratique pour les profils des enfants).')) : null,
    h('h4.form-sub', tr('Droits et bibliothèques')),
    admin, isNew ? null : disabled, all, libBox, err);
  return modal(isNew ? tr('Nouvel utilisateur') : tr('Modifier {display_name}', { display_name: user.display_name }), body, [
    { label: tr('Annuler'), value: null },
    { label: isNew ? tr('Créer') : tr('Enregistrer'), primary: true, handler: async () => {
      const isManaged = managed.querySelector('input').checked;
      const payload = {
        display_name: display.value.trim() || username.value.trim(),
        is_admin: !isManaged && admin.querySelector('input').checked,
        all_libraries: all.querySelector('input').checked,
        libraries: libChecks.filter((t) => t.querySelector('input').checked).map((t) => t.dataset.id),
        managed: isManaged,
        home: home.querySelector('input').checked,
        max_age: age.value ? Number(age.value) : null,
        allow_unrated: unrated.querySelector('input').checked,
        kids: kids.querySelector('input').checked,
        language: language.value,
        daily_minutes: minutes.value ? Number(minutes.value) : null,
        bedtime: bedOn.querySelector('input').checked ? `${bedStart.value || '21:00'}-${bedEnd.value || '07:00'}` : null,
        max_streams: streams.value ? Number(streams.value) : null,
        remote_kbps: remote.value ? Number(remote.value) : null,
      };
      if (pass.value && !isManaged) payload.password = pass.value;
      if (pin.value) payload.pin = pin.value;
      else if (clearPin && clearPin.querySelector('input').checked) payload.pin = '';
      if (!isNew) payload.disabled = disabled.querySelector('input').checked;
      try {
        const saved = isNew ? await post('/api/admin/users', { username: username.value.trim(), ...payload })
          : await put(`/api/admin/users/${user.id}`, payload);
        if (copyFrom.value) await post(`/api/admin/users/${(saved && saved.id) || user.id}/prefs`, { from: Number(copyFrom.value) });
        return true;
      } catch (e) { err.textContent = e.message; return false; }
    } },
  ]);
}

export async function usersTab(body, ctx) {
  const [users, libs, settings] = await Promise.all([get('/api/admin/users'), get('/api/admin/libraries'),
    get('/api/admin/settings')]);
  if (!ctx.isCurrent()) return;
  const libraries = libs.libraries;
  const defaultLang = settings.default_language;
  // langue proposée aux nouveaux comptes (et affichée avant la connexion)
  const langSel = select(LANGUAGES.map((l) => [l.id, l.name]), defaultLang, { onchange: async (e) => {
    try { await put('/api/admin/settings', { default_language: e.target.value }); toast(tr('Langue par défaut enregistrée'), 'ok'); reload(); }
    catch (ex) { toast(ex.message, 'bad'); }
  } });
  const langPanel = h('section.panel', h('h2', tr('Langue par défaut')),
    field(tr('Langue des nouveaux comptes'), langSel,
      tr('Utilisée pour les comptes créés ensuite et sur la page de connexion. Chaque utilisateur peut choisir la sienne dans Paramètres → Profil.')));
  const reload = () => usersTab(body, ctx);
  const libName = (id) => (libraries.find((l) => l.id === id) || { name: `#${id}` }).name;
  clear(body,
    h('div.toolbar', h('p.muted', tr('{length} compte(s). Historique, reprise de lecture et préférences sont propres à chaque compte.', { length: users.length })),
      h('button.btn.primary', { onclick: async () => { if (await userForm(null, libraries, defaultLang, users)) { toast(tr('Utilisateur créé'), 'ok'); reload(); } } }, tr('+ Ajouter'))),
    h('div.user-list', users.map((u) => h('div.user-row', { class: u.disabled ? 'user-row off' : 'user-row' },
      avatar(u, 40),
      h('div.grow', h('strong', u.display_name), h('span.muted', ` @${u.username}`),
        h('div.muted.small',
          u.is_admin ? h('span.tag.accent', tr('Admin')) : null,
          u.disabled ? h('span.tag', tr('Désactivé')) : null,
          u.managed ? h('span.tag', tr('Profil géré')) : null,
          u.max_age != null ? h('span.tag', tr('≤ {max_age} ans', { max_age: u.max_age })) : null,
          u.kids ? h('span.tag', tr('Interface enfant')) : null,
          u.has_pin ? h('span.tag', 'PIN') : null,
          h('span.tag', (LANGUAGES.find((l) => l.id === u.language) || { name: u.language }).name),
          u.two_factor ? h('span.tag.ok', tr('Double authentification')) : null,
          u.all_libraries ? tr('Toutes les bibliothèques') : (u.libraries.map(libName).join(', ') || tr('Aucune bibliothèque')),
          tr(' · vu {p1}', { p1: since(u.last_seen) }))),
      h('button.btn.small', { onclick: async () => { if (await userForm(u, libraries, defaultLang, users)) { toast(tr('Modifications enregistrées'), 'ok'); reload(); } } }, tr('Modifier')),
      u.id === state.me.user.id ? null : h('button.icon-btn', { title: tr('Supprimer'), onclick: async () => {
        if (!await confirmDialog(tr('Supprimer ce compte ?'), tr('L’historique et les préférences de {display_name} seront supprimés. Cette action est définitive.', { display_name: u.display_name }), tr('Supprimer'))) return;
        try { await del(`/api/admin/users/${u.id}`); toast(tr('Compte supprimé'), 'ok'); reload(); } catch (e) { toast(e.message, 'bad'); }
      } }, icon('trash'))))),
    langPanel);
}
