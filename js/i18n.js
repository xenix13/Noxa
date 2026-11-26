// Interface language. The text in the code is in French and serves as the key: tr('Paramètres') returns the
// translation in the chosen language (static/js/i18n/<language>.js), or the French when there is none.
// The language comes from the account (chosen in Settings → My account), kept on the device to show
// from the start; before sign-in, it is the server's default language (chosen by an administrator).
import en from './i18n/en.js';

export const LANGUAGES = [{ id: 'fr', name: 'Français' }, { id: 'en', name: 'English' }];
const DICTS = { en };
const STORE = 'nexis.lang';

// Before any choice: the server's language, or, when no administrator chose one yet (first setup), the
// browser's: French for a French browser, English for everything else.
function browserLang() {
  const first = ((navigator.languages && navigator.languages[0]) || navigator.language || '').toLowerCase();
  return first.startsWith('fr') ? 'fr' : 'en';
}

function initial() {
  let saved = null;
  try { saved = localStorage.getItem(STORE); } catch { /* stockage indisponible */ }
  const root = document.documentElement;
  const fallback = 'autoLang' in root.dataset ? browserLang() : root.dataset.defaultLang;
  return [saved, fallback].find((l) => LANGUAGES.some((x) => x.id === l)) || 'en';
}

export let lang = initial();
document.documentElement.lang = lang;

// Date and number format (toLocaleDateString, Intl…)
export const locale = () => (lang === 'en' ? 'en-US' : 'fr-FR');

// The same French word can need two translations: the key then carries a context, "bouton|Lecture"
// (shown "Lecture" in French, "Play" in English).
export function tr(text, vars) {
  const dict = DICTS[lang];
  let s = (dict && dict[text]) || text.slice(text.indexOf('|') + 1);
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k] ?? '') : m));
  return s;
}

// Singular or plural depending on the language ("0 épisode" in French, "0 episodes" in English).
// Les deux formes contiennent {n}.
export function plural(n, one, other, vars) {
  const single = lang === 'fr' ? Math.abs(n) < 2 : n === 1;
  return tr(single ? one : other, { n, ...vars });
}

// Language of the known account: if it changes the interface, it is kept and the page is reloaded (the text
// computed when the modules loaded must be redone). Returns true when a reload is started.
export function useLanguage(l) {
  if (!LANGUAGES.some((x) => x.id === l)) return false;
  try { localStorage.setItem(STORE, l); } catch { /* stockage indisponible */ }
  if (l === lang) return false;
  location.reload();
  return true;
}
