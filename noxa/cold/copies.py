"""Cold storage: the stores, the copies of a title, and titles brought back to the local cache."""
import hashlib
import json
import os
import secrets
import shutil
import threading
import time

from .. import env, i18n, media, mountd
from .storage import BACKENDS, ColdError, TOKEN_TTL, curl_sftp, log, rclone_exe


class CopiesMixin:
    # ---------------------------------------------------------------- stockages
    def stores(self, enabled_only=False):
        rows = self.ctx.db.all("SELECT * FROM cold_stores" + (" WHERE enabled=1" if enabled_only else "")
                               + " ORDER BY position, id")
        for r in rows:
            r["conf"] = json.loads(r["conf"] or "{}")
        return rows

    def store(self, sid):
        r = self.ctx.db.one("SELECT * FROM cold_stores WHERE id=?", (sid,))
        if r:
            r["conf"] = json.loads(r["conf"] or "{}")
        return r

    def backend(self, store):
        cls = BACKENDS.get(store["kind"])
        if not cls:
            raise ColdError("Type de stockage inconnu")
        return cls(store["conf"], self)

    def available(self):
        """Types utilisables sur cette installation (et pourquoi pas)."""
        smb_ok = os.path.exists(env("MOUNTD_SOCK") or mountd.SOCK) and not env("DOCKER")
        return {"local": None,
                "smb": None if smb_ok else i18n.tr("Indisponible ici (Docker, ou service de montage absent) : montez le "
                                                   "partage sur la machine, puis choisissez « Dossier de cette machine »."),
                "sftp": None if curl_sftp() else i18n.tr("curl avec SFTP est nécessaire (apt install curl)."),
                "webdav": None, "s3": None,
                "rclone": None if rclone_exe() else i18n.tr("rclone n'est pas installé (apt install rclone, puis rclone config)."),
                "peer": None if self.ctx.db.scalar("SELECT 1 FROM peers LIMIT 1") else i18n.tr("Aucun serveur lié : reliez d'abord un autre serveur Noxa.")}

    # ---------------------------------------------------------------- copies
    def copies(self, item_id):
        return self.ctx.db.all(
            "SELECT c.*, s.name AS store_name, s.kind, s.enabled, s.position FROM cold_copies c "
            "JOIN cold_stores s ON s.id=c.store_id WHERE c.item_id=? ORDER BY s.position, s.id", (item_id,))

    def local_missing(self, row):
        key = row.get("key") if isinstance(row, dict) else row["key"]
        return bool(key) and not media.is_strm(key) and not os.path.isfile(key)

    def source(self, row, prefer_cold=False):
        """Ce que ffmpeg doit lire pour cet élément : son fichier, sinon sa copie ramenée en local (cache), sinon une
        copie froide (chemin d'un dossier, ou « cold:http://127.0.0.1:…/cold/<jeton> »). None si rien n'est lisible."""
        return self.resolve(row, prefer_cold)[0]

    def resolve(self, row, prefer_cold=False):
        """-> (source, description) : description (probe) propre à une copie compressée, None sinon (celle de la fiche)."""
        key = row["key"]
        if not prefer_cold and key and (media.is_strm(key) or os.path.isfile(key)):
            return key, None
        if not prefer_cold:
            cached = self.cached(row["id"])
            if cached:
                return cached, self._probe_of(row, self.ctx.db.scalar("SELECT probe FROM cold_cache WHERE item_id=?", (row["id"],)))
        copies = [c for c in self.copies(row["id"]) if c["enabled"] and c["verified_at"]]
        # dossiers (disque, partage monté) d'abord : lecture directe, la plus rapide
        copies.sort(key=lambda c: (c["kind"] not in ("local", "smb"), c["position"]))
        for c in copies:
            store = self.store(c["store_id"])
            try:
                b = self.backend(store)
                lp = b.local_path(c["name"])
            except ColdError:
                continue
            if lp is not None:
                if os.path.isfile(lp):
                    return lp, self._probe_of(row, c["probe"])
                continue
            return "cold:" + self.stream_url(c["store_id"], c["name"], c["size"]), self._probe_of(row, c["probe"])
        return (key, None) if key and os.path.isfile(key) else (None, None)

    @staticmethod
    def _probe_of(row, copy_probe):
        """Description d'une copie compressée, avec les sous-titres externes de la fiche (fichiers à part)."""
        if not copy_probe:
            return None
        p = json.loads(copy_probe) if isinstance(copy_probe, str) else dict(copy_probe)
        try:
            item = json.loads(row["probe"] or "{}") if isinstance(row["probe"], str) else (row["probe"] or {})
        except (KeyError, IndexError, TypeError, ValueError):
            item = {}
        p["external"] = item.get("external", [])
        return json.dumps(p)

    def stream_url(self, store_id, name, size):
        now = time.time()
        with self.lock:
            for t in [t for t, v in self.tokens.items() if v["expires"] < now]:
                del self.tokens[t]
            tok = secrets.token_urlsafe(24)
            self.tokens[tok] = {"store": store_id, "name": name, "size": size, "expires": now + TOKEN_TTL}
        port = getattr(self.ctx, "http_port", None) or int(env("PORT", "8420"))
        return f"http://127.0.0.1:{port}/cold/{tok}"

    def token(self, tok):
        with self.lock:
            v = self.tokens.get(tok)
        return v if v and v["expires"] >= time.time() else None


    # ---------------------------------------------------------------- cache local (titres ramenés)
    def cache_conf(self):
        db = self.ctx.db
        return {"gb": float(db.setting("cold_cache_gb", "20") or 0), "keep_free_gb": float(db.setting("cold_cache_free_gb", "10") or 0),
                "next": int(db.setting("cold_prefetch", "2") or 0)}

    def cache_dir(self):
        return os.path.join(self.ctx.data_dir, "cold-cache")

    def cached(self, item_id):
        r = self.ctx.db.one("SELECT * FROM cold_cache WHERE item_id=?", (item_id,))
        if not r:
            return None
        if not os.path.isfile(r["path"]) or os.path.getsize(r["path"]) != r["size"]:
            self.ctx.db.run("DELETE FROM cold_cache WHERE item_id=?", (item_id,))
            return None
        self.ctx.db.run("UPDATE cold_cache SET last_used=? WHERE item_id=?", (int(time.time()), item_id))
        return r["path"]

    def prefetch(self, row):
        """Lecture depuis un stockage en ligne : ce titre, et les épisodes suivants de la série, ramenés en local."""
        c = self.cache_conf()
        if c["gb"] <= 0:
            return 0
        db = self.ctx.db
        ids = [row["id"]]
        if row.get("type") == "episode" and row.get("show_id") and c["next"] > 0:
            eps = db.all("SELECT e.id FROM items e LEFT JOIN items s ON s.id=e.parent_id WHERE e.show_id=? AND e.type='episode' "
                         "ORDER BY COALESCE(s.idx, 0)=0, s.idx, e.idx", (row["show_id"],))
            order = [e["id"] for e in eps]
            if row["id"] in order:
                ids += order[order.index(row["id"]) + 1:order.index(row["id"]) + 1 + c["next"]]
        added = 0
        with self.lock:
            for i in ids:
                if i not in self.fetch_queue and i != self.fetching:
                    self.fetch_queue.append(i)
                    added += 1
            if self.fetch_queue and not (self.fetcher and self.fetcher.is_alive()):
                self.fetcher = threading.Thread(target=self._fetch_loop, daemon=True, name="cold-cache")
                self.fetcher.start()
        return added

    def _fetch_loop(self):
        db = self.ctx.db
        try:
            while True:
                with self.lock:
                    if not self.fetch_queue:
                        self.fetching = None
                        return
                    self.fetching = self.fetch_queue.pop(0)
                try:
                    self._fetch(self.fetching)
                except Exception as e:
                    log.info("Titre %s non ramené en local : %s", self.fetching, e)
        finally:
            db.release()

    def _fetch(self, item_id):
        db = self.ctx.db
        row = db.one("SELECT id, key FROM items WHERE id=?", (item_id,))
        if not row or not self.local_missing(row) or self.cached(item_id):
            return
        # seulement les copies en ligne : un dossier ou un partage monté se lit déjà directement
        copies = [cp for cp in self.copies(item_id) if cp["enabled"] and cp["verified_at"] and cp["kind"] not in ("local", "smb")]
        if not copies:
            return
        cp = copies[0]
        if not self._make_room(cp["size"]):
            log.info("Pas de place pour ramener %s en local", cp["name"])
            return
        b = self.backend(self.store(cp["store_id"]))
        os.makedirs(self.cache_dir(), exist_ok=True)
        path = os.path.join(self.cache_dir(), f"{item_id}{os.path.splitext(cp['name'])[1] or '.mkv'}")
        tmp = path + ".part"
        sha = hashlib.sha256()
        try:
            with open(tmp, "wb") as out:
                for blk in b.open(cp["name"], 0, cp["size"] - 1):
                    out.write(blk)
                    sha.update(blk)
            if os.path.getsize(tmp) != cp["size"] or (cp["sha256"] and sha.hexdigest() != cp["sha256"]):
                raise ColdError("Copie ramenée incomplète ou altérée")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        now = int(time.time())
        db.run("INSERT OR REPLACE INTO cold_cache(item_id, path, size, fetched_at, last_used, probe) VALUES(?,?,?,?,?,?)",
               (item_id, path, cp["size"], now, now, cp["probe"]))

    def _make_room(self, need):
        """Place pour `need` octets dans le budget du cache (et en gardant de la place libre sur le disque) :
        les titres ramenés depuis le plus longtemps sans lecture repartent d'abord."""
        c = self.cache_conf()
        db = self.ctx.db
        budget = c["gb"] * 1e9
        if need > budget:
            return False
        playing = {int(r["rating_key"]) for r in db.all("SELECT rating_key FROM streams WHERE last_seen>?", (int(time.time()) - 300,))
                   if str(r["rating_key"]).isdigit()}
        rows = db.all("SELECT * FROM cold_cache ORDER BY last_used")
        used = sum(r["size"] for r in rows)
        os.makedirs(self.cache_dir(), exist_ok=True)

        def free_disk():
            try:
                return shutil.disk_usage(self.cache_dir()).free
            except OSError:
                return 0
        for r in rows:
            if used + need <= budget and free_disk() - need >= c["keep_free_gb"] * 1e9:
                break
            if r["item_id"] in playing:
                continue
            self.uncache(r["item_id"])
            used -= r["size"]
        return used + need <= budget and free_disk() - need >= c["keep_free_gb"] * 1e9

    def uncache(self, item_id):
        r = self.ctx.db.one("SELECT path FROM cold_cache WHERE item_id=?", (item_id,))
        if r:
            try:
                os.remove(r["path"])
            except OSError:
                pass
            self.ctx.db.run("DELETE FROM cold_cache WHERE item_id=?", (item_id,))

    def cache_status(self):
        r = self.ctx.db.one("SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM cold_cache")
        return {**self.cache_conf(), "count": r["n"], "bytes": r["bytes"], "fetching": self.fetching, "queued": len(self.fetch_queue)}
