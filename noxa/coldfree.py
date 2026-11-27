"""Freeing space: titles nobody has watched for N days move to cold storage, then their
file leaves the library. The title stays in Noxa (poster, overview, history) and plays from the copy.

Stages of a file (table cold_free, one row per movie or episode):
  proposed  -> proposed, waiting for an administrator's approval ("to approve" mode);
  approved  -> to archive: copy requested on the cold storage;
  pending   -> copy checked; the original will be deleted at delete_after (time to change one's mind);
  manual    -> to delete by hand (library with no way to delete);
  freed     -> original deleted: the title is only played from the cold storage.
A title watched again during the delay is not deleted. "Keep on the disk" (cold_keep) protects a
movie or a show for good.

Deleting the original, chosen per library:
  - arr    : through Radarr (movies) or Sonarr (shows) — Noxa keeps its read-only rights; the title is no longer
             monitored, otherwise they would download it again right away;
  - write  : Noxa erases the file itself (library mounted read-write);
  - manual : Noxa gives the list of files to erase.
"""
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

from . import i18n, media

log = logging.getLogger("noxa.coldfree")

DAY = 86400
METHODS = ("arr", "write", "manual")
DEFAULTS = {"enabled": False, "days": 30, "mode": "review", "store": None, "libraries": [], "keep_watchlist": True,
            "keep_rated": True, "keep_started": True, "min_free_gb": 0, "delay_days": 7, "methods": {},
            "radarr_url": "", "radarr_key": "", "sonarr_url": "", "sonarr_key": ""}
SECRETS = ("radarr_key", "sonarr_key")


class FreeError(Exception):
    pass


# ====================================================================== Radarr / Sonarr
class Arr:
    """Radarr / Sonarr API v3 (key in X-Api-Key)."""

    def __init__(self, url, key, what):
        self.url, self.key, self.what = (url or "").rstrip("/"), key or "", what

    def call(self, method, path, body=None, timeout=30):
        if not self.url or not self.key:
            raise FreeError(i18n.tr("{what} n'est pas configuré (adresse et clé d'API)").format(what=self.what))
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"X-Api-Key": self.key, "Accept": "application/json",
                                              **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read(64 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise FreeError(i18n.tr("{what} refuse la clé d'API").format(what=self.what))
            raise FreeError(f"{self.what} : HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            raise FreeError(i18n.tr("{what} injoignable : {e}").format(what=self.what, e=getattr(e, "reason", e)))
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise FreeError(i18n.tr("Réponse inattendue de {what}").format(what=self.what))

    def check(self):
        st = self.call("GET", "/api/v3/system/status")
        return {"version": (st or {}).get("version"), "app": (st or {}).get("appName") or self.what}


def _same_file(ours, theirs, our_size, their_size):
    """Is the file known to Radarr / Sonarr really ours? (same name; same size when known)"""
    if os.path.basename(ours or "") != os.path.basename((theirs or "").replace("\\", "/")):
        return False
    return not (our_size and their_size and int(our_size) != int(their_size))


def radarr_delete(arr, row):
    movies = arr.call("GET", f"/api/v3/movie?tmdbId={int(row['tmdb_id'])}") if row.get("tmdb_id") else []
    movie = movies[0] if movies else None
    if not movie:
        raise FreeError(i18n.tr("Film introuvable dans Radarr (identifiant TMDB)"))
    mf = movie.get("movieFile") or {}
    if not mf.get("id"):
        raise FreeError(i18n.tr("Radarr ne connaît pas de fichier pour ce film"))
    if not _same_file(row["key"], mf.get("path") or mf.get("relativePath"), row.get("file_size"), mf.get("size")):
        raise FreeError(i18n.tr("Le fichier connu de Radarr n'est pas celui de Noxa : rien n'est supprimé"))
    # stop monitoring first: otherwise Radarr would start a download again right away
    arr.call("PUT", "/api/v3/movie/editor", {"movieIds": [movie["id"]], "monitored": False})
    arr.call("DELETE", f"/api/v3/moviefile/{int(mf['id'])}")


def sonarr_delete(arr, row, show, season):
    series = []
    if show.get("tvdb_id"):
        series = arr.call("GET", f"/api/v3/series?tvdbId={int(show['tvdb_id'])}") or []
    if not series and show.get("tmdb_id"):
        series = [s for s in (arr.call("GET", "/api/v3/series") or []) if s.get("tmdbId") == show["tmdb_id"]]
    if not series:
        raise FreeError(i18n.tr("Série introuvable dans Sonarr (identifiants TVDB / TMDB)"))
    sid = series[0]["id"]
    eps = arr.call("GET", f"/api/v3/episode?seriesId={sid}") or []
    ep = next((e for e in eps if e.get("seasonNumber") == season and e.get("episodeNumber") == row["idx"]), None)
    if not ep or not ep.get("episodeFileId"):
        raise FreeError(i18n.tr("Sonarr ne connaît pas de fichier pour cet épisode"))
    ef = arr.call("GET", f"/api/v3/episodefile/{int(ep['episodeFileId'])}") or {}
    if not _same_file(row["key"], ef.get("path") or ef.get("relativePath"), row.get("file_size"), ef.get("size")):
        raise FreeError(i18n.tr("Le fichier connu de Sonarr n'est pas celui de Noxa : rien n'est supprimé"))
    same = [e["id"] for e in eps if e.get("episodeFileId") == ep["episodeFileId"]]  # file with several episodes
    arr.call("PUT", "/api/v3/episode/monitor", {"episodeIds": same, "monitored": False})
    arr.call("DELETE", f"/api/v3/episodefile/{int(ep['episodeFileId'])}")


# ====================================================================== the service
class ColdFree:
    def __init__(self, ctx):
        self.ctx = ctx

    # ---------------------------------------------------------------- settings
    def conf(self):
        try:
            c = json.loads(self.ctx.db.setting("cold_free", "{}") or "{}")
        except ValueError:
            c = {}
        return {**DEFAULTS, **c}

    def public(self):
        c = self.conf()
        out = {k: v for k, v in c.items() if k not in SECRETS}
        out["secrets_set"] = [k for k in SECRETS if c.get(k)]
        return out

    def save(self, b):
        c = self.conf()
        for k in ("enabled", "keep_watchlist", "keep_rated", "keep_started"):
            if k in b:
                c[k] = bool(b[k])
        for k, lo, hi in (("days", 7, 3650), ("delay_days", 0, 90), ("min_free_gb", 0, 100000)):
            if k in b:
                try:
                    v = int(b[k])
                except (TypeError, ValueError):
                    raise FreeError(i18n.tr("Réglage invalide : {k}").format(k=k))
                if not lo <= v <= hi:
                    raise FreeError(i18n.tr("Réglage invalide : {k}").format(k=k))
                c[k] = v
        if "mode" in b:
            if b["mode"] not in ("review", "auto"):
                raise FreeError(i18n.tr("Réglage invalide : {k}").format(k="mode"))
            c["mode"] = b["mode"]
        if "store" in b:
            c["store"] = int(b["store"]) if b["store"] else None
        if "libraries" in b:
            c["libraries"] = [int(x) for x in b["libraries"] or [] if str(x).isdigit()]
        if "methods" in b:
            m = {}
            for lib, how in (b["methods"] or {}).items():
                if str(lib).isdigit() and how in METHODS:
                    m[str(lib)] = how
            c["methods"] = m
        for k in ("radarr_url", "sonarr_url"):
            if k in b:
                v = str(b[k] or "").strip().rstrip("/")
                if v and not re.match(r"^https?://[^\s]{3,300}$", v):
                    raise FreeError(i18n.tr("Adresse invalide (ex. http://192.168.1.20:7878)"))
                c[k] = v
        for k in SECRETS:
            if b.get(k):
                v = str(b[k]).strip()
                if not re.match(r"^[A-Za-z0-9]{16,64}$", v):
                    raise FreeError(i18n.tr("Clé d'API invalide"))
                c[k] = v
        self.ctx.db.set_setting("cold_free", json.dumps(c))
        return self.public()

    def arr_for(self, lib_kind, c=None):
        c = c or self.conf()
        if lib_kind == "movie":
            return Arr(c["radarr_url"], c["radarr_key"], "Radarr")
        return Arr(c["sonarr_url"], c["sonarr_key"], "Sonarr")

    def method_for(self, lib, c=None):
        c = c or self.conf()
        how = c["methods"].get(str(lib["id"]))
        if how:
            return how
        # by default: Radarr / Sonarr when set up for this library type, otherwise by hand
        url, key = (c["radarr_url"], c["radarr_key"]) if lib["kind"] == "movie" else (c["sonarr_url"], c["sonarr_key"])
        return "arr" if url and key else "manual"

    # ---------------------------------------------------------------- activity
    def last_activity(self):
        """Last activity (playback, progress, "watched") per movie and per show (episodes included)."""
        db = self.ctx.db
        per_rk = {}
        for r in db.all("SELECT rk, MAX(t) AS t FROM (SELECT rating_key AS rk, updated_at AS t FROM progress "
                        "UNION ALL SELECT rating_key, watched_at FROM history UNION ALL SELECT rating_key, ended FROM play_log "
                        "UNION ALL SELECT rating_key, last_seen FROM streams) GROUP BY rk"):
            if str(r["rk"]).isdigit():
                per_rk[int(r["rk"])] = r["t"] or 0
        out = dict(per_rk)
        for r in db.all("SELECT id, show_id FROM items WHERE type='episode' AND show_id IS NOT NULL"):
            t = per_rk.get(r["id"])
            if t and t > out.get(r["show_id"], 0):
                out[r["show_id"]] = t
        return out

    def protected(self, c=None):
        """Movies and shows to keep on the disk: protected by hand, and per the settings (lists, ratings, in progress)."""
        c = c or self.conf()
        db = self.ctx.db
        keep = {r["item_id"] for r in db.all("SELECT item_id FROM cold_keep")}
        if c["keep_watchlist"]:
            keep |= {r["item_id"] for r in db.all("SELECT item_id FROM watchlist")}
        if c["keep_rated"]:
            keep |= {r["item_id"] for r in db.all("SELECT item_id FROM stars WHERE stars>=4")}
        if c["keep_started"]:
            for r in db.all("SELECT p.rating_key, p.show_key FROM progress p WHERE p.position_ms>0 AND p.watched=0"):
                for k in (r["show_key"], r["rating_key"]):
                    if k and str(k).isdigit():
                        keep.add(int(k))
        return keep

    # ---------------------------------------------------------------- candidats
    def candidates(self, c=None):
        """Movies and shows nobody has watched for `days` days (added before), with their files still
        in the library. -> [{root, title, type, library, size, last, files: [ids]}], biggest first."""
        c = c or self.conf()
        db = self.ctx.db
        now = int(time.time())
        limit = now - c["days"] * DAY
        libs = [l for l in db.all("SELECT * FROM libraries WHERE peer_id IS NULL AND kind IN ('movie','show')")
                if not c["libraries"] or l["id"] in c["libraries"]]
        if c["min_free_gb"]:
            libs = [l for l in libs if self._low_space(l, c["min_free_gb"])]
        if not libs:
            return []
        activity = self.last_activity()
        keep = self.protected(c)
        known = {r["item_id"] for r in db.all("SELECT item_id FROM cold_free")}
        out = []
        for lib in libs:
            for root in db.all("SELECT id, type, title, year, added_at FROM items WHERE library_id=? AND peer_id IS NULL "
                               "AND type IN ('movie','show')", (lib["id"],)):
                if root["id"] in keep:
                    continue
                last = max(activity.get(root["id"], 0), root["added_at"] or 0)
                if last > limit:
                    continue
                leaves = db.all("SELECT id, key, file_size FROM items WHERE id=? AND type='movie'" if root["type"] == "movie"
                                else "SELECT id, key, file_size FROM items WHERE show_id=? AND type='episode'", (root["id"],))
                files = [x for x in leaves if x["id"] not in known and x["key"] and not media.is_strm(x["key"])
                         and os.path.isfile(x["key"])]
                if not files:
                    continue
                out.append({"root": root["id"], "type": root["type"], "title": root["title"], "year": root["year"],
                            "library": lib["name"], "size": sum(x["file_size"] or 0 for x in files),
                            "last": activity.get(root["id"]) or None, "added_at": root["added_at"],
                            "files": [x["id"] for x in files]})
        out.sort(key=lambda x: -x["size"])
        return out

    @staticmethod
    def _low_space(lib, min_gb):
        for p in json.loads(lib["paths"] or "[]"):
            try:
                if os.path.isdir(p) and os.statvfs(p).f_bavail * os.statvfs(p).f_frsize < min_gb * 1e9:
                    return True
            except OSError:
                continue
        return False

    # ---------------------------------------------------------------- passage quotidien
    def run(self, job=None):
        """Scheduled task: new candidates (proposed or approved automatically), then progress of each file."""
        c = self.conf()
        found = 0
        if c["enabled"]:
            state = "approved" if c["mode"] == "auto" else "proposed"
            now = int(time.time())
            new_size = 0
            for cand in self.candidates(c):
                for i in cand["files"]:
                    size = self.ctx.db.scalar("SELECT file_size FROM items WHERE id=?", (i,)) or 0
                    self.ctx.db.run("INSERT OR IGNORE INTO cold_free(item_id, root_id, state, size, created_at) VALUES(?,?,?,?,?)",
                                    (i, cand["root"], state, size, now))
                    found += 1
                    new_size += size
            if found and state == "proposed":
                self._tell_admins(found, new_size)
        done = self.advance(c)
        return {"new": found, **done}

    def _tell_admins(self, n, size):
        from .i18n import tr
        for u in self.ctx.db.all("SELECT id, language FROM users WHERE is_admin=1 AND disabled=0"):
            lang = u["language"]
            self.ctx.push.notify(u["id"], tr("Place à libérer", lang),
                                 tr("{n} fichier(s) non regardés pourraient libérer {size} Go : à valider", lang).format(
                                     n=n, size=round(size / 1e9, 1)), "#/admin/cold", tag="cold-free")

    def approve(self, item_ids=None, roots=None):
        """Approval by an administrator (files, or whole movies / shows)."""
        db = self.ctx.db
        ids = set(int(i) for i in item_ids or [])
        for r in roots or []:
            ids |= {x["item_id"] for x in db.all("SELECT item_id FROM cold_free WHERE root_id=? AND state='proposed'", (int(r),))}
        now = int(time.time())
        for i in ids:
            db.run("UPDATE cold_free SET state='approved', approved_at=?, error=NULL WHERE item_id=? AND state='proposed'", (now, i))
        self.advance()
        return len(ids)

    def keep(self, root_id, user_id=None):
        """"Keep on the disk": protected for good; what was planned for this title is cancelled."""
        db = self.ctx.db
        db.run("INSERT OR IGNORE INTO cold_keep(item_id, created_at, user_id) VALUES(?,?,?)", (int(root_id), int(time.time()), user_id))
        db.run("DELETE FROM cold_free WHERE root_id=? AND state IN ('proposed','approved','pending','manual')", (int(root_id),))

    def unkeep(self, root_id):
        self.ctx.db.run("DELETE FROM cold_keep WHERE item_id=?", (int(root_id),))

    def on_archived(self, item_id):
        """Cold copy finished: the file moves to delayed deletion if it was planned."""
        if self.ctx.db.scalar("SELECT 1 FROM cold_free WHERE item_id=? AND state='approved'", (item_id,)):
            self.advance()

    def advance(self, c=None):
        c = c or self.conf()
        db = self.ctx.db
        cold = self.ctx.cold
        now = int(time.time())
        stats = {"queued": 0, "pending": 0, "freed": 0, "errors": 0}
        activity = None
        # 1. approved: cold copy, then delayed deletion
        for r in db.all("SELECT f.*, i.key FROM cold_free f JOIN items i ON i.id=f.item_id WHERE f.state='approved'"):
            if db.scalar("SELECT 1 FROM cold_copies c JOIN cold_stores s ON s.id=c.store_id WHERE c.item_id=? "
                         "AND c.verified_at IS NOT NULL AND s.enabled=1", (r["item_id"],)):
                db.run("UPDATE cold_free SET state='pending', delete_after=?, error=NULL WHERE item_id=?",
                       (now + c["delay_days"] * DAY, r["item_id"]))
                stats["pending"] += 1
                continue
            job = db.one("SELECT state, error FROM cold_jobs WHERE item_id=?", (r["item_id"],))
            if job and job["state"] == "error":
                db.run("UPDATE cold_free SET error=? WHERE item_id=?", (job["error"], r["item_id"]))
                stats["errors"] += 1
                continue
            if not job:
                store = c["store"] or next((s["id"] for s in cold.stores(enabled_only=True)), None)
                if not store:
                    db.run("UPDATE cold_free SET error=? WHERE item_id=?", (i18n.tr("Aucun stockage froid actif"), r["item_id"]))
                    stats["errors"] += 1
                    continue
                try:
                    cold.queue([r["item_id"]], store)
                    stats["queued"] += 1
                except Exception as e:
                    db.run("UPDATE cold_free SET error=? WHERE item_id=?", (str(e)[:300], r["item_id"]))
        # 2. delay over: the original is deleted (unless the title was watched meanwhile)
        for r in db.all("SELECT * FROM cold_free WHERE state IN ('pending','manual')"):
            row = db.one("SELECT i.*, l.kind AS lib_kind, l.id AS lib_id, l.paths AS lib_paths FROM items i "
                         "JOIN libraries l ON l.id=i.library_id WHERE i.id=?", (r["item_id"],))
            if not row:
                continue
            if activity is None:
                activity = self.last_activity()
            if activity.get(r["root_id"], 0) >= r["created_at"]:
                db.run("DELETE FROM cold_free WHERE item_id=?", (r["item_id"],))  # watched again: kept
                continue
            if not os.path.isfile(row["key"]):
                db.run("UPDATE cold_free SET state='freed', freed_at=?, error=NULL WHERE item_id=?", (now, r["item_id"]))
                stats["freed"] += 1
                continue
            if r["state"] == "manual" or (r["delete_after"] or 0) > now:
                continue
            how = self.method_for({"id": row["lib_id"], "kind": row["lib_kind"]}, c)
            try:
                self._prepare_caches(row)
                if how == "manual":
                    db.run("UPDATE cold_free SET state='manual', method='manual', error=NULL WHERE item_id=?", (r["item_id"],))
                    continue
                self.delete_original(row, how, c)
                db.run("UPDATE cold_free SET state='freed', freed_at=?, method=?, error=NULL WHERE item_id=?",
                       (now, how, r["item_id"]))
                stats["freed"] += 1
                log.warning("Place libérée : %s (%s)", row["key"], how)
            except (FreeError, OSError) as e:
                db.run("UPDATE cold_free SET error=?, method=? WHERE item_id=?", (str(e)[:300], how, r["item_id"]))
                stats["errors"] += 1
        # 3. file back in the library (downloaded again): the row has no reason to exist anymore
        for r in db.all("SELECT f.item_id, i.key FROM cold_free f JOIN items i ON i.id=f.item_id WHERE f.state='freed'"):
            if r["key"] and os.path.isfile(r["key"]):
                db.run("DELETE FROM cold_free WHERE item_id=?", (r["item_id"],))
        return stats

    def _prepare_caches(self, row):
        """Before the file leaves: internal subtitles extracted (they could only be extracted remotely afterwards)."""
        subs = self.ctx.subtitles
        try:
            job = subs.ensure_now(row)
            if job:
                subs.wait(job, 900)
        except Exception as e:
            log.info("Sous-titres de %s : %s", row["key"], e)
        if subs.missing(row):
            raise FreeError(i18n.tr("Sous-titres intégrés pas encore extraits : nouvel essai au prochain passage"))
        # subtitles placed next to the file (.srt, .ass…): copied into Noxa's data, the record points to them
        probe = json.loads(row["probe"] or "{}")
        lib_roots = [os.path.realpath(p) for p in json.loads(row["lib_paths"] or "[]") if p]
        changed = False
        for e in probe.get("external", []):
            path = e.get("path") or ""
            if not path or not any(os.path.realpath(path).startswith(r + os.sep) for r in lib_roots) or not os.path.isfile(path):
                continue  # already outside the library (OpenSubtitles, Whisper…)
            dest_dir = os.path.join(self.ctx.data_dir, "subtitles", "kept", str(row["id"]))
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(path))
            with open(path, "rb") as src, open(dest, "wb") as out:
                out.write(src.read(64 * 1024 * 1024))
            e["path"] = dest
            changed = True
        if changed:
            self.ctx.db.run("UPDATE items SET probe=? WHERE id=?", (json.dumps(probe), row["id"]))

    def delete_original(self, row, how, c=None):
        c = c or self.conf()
        if how == "write":
            # Noxa erases by itself: only in a library folder
            roots = [os.path.realpath(p) for p in json.loads(row["lib_paths"] or "[]") if p]
            real = os.path.realpath(row["key"])
            if not any(real.startswith(r + os.sep) for r in roots):
                raise FreeError(i18n.tr("Fichier hors des dossiers de la bibliothèque"))
            try:
                os.remove(real)
            except OSError as e:
                if e.errno in (30, 13, 1):  # EROFS, EACCES, EPERM
                    raise FreeError(i18n.tr("La bibliothèque est en lecture seule pour Noxa : choisissez Radarr / Sonarr ou la suppression à la main"))
                raise
            for sc in self.ctx.cold.sidecars(real):
                try:
                    os.remove(sc)
                except OSError:
                    pass
            return
        if how == "arr":
            if row["lib_kind"] == "movie":
                radarr_delete(self.arr_for("movie", c), row)
            else:
                season = self.ctx.db.scalar("SELECT idx FROM items WHERE id=?", (row["parent_id"],))
                show = self.ctx.db.one("SELECT tvdb_id, tmdb_id FROM items WHERE id=?", (row["show_id"],)) or {}
                sonarr_delete(self.arr_for("show", c), row, show, season)
            return
        raise FreeError("Méthode inconnue")

    # ---------------------------------------------------------------- status for the administration
    def status(self):
        db = self.ctx.db
        rows = db.all("SELECT f.*, i.title, i.type, i.key, i.idx, s.title AS show_title, p.idx AS season, r.title AS root_title, "
                      "r.type AS root_type FROM cold_free f JOIN items i ON i.id=f.item_id LEFT JOIN items s ON s.id=i.show_id "
                      "LEFT JOIN items p ON p.id=i.parent_id LEFT JOIN items r ON r.id=f.root_id "
                      "WHERE f.state!='freed' ORDER BY f.state, r.title, p.idx, i.idx")
        groups = {}
        for r in rows:
            g = groups.setdefault((r["state"], r["root_id"]), {"state": r["state"], "root": r["root_id"], "title": r["root_title"],
                                                               "type": r["root_type"], "size": 0, "files": [], "errors": [],
                                                               "delete_after": None})
            g["size"] += r["size"] or 0
            g["files"].append({"id": r["item_id"], "label": f"S{r['season'] or 0}E{r['idx'] or 0} · {r['title']}" if r["type"] == "episode"
                               else r["title"], "path": r["key"], "error": r["error"]})
            if r["error"] and r["error"] not in g["errors"]:
                g["errors"].append(r["error"])
            if r["delete_after"]:
                g["delete_after"] = max(g["delete_after"] or 0, r["delete_after"])
        freed = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM cold_free WHERE state='freed'")
        kept = db.all("SELECT k.item_id, i.title, i.type FROM cold_keep k JOIN items i ON i.id=k.item_id ORDER BY i.sort_title")
        return {"groups": list(groups.values()), "freed": freed, "kept": kept}


# ====================================================================== routes
def register(app, ctx, x):
    from .web import HTTPError
    db = ctx.db
    free = ctx.coldfree

    @app.route("GET", "/api/admin/cold/free", auth="admin")
    def free_status(req):
        libs = [{"id": l["id"], "name": l["name"], "kind": l["kind"], "method": free.method_for(l)}
                for l in db.all("SELECT id, name, kind FROM libraries WHERE peer_id IS NULL AND kind IN ('movie','show') ORDER BY position, name")]
        return {"conf": free.public(), "libraries": libs, **free.status()}

    @app.route("PUT", "/api/admin/cold/free", auth="admin")
    def free_save(req):
        try:
            return {"conf": free.save(req.json())}
        except FreeError as e:
            raise HTTPError(400, str(e))

    @app.route("POST", "/api/admin/cold/free/check", auth="admin")
    def free_check(req):
        """Test of Radarr or Sonarr with the given (or saved) address and key."""
        b = req.json()
        which = b.get("which")
        if which not in ("radarr", "sonarr"):
            raise HTTPError(400, "Service inconnu")
        c = free.conf()
        arr = Arr(b.get("url") or c[f"{which}_url"], b.get("key") or c[f"{which}_key"], "Radarr" if which == "radarr" else "Sonarr")
        db.release()
        try:
            return arr.check()
        except FreeError as e:
            raise HTTPError(400, str(e))

    @app.route("GET", "/api/admin/cold/free/preview", auth="admin")
    def free_preview(req):
        """What the rule would free now (without changing anything)."""
        c = {**free.conf(), "enabled": True}
        cands = free.candidates(c)
        return {"count": len(cands), "size": sum(x["size"] for x in cands), "items": cands[:200]}

    @app.route("POST", "/api/admin/cold/free/run", auth="admin")
    def free_run(req):
        db.release()
        return free.run()

    @app.route("POST", "/api/admin/cold/free/approve", auth="admin")
    def free_approve(req):
        b = req.json()
        return {"approved": free.approve(b.get("items"), b.get("roots"))}

    @app.route("POST", "/api/admin/cold/free/keep", auth="admin")
    def free_keep(req):
        b = req.json()
        rid = int(b.get("root") or 0)
        r = db.one("SELECT id, type, show_id FROM items WHERE id=?", (rid,))
        if not r:
            raise HTTPError(404, "Élément introuvable")
        root = r["show_id"] if r["type"] in ("season", "episode") else r["id"]
        if b.get("keep", True):
            free.keep(root, req.user["id"])
        else:
            free.unkeep(root)
        return {"ok": True, "root": root}

    @app.route("POST", "/api/admin/cold/free/cancel", auth="admin")
    def free_cancel(req):
        """Do not free this title this time (it may be proposed again later)."""
        b = req.json()
        db.run("DELETE FROM cold_free WHERE root_id=? AND state IN ('proposed','approved','pending','manual')", (int(b.get("root") or 0),))
        return {"ok": True}
