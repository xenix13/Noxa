"""Documentation screenshots, taken on the demo library (see demo_library.py).

python3 tools/screenshots/take.py <demo_media_folder> <output_folder>

Starts a throwaway Noxa server on a temporary data folder, creates an English-speaking admin and a few
profiles, adds the demo libraries, seeds some viewing history, then drives Chromium (Playwright) through the
pages shown in the docs. Images are written as JPEG (docs/images).
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.ui_smoke_stub import STUB, serve_webm  # noqa: E402
from seed import demo_server  # noqa: E402

media, out = sys.argv[1], sys.argv[2]
# Shown in the Libraries page: point a short path (e.g. /srv/media) at the demo folder for tidier screenshots.
# Run as a regular user: as root every folder looks writable and the page warns about it.
os.makedirs(out, exist_ok=True)
work = tempfile.mkdtemp()
webm = os.path.join(work, "player.webm")
# the player screenshot shows a real frame of the film (Chromium here cannot decode H.264: a short VP9 clip)
PLAYER = "Tears of Steel"
clip_src = os.path.join(media, "Movies", "Tears of Steel (2012)", "Tears of Steel (2012).mkv")
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", "200", "-t", "20", "-i", clip_src, "-an", "-vf", "scale=1280:-2",
                "-c:v", "libvpx-vp9", "-b:v", "1500k", "-deadline", "realtime", "-cpu-used", "8", webm], check=True)

S = demo_server(media, work)
server, ctx, base, admin, libs = S["server"], S["ctx"], S["base"], S["admin"], S["libs"]
movies, by_title, episodes, shows, show_by_title = S["movies"], S["by_title"], S["episodes"], S["shows"], S["show_by_title"]
db = ctx.db
uid = db.scalar("SELECT id FROM users WHERE username='alex'")
now = int(time.time())

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


with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
    bctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US", reduced_motion="reduce",
                               bypass_csp=True)
    bctx.route("**/vendor/hls.min.js", lambda r: r.fulfill(status=200, content_type="text/javascript", body=STUB))
    bctx.route("**/__test.webm", serve_webm(webm))
    page = bctx.new_page()
    page.on("pageerror", lambda e: print("[pageerror]", e))

    page.goto(base + "/#/login")
    page.wait_for_selector("input[autocomplete='username']")
    settle(page)
    save(page, "login.jpg")
    login(page)
    page.reload()

    go(page, "/#/profiles", wait=800)
    save(page, "profiles.jpg")
    go(page, "/#/", ".hero-title", 1500)
    save(page, "home.jpg")
    go(page, f"/#/library/{libs['Movies']}", ".grid .card")
    save(page, "library.jpg")
    go(page, f"/#/item/{by_title['Sintel']['id']}", ".detail-actions", 1200)
    save(page, "movie.jpg")
    go(page, f"/#/item/{show_by_title['Caminandes']['id']}", ".episode", 1500)
    save(page, "show.jpg", full=True, clip={"x": 0, "y": 0, "width": 1440, "height": 1400})

    go(page, f"/#/play/{by_title[PLAYER]['id']}", "video", 6000)
    page.mouse.move(700, 500)
    page.mouse.move(720, 760)
    page.wait_for_timeout(800)
    save(page, "player.jpg")

    if "Photos" in libs:
        go(page, f"/#/library/{libs['Photos']}", None, 1000)
        page.locator("text=Albums").first.click()
        settle(page, 1500)
        save(page, "photos.jpg")

    for name, route, wait in (("settings-dashboard.jpg", "/#/admin/dashboard", 1500),
                              ("settings-libraries.jpg", "/#/admin/libraries", 800),
                              ("settings-stats.jpg", "/#/admin/stats", 1500),
                              ("settings-transcoding.jpg", "/#/admin/transcoding", 800),
                              ("settings-cold.jpg", "/#/admin/cold", 1000),
                              ("settings-tasks.jpg", "/#/admin/tasks", 800),
                              ("settings-users.jpg", "/#/admin/users", 800),
                              ("settings-appearance.jpg", "/#/settings/appearance", 800),
                              ("settings-themes.jpg", "/#/settings/themes", 1500)):
        go(page, route, None, wait)
        save(page, name)

    # a store theme with another layout, to show what personalization can do
    theme = json.load(open(os.path.join(ROOT, "static/themes/ville-neon.json")))
    for prefs in ({"my_themes": [theme]}, {"theme": "t:" + theme["id"], "ui_style": "sidebar"}):
        st = page.context.request.put(base + "/api/me/prefs", data=json.dumps(prefs),
                                      headers={"Content-Type": "application/json", "X-Noxa": "1"})
        print("prefs", st.status, st.text()[:200])
    go(page, "/#/", ".hero-title", 1500)
    save(page, "home-neon-sidebar.jpg")
    for prefs in ({"theme": "light", "ui_style": "glass"},):
        page.context.request.put(base + "/api/me/prefs", data=json.dumps(prefs),
                                 headers={"Content-Type": "application/json", "X-Noxa": "1"})
    go(page, "/#/", ".hero-title", 1500)
    save(page, "home-light-glass.jpg")
    page.context.request.put(base + "/api/me/prefs", data=json.dumps({"theme": "dark", "ui_style": "classic"}),
                             headers={"Content-Type": "application/json", "X-Noxa": "1"})

    # phone
    phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                                has_touch=True, locale="en-US", reduced_motion="reduce")
    p2 = phone.new_page()
    p2.goto(base + "/#/login")
    p2.fill("input[autocomplete='username']", "alex")
    p2.fill("input[type='password']", "demo-password-1")
    p2.click("button:has-text('Sign in')")
    p2.wait_for_selector("text=Alex")  # several profiles: the app asks "Who's watching?" first
    p2.click("text=Alex")
    p2.wait_for_timeout(800)
    shots = []
    for route, sel in (("/#/", ".hero-title"), (f"/#/item/{by_title['Sintel']['id']}", ".detail-actions"),
                       (f"/#/library/{libs['Movies']}", ".grid .card")):
        go(p2, route, sel, 1200)
        shots.append(Image.open(io.BytesIO(p2.screenshot())).convert("RGB"))
    w, h = shots[0].size
    gap = 60
    sheet = Image.new("RGB", (len(shots) * w + (len(shots) + 1) * gap, h + 2 * gap), (14, 16, 22))
    for i, s in enumerate(shots):
        sheet.paste(s, (gap + i * (w + gap), gap))
    sheet = sheet.resize((sheet.width // 2, sheet.height // 2), Image.LANCZOS)
    sheet.save(os.path.join(out, "mobile.jpg"), quality=86, optimize=True)
    print("saved mobile.jpg")
    browser.close()
server.shutdown()
