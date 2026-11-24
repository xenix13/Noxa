"""Cold storage: HTTP routes."""
import json
import os
import re
import time

from .. import env, i18n, mountd
from .storage import BACKENDS, BLOCK, ColdError, KINDS, PEER_CHUNK, QUALITY, SMB, _file_blocks, log, public_conf, safe_name, valid_conf


# ====================================================================== routes
def register(app, ctx, x):
    from ..web import HTTPError, Response
    db = ctx.db
    cold = ctx.cold

    def err(e):
        return HTTPError(400, str(e))

    def store_out(s, counts):
        c = counts.get(s["id"]) or {}
        return {"id": s["id"], "name": s["name"], "kind": s["kind"], "enabled": bool(s["enabled"]), "position": s["position"],
                "conf": public_conf(s["conf"]), "copies": c.get("n") or 0, "bytes": c.get("bytes") or 0}

    @app.route("GET", "/api/admin/cold", auth="admin")
    def cold_status(req):
        st = cold.status()
        return {"stores": [store_out(s, st["counts"]) for s in cold.stores()], "available": cold.available(),
                "jobs": st["jobs"], "queued": st["queued"],
                "settings": {"cold_rate": float(db.setting("cold_rate", "0") or 0),
                             "cold_rate_play": float(db.setting("cold_rate_play", "10") or 10)}}

    @app.route("GET", r"/api/admin/cold/stores/(?P<sid>\d+)/free", auth="admin")
    def cold_free(req):
        s = cold.store(int(req.params["sid"]))
        if not s:
            raise HTTPError(404, "Stockage inconnu")
        db.release()
        try:
            return {"free": cold.backend(s).free()}
        except ColdError:
            return {"free": None}

    def prepare(kind, b, old=None):
        if kind not in KINDS:
            raise HTTPError(400, "Type de stockage inconnu")
        why = cold.available().get(kind)
        if why:
            raise HTTPError(400, why)
        try:
            return valid_conf(kind, b.get("conf") or {}, old)
        except ColdError as e:
            raise err(e)

    def test_conf(kind, conf):
        """Essai réel (écrire, relire, effacer un petit fichier). SFTP : l'empreinte du serveur d'abord."""
        b = BACKENDS[kind](conf, cold)
        if kind == "sftp" and not conf.get("hostkey"):
            return {"ok": False, "fingerprint": b.fingerprint()}
        b.check()
        return {"ok": True}

    def mount_smb(sid, conf):
        args = {"name": f"cold-{sid}", "host": conf["host"], "share": conf["share"], "subdir": conf["subdir"],
                "username": conf["username"], "password": conf.get("password", ""), "domain": conf.get("domain", ""),
                "version": conf.get("version", "auto"), "writable": True}
        try:
            r = mountd.call("add", args, sock_path=env("MOUNTD_SOCK"), timeout=90)
        except mountd.MountError as e:
            raise HTTPError(400, str(e))
        conf = {k: v for k, v in conf.items() if k != "password"}  # le mot de passe reste chez seance-mountd
        conf["mount"] = r.get("path") or os.path.join(mountd.MOUNT_ROOT, f"cold-{sid}")
        return conf

    @app.route("POST", "/api/admin/cold/test", auth="admin")
    def cold_test(req):
        b = req.json()
        kind = str(b.get("kind") or "")
        old = None
        if b.get("id"):
            s = cold.store(int(b["id"]))
            old = s["conf"] if s and s["kind"] == kind else None
        conf = prepare(kind, b, old)
        if kind == "smb":
            return {"ok": True, "note": i18n.tr("Le partage sera monté et essayé à l'enregistrement.")}
        db.release()
        try:
            return test_conf(kind, conf)
        except ColdError as e:
            raise err(e)

    @app.route("POST", "/api/admin/cold/stores", auth="admin")
    def cold_add(req):
        b = req.json()
        kind = str(b.get("kind") or "")
        name = str(b.get("name") or "").strip()[:80] or kind
        conf = prepare(kind, b)
        now = int(time.time())
        pos = (db.scalar("SELECT MAX(position) FROM cold_stores") or 0) + 1
        sid = db.run("INSERT INTO cold_stores(name, kind, conf, enabled, position, created_at) VALUES(?,?,?,1,?,?)",
                     (name, kind, json.dumps({}), pos, now)).lastrowid
        try:
            if kind == "smb":
                db.release()
                conf = mount_smb(sid, conf)
            if kind != "smb":
                db.release()
            r = test_conf(kind, conf) if kind != "smb" else (SMB(conf, cold).check() or {"ok": True})
            if not r.get("ok"):
                db.run("DELETE FROM cold_stores WHERE id=?", (sid,))
                return {"ok": False, "fingerprint": r.get("fingerprint")}
        except (ColdError, HTTPError) as e:
            db.run("DELETE FROM cold_stores WHERE id=?", (sid,))
            if kind == "smb":
                try:
                    mountd.call("remove", {"name": f"cold-{sid}"}, sock_path=env("MOUNTD_SOCK"))
                except mountd.MountError:
                    pass
            raise e if isinstance(e, HTTPError) else err(e)
        db.run("UPDATE cold_stores SET conf=? WHERE id=?", (json.dumps(conf), sid))
        log.warning("Stockage froid « %s » (%s) ajouté par %s", name, kind, req.user["username"])
        return {"ok": True, "id": sid}

    @app.route("PUT", r"/api/admin/cold/stores/(?P<sid>\d+)", auth="admin")
    def cold_edit(req):
        s = cold.store(int(req.params["sid"]))
        if not s:
            raise HTTPError(404, "Stockage inconnu")
        b = req.json()
        if "name" in b:
            db.run("UPDATE cold_stores SET name=? WHERE id=?", (str(b["name"]).strip()[:80] or s["kind"], s["id"]))
        if "enabled" in b:
            db.run("UPDATE cold_stores SET enabled=? WHERE id=?", (1 if b["enabled"] else 0, s["id"]))
        if "position" in b:
            db.run("UPDATE cold_stores SET position=? WHERE id=?", (int(b["position"]), s["id"]))
        if b.get("conf") and s["kind"] != "smb":
            conf = prepare(s["kind"], b, s["conf"])
            db.release()
            try:
                r = test_conf(s["kind"], conf)
            except ColdError as e:
                raise err(e)
            if not r.get("ok"):
                return r
            db.run("UPDATE cold_stores SET conf=? WHERE id=?", (json.dumps(conf), s["id"]))
        return {"ok": True}

    @app.route("DELETE", r"/api/admin/cold/stores/(?P<sid>\d+)", auth="admin")
    def cold_remove(req):
        s = cold.store(int(req.params["sid"]))
        if not s:
            raise HTTPError(404, "Stockage inconnu")
        # un titre dont c'est la seule copie (fichier absent de la bibliothèque) ne doit pas devenir illisible
        only = [r for r in db.all("SELECT c.item_id, i.key FROM cold_copies c JOIN items i ON i.id=c.item_id WHERE c.store_id=? "
                                  "AND NOT EXISTS (SELECT 1 FROM cold_copies o WHERE o.item_id=c.item_id AND o.store_id!=c.store_id)",
                                  (s["id"],)) if cold.local_missing(r)]
        if only:
            raise HTTPError(409, i18n.tr("{n} titre(s) n'existent plus que sur ce stockage : impossible de le retirer").format(n=len(only)))
        db.run("DELETE FROM cold_jobs WHERE store_id=?", (s["id"],))
        db.run("DELETE FROM cold_copies WHERE store_id=?", (s["id"],))  # les fichiers restent sur le stockage
        db.run("DELETE FROM cold_stores WHERE id=?", (s["id"],))
        if s["kind"] == "smb":
            db.release()
            try:
                mountd.call("remove", {"name": f"cold-{s['id']}"}, sock_path=env("MOUNTD_SOCK"))
            except mountd.MountError:
                pass
        return {"ok": True}

    @app.route("PUT", "/api/admin/cold/settings", auth="admin")
    def cold_settings(req):
        b = req.json()
        for k in ("cold_rate", "cold_rate_play"):
            if k in b:
                v = float(b[k] or 0)
                if not 0 <= v <= 10000:
                    raise HTTPError(400, "Débit invalide")
                db.set_setting(k, str(v))
        if "compress" in b:
            db.set_setting("cold_compress", "1" if b["compress"] else "0")
        if "compress_quality" in b:
            if b["compress_quality"] not in QUALITY:
                raise HTTPError(400, "Réglage invalide")
            db.set_setting("cold_compress_quality", b["compress_quality"])
        if "compress_height" in b:
            if int(b["compress_height"] or 0) not in (0, 2160, 1080, 720):
                raise HTTPError(400, "Réglage invalide")
            db.set_setting("cold_compress_height", str(int(b["compress_height"] or 0)))
        return {"ok": True}

    @app.route("GET", "/api/admin/cold/compress", auth="admin")
    def cold_compress_info(req):
        """Compression des archives : réglages, carte graphique utilisée, archives qui en profiteraient."""
        hw = ctx.hw()
        todo = [c for c in db.all("SELECT c.size, i.probe FROM cold_copies c JOIN items i ON i.id=c.item_id "
                                  "WHERE c.compressed=0 AND c.verified_at IS NOT NULL") if cold.compressible(json.loads(c["probe"] or "{}"))[0]]
        done = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(orig_size), 0) AS before, COALESCE(SUM(size), 0) AS after "
                      "FROM cold_copies WHERE compressed=1")
        return {**cold.compress_conf(), "hw": hw["engine"] if hw else None,
                "todo": {"count": len(todo), "bytes": sum(c["size"] for c in todo)}, "done": done}

    @app.route("POST", "/api/admin/cold/compress", auth="admin")
    def cold_compress_existing(req):
        return {"queued": cold.queue_compress(int(req.json().get("store") or 0) or None)}

    @app.route("POST", "/api/admin/cold/archive", auth="admin")
    def cold_archive(req):
        b = req.json()
        ids = [i for i in (b.get("ids") or []) if str(i).isdigit()][:5000]
        try:
            n = cold.queue(ids, int(b.get("store") or 0))
        except (ColdError, ValueError) as e:
            raise err(e)
        return {"queued": n}

    @app.route("POST", r"/api/admin/cold/jobs/(?P<jid>\d+)/retry", auth="admin")
    def cold_retry(req):
        db.run("UPDATE cold_jobs SET state='queued', error=NULL WHERE id=? AND state='error'", (int(req.params["jid"]),))
        cold.start()
        return {"ok": True}

    @app.route("DELETE", r"/api/admin/cold/jobs/(?P<jid>\d+)", auth="admin")
    def cold_cancel(req):
        cold.cancel(int(req.params["jid"]))
        return {"ok": True}

    @app.route("GET", r"/api/admin/cold/items/(?P<rk>\d+)", auth="admin")
    def cold_item(req):
        rid = int(req.params["rk"])
        return {"copies": [{"store_id": c["store_id"], "store": c["store_name"], "kind": c["kind"], "size": c["size"],
                            "archived_at": c["archived_at"], "verified_at": c["verified_at"], "name": c["name"]}
                           for c in cold.copies(rid)],
                "jobs": [j for j in cold.status()["jobs"] if j["item_id"] == rid]}

    @app.route("DELETE", r"/api/admin/cold/items/(?P<rk>\d+)/(?P<sid>\d+)", auth="admin")
    def cold_item_forget(req):
        db.release()
        try:
            ok = cold.forget(int(req.params["rk"]), int(req.params["sid"]))
        except ColdError as e:
            raise HTTPError(409, str(e))
        return {"ok": ok}

    # ---------------------------------------------------- cache local et accueil des archives des serveurs liés
    @app.route("GET", "/api/admin/cold/extra", auth="admin")
    def cold_extra(req):
        peers = {p["id"]: p["name"] for p in db.all("SELECT id, name FROM peers")}
        hosted = []
        root = os.path.join(ctx.data_dir, "cold-hosted")
        for d in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            if d.isdigit():
                size = sum(os.path.getsize(os.path.join(a, f)) for a, _, fs in os.walk(os.path.join(root, d)) for f in fs)
                hosted.append({"peer": peers.get(int(d), f"#{d}"), "bytes": size})
        return {"cache": cold.cache_status(), "host_gb": float(db.setting("cold_host_gb", "0") or 0), "hosted": hosted,
                "peers": [{"id": k, "name": v} for k, v in peers.items()]}

    @app.route("PUT", "/api/admin/cold/extra", auth="admin")
    def cold_extra_save(req):
        b = req.json()
        for k, setting, hi in (("cache_gb", "cold_cache_gb", 100000), ("keep_free_gb", "cold_cache_free_gb", 100000),
                               ("next", "cold_prefetch", 20), ("host_gb", "cold_host_gb", 1000000)):
            if k in b:
                try:
                    v = float(b[k] or 0)
                except (TypeError, ValueError):
                    raise HTTPError(400, "Réglage invalide")
                if not 0 <= v <= hi:
                    raise HTTPError(400, "Réglage invalide")
                db.set_setting(setting, str(int(v) if k == "next" else v))
        if b.get("clear_cache"):
            for r in db.all("SELECT item_id FROM cold_cache"):
                cold.uncache(r["item_id"])
        return {"ok": True}

    def hosted_path(req, name):
        if not req.fed_peer:
            raise HTTPError(403, "Réservé aux serveurs liés")
        if cold.hosted_quota() <= 0:
            raise HTTPError(403, i18n.tr("Ce serveur n'accueille pas d'archives (réglage de son administrateur)"))
        try:
            rel = safe_name(name or "")
        except ColdError:
            raise HTTPError(400, "Nom invalide")
        root = os.path.realpath(cold.hosted_dir(req.fed_peer["id"]))
        p = os.path.realpath(os.path.join(root, rel))
        if not p.startswith(root + os.sep):
            raise HTTPError(400, "Nom invalide")
        return p

    @app.route("GET", "/fed/cold/info", auth="peer")
    def fed_cold_info(req):
        quota = cold.hosted_quota()
        used = cold.hosted_used() if quota else 0
        return {"enabled": quota > 0, "quota": quota, "used": used, "free": max(0, quota - used)}

    @app.route("PUT", "/fed/cold/blob", auth="peer")
    def fed_cold_put(req):
        """Un morceau (16 Mo au plus) d'une archive envoyée par un serveur lié, écrit à la suite des précédents."""
        p = hosted_path(req, req.arg("name"))
        part = p + ".part"
        offset = int(req.arg("offset") or 0)
        length = int(req.headers.get("Content-Length") or 0)
        if length > 2 * PEER_CHUNK or length < 0:
            raise HTTPError(413, "Morceau trop gros")
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if offset == 0:
            have = 0
        elif offset != have:
            raise HTTPError(409, i18n.tr("Reprise attendue à {n}").format(n=have))
        if cold.hosted_used() - (os.path.getsize(part) if offset == 0 and os.path.exists(part) else 0) + length > cold.hosted_quota():
            raise HTTPError(507, i18n.tr("Plus de place réservée aux archives sur ce serveur"))
        db.release()
        os.makedirs(os.path.dirname(part), exist_ok=True)
        with open(part, "r+b" if offset else "wb") as f:
            f.seek(offset)
            left = length
            while left > 0:
                b = req.handler.rfile.read(min(BLOCK, left))
                if not b:
                    req._body = b""
                    raise HTTPError(400, "Morceau incomplet")
                f.write(b)
                left -= len(b)
            f.truncate()
        req._body = b""  # corps déjà lu ici : le serveur web ne doit pas le relire
        return {"ok": True, "size": offset + length}

    @app.route("POST", "/fed/cold/commit", auth="peer")
    def fed_cold_commit(req):
        p = hosted_path(req, req.arg("name"))
        part = p + ".part"
        size = int(req.arg("size") or -1)
        if not os.path.exists(part) or os.path.getsize(part) != size:
            raise HTTPError(409, "Archive incomplète")
        os.replace(part, p)
        return {"ok": True}

    @app.route("GET", "/fed/cold/stat", auth="peer")
    def fed_cold_stat(req):
        p = hosted_path(req, req.arg("name"))
        if not os.path.isfile(p):
            raise HTTPError(404, "Introuvable")
        return {"size": os.path.getsize(p)}

    def byte_range(req, size):
        """En-tête Range -> (début, fin, statut) ; None si la plage est hors du fichier."""
        start, end, status = 0, size - 1, 200
        rng = (req.headers.get("Range") or "").strip()
        m = re.match(r"^bytes=(\d*)-(\d*)$", rng) if rng else None
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                return None
            status = 206
        return start, end, status

    def ranged(start, end, status, size, gen):
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(stream=gen, status=status, content_type="application/octet-stream", length=end - start + 1,
                        headers=headers)

    @app.route("GET", "/fed/cold/blob", auth="peer")
    def fed_cold_get(req):
        p = hosted_path(req, req.arg("name"))
        if not os.path.isfile(p):
            raise HTTPError(404, "Introuvable")
        size = os.path.getsize(p)
        r = byte_range(req, size)
        if r is None:
            return Response(b"", 416, "text/plain", {"Content-Range": f"bytes */{size}"})
        db.release()
        return ranged(*r, size, _file_blocks(p, r[0], r[1]))

    @app.route("DELETE", "/fed/cold/blob", auth="peer")
    def fed_cold_delete(req):
        p = hosted_path(req, req.arg("name"))
        for f in (p, p + ".part"):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        return {"ok": True}

    # lecture d'une copie froide par ffmpeg (adresse locale, jeton secret, jamais derrière un proxy)
    @app.route("GET", r"/cold/(?P<tok>[A-Za-z0-9_-]{20,64})", auth=None)
    def cold_stream(req):
        t = cold.token(req.params["tok"])
        if not t or req.client_ip not in ("127.0.0.1", "::1") or req.headers.get("X-Forwarded-For"):
            raise HTTPError(404, "Introuvable")
        store = cold.store(t["store"])
        if not store:
            raise HTTPError(404, "Introuvable")
        size = t["size"]
        r = byte_range(req, size)
        if r is None:
            return Response(b"", 416, "text/plain", {"Content-Range": f"bytes */{size}"})
        start, end, status = r
        db.release()
        b = cold.backend(store)

        def gen():
            try:
                yield from b.open(t["name"], start, end)
            except ColdError as e:
                log.warning("Lecture froide de %s : %s", t["name"], e)
        return ranged(start, end, status, size, gen())
