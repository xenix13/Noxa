// Bandwidth between this device and the server, for automatic quality: a few megabytes downloaded, and
// the measurement kept 10 minutes (the next playback starts without waiting).
const KEY = 'nexis-bandwidth';
const KEEP = 10 * 60 * 1000;
const MAX_MS = 3000;      // a measurement never delays the start by more than 3 s
const MAX_BYTES = 32 << 20;
const WARMUP = 1 << 20;   // first megabyte not counted: the connection is still ramping up

export function saveBandwidth(kbps) {
  try { sessionStorage.setItem(KEY, JSON.stringify({ kbps, at: Date.now() })); } catch { /* stockage indisponible */ }
}

function cached() {
  try {
    const v = JSON.parse(sessionStorage.getItem(KEY) || 'null');
    return v && Date.now() - v.at < KEEP ? v.kbps : null;
  } catch { return null; }
}

// -> kbit/s, or 0 when the measurement fails (the server then uses this device's last measurement)
export async function measureBandwidth() {
  const hit = cached();
  if (hit) return hit;
  const ctl = new AbortController();
  let got = 0, t1 = 0, t0 = 0, timed = 0;
  const timer = setTimeout(() => ctl.abort(), MAX_MS + 2000);
  try {
    const res = await fetch(`/api/bandwidth?size=${MAX_BYTES}`, { cache: 'no-store', credentials: 'same-origin', signal: ctl.signal });
    if (!res.ok || !res.body) return 0;
    const reader = res.body.getReader();
    t1 = performance.now(); // from the arrival of the headers: the server's response time does not count
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      got += value.byteLength;
      const now = performance.now();
      if (!t0 && got >= WARMUP) t0 = now;
      else if (t0) timed += value.byteLength;
      if (now - t1 > MAX_MS) { ctl.abort(); break; }
    }
  } catch { /* cut at the end of the allotted time: keep what arrived */ } finally { clearTimeout(timer); }
  const end = performance.now();
  let kbps;
  if (t0 && timed >= 512 * 1024 && end > t0) kbps = Math.round((timed * 8) / (end - t0));
  else if (t1 && got >= 256 * 1024 && end > t1) kbps = Math.round((got * 8) / (end - t1));
  else return 0;
  saveBandwidth(kbps);
  return kbps;
}
