// Push notifications on this device: subscription with the browser's service, then with the Noxa server.
import { get, post } from './api.js';
import { tr } from './i18n.js';

// Android app: its web view does not do Web Push, the app itself shows the notifications
// (it fetches them from the server with a token specific to the device)
const android = () => window.NoxaAndroid && typeof window.NoxaAndroid.pushEnable === 'function';
const oldAndroid = () => window.NoxaAndroid && !android(); // app 1.0: needs an update

export const pushSupported = () => android() || (window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window
  && 'Notification' in window);

const keyBytes = (b64) => {
  const s = atob(b64.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (b64.length % 4)) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
};

export async function pushState() {
  if (android()) {
    let st = {};
    try { st = JSON.parse(window.NoxaAndroid.pushStatus()); } catch { /* ignore */ }
    if (st.registered && !st.allowed) return 'denied';
    return st.registered ? 'on' : 'off';
  }
  if (oldAndroid()) return 'update';
  if (!pushSupported()) return 'unsupported';
  if (Notification.permission === 'denied') return 'denied';
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return 'off';
  const r = await get(`/api/push/key?endpoint=${encodeURIComponent(sub.endpoint)}`);
  return r.subscribed ? 'on' : 'off';
}

export async function enablePush() {
  if (android()) {
    const { token } = await post('/api/push/device');
    // the app asks for Android's permission (version 13 and later) then answers
    const ok = await new Promise((resolve) => { window.onNoxaPush = window.onNexisPush = resolve; window.NoxaAndroid.pushEnable(token); });
    // refused: the subscription stays, messages will arrive as soon as they are allowed in the settings
    if (!ok) throw new Error(tr('Notifications refusées pour l’application Noxa : autorisez-les dans les réglages d’Android.'));
    return;
  }
  if (await Notification.requestPermission() !== 'granted') throw new Error(tr('Notifications refusées dans le navigateur'));
  const reg = await navigator.serviceWorker.ready;
  const { key } = await get('/api/push/key');
  if (!key) throw new Error(tr('Le serveur ne peut pas envoyer de notifications (openssl manquant ?)'));
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
  await post('/api/push/subscribe', { endpoint: sub.endpoint });
}

// Android app: fetch the messages right away (test notification)
export function pushCheckNow() { if (android()) window.NoxaAndroid.pushCheck(); }
export function openPushSettings() { if (android()) window.NoxaAndroid.notificationSettings(); }

export async function disablePush() {
  if (android()) {
    const token = window.NoxaAndroid.pushDisable();
    if (token) await post('/api/push/device/remove', { token }).catch(() => {});
    return;
  }
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return;
  await post('/api/push/unsubscribe', { endpoint: sub.endpoint }).catch(() => {});
  await sub.unsubscribe();
}
