// Following an update started from the administration: progress by steps, waiting for the restart
// (the server, or the proxy in front of it, stops answering for a few seconds), then automatic reload.
// The state is kept in sessionStorage: if the page is reloaded during the installation, following resumes.
import { get } from './api.js';
import { h, toast } from './ui.js';
import { tr } from './i18n.js';

const KEY = 'nexis-update';
const LIMIT = 15 * 60 * 1000;

function saved() {
  try {
    const v = JSON.parse(sessionStorage.getItem(KEY) || 'null');
    return v && Date.now() - v.at < LIMIT ? v : null;
  } catch { return null; }
}
function save(v) { try { if (v) sessionStorage.setItem(KEY, JSON.stringify(v)); else sessionStorage.removeItem(KEY); } catch { /* ignore */ } }

export const updatePending = () => !!saved();

let overlay = null;

// Before reloading: each interface file is downloaded again (the browser cache could otherwise
// serve the old version again), and the service worker is told there is a new one.
async function refreshShell() {
  try {
    const code = await (await fetch('/sw.js', { cache: 'reload' })).text();
    const m = /^const SHELL = (.*);$/m.exec(code);
    const files = m ? JSON.parse(m[1]) : [];
    await Promise.all(files.map((u) => fetch(u, { cache: 'reload' }).catch(() => null)));
    const reg = navigator.serviceWorker && await navigator.serviceWorker.getRegistration();
    if (reg) await reg.update();
  } catch { /* at worst, plain reload */ }
}

// from: version installed at start
export function followUpdate(from) {
  const st = saved() || { from, at: Date.now() };
  save(st);
  if (overlay) return;
  const bar = h('div.update-bar-fill');
  const pct = h('span.update-pct', '0 %');
  const step = h('p.update-step', tr('Démarrage…'));
  const log = h('pre.log');
  const details = h('details.update-log', h('summary.small', tr('Journal de l’installation')), log);
  const actions = h('div.modal-actions');
  overlay = h('div.update-overlay', { role: 'dialog', 'aria-modal': 'true', 'aria-label': tr('Mise à jour de Noxa') },
    h('div.update-box', h('h2', tr('Mise à jour de Noxa')),
      h('p.muted.small', tr('Cette page se rechargera automatiquement à la fin de l’installation.')),
      h('div.update-bar', bar), h('div.update-row', step, pct), details, actions));
  document.body.append(overlay);

  let percent = 0;
  let seenRunning = false;
  const show = (p, text) => {
    percent = Math.max(percent, p);
    bar.style.width = `${percent}%`;
    pct.textContent = `${Math.round(percent)} %`;
    step.textContent = text;
  };
  const finish = async (text) => {
    save(null);
    show(100, text);
    await refreshShell();
    setTimeout(() => location.reload(), 600);
  };
  const fail = (text) => {
    save(null);
    step.textContent = text;
    overlay.classList.add('failed');
    details.open = true;
    actions.replaceChildren(h('button.btn', { onclick: () => { overlay.remove(); overlay = null; } }, tr('Fermer')));
  };
  show(2, tr('Démarrage…'));

  const tick = async () => {
    let up = null;
    try { up = await get('/api/status'); } catch { /* restart in progress */ }
    if (up && up.version !== st.from) { finish(tr('Noxa {version} est installé. Rechargement…', { version: up.version })); return; }
    if (up) {
      try {
        const u = await get('/api/admin/update');
        if (u.log) log.textContent = u.log.join('\n');
        if (u.running) seenRunning = true;
        // (a "finished" log seen before the installation started is the one from the previous time)
        if (u.done && !u.running && seenRunning) {
          if (u.ok) { finish(tr('Mise à jour terminée. Rechargement…')); return; }
          fail(tr('La mise à jour a échoué : le détail est dans le journal ci-dessous.'));
          return;
        }
        if (u.running) show(u.percent || 3, `${u.step || tr('Installation')}…`);
      } catch { /* the system helper restarts too */ }
    } else {
      seenRunning = true; // server down: it is indeed the running installation that restarts it
      show(85, tr('Redémarrage de Noxa…'));
    }
    if (Date.now() - st.at > LIMIT) {
      fail(tr('Mise à jour non terminée après 15 minutes. Le journal se trouve dans /var/log/seance-update.log sur le serveur.'));
      return;
    }
    setTimeout(tick, 2000);
  };
  setTimeout(tick, 1500);
}

// When the page loads: resumes following an update in progress, or announces the one that just finished
export async function resumeUpdate(status) {
  const st = saved();
  if (!st) return false;
  if (status && status.version !== st.from) {
    save(null);
    toast(tr('Noxa {version} est installé', { version: status.version }), 'ok');
    return false;
  }
  followUpdate(st.from);
  return true;
}
