"""Linked servers: HTTP routes (the federation port and the admin API)."""
import re
import time

from .. import __version__
from ..discover import Seerr, SeerrError
from ..metadata import MetaError, TMDB
from ..web import HTTPError, Response
from .common import CATALOG_PAGE, FedError, PROGRESS_PAGE, SEERR_ALLOWED, TMDB_PATH, valid_address


def register(app, ctx, x):
    db = ctx.db
    fed = ctx.fed
    fed.attach(app, x)

    # ------------------------------------------------------------ between servers (linked-servers port)
    @app.route("POST", "/fed/join", auth="join")
    def fed_join(req):
        return fed.accept_join(req)

    @app.route("POST", "/fed/join/preview", auth="join")
    def fed_join_preview(req):
        return fed.accept_preview(req)

    @app.route("GET", "/fed/hello", auth="peer")
    def fed_hello(req):
        return {"uid": fed.uid(), "name": fed.name(), "version": __version__, "time": int(time.time()),
                "now_ms": int(time.time() * 1000), "load": fed.local_load(),
                "caps": {"tmdb": bool(db.setting("tmdb_key")), "seerr": bool(db.setting("seerr_url") and db.setting("seerr_key"))}}

    @app.route("GET", "/fed/reverse", auth="peer")
    def fed_reverse(req):
        """Tunnel: the linked server leaves us this connection so that we send it our requests."""
        hd = req.handler
        hd.wfile.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: nexis-tunnel\r\nConnection: Upgrade\r\n\r\n")
        hd.wfile.flush()
        pool = fed.pool_for(req.fed_peer["id"])
        db.release()
        pool.offer(hd.connection)
        resp = Response()
        resp.hijacked = True
        return resp

    @app.route("GET", "/fed/users", auth="peer")
    def fed_users(req):
        return fed.users_feed(req.int_arg("since", 0, 0))

    @app.route("GET", r"/fed/avatar/(?P<uid>[0-9a-f]{32})", auth="peer")
    def fed_avatar(req):
        u = db.one("SELECT id FROM users WHERE uid=?", (req.params["uid"],))
        try:
            with open(fed.avatar_path(u["id"]) if u else "", "rb") as f:
                return Response(f.read(), 200, "image/jpeg")
        except OSError:
            raise HTTPError(404, "Introuvable")

    @app.route("GET", "/fed/catalog", auth="peer")
    def fed_catalog(req):
        return fed.catalog_feed(req.int_arg("since", 0, 0), req.int_arg("limit", CATALOG_PAGE, 1, 2000))

    @app.route("GET", "/fed/progress", auth="peer")
    def fed_progress(req):
        return fed.progress_feed(req.int_arg("since", 0, 0), req.int_arg("limit", PROGRESS_PAGE, 1, 5000))

    @app.route("GET", r"/fed/img/(?P<rk>\d{1,12})/(?P<kind>thumb|art)", auth="peer")
    def fed_img(req):
        r = db.one("SELECT * FROM items WHERE id=? AND peer_id IS NULL", (int(req.params["rk"]),))
        if not r:
            raise HTTPError(404, "Introuvable")
        w, h = req.int_arg("w", 300, 40, 1920), req.int_arg("h", 450, 40, 1920)
        path = ctx.images.get(r, req.params["kind"], w, h)
        if not path:
            raise HTTPError(404, "Pas d'image")
        with open(path, "rb") as f:
            return Response(f.read(), 200, "image/jpeg")

    @app.route("POST", "/fed/tmdb", auth="peer")
    def fed_tmdb(req):
        """TMDB key lent to a linked server: it has us look up its metadata (read only)."""
        b = req.json()
        path, params = str(b.get("path") or ""), b.get("params") or {}
        if not TMDB_PATH.match(path) or not isinstance(params, dict) or len(params) > 20 or not all(
                isinstance(k, str) and isinstance(v, (str, int, float)) for k, v in params.items()):
            raise HTTPError(400, "Requête TMDB invalide")
        lang = str(b.get("lang") or "")
        if not re.match(r"^[a-z]{2}(-[A-Z]{2})?$", lang):
            lang = None
        t = TMDB(db.setting("tmdb_key"), db.setting("meta_language", "fr-FR"), db.setting("meta_country", "FR"))
        if not t.enabled:
            raise HTTPError(409, "Ce serveur n'a plus de clé TMDB")
        db.release()
        try:
            return t.get(path, lang=lang, **params)
        except MetaError as e:
            raise HTTPError(404 if "Introuvable" in str(e) else 502, str(e))

    @app.route("POST", "/fed/seerr", auth="peer")
    def fed_seerr(req):
        """Seerr lent to a linked server: statuses, Seerr accounts and new requests, nothing else."""
        b = req.json()
        method, path = b.get("method"), str(b.get("path") or "")
        if not any(method == m and rx.match(path) for m, rx in SEERR_ALLOWED):
            raise HTTPError(403, "Requête Seerr non autorisée entre serveurs liés")
        body, user = b.get("body"), b.get("user")
        if body is not None and not isinstance(body, dict) or user is not None and not isinstance(user, int):
            raise HTTPError(400, "Requête Seerr invalide")
        s = Seerr(db.setting("seerr_url"), db.setting("seerr_key"))
        if not s.enabled:
            raise HTTPError(409, "Ce serveur n'a plus de Seerr configuré")
        db.release()
        try:
            return {"data": s.call(method, path, body, user=user)}
        except SeerrError as e:
            raise HTTPError(502, str(e))

    @app.route("POST", "/fed/leave", auth="peer")
    def fed_leave(req):
        fed.remove(req.fed_peer["id"], notify=False)
        return {"ok": True}

    # ------------------------------------------------------------ administration
    def checked(fn, *args):
        try:
            return fn(*args)
        except FedError as e:
            raise HTTPError(404 if "introuvable" in str(e) else 502, str(e))

    # ------------------------------------------------------------ administration of the linked servers from this one
    @app.route("GET", "/api/admin/servers", auth="admin")
    def admin_servers(req):
        """Servers this administrator can set up from here: this one, and the linked servers."""
        return {"local": {"id": "", "name": fed.name(), "version": __version__},
                "peers": [{"id": str(p["id"]), "name": p["name"], "online": bool(p["online"]), "version": p["version"]}
                          for p in fed.peers()],
                "remote_admin": db.setting("fed_remote_admin", "1") == "1"}

    @app.route("PUT", "/api/admin/servers", auth="admin")
    def admin_servers_put(req):
        """Accept or not to be administered from a linked server (setting specific to each server, made on site)."""
        if "remote_admin" in req.json():
            db.set_setting("fed_remote_admin", "1" if req.json()["remote_admin"] else "0")
        return admin_servers(req)

    @app.route("GET", "/api/admin/federation", auth="admin")
    def admin_federation(req):
        return fed.view()

    @app.route("PUT", "/api/admin/federation", auth="admin")
    def admin_federation_put(req):
        b = req.json()
        if "name" in b:
            name = str(b["name"] or "").strip()
            if not 1 <= len(name) <= 40:
                raise HTTPError(400, "Nom du serveur : 1 à 40 caractères")
            db.set_setting("server_name", name)
        if "address" in b:
            db.set_setting("fed_address", valid_address(b["address"]))
        if "busy" in b:
            try:
                n = int(b["busy"])
            except (TypeError, ValueError):
                n = -1
            if not 0 <= n <= 50:
                raise HTTPError(400, "Nombre de lectures invalide (0 à 50)")
            db.set_setting("fed_busy", str(n))
        fed.wake.set()
        return fed.view()

    @app.route("POST", "/api/admin/federation/invite", auth="admin")
    def admin_federation_invite(req):
        return checked(fed.invite, req.json().get("address"))

    @app.route("POST", "/api/admin/federation/join/preview", auth="admin")
    def admin_federation_join_preview(req):
        out = fed.join_preview(req.json().get("invite"))
        out.pop("remote")
        return out

    @app.route("DELETE", "/api/admin/federation/notices", auth="admin")
    def admin_federation_notices(req):
        db.set_setting("fed_notices", None)
        return fed.view()

    @app.route("POST", "/api/admin/federation/join", auth="admin")
    def admin_federation_join(req):
        b = req.json()
        peer = fed.join(b.get("invite"), b.get("address"), b.get("accounts") if isinstance(b.get("accounts"), dict) else None)
        db.release()
        fed.sync(peer["id"])
        return fed.view()

    @app.route("PUT", r"/api/admin/federation/peers/(?P<pid>\d{1,6})", auth="admin")
    def admin_federation_peer(req):
        pid = int(req.params["pid"])
        checked(fed.peer, pid)
        db.run("UPDATE peers SET address=? WHERE id=?", (valid_address(req.json().get("address")), pid))
        with fed.lock:
            fed.tunnel_fail.pop(pid, None)
        fed.wake.set()
        return fed.view()

    @app.route("PUT", r"/api/admin/federation/libraries/(?P<lid>\d{1,6})", auth="admin")
    def admin_federation_library(req):
        """Library received from a linked server: new library, or merged with one of ours."""
        lib = db.one("SELECT * FROM libraries WHERE id=? AND peer_id IS NOT NULL", (int(req.params["lid"]),))
        if not lib:
            raise HTTPError(404, "Bibliothèque introuvable")
        target = req.json().get("merge_into")
        if target not in (None, "", 0, "0"):
            t = db.one("SELECT id, kind FROM libraries WHERE id=? AND peer_id IS NULL", (int(target) if str(target).isdigit() else 0,))
            if not t:
                raise HTTPError(404, "Bibliothèque introuvable")
            if t["kind"] != lib["kind"]:
                raise HTTPError(400, "Fusion impossible entre une bibliothèque de films et une de séries")
            target = t["id"]
            # who has access to our library has access to what is merged into it (and the linked server learns it)
            db.run("INSERT OR IGNORE INTO user_libraries(user_id, section_id) SELECT user_id, ? FROM user_libraries "
                   "WHERE section_id=?", (str(lib["id"]), str(target)))
        else:
            target = None
        db.run("UPDATE libraries SET merge_into=? WHERE id=?", (target, lib["id"]))
        fed.relink(lib["peer_id"])
        return fed.view()

    @app.route("POST", r"/api/admin/federation/peers/(?P<pid>\d{1,6})/sync", auth="admin")
    def admin_federation_sync(req):
        pid = int(req.params["pid"])
        checked(fed.peer, pid)
        db.release()
        fed.sync(pid)
        return fed.view()

    @app.route("DELETE", r"/api/admin/federation/peers/(?P<pid>\d{1,6})", auth="admin")
    def admin_federation_remove(req):
        checked(fed.remove, int(req.params["pid"]))
        return fed.view()
