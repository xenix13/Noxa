"""Records the short tour shown at the top of the README, on the generated demo library (see demo_library.py).

CLIP=<a film> python3 tools/screenshots/record_demo.py <demo_media_folder> <output_folder>

Starts a throwaway Noxa server, creates the demo accounts and history like the screenshots did, then drives
Chromium through home, a movie library, a movie page, the player (playing 20 s of CLIP), a show and the photo
albums. Chromium's screencast writes <output_folder>/frames/*.jpg and frames.txt, an ffmpeg concat list with
the real duration of each frame (docs/development.md has the ffmpeg command that makes docs/images/demo.webp).
"""
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from noxa.api import Ctx, build_app  # noqa: E402
from noxa.web import Server, make_handler  # noqa: E402
from tests.test_api import STATIC, Client, wait_scan  # noqa: E402
from tests.ui_smoke_stub import STUB, serve_webm  # noqa: E402

media, out = sys.argv[1], sys.argv[2]
# Shown in the Libraries page: point a short path (e.g. /srv/media) at the demo folder for tidier screenshots.
# Run as a regular user: as root every folder looks writable and the page warns about it.
os.makedirs(out, exist_ok=True)
work = tempfile.mkdtemp()
webm = os.path.join(work, "player.webm")
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", "200", "-t", "20", "-i", os.environ["CLIP"], "-an",
                "-vf", "scale=1280:-2", "-c:v", "libvpx-vp9", "-b:v", "1500k", "-deadline", "realtime",
                "-cpu-used", "8", webm], check=True)

ctx = Ctx(os.path.join(work, "data"), STATIC)
server = Server(("127.0.0.1", 0), make_handler(build_app(ctx)))
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_address[1]}"

admin = Client(base)
st, r = admin.call("POST", "/api/setup", {"username": "alex", "password": "demo-password-1", "display_name": "Alex",
                                          "language": "en"}, headers={"X-Setup-Code": ctx.setup_code})
assert st == 200, r
libs = {}
for name, kind, sub in (("Movies", "movie", "Movies"), ("Shows", "show", "Shows"), ("Photos", "photo", "Photos")):
    st, r = admin.call("POST", "/api/admin/libraries", {"name": name, "kind": kind, "paths": [os.path.join(media, sub)]})
    assert st == 200, r
    libs[name] = r.get("id") or r.get("library", {}).get("id")
    wait_scan(admin, timeout=900)
st, lst = admin.call("GET", "/api/admin/libraries")
libs = {lib["name"]: lib["id"] for lib in (lst["libraries"] if isinstance(lst, dict) else lst)}
admin.call("PUT", "/api/me/prefs", {"audio_lang": "eng", "sub_lang": "eng"})
for name, display in (("sam", "Sam"), ("kids", "Kids")):
    st, r = admin.call("POST", "/api/admin/users", {"username": name, "display_name": display, "managed": True,
                                                    "language": "en"})
    assert st == 200, r

db = ctx.db
# the scan and the .nfo metadata finish in the background: wait until every demo title is there
for _ in range(300):
    if (db.scalar("SELECT COUNT(*) FROM items WHERE type='movie'") >= 12
            and db.scalar("SELECT COUNT(*) FROM items WHERE type='show' AND title IN "
                          "('Harbor Lights', 'Signal Lost', 'Kitchen Atlas')") == 3):
        break
    time.sleep(1)
movies = db.all("SELECT id, title, duration_ms FROM items WHERE type='movie' ORDER BY title")
by_title = {m["title"]: m for m in movies}
episodes = db.all("SELECT id, title, duration_ms, parent_id FROM items WHERE type='episode' ORDER BY id")
shows = db.all("SELECT id, title FROM items WHERE type='show' ORDER BY title")
show_by_title = {s["title"]: s for s in shows}
uid = db.scalar("SELECT id FROM users WHERE username='alex'")
now = int(time.time())

# continue watching: a few movies and an episode in progress
for title, frac in (("The Quiet Orbit", 0.42), ("Glass Tide", 0.67), ("Velvet Circuit", 0.18)):
    m = by_title[title]
    admin.call("POST", "/api/progress", {"id": str(m["id"]), "position": int(m["duration_ms"] * frac),
                                         "duration": m["duration_ms"], "state": "stopped"})
for e in episodes[:2]:
    admin.call("POST", "/api/progress", {"id": str(e["id"]), "position": e["duration_ms"], "duration": e["duration_ms"],
                                         "state": "stopped", "ended": True})
e = episodes[2]
admin.call("POST", "/api/progress", {"id": str(e["id"]), "position": int(e["duration_ms"] * 0.3),
                                     "duration": e["duration_ms"], "state": "stopped"})
for t in ("Halcyon Drift", "Low Light Requiem", "Paper Lanterns"):
    db.run("INSERT OR IGNORE INTO watchlist(user_id, item_id, added_at) VALUES(?,?,?)", (uid, by_title[t]["id"], now))
db.run("INSERT OR IGNORE INTO watchlist(user_id, item_id, added_at) VALUES(?,?,?)",
       (uid, show_by_title["Signal Lost"]["id"], now))
for t, v in (("Northbound Static", 5), ("Salt & Ember", 4), ("Pinewatch", 3)):
    db.run("INSERT OR REPLACE INTO ratings(user_id, item_id, value, updated_at) VALUES(?,?,?,?)",
           (uid, by_title[t]["id"], v, now))

# cold storage: a "USB disk" folder holding copies of three movies
usb = os.path.join(work, "usb-disk")
os.makedirs(usb)
st, r = admin.call("POST", "/api/admin/cold/stores", {"kind": "local", "name": "USB disk", "conf": {"path": usb}})
assert st == 200, r
admin.call("POST", "/api/admin/cold/archive", {"ids": [by_title[t]["id"] for t in ("Pinewatch", "Paper Lanterns",
                                                                                   "Seven Summers South")],
                                               "store": r["id"]})
for _ in range(600):
    st, c = admin.call("GET", "/api/admin/cold")
    if not c.get("jobs"):
        break
    time.sleep(1)

# statistics: 30 days of plausible plays for three accounts
rnd = random.Random(7)
users = [r["id"] for r in db.all("SELECT id FROM users")]
pool = [(m["id"], m["title"], m["duration_ms"]) for m in movies] + [(e["id"], e["title"], e["duration_ms"]) for e in episodes]
for day in range(30):
    for _ in range(rnd.randint(1, 5)):
        item = rnd.choice(pool)
        secs = int(item[2] / 1000 * rnd.uniform(0.3, 1.0))
        end = now - day * 86400 - rnd.randint(0, 6 * 3600)
        db.run("INSERT INTO play_log(user_id, rating_key, title, mode, kbps, started, ended, seconds) "
               "VALUES(?,?,?,?,?,?,?,?)", (rnd.choice(users), str(item[0]), item[1],
                                           rnd.choice(("direct", "direct", "remux", "transcode")),
                                           rnd.choice((4000, 8000, 12000)), end - secs, end, secs))


def save(page, name, full=False, clip=None):
    png = page.screenshot(full_page=full, clip=clip)
    Image.open(io.BytesIO(png)).convert("RGB").save(os.path.join(out, name), quality=86, optimize=True)
    print("saved", name, flush=True)


def settle(page, ms=600):
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass  # the player keeps loading video segments
    try:
        page.wait_for_function("[...document.images].every((i) => i.complete)", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def go(page, route, sel=None, wait=600):
    """Hash navigation does not reload the app: reload so that new preferences and sessions are picked up."""
    page.goto(base + route)
    page.reload()
    if sel:
        try:
            page.wait_for_selector(sel)
        except Exception:
            page.screenshot(path=os.path.join(out, "debug-timeout.png"))
            raise
    settle(page, wait)


def login(page, user="alex", password="demo-password-1"):
    c = page.context
    st = c.request.post(base + "/api/login", data=json.dumps({"username": user, "password": password}),
                        headers={"Content-Type": "application/json", "X-Noxa": "1"})
    assert st.ok, st.text()



def nav(page, route, sel=None, ms=1800):
    page.evaluate("r => { location.hash = r }", route.lstrip("/"))
    if sel:
        page.wait_for_selector(sel)
    settle(page, 300)
    page.wait_for_timeout(ms)


def glide(page, x0, y0, x1, y1, steps=25):
    page.mouse.move(x0, y0)
    page.mouse.move(x1, y1, steps=steps)


def scroll(page, dy, steps=20, pause=25):
    for _ in range(steps):
        page.mouse.wheel(0, dy / steps)
        page.wait_for_timeout(pause)


with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
    W, H = 1280, 800
    # warm-up context: log in and load every page once, so the recording has no spinners
    warm = browser.new_context(viewport={"width": W, "height": H}, locale="en-US", bypass_csp=True)
    warm.route("**/vendor/hls.min.js", lambda r: r.fulfill(status=200, content_type="text/javascript", body=STUB))
    warm.route("**/__test.webm", serve_webm(webm))
    wp = warm.new_page()
    login(wp)
    state = warm.storage_state()
    for route in ("/#/", f"/#/library/{libs['Movies']}", f"/#/item/{by_title['The Quiet Orbit']['id']}",
                  f"/#/item/{show_by_title['Harbor Lights']['id']}", f"/#/library/{libs['Photos']}"):
        wp.goto(base + route)
        settle(wp, 500)
    warm.close()

    bctx = browser.new_context(viewport={"width": W, "height": H}, locale="en-US", bypass_csp=True,
                               storage_state=state)
    bctx.route("**/vendor/hls.min.js", lambda r: r.fulfill(status=200, content_type="text/javascript", body=STUB))
    bctx.route("**/__test.webm", serve_webm(webm))
    page = bctx.new_page()
    page.on("pageerror", lambda e: print("[pageerror]", e))
    # Chromium's screencast: a JPEG frame each time the page changes, with its timestamp
    frames_dir = os.path.join(out, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    frames = []
    cdp = bctx.new_cdp_session(page)

    def on_frame(ev):
        n = len(frames)
        with open(os.path.join(frames_dir, f"{n:05d}.jpg"), "wb") as f:
            f.write(__import__("base64").b64decode(ev["data"]))
        frames.append(ev["metadata"]["timestamp"])
        cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})

    cdp.on("Page.screencastFrame", on_frame)
    t0 = time.time()
    page.goto(base + "/#/")
    page.wait_for_selector(".hero-title")
    settle(page, 200)
    cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 85, "maxWidth": W, "maxHeight": H})
    start = time.time()
    page.wait_for_timeout(1800)
    glide(page, 640, 300, 360, 700)
    page.wait_for_timeout(700)
    scroll(page, 520)
    page.wait_for_timeout(1200)
    scroll(page, -520)

    nav(page, f"/#/library/{libs['Movies']}", ".grid .card", 900)
    glide(page, 300, 400, 560, 330)
    page.wait_for_timeout(900)
    nav(page, f"/#/item/{by_title['The Quiet Orbit']['id']}", ".detail-actions", 1600)
    glide(page, 700, 500, 430, 255, 20)
    page.wait_for_timeout(700)
    nav(page, f"/#/play/{by_title['Northbound Static']['id']}", "video", 2500)
    glide(page, 640, 400, 700, 760, 15)
    page.wait_for_timeout(1800)
    nav(page, f"/#/item/{show_by_title['Harbor Lights']['id']}", ".episode", 1200)
    scroll(page, 600, 25)
    page.wait_for_timeout(1500)
    nav(page, f"/#/library/{libs['Photos']}", ".album-card", 1800)
    end = time.time()
    cdp.send("Page.stopScreencast")
    bctx.close()
    browser.close()
    # ffmpeg concat list: each frame lasts until the next one
    with open(os.path.join(out, "frames.txt"), "w") as f:
        for i, ts in enumerate(frames):
            nxt = frames[i + 1] if i + 1 < len(frames) else end
            f.write(f"file 'frames/{i:05d}.jpg'\nduration {max(nxt - ts, 0.001):.4f}\n")
        f.write(f"file 'frames/{len(frames) - 1:05d}.jpg'\n")
    print("FRAMES", len(frames), f"{end - start:.1f}s", flush=True)
