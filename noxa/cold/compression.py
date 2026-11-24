"""Cold storage: archives hosted for linked servers, and HEVC compression of the archives."""
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import threading
import time

from .. import i18n, media
from .storage import ColdError, MIN_GAIN, QUALITY, SAMPLE, SAMPLES, _Reader, _file_blocks, compress_cmd, log


class CompressionMixin:
    # ---------------------------------------------------------------- archives d'autres serveurs (hébergées ici)
    def hosted_dir(self, peer_id):
        return os.path.join(self.ctx.data_dir, "cold-hosted", str(int(peer_id)))

    def hosted_used(self):
        root = os.path.join(self.ctx.data_dir, "cold-hosted")
        total = 0
        for d, _, files in os.walk(root):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(d, f))
                except OSError:
                    pass
        return total

    def hosted_quota(self):
        return float(self.ctx.db.setting("cold_host_gb", "0") or 0) * 1e9


    # ---------------------------------------------------------------- compression des archives (HEVC)
    def compress_conf(self):
        db = self.ctx.db
        q = db.setting("cold_compress_quality", "balanced")
        return {"enabled": db.setting("cold_compress", "0") == "1", "quality": q if q in QUALITY else "balanced",
                "height": int(db.setting("cold_compress_height", "0") or 0)}

    @staticmethod
    def compressible(probe):
        """(oui ?, raison) : vidéo déjà compacte (HEVC, AV1, VP9) ou HDR (gardée telle quelle pour ne rien abîmer)."""
        from ..transcoder import HDR_TRANSFERS
        v = (probe or {}).get("video")
        if not v:
            return False, i18n.tr("pas de vidéo")
        if v.get("codec") in ("hevc", "av1", "vp9"):
            return False, i18n.tr("déjà en {codec}").format(codec=v["codec"].upper())
        if v.get("transfer") in HDR_TRANSFERS or "dovi" in (v.get("profile") or "").lower():
            return False, i18n.tr("vidéo HDR, gardée telle quelle")
        return True, None

    def _viewers_converting(self):
        try:
            return bool(self.ctx.db.scalar("SELECT 1 FROM streams WHERE last_seen>? AND json_extract(params, '$.mode')='transcode' LIMIT 1",
                                           (int(time.time()) - 40,)))
        except Exception:
            return False

    def compress(self, src, row, job):
        """Réencode la vidéo en HEVC (carte graphique si configurée, sinon processeur), sons, sous-titres, chapitres et
        polices recopiés tels quels. -> (fichier, description) ; None si ce n'est pas utile (raison dans job["note"])."""
        probe = json.loads(row["probe"] or "{}") if isinstance(row["probe"], str) else (row["probe"] or {})
        ok, why = self.compressible(probe)
        if not ok:
            job["note"] = i18n.tr("Sans compression : {why}").format(why=why)
            return None
        work = os.path.join(self.ctx.data_dir, "cold-work")
        os.makedirs(work, exist_ok=True)
        need = (row["file_size"] or 0) + 2 * 1024 ** 3
        if shutil.disk_usage(work).free < need:
            job["note"] = i18n.tr("Sans compression : pas assez de place pour le fichier de travail ({n} Go)").format(n=round(need / 1e9, 1))
            return None
        c = self.compress_conf()
        hw = self.ctx.hw() if hasattr(self.ctx, "hw") else None
        out = os.path.join(work, f"{row['id']}-{secrets.token_hex(4)}.mkv")
        cmd = compress_cmd(src, out, probe, hw, c["quality"], c["height"])
        job.update(phase="compress", pct=0, hw=bool(hw))
        duration = float(probe.get("duration") or 0) or 1
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                 preexec_fn=lambda: os.nice(19))
        except OSError as e:
            job["note"] = i18n.tr("Sans compression : {why}").format(why=e)
            return None
        err = []
        threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True).start()
        stop = threading.Event()

        def watch():
            # annulation ; et par le processeur, pause pendant qu'une lecture est convertie (elle passe d'abord)
            paused = False
            while not stop.wait(2):
                if job.get("cancel"):
                    p.kill()
                    return
                busy = not hw and self._viewers_converting()
                if busy != paused:
                    try:
                        os.kill(p.pid, 19 if busy else 18)  # SIGSTOP / SIGCONT
                        paused = busy
                        job["paused"] = busy
                    except OSError:
                        return
        threading.Thread(target=watch, daemon=True).start()
        try:
            for line in p.stdout:
                m = re.match(rb"out_time_us=(\d+)", line)
                if m:
                    job["pct"] = min(1.0, int(m.group(1)) / 1e6 / duration)
            p.wait()
        finally:
            stop.set()
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()
        job["phase"] = "copy"
        if job.get("cancel"):
            self._rm(out)
            raise ColdError("Archivage annulé")
        if p.returncode != 0 or not os.path.isfile(out):
            msg = (err[0] if err else b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["?"]
            log.warning("Compression de %s impossible : %s", row["key"], msg[0])
            job["note"] = i18n.tr("Sans compression : {why}").format(why=msg[0][:200])
            self._rm(out)
            return None
        src_size = row["file_size"] or 0
        if src_size and os.path.getsize(out) > MIN_GAIN * src_size:
            job["note"] = i18n.tr("Sans compression : gain trop faible")
            self._rm(out)
            return None
        try:
            new = media.probe(out)
        except media.MediaError as e:
            self._rm(out)
            job["note"] = i18n.tr("Sans compression : {why}").format(why=e)
            return None
        return out, new

    @staticmethod
    def _rm(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def recompress(self, item_id, store_id, job):
        """Compresse une copie déjà archivée : relue depuis le stockage, réencodée, renvoyée, l'ancienne effacée."""
        db = self.ctx.db
        c = db.one("SELECT * FROM cold_copies WHERE item_id=? AND store_id=?", (item_id, store_id))
        row = db.one("SELECT id, key, library_id, file_size, probe FROM items WHERE id=?", (item_id,))
        if not c or not row or c["compressed"]:
            return None
        b = self.backend(self.store(store_id))
        lp = b.local_path(c["name"])
        src = lp if lp else "cold:" + self.stream_url(store_id, c["name"], c["size"])
        res = self.compress(src, {**dict(row), "file_size": c["size"]}, job)
        if not res:
            return None
        tmp, probe = res
        try:
            name = os.path.splitext(c["name"])[0] + ".mkv"
            if name == c["name"]:
                name = os.path.splitext(c["name"])[0] + ".hevc.mkv"
            size, sha = self._put_verified(b, name, tmp, job)
            now = int(time.time())
            db.run("UPDATE cold_copies SET name=?, size=?, sha256=?, compressed=1, orig_size=?, probe=?, verified_at=? WHERE id=?",
                   (name, size, sha, c["orig_size"] or c["size"], json.dumps(probe), now, c["id"]))
            try:
                b.delete(c["name"])
            except ColdError as e:
                log.info("Ancienne copie %s non effacée : %s", c["name"], e)
            self.uncache(item_id)
            return name
        finally:
            self._rm(tmp)

    def _put_verified(self, b, name, path, job):
        size = os.path.getsize(path)
        job["total"], job["done"] = size, 0
        reader = _Reader(_file_blocks(path), job, self)
        b.put(name, reader, size)
        if reader.done != size or b.size(name) != size:
            raise ColdError("Copie incomplète sur le stockage")
        for off in sorted({0, max(0, size - SAMPLE)} | {random.randrange(0, max(1, size - SAMPLE)) for _ in range(SAMPLES)}):
            end = min(size - 1, off + SAMPLE - 1)
            if b"".join(b.open(name, off, end)) != b"".join(_file_blocks(path, off, end)):
                raise ColdError("La copie relue ne correspond pas au fichier")
        return size, reader.sha.hexdigest()

    def queue_compress(self, store_id=None):
        """Compresser les archives déjà faites (celles qui en valent la peine)."""
        db = self.ctx.db
        now = int(time.time())
        n = 0
        for c in db.all("SELECT c.item_id, c.store_id, i.probe FROM cold_copies c JOIN items i ON i.id=c.item_id "
                        "WHERE c.compressed=0 AND c.verified_at IS NOT NULL" + (" AND c.store_id=?" if store_id else ""),
                        (store_id,) if store_id else ()):
            if not self.compressible(json.loads(c["probe"] or "{}"))[0]:
                continue
            db.run("INSERT INTO cold_jobs(item_id, store_id, state, kind, created_at, updated_at) VALUES(?,?,'queued','compress',?,?) "
                   "ON CONFLICT(item_id, store_id) DO UPDATE SET state='queued', kind='compress', error=NULL, updated_at=excluded.updated_at",
                   (c["item_id"], c["store_id"], now, now))
            n += 1
        if n:
            self.start()
        return n
