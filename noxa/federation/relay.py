"""Linked servers: requests relayed from a device to the server that has the file."""
import hashlib
import http.client
import json
import os
import re
import threading
import time
import urllib.parse

from ..catalog import SID, stream_lists
from ..web import HTTPError, MAX_BODY, Response, json_response
from .common import ADMIN_ITEM, ADMIN_STREAM, DOWNLOAD_JOB, FedError, ITEM_SUB, STREAM, STREAMED_UPLOAD, WATCHED, _Limited, log


class RelayMixin:
    # ------------------------------------------------------------------ relay (device -> server that has the file)
    def mirror(self, rk):
        return self.ctx.db.one("SELECT id, peer_id, remote_id, type FROM items WHERE id=? AND peer_id IS NOT NULL",
                               (int(rk),))

    def image(self, item, kind, w, h):
        tag = hashlib.sha1(f"fed|{item['peer_id']}|{item['remote_id']}|{kind}|{item['img_version']}|{w}|{h}".encode()).hexdigest()
        dest = os.path.join(self.ctx.data_dir, "cache", "img", tag + ".jpg")
        if os.path.isfile(dest):
            return dest
        try:
            peer = self.peer(item["peer_id"])
            if not peer["online"]:  # linked server unreachable: no waiting, the picture will come when it is back
                return None
            self.ctx.db.release()
            resp, close = self.request(peer, "GET", f"/fed/img/{int(item['remote_id'])}/{kind}?w={w}&h={h}", timeout=10)
            try:
                data = resp.read(20 * 1024 * 1024) if resp.status == 200 else None
            finally:
                close()
        except (FedError, OSError, http.client.HTTPException) as e:
            log.debug("Image d'un serveur lié : %s", e)
            return None
        if not data:
            return None
        tmp = dest + f".{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return dest

    def relay(self, req, peer_id, path):
        peer = self.peer(peer_id)
        qs = urllib.parse.urlencode(req.query, doseq=True)
        if qs:
            path += "?" + qs
        headers = {k: req.headers[k] for k in ("Range", "If-None-Match") if req.headers.get(k)}
        body = None
        if req.method in ("POST", "PUT"):
            length = int(req.headers.get("Content-Length") or 0) if str(req.headers.get("Content-Length") or "0").isdigit() else 0
            if STREAMED_UPLOAD.match(path.split("?")[0]) and length > MAX_BODY:
                # big file (backup archive): passed on in chunks, without keeping it all in memory
                body = _Limited(req.handler.rfile, length)
                headers["Content-Length"] = str(length)
                req._body = b""
            else:
                body = req.body
        self.ctx.db.release()
        try:
            resp, close = self.request(peer, req.method, path, body, headers=headers, user=req.user, timeout=90)
        except FedError as e:
            raise HTTPError(502, str(e))
        out = {k: resp.getheader(k) for k in ("Content-Range", "Accept-Ranges", "Cache-Control", "ETag",
                                               "Content-Encoding") if resp.getheader(k)}
        length = resp.getheader("Content-Length")

        def gen():
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                yield chunk
        return Response(stream=gen(), status=resp.status, content_type=resp.getheader("Content-Type") or "application/octet-stream",
                        length=int(length) if length and length.isdigit() else None, headers=out, on_close=close)

    def admin_relay(self, req, target):
        """Settings of a linked server, picked in the server selector: the admin request is passed
        on as is, on behalf of the same administrator (it checks the rights itself)."""
        try:
            pid = int(target)
            peer = self.peer(pid)
        except (ValueError, FedError):
            raise HTTPError(404, "Serveur lié introuvable")
        req.query = {k: v for k, v in req.query.items() if k != "_server"}
        log.info("Administration de %s par %s : %s %s", peer["name"], req.user["username"], req.method, req.path)
        return self.relay(req, pid, req.path)

    def active_source(self, user_id, rk):
        """Copy of a linked server being played by this account for this item (the most recent)."""
        with self.lock:
            for e in reversed(list(self.proxied.values())):
                if e[1] == user_id and e[2] == int(rk):
                    return e
        return None

    def intercept(self, req):
        """Request from a device connected to this server, about an item of a linked server: relayed.
        -> Response, or None when the local route handles it (possibly after telling the linked server)."""
        if not self.has_peers():
            return None
        if not req.user:
            # stream of a linked server played by a TV (no session): relayed when this stream was started for it
            m = STREAM.match(req.path) if req.method == "GET" else None
            entry = self.proxied.get(m.group("sid")) if m else None
            row = self.ctx.db.one("SELECT params FROM streams WHERE id=?", (m.group("sid"),)) if entry else None
            if row and json.loads(row["params"] or "{}").get("cast"):
                req.user = self.ctx.db.one("SELECT * FROM users WHERE id=?", (entry[1],))
                resp = self.relay(req, entry[0], req.path)
                req.user = None
                resp.headers["Access-Control-Allow-Origin"] = "*"
                return resp
            return None
        path, x = req.path, self.x
        m = DOWNLOAD_JOB.match(path)
        if m and req.method in ("GET", "DELETE"):
            entry = self.dl_jobs.get(m.group(1))
            if not entry:
                return None
            if entry[1] != req.user["id"]:
                raise HTTPError(404, "Téléchargement introuvable : relancez-le")
            if req.method == "DELETE":
                self.dl_jobs.pop(m.group(1), None)
                self.send_later(entry[0], "DELETE", path, None, req.user)
                return json_response({"ok": True})
            if m.group(2):
                return self.relay(req, entry[0], path)
            return self.download_view(req, entry, path)
        if req.method == "GET":
            m = ITEM_SUB.match(path)
            if m:
                r = self.mirror(m.group(1))
                if r:
                    x.require_item(req.user, m.group(1))
                    return self.relay(req, r["peer_id"], f"/api/items/{r['remote_id']}/{m.group(2)}")
                # merged title played from a linked server's copy: subtitles, previews, chapters of that copy
                e = self.active_source(req.user["id"], m.group(1))
                if e:
                    x.require_item(req.user, m.group(1))
                    return self.relay(req, e[0], f"/api/items/{e[3]}/{m.group(2)}")
                return None
            m = STREAM.match(path)
            if m:
                entry = self.proxied.get(m.group("sid"))
                if not entry:
                    return None
                if entry[1] != req.user["id"]:
                    raise HTTPError(404, "Flux expiré")
                now = int(time.time())
                self.ctx.db.run("UPDATE streams SET last_seen=? WHERE id=? AND last_seen<?", (now, m.group("sid"), now - 15))
                return self.relay(req, entry[0], path)
            return None
        if req.method == "POST" and path == "/api/play":
            return self.play(req)
        if req.method == "POST" and path == "/api/progress":
            b = req.json()
            e = self.proxied.get(str(b.get("stream") or ""))
            if e and str(e[2]) == str(b.get("id")):
                self.send_later(e[0], "POST", "/api/progress", dict(b, id=str(e[3])), req.user)
            elif str(b.get("id") or "").isdigit():
                r = self.mirror(b["id"])
                if r:
                    fwd = dict(b, id=str(r["remote_id"]))
                    fwd.pop("stream", None)
                    self.send_later(r["peer_id"], "POST", "/api/progress", fwd, req.user)
            return None
        if req.method == "POST" and path == "/api/stop":
            entry = self.proxied.pop(str(req.json().get("stream") or ""), None)
            if entry:
                self.send_later(entry[0], "POST", "/api/stop", req.json(), req.user)
            return None
        if req.method == "DELETE":
            m = ADMIN_STREAM.match(path)
            entry = self.proxied.pop(m.group("sid"), None) if m else None
            if entry:
                owner = self.ctx.db.one("SELECT uid FROM users WHERE id=?", (entry[1],))
                if owner:
                    self.send_later(entry[0], "POST", "/api/stop", {"stream": m.group("sid")}, owner)
            return None
        m = WATCHED.match(path)
        if m and req.method == "POST":
            r = self.mirror(m.group(1))
            if r:
                self.send_later(r["peer_id"], "POST", f"/api/items/{r['remote_id']}/watched", req.json(), req.user)
            return None
        if req.method == "POST" and path in ("/api/downloads/options", "/api/downloads"):
            return self.download(req)
        m = ADMIN_ITEM.match(path)
        if m and self.mirror(m.group(1)):
            raise HTTPError(400, "Cet élément vient d'un serveur lié : modifiez-le sur ce serveur")
        return None

    # downloads (offline) -------------------------------------------
    def download(self, req):
        """Title of a linked server, or only present there: the download is prepared by it and relayed.
        A title whose file this server has is downloaded from here (nothing goes through the link)."""
        b = req.json()
        rk = str(b.get("id") or "")
        if not rk.isdigit():
            return None
        db = self.ctx.db
        r = db.one("SELECT id, peer_id, key FROM items WHERE id=?", (int(rk),))
        if not r or r["peer_id"] is None and (r["key"] and os.path.isfile(r["key"])
                                              or not db.scalar("SELECT 1 FROM items WHERE alias_of=? LIMIT 1", (r["id"],))):
            return None
        if db.setting("downloads", "1") != "1":
            raise HTTPError(403, "Les téléchargements sont désactivés par l'administrateur")
        row = self.x.require_item(req.user, rk)
        cands = [c for c in self.sources(row) if c["peer_id"] is not None]
        if not cands:
            raise HTTPError(503, "Aucune copie de ce titre n'est disponible pour l'instant")
        c = cands[0]
        fwd = dict(self.remap(b, row, c) if c["id"] != row["id"] else b, id=str(c["remote_id"]))
        if "burn" in b and b.get("burn") is not None:  # subtitles to burn in: the same language in this copy
            fwd["burn"] = self.remap({"subtitle": b["burn"]}, row, c).get("subtitle") or None
        peer = self.peer(c["peer_id"])
        db.release()
        try:
            status, data = self.call(peer, "POST", req.path, fwd, user=req.user, timeout=60)
        except FedError as e:
            raise HTTPError(502, str(e))
        if status != 200:
            raise HTTPError(status, data.get("error") or "Téléchargement impossible")
        if req.path == "/api/downloads":
            jid = str(data.get("id") or "")
            if not re.match(r"^[A-Za-z0-9_-]{8,20}$", jid):
                raise HTTPError(502, "Réponse inattendue du serveur lié")
            with self.lock:
                self.dl_jobs[jid] = (peer["id"], req.user["id"], row["id"])
            data["item_id"] = str(row["id"])
        return json_response(data)

    def download_view(self, req, entry, path):
        try:
            status, data = self.call(self.peer(entry[0]), "GET", path, user=req.user, timeout=20)
        except FedError as e:
            raise HTTPError(502, str(e))
        if status != 200:
            if status == 404:
                self.dl_jobs.pop(path.rsplit("/", 1)[1], None)
            raise HTTPError(status, data.get("error") or "Téléchargement introuvable : relancez-le")
        data["item_id"] = str(entry[2])
        return json_response(data)

    # choice of the copy ------------------------------------------------------
    def sources(self, row):
        """Playable copies of a title: ours (when the file is here) and those of the reachable linked servers."""
        if row["peer_id"] is not None:
            return [row]
        out = [row] if row["key"] and os.path.isfile(row["key"]) else []
        return out + self.ctx.db.all("SELECT i.* FROM items i JOIN peers p ON p.id=i.peer_id WHERE i.alias_of=? "
                                     "AND p.online=1 ORDER BY i.id", (row["id"],))

    @staticmethod
    def _tracks(row):
        return stream_lists(json.loads(row["probe"] or "{}"))

    def rank(self, req, row, cands, b):
        """The copy that has the wanted language (audio, then the subtitles asked for) when only one has it; then the one of a
        server that is not already loaded (enough streams running); then the best quality (resolution,
        then bitrate); when equal, the least busy server's, then ours."""
        x, uid = self.x, req.user["id"]
        audio0, subs0 = self._tracks(row)
        tp = x.track_prefs(uid, row) or {}
        want_a = next((a["lang"] for a in audio0 if a["id"] == b.get("audio")), None) \
            or (tp.get("audio_lang") if tp.get("audio_lang") not in (None, "", "off") else None) \
            or x.get_prefs(uid).get("audio_lang") or None
        want_s = next((t["lang"] for t in subs0 if t["id"] == b.get("subtitle")), None) if b.get("subtitle") else None
        limit, here = self.busy_limit(), self.local_load()

        def score(c):
            audio, subs = self._tracks(c)
            probe = json.loads(c["probe"] or "{}")
            video = probe.get("video") or {}
            load = here if c["peer_id"] is None else int((self.status.get(c["peer_id"]) or {}).get("load") or 0)
            return (not want_a or any(a["lang"] == want_a for a in audio),
                    not want_s or any(t["lang"] == want_s for t in subs),
                    not (limit and load >= limit),
                    int(video.get("height") or 0), int(probe.get("bitrate") or 0), -load, c["peer_id"] is None)
        return sorted(cands, key=score, reverse=True)

    def remap(self, b, row, c):
        """Tracks chosen on the title page (those of our copy) -> the same languages in the copy picked."""
        if c["id"] == row["id"]:
            return b
        audio0, subs0 = self._tracks(row)
        audio, subs = self._tracks(c)
        out = dict(b)
        a = next((t for t in audio0 if t["id"] == b.get("audio")), None)
        if b.get("audio") is not None:
            same = [t for t in audio if a and t["lang"] == a["lang"]]
            same.sort(key=lambda t: (t["codec"] == a["codec"], t["channels"] == a["channels"]), reverse=True)
            out["audio"] = same[0]["id"] if same else None
        t0 = next((t for t in subs0 if t["id"] == b.get("subtitle")), None) if b.get("subtitle") else None
        if b.get("subtitle"):
            same = [t for t in subs if t0 and t["lang"] == t0["lang"] and bool(t["forced"]) == bool(t0["forced"])]
            same.sort(key=lambda t: t["codec"] == t0["codec"], reverse=True)
            out["subtitle"] = same[0]["id"] if same else 0
        return out

    def _drop_proxied(self, sid, user):
        entry = self.proxied.pop(sid, None) if sid else None
        if entry:
            self.send_later(entry[0], "POST", "/api/stop", {"stream": sid}, user)
        return entry

    def play(self, req):
        b = req.json()
        rk = str(b.get("id") or "")
        old = str(b.get("replace") or "")
        if not rk.isdigit():
            return None
        x, db = self.x, self.ctx.db
        r = db.one("SELECT id, peer_id FROM items WHERE id=?", (int(rk),))
        if not r or (r["peer_id"] is None and not db.scalar("SELECT 1 FROM items WHERE alias_of=? LIMIT 1", (r["id"],))):
            self._drop_proxied(old, req.user)  # relayed stream replaced by a local playback: the other one is stopped
            return None
        row = x.require_item(req.user, rk)
        cands = self.sources(row)
        # same title started again (seek, track or quality change): same copy, its tracks
        e = self.proxied.get(old) if old else None
        sticky = None
        if e and e[2] == row["id"]:
            sticky = next((c for c in cands if c["peer_id"] == e[0] and c["remote_id"] == e[3]), None)
        elif old and db.scalar("SELECT 1 FROM streams WHERE id=? AND rating_key=?", (old, rk)):
            sticky = next((c for c in cands if c["peer_id"] is None), None)
        order = [sticky] + [c for c in cands if c is not sticky] if sticky else self.rank(req, row, cands, b)
        if not order and row["peer_id"] is None:
            return None  # no copy: the local route explains (file not found)
        error = None
        for c in order:
            body = b if c is sticky else self.remap(b, row, c)
            if c["peer_id"] is None:
                self._drop_proxied(old, req.user)
                req._body = json.dumps(body).encode()
                return None
            try:
                return self.play_remote(req, row, c, body, old)
            except HTTPError as ex:
                log.info("Lecture depuis le serveur lié %s impossible : %s", c["peer_id"], ex.message)
                error = ex
        raise error or HTTPError(503, "Aucune copie de ce titre n'est disponible pour l'instant")

    def play_remote(self, req, row, c, body, old):
        x, db = self.x, self.ctx.db
        uid = req.user["id"]
        peer = self.peer(c["peer_id"])
        fwd = dict(body, id=str(c["remote_id"]))
        fwd.pop("playlist", None)
        e = self.proxied.get(old) if old else None
        if old and not e:
            x.stop_stream(old, uid)  # local stream replaced by the linked server's
            fwd.pop("replace", None)
        elif e and e[0] != peer["id"]:
            self._drop_proxied(old, req.user)
            fwd.pop("replace", None)
        nxt = x.next_episode(row)
        if body.get("playlist"):
            nxt = x.playlist_next(req.user, body["playlist"], row["id"]) if str(body["playlist"]).isdigit() else None
        db.release()
        try:
            status, data = self.call(peer, "POST", "/api/play", fwd, user=req.user, timeout=90)
        except FedError as ex:
            raise HTTPError(502, str(ex))
        if status != 200:
            raise HTTPError(status, data.get("error") or "Lecture impossible",
                            {k: v for k, v in data.items() if k != "error"})
        sid = str(data.get("stream") or "")
        if not re.match(rf"^{SID}$", sid):
            raise HTTPError(502, "Réponse inattendue du serveur lié")
        with self.lock:
            if e and e[0] == peer["id"]:
                self.proxied.pop(old, None)  # replaced there by the new stream
            self.proxied[sid] = (peer["id"], uid, row["id"], c["remote_id"])
            if not (e and e[0] == peer["id"]):  # one more stream there, until its next announcement
                st = self.status.setdefault(peer["id"], {})
                st["load"] = int(st.get("load") or 0) + 1
        if old and e:
            db.run("DELETE FROM streams WHERE id=? AND user_id=?", (old, uid))
        rk = str(row["id"])
        card = x.card(row)
        remote_prefix, local_prefix = f"/api/items/{c['remote_id']}/", f"/api/items/{rk}/"
        for k in ("subtitle_overlay", "subtitle_ass", "subtitle_pgs"):
            if data.get(k):
                data[k] = data[k].replace(remote_prefix, local_prefix, 1)
        data["item"] = card
        data["next"] = x.card(nxt) if nxt else None
        now = int(time.time())
        title = card["title"] if card["type"] == "movie" else f"{card.get('show')} · S{card.get('season')}E{card.get('episode')}"
        params = {"mode": data.get("mode"), "reasons": data.get("reasons") or [], "peer": peer["name"], "cast": bool(body.get("cast"))}
        db.run("INSERT OR REPLACE INTO streams(id,user_id,plex_session,rating_key,params,title,quality,position_ms,"
               "created_at,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (sid, uid, sid, rk, json.dumps(params), title, data.get("quality"), int(data.get("offset") or 0), now, now))
        if row["type"] != "extra":
            x.upsert_meta(uid, card)
        return json_response(data)
