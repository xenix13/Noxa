// Settings specific to this device (this browser, this TV), kept in the browser and taking priority over
// the profile's: playback quality, subtitle size (a TV is watched from further away), animations,
// remote control navigation.
const KEY = 'nexis-device';

export function device() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || '{}');
    return v && typeof v === 'object' ? v : {};
  } catch { return {}; }
}

export function setDevice(patch) {
  const d = { ...device(), ...patch };
  for (const k of Object.keys(d)) if (d[k] === '' || d[k] == null) delete d[k];
  try { localStorage.setItem(KEY, JSON.stringify(d)); } catch { /* storage unavailable: setting for this session */ }
  applyDevice();
  return d;
}

// profile preferences, corrected by the device's
export function deviceOverrides() {
  const d = device();
  const o = {};
  if (d.quality) o.quality = d.quality;
  if (d.sub_size) o.sub_size = Number(d.sub_size);
  return o;
}

// modest device (little memory or few cores, reduced transparency asked by the system): lighter effects
export function modestDevice() {
  const mem = navigator.deviceMemory;
  const cores = navigator.hardwareConcurrency;
  const lessGlass = window.matchMedia && window.matchMedia('(prefers-reduced-transparency: reduce)').matches;
  return !!((mem && mem <= 2) || (cores && cores <= 2) || lessGlass);
}

export function applyDevice() {
  const d = device();
  const r = document.documentElement;
  r.classList.toggle('reduce-motion', d.motion === 'reduced');
  // visual effects: blur, transparency, shadows; "lighter" for a TV or a modest device
  r.classList.toggle('lite', d.effects === 'lite' || (d.effects !== 'full' && modestDevice()));
  if (d.remote === 'on') r.classList.add('remote');
  else if (d.remote === 'off') r.classList.remove('remote');
}

// Id of this device (remote control): drawn at random once, kept in the browser
export function deviceId() {
  try {
    let id = localStorage.getItem('nexis-device-id');
    if (!id) {
      id = Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, '0')).join('');
      localStorage.setItem('nexis-device-id', id);
    }
    return id;
  } catch { return 'session' + Math.random().toString(16).slice(2, 14); }
}

// Device name shown in the remote: chosen in This device, otherwise browser and system
export function deviceName() {
  const set = device().name;
  if (set) return set;
  const ua = navigator.userAgent;
  const browser = /edg\//i.test(ua) ? 'Edge' : /firefox|fxios/i.test(ua) ? 'Firefox' : /chrome|crios/i.test(ua) ? 'Chrome'
    : /safari/i.test(ua) ? 'Safari' : 'Navigateur';
  const os = /android/i.test(ua) ? (/mobile/i.test(ua) ? 'Android' : 'Android (tablette)') : /iphone/i.test(ua) ? 'iPhone'
    : /ipad/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1) ? 'iPad'
      : /windows/i.test(ua) ? 'Windows' : /mac os/i.test(ua) ? 'Mac' : /linux|cros/i.test(ua) ? 'Linux' : '';
  return os ? `${browser} · ${os}` : browser;
}
