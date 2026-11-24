"""Linked servers: pairing (invitations) and unpairing."""
import hashlib
import re
import secrets
import time

from .. import __version__
from ..auth import USERNAME, USERNAME_RULE, find_user, normalize_username
from ..web import HTTPError
from .common import FedError, HEX32, HEX64, INVITE_TTL, decode_invite, encode_invite, free_username, log, valid_address


class PairingMixin:
    # ------------------------------------------------------------------ jumelage
    def invite(self, address=None):
        self.listen()
        db = self.ctx.db
        token = secrets.token_urlsafe(24)
        now = int(time.time())
        db.run("DELETE FROM fed_invites WHERE expires_at<?", (now,))
        db.run("INSERT INTO fed_invites(token_hash, expires_at) VALUES(?,?)",
               (hashlib.sha256(token.encode()).hexdigest(), now + INVITE_TTL))
        address = valid_address(address) or self.local_address()
        return {"invite": encode_invite({"id": self.uid(), "n": self.name(), "a": address, "f": self.fingerprint(),
                                         "t": token}),
                "address": address, "expires_at": now + INVITE_TTL}

    def _invite_target(self, text):
        data = decode_invite(text)
        db = self.ctx.db
        if data["id"] == self.uid():
            raise HTTPError(400, "Cette invitation vient de ce serveur : collez-la sur l'autre serveur")
        known = db.scalar("SELECT id FROM peers WHERE uid=? AND online=1 AND last_seen>?", (data["id"], int(time.time()) - 120))
        if known:
            raise HTTPError(409, "Ce serveur est déjà lié")
        return data, {"address": data["a"], "fingerprint": data["f"], "secret": ""}

    def _call_target(self, data, target, path, payload):
        self.ctx.db.release()
        try:
            status, r = self.call(target, "POST", path, payload, timeout=60)
        except (FedError, OSError) as e:
            raise HTTPError(502, f"Impossible de joindre {data['a']} : {e}")
        if status != 200:
            raise HTTPError(status if 400 <= status < 500 else 502, r.get("error") or "Jumelage refusé")
        return r

    def join_preview(self, text):
        """Avant de lier : les identifiants présents sur les deux serveurs, pour que l'administrateur dise s'il
        s'agit de la même personne (fusion) ou de deux personnes (le compte d'ici est renommé)."""
        data, target = self._invite_target(text)
        r = self._call_target(data, target, "/fed/join/preview", {"token": data["t"]})
        remote = {str(u.get("username") or "").casefold(): u for u in r.get("users") or [] if isinstance(u, dict)}
        suffix = re.sub(r"[^\w.-]+", "", self.name().lower())[:12] or "2"
        conflicts = []
        for u in self.ctx.db.all("SELECT username, display_name, uid FROM users ORDER BY username COLLATE NOCASE"):
            other = remote.get(u["username"].casefold())
            if other and other.get("uid") != u["uid"]:  # même uid : déjà le même compte (serveurs déjà liés avant)
                base = f"{u['username'][:31 - len(suffix)]}-{suffix}"
                conflicts.append({"username": u["username"], "display_name": u["display_name"],
                                  "remote_display_name": str(other.get("display_name") or "")[:50],
                                  "suggested": base if not (find_user(self.ctx.db, base) or base.casefold() in remote)
                                  else free_username(self.ctx.db, u["username"], remote)})
        return {"server": str(r.get("name") or "Noxa")[:40], "conflicts": conflicts, "remote": list(remote)}

    def join(self, text, address=None, accounts=None):
        """Ce serveur rejoint celui qui a créé l'invitation. accounts : {identifiant: {"merge": bool, "rename": nom}}
        pour les identifiants présents des deux côtés ; sans choix, ils restent séparés (le nôtre est renommé)."""
        address = valid_address(address)
        db = self.ctx.db
        preview = self.join_preview(text)
        data, target = self._invite_target(text)
        accounts = {str(k).casefold(): v for k, v in (accounts or {}).items() if isinstance(v, dict)}
        renames, taken = [], set(preview["remote"])
        for c in preview["conflicts"]:
            choice = accounts.get(c["username"].casefold(), {})
            if choice.get("merge"):
                continue
            new = normalize_username(choice.get("rename") or c["suggested"])
            if not USERNAME.match(new):
                raise HTTPError(400, USERNAME_RULE)
            if new.casefold() in taken or (find_user(db, new) and new.casefold() != c["username"].casefold()):
                raise HTTPError(409, f"Identifiant déjà pris : {new}")
            taken.add(new.casefold())
            renames.append((c["username"], new))
        try:
            self.listen()
        except FedError as e:
            raise HTTPError(500, str(e))
        with db.tx():
            for old, new in renames:
                db.run("UPDATE users SET username=? WHERE username=?", (new, old))
        payload = {"token": data["t"], "uid": self.uid(), "name": self.name(), "fingerprint": self.fingerprint(),
                   "address": address, "version": __version__, "users": self.users_payload()}
        try:
            r = self._call_target(data, target, "/fed/join", payload)
        except HTTPError:
            with db.tx():
                for old, new in renames:
                    db.run("UPDATE users SET username=? WHERE username=?", (old, new))
            raise
        for old, new in renames:
            log.warning("Jumelage : compte %s renommé %s (personne différente de celle de l'autre serveur)", old, new)
        if not (HEX32.match(str(r.get("uid"))) and isinstance(r.get("secret"), str) and len(r["secret"]) >= 32):
            raise HTTPError(502, "Réponse inattendue du serveur lié")
        with db.tx():
            pid = self._save_peer(r["uid"], r.get("name"), data["a"], data["f"], r["secret"], r.get("version"))
        self._has_peers = True
        peer = self.peer(pid)
        self.avatar_todo.setdefault(pid, []).extend(self.apply_users(peer, r.get("users") or [], [], mode="adopt"))
        log.warning("Serveur lié ajouté : %s (%s)", peer["name"], peer["address"])
        self.spawn_tunnels(pid)
        self.wake.set()
        return peer

    def _save_peer(self, uid, name, address, fingerprint, secret, version):
        """Nouveau serveur lié, ou nouveau jumelage d'un serveur déjà connu (délié de son côté seulement, ou
        réinstallé) : son ancien secret et son ancien certificat sont remplacés."""
        db = self.ctx.db
        name, version = str(name or "Noxa")[:40], str(version or "")[:20]
        pid = db.scalar("SELECT id FROM peers WHERE uid=?", (uid,))
        if pid:
            db.run("UPDATE peers SET name=?, address=?, fingerprint=?, secret=?, version=?, online=1, error=NULL WHERE id=?",
                   (name, address, fingerprint, secret, version, pid))
        else:
            pid = db.run("INSERT INTO peers(uid,name,address,fingerprint,secret,created_at,version) VALUES(?,?,?,?,?,?,?)",
                         (uid, name, address, fingerprint, secret, int(time.time()), version)).lastrowid
        self.reset_link(pid)  # (un identifiant de serveur supprimé peut être réattribué)
        return pid

    def _invite_row(self, req):
        ctx, db = self.ctx, self.ctx.db
        ctx.throttle.check("fedjoin:" + req.client_ip)
        token = str(req.json().get("token") or "")
        row = db.one("SELECT * FROM fed_invites WHERE token_hash=? AND expires_at>?",
                     (hashlib.sha256(token.encode()).hexdigest(), int(time.time())))
        if not row:
            ctx.throttle.fail("fedjoin:" + req.client_ip)
            raise HTTPError(403, "Invitation invalide ou expirée : créez-en une nouvelle")
        return row

    def accept_preview(self, req):
        """Avec une invitation valide : nos identifiants, pour repérer ceux qui existent aussi chez l'autre."""
        self._invite_row(req)
        return {"name": self.name(), "users": [{"username": u["username"], "display_name": u["display_name"], "uid": u["uid"]}
                                               for u in self.ctx.db.all("SELECT username, display_name, uid FROM users")]}

    def accept_join(self, req):
        """Demande d'adhésion d'un autre serveur, avec le jeton d'une invitation de ce serveur."""
        db = self.ctx.db
        row = self._invite_row(req)
        b = req.json()
        uid, fp = str(b.get("uid") or ""), str(b.get("fingerprint") or "")
        if not HEX32.match(uid) or not HEX64.match(fp):
            raise HTTPError(400, "Demande de jumelage invalide")
        if uid == self.uid():
            raise HTTPError(400, "Cette invitation vient de ce serveur : collez-la sur l'autre serveur")
        address = valid_address(b.get("address"))
        secret = secrets.token_urlsafe(32)
        with db.tx():
            db.run("DELETE FROM fed_invites WHERE token_hash=?", (row["token_hash"],))
            pid = self._save_peer(uid, b.get("name"), address, fp, secret, b.get("version"))
        self._has_peers = True
        peer = self.peer(pid)
        users = b.get("users") if isinstance(b.get("users"), list) else []
        self.avatar_todo.setdefault(pid, []).extend(self.apply_users(peer, users, [], mode="keep"))
        log.warning("Serveur lié ajouté : %s (%s)", peer["name"], address or "par tunnel")
        self.wake.set()
        return {"uid": self.uid(), "name": self.name(), "version": __version__, "secret": secret,
                "users": self.users_payload()}

    def remove(self, pid, notify=True):
        peer = self.peer(pid)
        if notify:
            try:
                self.ctx.db.release()
                self.call(peer, "POST", "/fed/leave", {}, timeout=5)
            except (FedError, OSError):
                pass
        db = self.ctx.db
        db.run("DELETE FROM items WHERE peer_id=?", (pid,))  # y compris ceux rangés dans nos bibliothèques (fusion)
        db.run("DELETE FROM peers WHERE id=?", (pid,))  # et ses bibliothèques recopiées
        self._has_peers = None
        self.reset_link(pid)
        log.warning("Serveur lié retiré : %s", peer["name"])
