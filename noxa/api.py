"""Noxa: assembles the server, with its shared context (database, scanner, playback…) and its HTTP application.

The routes are grouped by topic in noxa/routes/ (accounts, library, playback, administration) and in the feature
modules (collections_api, discover, tasks, livetv, syncplay).
"""
import hmac
import logging
import os
import secrets
import threading
import time
import types

from . import __version__, apps, backup, books, catalog, cold, coldfree, health, limits, opensubs, stats, themes, tracking, whisper, collections_api, discover, dlna, federation, i18n, push, remote, livetv, media, notify, photos, suggest, syncplay, tasks, update
from .auth import Throttle, load_user
from .chapters import ChapterImages
from .db import DB
from .downloads import Downloads
from .images import Images
from .introdetect import FingerprintCache
from .metadata import Metadata
from .ratings import age_of
from .routes import account, admin, library
from .routes import media as media_routes
from .scanner import Scanner
from .subcache import SubtitleCache, pgs_tracks, text_tracks
from .transcoder import Transcoder
from .watcher import Watcher
from .web import App, HTTPError

log = logging.getLogger("noxa")


class Ctx:
    def __init__(self, data_dir, static_dir, cookie_secure=False, trust_proxy=False):
        self.data_dir = data_dir
        self.static_dir = static_dir
        self.cookie_secure = cookie_secure
        self.trust_proxy = trust_proxy
        for d in ("cache/img", "cache/orig", "cache/subs"):
            os.makedirs(os.path.join(data_dir, d), exist_ok=True)
        self.restored = backup.apply_pending(data_dir)  # restore prepared before the shutdown: before opening the database
        self.restart = None  # provided by __main__: stop with an exit code that makes the supervisor start Noxa again
        self.db = DB(os.path.join(data_dir, "seance.db"))
        self.throttle = Throttle()
        self.lock = threading.Lock()
        self.metadata = Metadata(self)
        self.images = Images(self)
        self.scanner = Scanner(self)
        self.transcoder = Transcoder(os.path.join(data_dir, "transcodes"), self.limits, self.hw)
        self.downloads = Downloads(self)
        self.subtitles = SubtitleCache(self)
        self.chapter_images = ChapterImages(self)
        self.fingerprints = FingerprintCache(os.path.join(data_dir, "cache", "fingerprints"))
        self.watcher = Watcher(self)
        self.tasks = tasks.TaskManager(self)
        self.live = livetv.LiveTV(self)
        self.together = syncplay.Groups(self)
        self.fed = federation.Federation(self)
        self.dlna = dlna.DLNA(self)
        self.remote = remote.Remote()
        self.push = push.Push(self)
        self.opensubs = opensubs.OpenSubtitles(self)
        self.account_limits = limits.Limits(self)
        self.health = health.Health(self)
        self.whisper = whisper.Whisper(self)
        self.tracking = tracking.Tracking(self)
        self.themes = themes.Store(self, static_dir)
        self.apps = apps.Apps(self)
        self.cold = cold.Cold(self)
        self.coldfree = coldfree.ColdFree(self)
        self.http_port = None  # web port (set by __main__): local address of the cold copies
        for r in self.db.all("SELECT id, content_rating FROM items WHERE content_rating IS NOT NULL AND age IS NULL"):
            age = age_of(r["content_rating"])
            if age is not None:
                self.db.run("UPDATE items SET age=? WHERE id=?", (age, r["id"]))
        i18n.set_server_default(i18n.default_language(self.db))
        if not self.db.setting("webhook_token"):
            self.db.set_setting("webhook_token", secrets.token_urlsafe(24))
        self.sub_locks = {}
        self.update_latest = None  # newer version found on GitHub (indicator for administrators)
        self.update_checked = 0
        self.setup_code = None
        if not self.db.scalar("SELECT COUNT(*) FROM users"):
            self.setup_code = secrets.token_hex(4).upper()
            path = os.path.join(data_dir, "setup-code.txt")
            with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                f.write(self.setup_code + "\n")
            log.warning("Premier lancement : code d'installation = %s (aussi dans %s)", self.setup_code, path)
        if not media.available():
            log.error("ffmpeg/ffprobe introuvables : installe-les (apt install ffmpeg)")
        self.db.release()

    def note_latest(self, latest):
        self.update_latest = latest if latest and update.newer(latest, __version__) else None

    def check_update(self):
        """Background check (every 6 h): the indicator in the top bar."""
        try:
            self.note_latest(update.check_latest()["latest"])
        except update.UpdateError as e:
            log.debug("Vérification de mise à jour : %s", e)

    def setting_int(self, key, default):
        try:
            return int(self.db.setting(key, default))
        except (TypeError, ValueError):
            return default

    def limits(self):
        cpus = os.cpu_count() or 2
        return (self.setting_int("max_streams", 6),
                self.setting_int("max_transcodes", 1 if cpus <= 2 else 2))

    def viewing(self):
        """Is anyone watching right now (playback reported less than 40 s ago)?"""
        try:
            return bool(self.db.scalar("SELECT 1 FROM streams WHERE last_seen > ? LIMIT 1", (int(time.time()) - 40,)))
        finally:
            self.db.release()

    def hw(self):
        """Hardware transcoding chosen by the administrator, or None (processor)."""
        engine = self.db.setting("hw_engine", "none")
        if engine not in ("nvenc", "vaapi"):
            return None
        return {"engine": engine, "device": self.db.setting("hw_device", "/dev/dri/renderD128")}

    def on_library_changed(self):
        try:
            self.fed.relink_all()  # new titles: merged with those of the linked servers
        except Exception:
            log.exception("Fusion avec les serveurs liés")
        try:
            self.tasks.after_scan(self.scanner.status().get("added", 0))
        except Exception:
            log.exception("Tâches après le scan")
        try:
            if self.scanner.status().get("added", 0):
                self.push.after_scan(self.x)  # new episodes of followed shows: push notification
        except Exception:
            log.exception("Notifications push après le scan")

    def prefetch_missing_subtitles(self):
        """At start-up: queues the files whose internal subtitles are not ready yet."""
        if self.db.setting("subs_prefetch", "1") != "1" and self.db.setting("trickplay", "1") != "1":
            return
        n = 0
        for r in self.db.all("SELECT id, key, probe, file_size, file_mtime, duration_ms FROM items WHERE type IN ('movie','episode') "
                             "AND peer_id IS NULL ORDER BY added_at DESC"):
            if self.subtitles.has_work(r):
                self.subtitles.enqueue(r["id"])
                n += 1
        if n:
            log.info("%d fichier(s) en file pour la préparation des sous-titres", n)
        self.db.release()

    def on_new_media(self, item_id, info):
        """New file (or replaced file): its subtitles are exported right after the scan, in the background."""
        if (self.db.setting("subs_prefetch", "1") == "1" and (text_tracks(info) or pgs_tracks(info))) or \
                (self.db.setting("trickplay", "1") == "1" and info.get("video")):
            self.subtitles.enqueue(item_id)


def build_app(ctx):
    """Assembles the application: common access checks, shared catalogue, then the routes by topic."""
    x = ctx.x = types.SimpleNamespace()  # tools shared by the route modules (ctx.x: for background tasks)
    app = App()
    app.static_dir = ctx.static_dir
    db = ctx.db
    app.teardown.append(db.release)
    # language of the messages: first the interface's (header), refined by the account once known
    app.begin = lambda req: i18n.set_lang((req.headers.get("X-Lang") or "").strip())
    app.translate = i18n.tr
    app.default_lang = lambda: i18n.default_language(db)
    app.lang_auto = lambda: not i18n.configured_language(db)

    def setup_allowed(req):
        if req.user and req.user["is_admin"]:
            return True
        code = req.headers.get("X-Setup-Code") or ""
        return bool(ctx.setup_code) and hmac.compare_digest(code.strip().upper(), ctx.setup_code)

    def before(req, opts):
        auth = opts.get("auth", "user")
        if auth == "dlna":
            # DLNA server: no account (TVs have none), only when it is turned on and from the
            # local network
            if not ctx.dlna.enabled():
                raise HTTPError(404, "Introuvable")
            if not dlna.is_local(req.client_ip):
                raise HTTPError(403, "Réservé au réseau local")
            return
        if auth == "webhook":
            token = req.arg("token") or ""
            if not hmac.compare_digest(token, db.setting("webhook_token") or "\0"):
                raise HTTPError(403, "Jeton invalide")
            return
        # routes between linked servers: only on their port, by a paired server (or with an invitation)
        if auth == "peer" and not req.fed_peer or auth == "join" and not req.fed_listener:
            raise HTTPError(404, "Introuvable")
        if auth in ("peer", "join"):
            return
        if req.fed_peer:
            # request from a linked server on behalf of an account (the same one, replicated): playing an item of this
            # server from the other one. No cookie or anti-CSRF header: the request is signed by the server.
            remote_admin = federation.PEER_ADMIN_PATHS.match(req.path)
            if not (federation.PEER_USER_PATHS.match(req.path) or remote_admin):
                raise HTTPError(403, "Non autorisé entre serveurs liés")
            if remote_admin and db.setting("fed_remote_admin", "1") != "1":
                raise HTTPError(403, "Ce serveur n'accepte pas d'être administré depuis un serveur lié")
            req.user = ctx.fed.user_for(req)
            if remote_admin and req.user:
                log.info("Administration depuis %s par %s : %s %s", req.fed_peer["name"], req.user["username"],
                         req.method, req.path)
        else:
            # CSRF protection: another site cannot add this header without the server's consent (CORS)
            # (X-Seance: the same header under Noxa's former internal name, still sent by apps older than 4.5)
            if req.method != "GET" and "1" not in (req.headers.get("X-Noxa"), req.headers.get("X-Seance")):
                raise HTTPError(403, "En-tête de requête manquant")
            req.user = load_user(db, req)
        i18n.set_lang(i18n.request_language(req, db))
        if auth == "user" and not req.user:
            raise HTTPError(401, "Connexion requise")
        if auth == "admin" and not (req.user and req.user["is_admin"]):
            raise HTTPError(403 if req.user else 401, "Réservé aux administrateurs")
        if auth == "setup":
            ctx.throttle.check("setup:" + req.client_ip)
            if not setup_allowed(req):
                ctx.throttle.fail("setup:" + req.client_ip)
                raise HTTPError(403, "Code d'installation invalide")
        if not req.fed_peer:
            # settings of a linked server picked in the server selector: the admin request is forwarded to it
            # X-Nexis-Server: the same header from pages cached before 4.5
            target = req.headers.get("X-Noxa-Server") or req.headers.get("X-Nexis-Server") or req.arg("_server")
            if target and req.path.startswith("/api/admin/") and not federation.LOCAL_ADMIN_ONLY.match(req.path):
                if not (req.user and req.user["is_admin"]):
                    raise HTTPError(403, "Réservé aux administrateurs")
                return ctx.fed.admin_relay(req, target)
            # item of a linked server: the request is relayed to it (or it is told)
            return ctx.fed.intercept(req)
    app.before.append(before)

    catalog.install(ctx, x)
    for module in (account, library, media_routes, admin, collections_api, discover, tasks, livetv, syncplay, suggest, notify, photos, dlna, remote, push,
                   opensubs, health, whisper, tracking, books, backup, themes, stats, apps, cold, coldfree, federation):
        module.register(app, ctx, x)
    return app


def janitor(ctx, stop_event):
    """Stops abandoned playback and purges sessions (the rest is in the scheduled tasks)."""
    while not stop_event.wait(30):
        db = ctx.db
        try:
            now = int(time.time())
            for sid in ctx.transcoder.idle(150):
                ctx.transcoder.stop(sid)
            db.run("DELETE FROM streams WHERE last_seen < ?", (now - 600,))
            db.run("DELETE FROM sessions WHERE expires_at < ?", (now,))
            ctx.live.reap()
            ctx.together.reap()
            ctx.downloads.reap()
            ctx.health.tick()  # alerts for administrators (every 5 min, in the background)
            if time.time() - ctx.update_checked > 6 * 3600:
                ctx.update_checked = time.time()
                threading.Thread(target=ctx.check_update, daemon=True).start()
        except Exception:
            log.exception("Erreur du nettoyage périodique")
        finally:
            db.release()
