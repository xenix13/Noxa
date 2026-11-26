"""Builds the online demo: Noxa's web interface working without a server, from recorded answers.

python3 tools/demo/build.py <free_library_folder> <output_folder>
python3 tools/demo/build.py --interface-only <site_folder>   # new version of the interface, same recordings

<free_library_folder> is the library made by tools/screenshots/free_library.py (Blender open movies and public
domain classics). The tool:
1. starts a throwaway Noxa server on it, with the accounts and history of the documentation screenshots
   (tools/screenshots/seed.py);
2. opens the interface in Chromium and follows every link it can reach (pages, title pages, seasons, people,
   albums, settings), recording each answer of the API and each picture;
3. cuts a short clip of every movie and episode (H.264 / AAC, playable by every browser);
4. writes a static site: the interface (static/), the recordings (demo/), and a service worker (demo-sw.js)
   that answers the interface's requests from the recordings, so the site works on any static host such as
   GitHub Pages, in a sub-folder.

In the demo nothing is saved: requests that change something get an empty success, and a banner says so.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tools", "screenshots"))


CLIP_SECONDS = 30
BANNER = (f"Noxa demo: nothing you change is saved, and videos are {CLIP_SECONDS}-second clips. "
          '<a href="https://github.com/xenix13/Noxa">Install your own</a>'
          '<span class="demo-credits">Films: Blender Foundation (CC BY), public domain classics</span>')
MAX_PAGES = 400
# pages never opened by the recorder: they change something, or have nothing to show offline
SKIP = re.compile(r"^#/(play|offline|login|setup|logout|share)(/|\?|$)")


def clip(src, dst):
    """30 s from the middle of the video, 720p H.264 + stereo AAC, ready to stream (faststart)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", src],
                         capture_output=True, text=True).stdout.strip()
    start = max(0.0, float(out or 0) * 0.4)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{start:.1f}", "-t", str(CLIP_SECONDS), "-i", src,
                    "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset",
                    "slow", "-crf", "27", "-profile:v", "high", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2",
                    "-b:a", "96k", "-movflags", "+faststart", dst], check=True)


def write_interface(out):
    """The interface (static/) and the demo's service worker in the site folder, with paths relative to the
    site (it may live in a sub-folder). Used by the full build and by --interface-only (new version, same
    recordings)."""
    for name in os.listdir(out) if os.path.isdir(out) else []:
        if name not in ("demo", ".nojekyll", ".git"):
            p = os.path.join(out, name)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    static = os.path.join(ROOT, "static")
    for name in os.listdir(static):
        if name in ("index.html", "sw.js"):
            continue
        src = os.path.join(static, name)
        (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, os.path.join(out, name))
    # absolute addresses in the stylesheets (fonts, pictures): relative, so that the first visit works before
    # the demo's service worker controls the page
    for d, _, names in os.walk(os.path.join(out, "css")):
        for n in names:
            if n.endswith(".css"):
                f = os.path.join(d, n)
                css = open(f, encoding="utf-8").read()
                with open(f, "w", encoding="utf-8") as w:
                    w.write(re.sub(r"url\((['\"]?)/(?!/)", r"url(\1../", css))
    html = open(os.path.join(static, "index.html"), encoding="utf-8").read()
    html = re.sub(r'(href|src)="/(?!/)', r'\1="', html)
    html = html.replace('<script type="module" src="js/app.js"></script>', open(os.path.join(HERE, "boot.html"), encoding="utf-8").read())
    html = html.replace("</body>", f'  <div class="demo-banner">{BANNER}</div>\n</body>')
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    shutil.copy2(os.path.join(HERE, "demo-sw.js"), os.path.join(out, "demo-sw.js"))
    with open(os.path.join(out, ".nojekyll"), "w"):
        pass  # GitHub Pages: serve the files as they are


def main():
    if sys.argv[1] == "--interface-only":
        write_interface(sys.argv[2])
        print(f"interface updated in {sys.argv[2]}")
        return
    # only needed to record: --interface-only works without Playwright nor the library
    from playwright.sync_api import sync_playwright
    from seed import demo_server
    media, out = sys.argv[1], sys.argv[2]
    work = tempfile.mkdtemp()
    S = demo_server(media, work)
    base, db = S["base"], S["ctx"].db
    shutil.rmtree(out, ignore_errors=True)
    rec_dir = os.path.join(out, "demo", "r")
    os.makedirs(rec_dir)
    index = {}  # "/api/path?query" -> {"f": file, "t": content type}

    def record(resp):
        u = urlsplit(resp.url)
        if not u.path.startswith("/api/") or resp.request.method != "GET" or resp.status != 200:
            return
        ctype = (resp.headers.get("content-type") or "").split(";")[0]
        if ctype == "text/event-stream":
            return
        key = u.path + (f"?{u.query}" if u.query else "")
        if key in index:
            return
        try:
            body = resp.body()
        except Exception:
            return
        ext = ".json" if ctype == "application/json" else os.path.splitext(u.path)[1] or ".bin"
        name = hashlib.sha1(key.encode()).hexdigest()[:16] + ext
        with open(os.path.join(rec_dir, name), "wb") as f:
            f.write(body)
        index[key] = {"f": name, "t": ctype or "application/octet-stream"}

    with sync_playwright() as pw:
        exe = os.environ.get("PW_CHROMIUM")
        browser = pw.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if exe else pw.chromium.launch()
        # recorded at 2x: pictures stay sharp on high-density screens
        bctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="en-US",
                                   reduced_motion="reduce")
        r = bctx.request.post(base + "/api/login", data=json.dumps({"username": "alex", "password": "demo-password-1"}),
                              headers={"Content-Type": "application/json", "X-Noxa": "1"})
        assert r.ok, r.text()
        page = bctx.new_page()
        page.on("response", record)

        seen, todo = set(), ["#/"]
        todo += [f"#/library/{i}" for i in S["libs"].values()]
        todo += [f"#/item/{r['id']}" for r in db.all("SELECT id FROM items WHERE type IN ('movie','show','season','episode')")]
        todo += ["#/watchlist", "#/playlists", "#/history", "#/together", "#/remote", "#/downloads", "#/apps",
                 "#/search?q=the", "#/search?q=night", "#/settings"]
        todo += [f"#/settings/{x}" for x in "profile playback appearance themes device services privacy layout".split()]
        todo += [f"#/admin/{x}" for x in ("dashboard stats users update backups federation tasks libraries shares cold "
                                          "metadata detection requests transcoding subtitles dlna downloads live").split()]
        while todo and len(seen) < MAX_PAGES:
            route = todo.pop(0)
            if route in seen or SKIP.match(route):
                continue
            seen.add(route)
            page.goto(base + "/" + route)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(500)
            for href in page.eval_on_selector_all("a[href^='#/']", "els => els.map((e) => e.getAttribute('href'))"):
                if href not in seen and not SKIP.match(href):
                    todo.append(href)
        print(f"{len(seen)} pages, {len(index)} answers recorded", flush=True)
        browser.close()
    S["server"].shutdown()

    # clips: one per movie, episode and extra with a file
    clips = os.path.join(out, "demo", "clips")
    os.makedirs(clips)
    videos = db.all("SELECT id, key FROM items WHERE type IN ('movie','episode','extra') AND key IS NOT NULL")
    for v in videos:  # key: the item's file
        if os.path.isfile(v["key"]):
            clip(v["key"], os.path.join(clips, f"{v['id']}.mp4"))
    print(f"{len(videos)} clips", flush=True)

    write_interface(out)
    with open(os.path.join(out, "demo", "index.json"), "w", encoding="utf-8") as f:
        json.dump({"built": int(time.time()), "clip_seconds": CLIP_SECONDS, "answers": index,
                   "clips": sorted(int(n[:-4]) for n in os.listdir(clips))}, f, separators=(",", ":"))
    size = sum(os.path.getsize(os.path.join(d, n)) for d, _, ns in os.walk(out) for n in ns)
    print(f"demo written to {out} ({size / 2**20:.0f} MB)")


if __name__ == "__main__":
    main()
