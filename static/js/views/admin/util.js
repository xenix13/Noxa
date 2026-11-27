// Small helpers shared by the admin pages.
import { tr } from '../../i18n.js';

export function since(ts) {
  if (!ts) return tr('jamais');
  const s = Date.now() / 1000 - ts;
  if (s < 120) return tr('à l’instant');
  if (s < 3600) return tr('il y a {p1} min', { p1: Math.round(s / 60) });
  if (s < 86400) return tr('il y a {p1} h', { p1: Math.round(s / 3600) });
  return tr('il y a {p1} j', { p1: Math.round(s / 86400) });
}
