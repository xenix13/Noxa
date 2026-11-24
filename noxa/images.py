"""Item pictures: TMDB or local files, otherwise a frame of the video. Resized and cached."""
import hashlib
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from . import media, photos
from .metadata import TMDB_IMG

log = logging.getLogger("noxa.images")
_dl_lock = threading.Lock()
# A picture that cannot be found (no poster, frame impossible, linked server or TMDB unreachable) is not looked for
# again at each display: without this, each return to the home page started dozens of downloads
# and ffmpeg frame grabs that kept the browser's connections busy for long minutes.
MISSING_TTL = 1800      # picture missing here: asked for again at most every 30 min
PEER_MISSING_TTL = 300  # picture from a linked server: 5 min
TMDB_PAUSE = 120        # TMDB unreachable: no download for 2 min
MAX_MISSING = 50000


class Images:
    def __init__(self, ctx):
        self.ctx = ctx
        self.cache = os.path.join(ctx.data_dir, "cache", "img")
        self.orig = os.path.join(ctx.data_dir, "cache", "orig")
        os.makedirs(self.cache, exist_ok=True)
        os.makedirs(self.orig, exist_ok=True)
        self._missing = {}  # key -> instant until which it is not looked for anymore
        self._missing_lock = threading.Lock()
        self._tmdb_down_until = 0

    def _is_missing(self, key):
        with self._missing_lock:
            until = self._missing.get(key)
            if until and until > time.time():
                return True
            self._missing.pop(key, None)
            return False

    def _mark_missing(self, key, ttl):
        with self._missing_lock:
            if len(self._missing) >= MAX_MISSING:
                now = time.time()
                self._missing = {k: v for k, v in self._missing.items() if v > now}
                if len(self._missing) >= MAX_MISSING:
                    self._missing.clear()
            self._missing[key] = time.time() + ttl

    def forget_missing(self):
        """After a change of pictures (metadata refreshed, linked server back): everything is asked for again."""
        with self._missing_lock:
            self._missing.clear()

    def _download(self, tmdb_path, kind):
        name = "tmdb_" + tmdb_path.strip("/").replace("/", "_")
        dest = os.path.join(self.orig, name)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return dest
        if time.time() < self._tmdb_down_until:
            raise OSError("TMDB injoignable pour le moment")
        size = "w1280" if kind == "backdrop" else ("w780" if kind == "poster" else "w780")
        url = f"{TMDB_IMG}/{size}{tmdb_path}"
        req = urllib.request.Request(url, headers={"User-Agent": "Seance/2"})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                data = r.read(15 * 1024 * 1024)
        except urllib.error.HTTPError:
            raise  # picture missing at TMDB: only this one is concerned
        except (OSError, ValueError):
            self._tmdb_down_until = time.time() + TMDB_PAUSE  # network down, DNS, timeout: wait a little
            raise
        tmp = dest + f".{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return dest

    def _source(self, spec, kind):
        if not spec:
            return None
        if spec.startswith("local:"):
            p = spec[6:]
            return p if os.path.isfile(p) else None
        if spec.startswith("tmdb:"):
            try:
                return self._download(spec[5:], kind)
            except Exception as e:
                log.debug("Image TMDB %s : %s", spec, e)
                return None
        return None

    def _first_episode(self, show_id):
        return self.ctx.db.one("SELECT e.* FROM items e JOIN items s ON s.id=e.parent_id WHERE e.show_id=? AND "
                               "e.type='episode' ORDER BY s.idx=0, s.idx, e.idx LIMIT 1", (show_id,))

    def resolve(self, item, kind):
        """-> ('file', path) or ('frame', video file, instant) or None."""
        db = self.ctx.db
        t = item["type"]
        show = db.one("SELECT * FROM items WHERE id=?", (item["show_id"],)) if item["show_id"] else None
        if kind == "thumb":
            chain = []
            if t in ("movie", "show"):
                chain = [(item["poster"], "poster")]
            elif t == "season":
                chain = [(item["poster"], "poster"), (show and show["poster"], "poster")]
            elif t == "episode":
                chain = [(item["still"], "still")]
        else:
            chain = [(item["backdrop"], "backdrop")] if t in ("movie", "show") else [(show and show["backdrop"], "backdrop")]
        for spec, k in chain:
            src = self._source(spec, k)
            if src:
                return ("file", src)
        # fallback: a frame of the video
        video = None
        if t in ("movie", "episode", "extra"):
            video = item
        elif t in ("show", "season"):
            video = self._first_episode(item["show_id"] or item["id"])
        if video and video["key"] and os.path.isfile(video["key"]) and not media.is_strm(video["key"]):
            dur = (video["duration_ms"] or 0) / 1000
            return ("frame", video["key"], max(1.0, dur * (0.2 if t == "movie" else 0.15)) if dur else 10.0)
        return None

    def get_collection(self, coll, kind, w, h):
        spec = coll["poster"] if kind == "thumb" else coll["backdrop"]
        src = self._source(spec, "poster" if kind == "thumb" else "backdrop")
        if not src:
            # no picture of its own: the one of the collection's first item
            first = self.ctx.db.one("SELECT i.* FROM collection_items c JOIN items i ON i.id=c.item_id "
                                    "WHERE c.collection_id=? ORDER BY i.year, i.sort_title LIMIT 1", (coll["id"],))
            return self.get(first, kind, w, h) if first else None
        tag = hashlib.sha1(f"coll{coll['id']}|{kind}|{coll['img_version']}|{src}|{w}|{h}".encode()).hexdigest()
        dest = os.path.join(self.cache, tag + ".jpg")
        if not os.path.isfile(dest):
            try:
                media.resize_image(src, dest, w, h)
            except media.MediaError:
                return None
        return dest

    def get(self, item, kind, w, h):
        # name of the picture asked for: changes with the metadata (new poster), so a picture
        # not found yesterday is looked for again as soon as a source appears
        key = (f"{item.get('peer_id') or ''}|{item['id']}|{kind}|{item.get('img_version')}|{item.get('poster')}|"
               f"{item.get('backdrop')}|{item.get('still')}|{w}|{h}")
        if self._is_missing(key):
            return None
        path = self._get(item, kind, w, h)
        if not path:
            self._mark_missing(key, PEER_MISSING_TTL if item.get("peer_id") else MISSING_TTL)
        return path

    def _get(self, item, kind, w, h):
        if item.get("peer_id"):  # item of a linked server: its picture comes from it
            return self.ctx.fed.image(item, kind, w, h)
        if item["type"] in ("photo", "album"):  # photo library: the photo itself (for the album: the most recent)
            photo = item if item["type"] == "photo" else photos.cover(self.ctx.db, item["id"])
            return photos.render(self.ctx, photo, w, h, crop=True) if photo else None
        if item["type"] == "book":  # audiobook: its cover (file next to it, or embedded picture)
            from . import books
            return books.cover_path(self.ctx, item, w, h)
        src = self.resolve(item, kind)
        if not src:
            return None
        tag = hashlib.sha1(f"{item['id']}|{kind}|{item['img_version']}|{src}|{w}|{h}".encode()).hexdigest()
        dest = os.path.join(self.cache, tag + ".jpg")
        if os.path.isfile(dest):
            return dest
        try:
            if src[0] == "file":
                media.resize_image(src[1], dest, w, h)
            else:
                media.grab_frame(src[1], dest, src[2], w, h)
        except media.MediaError as e:
            log.debug("Image %s/%s : %s", item["id"], kind, e)
            return None
        return dest
