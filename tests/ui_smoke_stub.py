"""Fake hls.js for the browser runs (the test Chromium does not decode H.264)."""
STUB = """
window.__hls = [];
window.Hls = class {
  static isSupported() { return true; }
  static Events = { ERROR: 'error', MANIFEST_PARSED: 'parsed', FRAG_BUFFERED: 'frag' };
  static ErrorTypes = { MEDIA_ERROR: 'media' };
  constructor(cfg) { this.h = {}; this.start = (cfg && cfg.startPosition) || 0; }
  on(ev, fn) { this.h[ev] = fn; }
  loadSource(u) { fetch(u).then(r => r.text()).then(t => window.__hls.push(t.includes('#EXTINF'))); }
  attachMedia(v) { v.src = '/__test.webm'; v.addEventListener('loadedmetadata', () => { if (this.start > 0) v.currentTime = this.start; this.h.parsed && this.h.parsed(); }, { once: true }); }
  destroy() {} recoverMediaError() {}
};"""


def serve_webm(path):
    """Playwright handler that serves the test video with partial requests (Range): without them,
    Chromium cannot seek in the video (every seek goes back to the start)."""
    with open(path, "rb") as f:
        data = f.read()

    def handler(route):
        rng = route.request.headers.get("range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a or 0)
            end = min(int(b) if b else len(data) - 1, len(data) - 1)
            route.fulfill(status=206, body=data[start:end + 1], headers={
                "Content-Type": "video/webm", "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{len(data)}", "Content-Length": str(end - start + 1)})
        else:
            route.fulfill(status=200, body=data, headers={"Content-Type": "video/webm", "Accept-Ranges": "bytes"})
    return handler
