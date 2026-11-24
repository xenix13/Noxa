"""Linked servers: shared services (TMDB, Seerr), load balancing, titles across servers, watch together, admin status."""
import json
import threading
import time
import urllib.parse

from .. import i18n
from .common import FedError, HEX32, PeerSeerr, PeerTMDB, log


class SharedMixin:
    # ------------------------------------------------------------------ services partagés (TMDB, Seerr)
    def provider(self, cap):
        """Serveur lié joignable qui a ce service (clé TMDB, Seerr) et le prête aux autres, ou None."""
        if not self.has_peers():
            return None
        for p in self.peers():
            st = self.status.get(p["id"]) or {}
            if st.get("online") and st.get(cap):
                return p
        return None

    def shared_tmdb(self, language, country):
        p = self.provider("tmdb")
        return PeerTMDB(self, p, language, country) if p else None

    def shared_seerr(self):
        p = self.provider("seerr")
        return PeerSeerr(self, p) if p else None

    def kick_metadata(self):
        """Une clé TMDB devient disponible par un serveur lié : les titres restés sans métadonnées faute de clé
        sont cherchés (une fois par serveur prêteur)."""
        p = self.provider("tmdb")
        db = self.ctx.db
        if not p or db.setting("tmdb_key") or db.setting("fed_tmdb_from") == p["uid"]:
            return
        db.set_setting("fed_tmdb_from", p["uid"])
        n = db.run("UPDATE items SET meta_state='pending' WHERE peer_id IS NULL AND locked=0 AND type IN ('movie','show') "
                   "AND meta_state IN ('local','nomatch','error')").rowcount
        if n:
            log.info("Métadonnées : %d titre(s) à chercher avec la clé TMDB de %s", n, p["name"])
            threading.Thread(target=self._process_pending, daemon=True, name="fed-meta").start()

    def _process_pending(self):
        try:
            self.ctx.metadata.process_pending()
        except Exception:
            log.exception("Métadonnées par la clé TMDB d'un serveur lié")
        finally:
            self.ctx.db.release()

    # ------------------------------------------------------------------ charge (répartition des lectures)
    def local_load(self):
        """Lectures servies par ce serveur (les flux qu'il relaie depuis un serveur lié ne comptent pas)."""
        cut = int(time.time()) - 60
        ids = [r["id"] for r in self.ctx.db.all("SELECT id FROM streams WHERE last_seen>?", (cut,))]
        with self.lock:
            return sum(1 for i in ids if i not in self.proxied)

    def busy_limit(self):
        try:
            return max(0, int(self.ctx.db.setting("fed_busy", "3")))
        except ValueError:
            return 3

    # ------------------------------------------------------------------ titres d'un serveur à l'autre
    def ref_of(self, item_id):
        """Désignation d'un élément valable sur tous les serveurs liés : le serveur qui a le fichier et son numéro là-bas."""
        r = self.ctx.db.one("SELECT i.id, i.remote_id, p.uid FROM items i LEFT JOIN peers p ON p.id=i.peer_id WHERE i.id=?",
                            (int(item_id),))
        if not r:
            return None
        return {"s": r["uid"], "i": r["remote_id"]} if r["uid"] else {"s": self.uid(), "i": r["id"]}

    def resolve_ref(self, ref):
        """Élément d'ici (celui qu'on affiche) correspondant à une désignation reçue d'un serveur lié, ou None."""
        if not isinstance(ref, dict) or not HEX32.match(str(ref.get("s"))) or not isinstance(ref.get("i"), int):
            return None
        db = self.ctx.db
        if ref["s"] == self.uid():
            return db.scalar("SELECT id FROM items WHERE id=? AND peer_id IS NULL", (ref["i"],))
        r = db.one("SELECT i.id, i.alias_of FROM items i JOIN peers p ON p.id=i.peer_id WHERE p.uid=? AND i.remote_id=?",
                   (ref["s"], ref["i"]))
        return (r["alias_of"] or r["id"]) if r else None

    def local_user_id(self, uid):
        return self.ctx.db.scalar("SELECT id FROM users WHERE uid=?", (str(uid or ""),))

    # ------------------------------------------------------------------ regarder ensemble entre serveurs
    def together_changed(self):
        """Les groupes de ce serveur ont changé : les serveurs liés rafraîchissent leur liste tout de suite."""
        if not self.has_peers():
            return
        for p in self.peers():
            if (self.status.get(p["id"]) or {}).get("online"):
                self.send_later(p["id"], "POST", "/fed/together/changed", {}, None)

    def present_uids(self):
        """Comptes qui ont Noxa ouvert ici : ils restent membres des groupes d'un serveur lié pendant qu'ils cherchent."""
        ids = sorted(self.ctx.together.present())
        if not ids:
            return []
        return [r["uid"] for r in self.ctx.db.all(f"SELECT uid FROM users WHERE id IN ({','.join('?' * len(ids))})", ids)]

    def fetch_groups(self, peer):
        q = urllib.parse.urlencode({"present": ",".join(self.present_uids())})
        data = self.call_ok(peer, "GET", f"/fed/together?{q}", timeout=8)
        groups = [g for g in data.get("groups") or [] if isinstance(g, dict) and isinstance(g.get("id"), str)]
        with self.lock:
            old = self.rgroups.get(peer["id"])
            self.rgroups[peer["id"]] = (time.monotonic(), groups)
            for g in groups:
                self.rgroup_peer[g["id"]] = peer["id"]
        if old is None or old[1] != groups:
            self.ctx.together.wake()
        return groups

    def remote_groups(self, max_age=3):
        """[(serveur lié, groupes)] des serveurs liés joignables (gardés quelques secondes)."""
        if not self.has_peers():
            return []
        out = []
        for p in self.peers():
            if not (self.status.get(p["id"]) or {}).get("online"):
                continue
            hit = self.rgroups.get(p["id"])
            if hit and time.monotonic() - hit[0] < max_age:
                out.append((p, hit[1]))
                continue
            try:
                out.append((p, self.fetch_groups(p)))
            except (FedError, OSError) as e:
                log.debug("Groupes du serveur lié %s : %s", p["id"], e)
                if hit:
                    out.append((p, hit[1]))
            finally:
                self.ctx.db.release()
        return out

    def group_peer(self, gid):
        """Serveur lié qui anime ce groupe, ou None."""
        pid = self.rgroup_peer.get(gid)
        if pid is None:
            self.remote_groups(max_age=0)
            pid = self.rgroup_peer.get(gid)
        if pid is None:
            return None
        try:
            return self.peer(pid)
        except FedError:
            return None

    def forget_groups(self, pid):
        with self.lock:
            self.rgroups.pop(pid, None)

    def to_local(self, pid, ev):
        """Événement ou état d'un groupe animé par un serveur lié -> nos numéros (titre, comptes) et notre horloge."""
        shift = int((self.status.get(pid) or {}).get("clock") or 0)
        for k in ("ref", "server_now"):
            if isinstance(ev.get(k), (int, float)):
                ev[k] = int(ev[k] - shift)
        if "item_ref" in ev:
            rk = self.resolve_ref(ev.pop("item_ref"))
            ev["item_id"] = str(rk) if rk else None
        if "waiting_uids" in ev:
            ev["waiting_ids"] = [i for i in (self.local_user_id(u) for u in ev.pop("waiting_uids") or []) if i]
        if isinstance(ev.get("members"), list):
            for m in ev["members"]:
                u = self.ctx.db.one("SELECT id, avatar_ver FROM users WHERE uid=?", (str(m.get("uid") or ""),))
                m["id"] = u["id"] if u else 0
                m["avatar"] = f"/api/users/{u['id']}/avatar?v={u['avatar_ver']}" if u and u["avatar_ver"] else None
                m.pop("uid", None)
        return ev

    # ------------------------------------------------------------------ état pour l'administration
    def view(self):
        db = self.ctx.db
        peers = []
        for p in self.peers():
            st = self.status.get(p["id"], {})
            pool = self.pools.get(p["id"])
            peers.append({
                "id": p["id"], "name": p["name"], "address": p["address"], "fingerprint": p["fingerprint"],
                "created_at": p["created_at"], "last_seen": p["last_seen"], "version": p["version"],
                "error": i18n.tr(p["error"]) if p["error"] else None, "online": st.get("online"),
                "latency": st.get("latency"), "via": st.get("via"), "load": st.get("load"),
                "shares": [k for k in ("tmdb", "seerr") if st.get(k)],
                "tunnel_in": pool.count() if pool else 0, "tunnel_out": self.tunnels.get(p["id"], 0),
                "libraries": [{"id": str(l["id"]), "name": l["name"], "kind": l["kind"], "count": l["n"],
                               "merged": l["n_alias"], "merge_into": str(l["merge_into"]) if l["merge_into"] else None}
                              for l in db.all(
                    "SELECT l.id, l.name, l.kind, l.merge_into, (SELECT COUNT(*) FROM items i WHERE i.src_lib=l.id AND "
                    "i.type IN ('movie','show')) AS n, (SELECT COUNT(*) FROM items i WHERE i.src_lib=l.id AND "
                    "i.type IN ('movie','show') AND i.alias_of IS NOT NULL) AS n_alias "
                    "FROM libraries l WHERE l.peer_id=? ORDER BY l.position", (p["id"],))]})
        try:
            notices = json.loads(db.setting("fed_notices") or "[]")
        except ValueError:
            notices = []
        return {"notices": notices, "local_libraries": [{"id": str(l["id"]), "name": l["name"], "kind": l["kind"]} for l in db.all(
                    "SELECT id, name, kind FROM libraries WHERE peer_id IS NULL ORDER BY position, name")],"name": self.name(), "uid": self.uid(), "fingerprint": self.fingerprint(), "port": self.port,
                "listening": bool(self.server), "address": self.local_address(),
                "address_manual": db.setting("fed_address") or "", "busy": self.busy_limit(), "load": self.local_load(),
                "peers": peers}
