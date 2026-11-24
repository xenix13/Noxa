"""Cold storage: bandwidth, names on the store, the archiving queue, one copy, and the admin status."""
import json
import os
import random
import threading
import time

from .. import i18n, media
from .storage import ColdError, SAMPLE, SAMPLES, SIDECAR_EXT, _BytesReader, _Reader, _file_blocks, log, safe_name


class JobsMixin:
    # ---------------------------------------------------------------- rate
    def rate_limit(self):
        """Bytes per second allowed for a copy (0: no limit). Lower during a playback."""
        db = self.ctx.db
        try:
            playing = db.scalar("SELECT COUNT(*) FROM streams WHERE last_seen>?", (int(time.time()) - 90,))
        except Exception:
            playing = 0
        mb = float(db.setting("cold_rate_play" if playing else "cold_rate", "10" if playing else "0") or 0)
        return int(mb * 1024 * 1024)

    # ---------------------------------------------------------------- names on the storage
    def name_for(self, row, store_id):
        db = self.ctx.db
        lib = db.one("SELECT name, paths FROM libraries WHERE id=?", (row["library_id"],)) or {"name": "Noxa", "paths": "[]"}
        key = row["key"]
        rel = os.path.basename(key)
        for root in json.loads(lib["paths"] or "[]"):
            if root and key.startswith(os.path.join(root, "")):
                rel = os.path.relpath(key, root)
                break
        name = safe_name(f"{lib['name']}/{rel}")
        taken = db.scalar("SELECT item_id FROM cold_copies WHERE store_id=? AND name=?", (store_id, name))
        if taken and taken != row["id"]:
            stem, ext = os.path.splitext(name)
            name = f"{stem} ({row['id']}){ext}"
        return name

    @staticmethod
    def sidecars(key):
        """Subtitles placed next to the file (same name): copied with it."""
        d, base = os.path.split(key)
        stem = os.path.splitext(base)[0]
        try:
            names = os.listdir(d)
        except OSError:
            return []
        return sorted(os.path.join(d, n) for n in names
                      if n != base and n.startswith(stem) and n.lower().endswith(SIDECAR_EXT) and os.path.isfile(os.path.join(d, n)))

    # ---------------------------------------------------------------- file d'archivage
    def queue(self, item_ids, store_id):
        db = self.ctx.db
        if not self.store(store_id):
            raise ColdError("Stockage inconnu")
        ids = set()
        for i in item_ids:
            r = db.one("SELECT id, type FROM items WHERE id=? AND peer_id IS NULL", (int(i),))
            if not r:
                continue
            if r["type"] in ("movie", "episode", "extra"):
                ids.add(r["id"])
            elif r["type"] in ("show", "season"):
                col = "show_id" if r["type"] == "show" else "parent_id"
                ids.update(x["id"] for x in db.all(f"SELECT id FROM items WHERE {col}=? AND type='episode' AND peer_id IS NULL",
                                                   (r["id"],)))
        now = int(time.time())
        added = 0
        for i in sorted(ids):
            if db.scalar("SELECT 1 FROM cold_copies WHERE item_id=? AND store_id=?", (i, store_id)):
                continue
            db.run("INSERT INTO cold_jobs(item_id, store_id, state, created_at, updated_at) VALUES(?,?,'queued',?,?) "
                   "ON CONFLICT(item_id, store_id) DO UPDATE SET state='queued', error=NULL, updated_at=excluded.updated_at",
                   (i, store_id, now, now))
            added += 1
        if added:
            self.start()
        return added

    def start(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                self.wake.set()
                return
            self.worker = threading.Thread(target=self._loop, daemon=True, name="cold")
            self.worker.start()

    def cancel(self, job_id):
        db = self.ctx.db
        if self.running and self.running.get("id") == job_id:
            self.running["cancel"] = True
        db.run("DELETE FROM cold_jobs WHERE id=? AND state!='running'", (job_id,))

    def _loop(self):
        db = self.ctx.db
        try:
            db.run("UPDATE cold_jobs SET state='queued' WHERE state='running'")  # Noxa stopped during a copy
            while True:
                job = db.one("SELECT * FROM cold_jobs WHERE state='queued' ORDER BY created_at, id LIMIT 1")
                if not job:
                    break
                db.run("UPDATE cold_jobs SET state='running', updated_at=? WHERE id=?", (int(time.time()), job["id"]))
                self.running = {"id": job["id"], "item_id": job["item_id"], "store_id": job["store_id"], "done": 0,
                                "total": 0, "cancel": False}
                try:
                    if job["kind"] == "compress":
                        self.recompress(job["item_id"], job["store_id"], self.running)
                    else:
                        self.archive(job["item_id"], job["store_id"], self.running)
                    if self.running.get("note"):
                        log.info("Archivage de l'élément %s : %s", job["item_id"], self.running["note"])
                    db.run("DELETE FROM cold_jobs WHERE id=?", (job["id"],))
                    free = getattr(self.ctx, "coldfree", None)
                    if free:
                        free.on_archived(job["item_id"])
                except Exception as e:
                    msg = str(e) if isinstance(e, (ColdError, OSError)) else i18n.tr("Erreur inattendue : {e}").format(e=e)
                    if not isinstance(e, (ColdError, OSError)):
                        log.exception("Archivage de l'élément %s", job["item_id"])
                    if self.running.get("cancel"):
                        db.run("DELETE FROM cold_jobs WHERE id=?", (job["id"],))
                    else:
                        log.warning("Archivage de l'élément %s : %s", job["item_id"], msg)
                        db.run("UPDATE cold_jobs SET state='error', error=?, updated_at=? WHERE id=?",
                               (msg[:500], int(time.time()), job["id"]))
                finally:
                    self.running = None
        finally:
            db.release()

    # ---------------------------------------------------------------- one copy
    def archive(self, item_id, store_id, job=None):
        """Copies the item's file (and its neighbouring subtitles) to the storage, then checks it."""
        job = job if job is not None else {"done": 0, "total": 0, "cancel": False}
        db = self.ctx.db
        row = db.one("SELECT id, key, library_id, file_size, probe FROM items WHERE id=?", (item_id,))
        store = self.store(store_id)
        if not row or not store:
            raise ColdError("Élément ou stockage introuvable")
        if media.is_strm(row["key"] or ""):
            raise ColdError("Un fichier .strm n'a rien à archiver")
        b = self.backend(store)
        name = self.name_for(row, store_id)
        meta = {"compressed": 0, "orig_size": None, "probe": None}
        # source: the library file, otherwise a copy already made on another storage
        if row["key"] and os.path.isfile(row["key"]):
            src = row["key"]
            size = os.path.getsize(row["key"])
            blocks = _file_blocks(row["key"])
            reread = lambda s, e: b"".join(_file_blocks(row["key"], s, e))
            sidecars = self.sidecars(row["key"])
        else:
            other = next((c for c in self.copies(item_id) if c["store_id"] != store_id and c["verified_at"]), None)
            if not other:
                raise ColdError("Fichier introuvable (partage non monté ?) et aucune autre copie")
            ob = self.backend(self.store(other["store_id"]))
            size = other["size"]
            olp = ob.local_path(other["name"])
            src = olp if olp else "cold:" + self.stream_url(other["store_id"], other["name"], size)
            blocks = ob.open(other["name"], 0, size - 1)
            reread = lambda s, e: b"".join(ob.open(other["name"], s, e))
            sidecars = []
            if other["compressed"]:
                meta = {"compressed": 1, "orig_size": other["orig_size"], "probe": other["probe"]}
                name = os.path.splitext(name)[0] + ".mkv"
        tmp = None
        if self.compress_conf()["enabled"] and not meta["compressed"]:
            res = self.compress(src, row, job)
            if res:
                tmp, probe = res
                orig = size
                size = os.path.getsize(tmp)
                blocks = _file_blocks(tmp)
                reread = lambda s, e: b"".join(_file_blocks(tmp, s, e))
                name = os.path.splitext(name)[0] + ".mkv"
                meta = {"compressed": 1, "orig_size": orig, "probe": json.dumps(probe)}
        try:
            return self._archive_put(b, name, size, blocks, reread, sidecars, job, item_id, store_id, meta)
        finally:
            if tmp:
                self._rm(tmp)

    def _archive_put(self, b, name, size, blocks, reread, sidecars, job, item_id, store_id, meta):
        db = self.ctx.db
        job["total"] = size
        reader = _Reader(blocks, job, self)
        b.put(name, reader, size)
        if reader.done != size:
            raise ColdError("Lecture du fichier interrompue")
        # check: size on the storage, and chunks read back (start, end, a few at random)
        if b.size(name) != size:
            raise ColdError("Copie incomplète sur le stockage")
        offsets = {0, max(0, size - SAMPLE)} | {random.randrange(0, max(1, size - SAMPLE)) for _ in range(SAMPLES)}
        for off in sorted(offsets):
            end = min(size - 1, off + SAMPLE - 1)
            if b"".join(b.open(name, off, end)) != reread(off, end):
                raise ColdError("La copie relue ne correspond pas au fichier")
        extras = []
        for sc in sidecars:
            try:
                with open(sc, "rb") as f:
                    data = f.read()
                sname = safe_name(os.path.dirname(name) + "/" + os.path.basename(sc)) if "/" in name else safe_name(os.path.basename(sc))
                b.put(sname, _BytesReader(data), len(data))
                extras.append({"name": sname, "file": os.path.basename(sc), "size": len(data)})
            except (OSError, ColdError) as e:
                log.info("Sous-titres %s non archivés : %s", sc, e)
        now = int(time.time())
        db.run("INSERT INTO cold_copies(item_id, store_id, name, size, sha256, extras, archived_at, verified_at, compressed, orig_size, probe) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id, store_id) DO UPDATE SET name=excluded.name, size=excluded.size, "
               "sha256=excluded.sha256, extras=excluded.extras, archived_at=excluded.archived_at, verified_at=excluded.verified_at, "
               "compressed=excluded.compressed, orig_size=excluded.orig_size, probe=excluded.probe",
               (item_id, store_id, name, size, reader.sha.hexdigest(), json.dumps(extras), now, now,
                meta["compressed"], meta["orig_size"], meta["probe"]))
        return name

    def forget(self, item_id, store_id, delete_file=True):
        """Removes a cold copy (and its file on the storage)."""
        c = self.ctx.db.one("SELECT * FROM cold_copies WHERE item_id=? AND store_id=?", (item_id, store_id))
        if not c:
            return False
        row = self.ctx.db.one("SELECT key FROM items WHERE id=?", (item_id,))
        others = self.ctx.db.scalar("SELECT COUNT(*) FROM cold_copies WHERE item_id=? AND store_id!=?", (item_id, store_id))
        if row and self.local_missing(row) and not others:
            raise ColdError("C'est la seule copie de ce titre : le fichier n'est plus dans la bibliothèque")
        if delete_file:
            b = self.backend(self.store(store_id))
            b.delete(c["name"])
            for x in json.loads(c["extras"] or "[]"):
                try:
                    b.delete(x["name"])
                except ColdError:
                    pass
        self.ctx.db.run("DELETE FROM cold_copies WHERE id=?", (c["id"],))
        return True

    # ---------------------------------------------------------------- status for the administration
    def status(self):
        db = self.ctx.db
        jobs = db.all("SELECT j.*, i.title, i.type, s.title AS show_title, i.idx, p.idx AS season FROM cold_jobs j "
                      "JOIN items i ON i.id=j.item_id LEFT JOIN items s ON s.id=i.show_id LEFT JOIN items p ON p.id=i.parent_id "
                      "ORDER BY j.state='running' DESC, j.state='error', j.created_at, j.id LIMIT 200")
        run = self.running
        out = []
        for j in jobs:
            label = (f"{j['show_title']} · S{j['season'] or 0}E{j['idx'] or 0} · {j['title']}" if j["type"] == "episode"
                     else j["title"])
            d = {"id": j["id"], "item_id": j["item_id"], "store_id": j["store_id"], "state": j["state"], "title": label,
                 "error": j["error"]}
            d["kind"] = j["kind"]
            if run and run.get("id") == j["id"]:
                d.update(done=run.get("done", 0), total=run.get("total", 0), phase=run.get("phase") or "copy",
                         pct=run.get("pct", 0), paused=bool(run.get("paused")), hw=run.get("hw"))
            out.append(d)
        counts = {r["store_id"]: r for r in db.all("SELECT store_id, COUNT(*) AS n, SUM(size) AS bytes FROM cold_copies GROUP BY store_id")}
        return {"jobs": out, "queued": db.scalar("SELECT COUNT(*) FROM cold_jobs WHERE state='queued'"), "counts": counts}
