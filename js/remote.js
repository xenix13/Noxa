// Navigation with a remote control, a keyboard and a gamepad (Android TV, PC with a gamepad).
// The arrows lead to the closest element in their direction (like Android TV's spatial navigation),
// OK / Enter opens, Back / Escape closes what is open or goes back. A gamepad is translated into
// these same keys: d-pad or left stick = arrows, A = OK, B = back, Start = play / pause.

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea, [tabindex="0"]';
const KEYS = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right' };

// TV remote or gamepad: a few behaviours are adapted (player: up / down show the controls)
export const remoteMode = () => document.documentElement.classList.contains('remote');
import { device } from './device.js';

const markRemote = () => { if (device().remote !== 'off') document.documentElement.classList.add('remote'); };
if (/android tv|googletv|smart-?tv|aft[a-z]|bravia|crkey|tizen|web0s|noxaapp|nexisapp/i.test(navigator.userAgent)) markRemote();

const visible = (el) => {
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return false;
  const st = getComputedStyle(el);
  return st.visibility !== 'hidden' && st.display !== 'none' && !el.closest('[hidden], [inert]');
};

// Area to search: open dialog, open menu, otherwise the whole page
function scope() {
  const modals = document.querySelectorAll('.modal-overlay');
  if (modals.length) return modals[modals.length - 1];
  const menu = [...document.querySelectorAll('.more-list, .user-menu, .player-menu, .chapter-panel')].find((m) => !m.hidden && visible(m));
  if (menu && menu.contains(document.activeElement)) return menu;
  return document.querySelector('.player:not(.mini)') || document.body;
}

// The closest element in the direction: distance along the axis, the sideways gap counts three times more
function nearest(from, dir, candidates) {
  const a = from.getBoundingClientRect();
  const ax = a.left + a.width / 2;
  const ay = a.top + a.height / 2;
  let best = null;
  let bestScore = Infinity;
  for (const el of candidates) {
    if (el === from || from.contains(el)) continue;
    const b = el.getBoundingClientRect();
    const bx = b.left + b.width / 2;
    const by = b.top + b.height / 2;
    let main;
    let side;
    if (dir === 'left' || dir === 'right') {
      main = dir === 'right' ? b.left - a.right : a.left - b.right;
      if ((dir === 'right' ? bx <= ax : bx >= ax)) continue;
      // left / right stay on the same line (at the end of a row, no jump elsewhere)
      if (b.bottom <= a.top + 2 || b.top >= a.bottom - 2) continue;
      side = Math.abs(by - ay) * 0.2;
    } else {
      main = dir === 'down' ? b.top - a.bottom : a.top - b.bottom;
      if ((dir === 'down' ? by <= ay : by >= ay)) continue;
      side = Math.max(0, Math.max(b.left, a.left) - Math.min(b.right, a.right)) * 0.5 || Math.abs(bx - ax) * 0.05;
    }
    const score = Math.max(main, -a.height / 2) + side * 3;
    if (score < bestScore) { bestScore = score; best = el; }
  }
  return best;
}

function firstTarget(root) {
  // first sensible element: page content rather than the top bar
  const main = root.querySelector('.content') || root;
  return [...main.querySelectorAll(FOCUSABLE)].find(visible) || [...root.querySelectorAll(FOCUSABLE)].find(visible);
}

function move(dir) {
  const root = scope();
  const cur = document.activeElement && document.activeElement !== document.body && root.contains(document.activeElement)
    ? document.activeElement : null;
  const target = cur ? nearest(cur, dir, [...root.querySelectorAll(FOCUSABLE)].filter(visible)) : firstTarget(root);
  if (!target) return false;
  target.focus({ preventScroll: true });
  target.scrollIntoView({ block: dir === 'up' || dir === 'down' ? 'center' : 'nearest', inline: 'nearest', behavior: 'smooth' });
  return true;
}

function back() {
  const open = [...document.querySelectorAll('.more-list, .user-menu')].find((m) => !m.hidden);
  if (open) { open.hidden = true; return; }
  if (document.querySelector('.modal-overlay, .player')) return; // dialogs and player handle their own "back"
  if (location.hash && location.hash !== '#/' && history.length > 1) history.back();
}

function onKey(e) {
  if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
  const dir = KEYS[e.key];
  const t = e.target;
  const typing = t.closest && t.closest('input:not([type="checkbox"]):not([type="range"]), textarea');
  if (dir) {
    // in a text field, left / right move the cursor; up / down leave it.
    // A drop-down list is handled like a button: OK opens it, the arrows move past it.
    if (typing && (dir === 'left' || dir === 'right')) return;
    if (document.querySelector('.player:not(.mini)')) return; // the player has its own arrows (see player.js)
    if (t.closest && t.closest('.timeline, .row-track input')) return;
    if (move(dir)) e.preventDefault();
    return;
  }
  if ((e.key === 'Backspace' && !typing) || e.key === 'BrowserBack' || e.key === 'GoBack') {
    e.preventDefault();
    back();
  }
}
document.addEventListener('keydown', onKey);

// Android TV app: the remote's Back key arrives here. -> true when Noxa handled it,
// false when already on the home page (the app then offers to quit).
window.nexisBack = () => {
  const menuOpen = [...document.querySelectorAll('.more-list, .user-menu')].some((m) => !m.hidden);
  if (menuOpen) { back(); return true; }
  if (document.querySelector('.modal-overlay, .player:not(.mini)')) {
    (document.activeElement || document.body).dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
    return true;
  }
  if (location.hash && location.hash !== '#/' && location.hash !== '#') { history.back(); return true; }
  return false;
};
// the player asks for spatial navigation in its controls
export { move as moveFocus };

// ------------------------------------------------------------------ manette (Gamepad API)
// Synthetic events do not trigger the browser's default actions: A therefore calls click().
const PAD = { 12: 'ArrowUp', 13: 'ArrowDown', 14: 'ArrowLeft', 15: 'ArrowRight', 0: 'Enter', 1: 'Escape', 9: ' ' };
const held = {};
let polling = false;

function press(key) {
  markRemote();
  const el = document.activeElement || document.body;
  const ev = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
  el.dispatchEvent(ev);
  if (ev.defaultPrevented) return;
  if (key === 'Enter' && el !== document.body) el.click();
  if (key === 'Escape') back();
}

function poll() {
  const pads = navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean) : [];
  if (!pads.length) { polling = false; return; }
  const now = performance.now();
  for (const pad of pads) {
    const axes = pad.axes || [];
    const stick = { 12: axes[1] < -0.6, 13: axes[1] > 0.6, 14: axes[0] < -0.6, 15: axes[0] > 0.6 };
    for (const [btn, key] of Object.entries(PAD)) {
      const down = (pad.buttons[btn] && pad.buttons[btn].pressed) || stick[btn];
      const id = `${pad.index}:${btn}`;
      if (!down) { delete held[id]; continue; }
      const repeat = key.startsWith('Arrow');
      if (!held[id]) { held[id] = now + 380; press(key); } // first press, then repeat while held
      else if (repeat && now >= held[id]) { held[id] = now + 110; press(key); }
    }
  }
  requestAnimationFrame(poll);
}
window.addEventListener('gamepadconnected', () => {
  markRemote();
  if (!polling) { polling = true; requestAnimationFrame(poll); }
});
