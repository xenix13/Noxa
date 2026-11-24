"""Linked servers: identity (certificate), start-up, the list of linked servers, outgoing connections and the tunnel."""
import hashlib
import hmac
import http.client
import json
import os
import queue
import secrets
import select
import socket
import ssl
import subprocess
import threading
import time

from .. import i18n
from ..web import TLSServer, make_handler
from .common import FedError, HEX32, IDLE_TUNNELS, Pool, SYNC_EVERY, TUNNEL_TTL, keepalive, log, split_address


class LinksMixin:
    # ------------------------------------------------------------------ identité
    def uid(self):
        db = self.ctx.db
        v = db.setting("server_uid")
        if not v:
            v = secrets.token_hex(16)
            db.set_setting("server_uid", v)
        return v

    def name(self):
        return self.ctx.db.setting("server_name") or (socket.gethostname() or "Noxa")[:40]

    def ready(self):
        return os.path.isfile(self.cert) and os.path.isfile(self.key)

    def ensure_cert(self):
        """Certificat propre à ce serveur, créé au premier jumelage (clé EC P-256, valable 100 ans)."""
        if self.ready():
            return
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        key_tmp, cert_tmp = self.key + ".tmp", self.cert + ".tmp"
        cmd = ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
               "-keyout", key_tmp, "-out", cert_tmp, "-days", "36500", "-subj", f"/CN=Noxa {self.uid()[:12]}"]
        old = os.umask(0o077)
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        except FileNotFoundError:
            raise FedError("openssl est introuvable sur le serveur : installez-le (apt install openssl)")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.error("Certificat de fédération : %s", getattr(e, "stderr", e))
            raise FedError("Création du certificat du serveur impossible")
        finally:
            os.umask(old)
        os.chmod(key_tmp, 0o600)
        os.replace(key_tmp, self.key)
        os.replace(cert_tmp, self.cert)
        self._fp = None

    def fingerprint(self):
        if not self._fp and self.ready():
            with open(self.cert) as f:
                self._fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert(f.read())).hexdigest()
        return self._fp

    def local_address(self):
        """Adresse de ce serveur proposée dans les invitations (réglable par l'administrateur)."""
        manual = self.ctx.db.setting("fed_address")
        if manual:
            return manual
        ip = "127.0.0.1"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))  # aucune donnée envoyée : sert à connaître l'adresse locale de sortie
                ip = s.getsockname()[0]
        except OSError:
            pass
        return f"{ip}:{self.port}"

    # --------------------------------------------------------------- démarrage
    def attach(self, app, x):
        self.x = x
        self.handler = make_handler(app, fed=self)

    def listen(self):
        with self.lock:
            if self.server:
                return
            self.ensure_cert()
            sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            sctx.minimum_version = ssl.TLSVersion.TLSv1_2
            sctx.load_cert_chain(self.cert, self.key)
            try:
                self.server = TLSServer((self.host, self.port), self.handler, sctx)
            except OSError as e:
                raise FedError(f"Port {self.port} indisponible pour les serveurs liés : {e.strerror or e}")
            self.port = self.server.server_address[1]
            threading.Thread(target=self.server.serve_forever, daemon=True, name="fed-listen").start()
            log.info("Serveurs liés : écoute TLS sur le port %d", self.port)

    def has_peers(self):
        if self._has_peers is None:
            self._has_peers = bool(self.ctx.db.scalar("SELECT 1 FROM peers LIMIT 1"))
        return self._has_peers

    def start(self, loop=True):
        try:
            if self.ready() or self.has_peers():
                self.listen()
        except FedError as e:
            log.error("%s", e)
        finally:
            self.ctx.db.release()
        if loop:
            threading.Thread(target=self.run, daemon=True, name="fed-sync").start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.server:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.sync_all()
            except Exception:
                log.exception("Synchronisation des serveurs liés")
            finally:
                self.ctx.db.release()
            self.wake.wait(SYNC_EVERY)
            self.wake.clear()

    def sync_all(self):
        if not self.has_peers():
            return
        live = {r["id"] for r in self.ctx.db.all("SELECT id FROM streams")}
        with self.lock:
            for sid in [s for s in self.proxied if s not in live]:
                self.proxied.pop(sid, None)
        for p in self.peers():
            if p["address"]:
                self.spawn_tunnels(p["id"])
            self.sync(p["id"])

    # ------------------------------------------------------------------ serveurs liés
    def peers(self):
        return self.ctx.db.all("SELECT * FROM peers ORDER BY name COLLATE NOCASE")

    def peer(self, pid):
        p = self.ctx.db.one("SELECT * FROM peers WHERE id=?", (pid,))
        if not p:
            raise FedError("Serveur lié introuvable")
        return p

    def authenticate(self, headers):
        puid, key = headers.get("X-Fed-Peer") or "", headers.get("X-Fed-Key") or ""
        if not HEX32.match(puid) or not key:
            return None
        p = self.ctx.db.one("SELECT * FROM peers WHERE uid=?", (puid,))
        if p and hmac.compare_digest(key.encode(), p["secret"].encode()):
            return p
        return None

    def user_for(self, req):
        """Compte au nom duquel le serveur lié fait la requête (même compte, répliqué)."""
        uid = req.headers.get("X-Fed-User") or ""
        if not HEX32.match(uid):
            return None
        u = self.ctx.db.one("SELECT * FROM users WHERE uid=? AND disabled=0", (uid,))
        if u:
            for k in ("password_hash", "totp_secret", "totp_pending", "totp_recovery"):
                u.pop(k, None)
        return u

    # ------------------------------------------------------------------ connexions sortantes
    def _client_context(self):
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE  # la confiance vient de l'empreinte épinglée, vérifiée juste après
        c.minimum_version = ssl.TLSVersion.TLSv1_2
        return c

    def _tls_connect(self, peer, timeout):
        host, port = split_address(peer["address"])
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            s = self._client_context().wrap_socket(raw)
        except (OSError, ssl.SSLError):
            raw.close()
            raise
        der = s.getpeercert(binary_form=True)
        if not der or not hmac.compare_digest(hashlib.sha256(der).hexdigest(), peer["fingerprint"]):
            s.close()
            raise FedError("Certificat inattendu : ce n'est pas le serveur jumelé")
        return s

    def _open(self, peer, timeout):
        """Connexion vers un serveur lié : tunnel qu'il nous a ouvert, sinon connexion directe."""
        pool = self.pools.get(peer.get("id"))
        got = pool.take(0) if pool else None
        if got:
            return got[0], got[1], "tunnel"
        error = None
        if peer.get("address"):
            try:
                return self._tls_connect(peer, min(timeout, 15)), None, "direct"
            except FedError:
                raise
            except OSError as e:
                error = e
        if pool:
            got = pool.take(min(timeout, 10))
            if got:
                return got[0], got[1], "tunnel"
        if error:
            raise FedError(f"Serveur injoignable ({peer['address']}) : {error.strerror or error}")
        raise FedError("Serveur injoignable : il n'a pas d'adresse et n'a pas ouvert de tunnel")

    def request(self, peer, method, path, body=None, headers=None, user=None, timeout=30):
        try:
            return self._request(peer, method, path, body, headers, user, timeout)
        except FedError as e:
            # connexion de tunnel morte (l'autre serveur a changé d'adresse IP) : la requête n'est pas arrivée,
            # on la refait une fois par une autre connexion
            if getattr(e, "retry", False):
                return self._request(peer, method, path, body, headers, user, timeout)
            raise

    def _request(self, peer, method, path, body, headers, user, timeout):
        sock, release, via = self._open(peer, timeout)
        sock.settimeout(timeout)
        conn = http.client.HTTPConnection("nexis-peer", timeout=timeout)
        conn.sock = sock
        h = {"X-Fed-Peer": self.uid(), "X-Fed-Key": peer.get("secret") or "", "Connection": "close",
             "X-Lang": i18n.current()}
        if user:
            h["X-Fed-User"] = user["uid"]
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})

        def close():
            try:
                conn.close()
            finally:
                if release:
                    release()
        sent = False
        try:
            conn.request(method, path, body=body, headers=h)
            sent = True
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            close()
            err = FedError(f"Le serveur lié ne répond pas : {e}")
            err.retry = via == "tunnel" and (not sent or method == "GET" or isinstance(
                e, (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)))
            raise err
        if peer.get("id"):
            self.status.setdefault(peer["id"], {})["via"] = via
        return resp, close

    def call(self, peer, method, path, data=None, user=None, timeout=30):
        resp, close = self.request(peer, method, path, data, user=user, timeout=timeout)
        try:
            raw = resp.read(256 * 1024 * 1024)
        except (OSError, http.client.HTTPException) as e:
            raise FedError(f"Le serveur lié ne répond pas : {e}")
        finally:
            close()
        try:
            return resp.status, json.loads(raw or b"{}")
        except ValueError:
            raise FedError("Réponse inattendue du serveur lié")

    def call_ok(self, peer, method, path, data=None, user=None, timeout=30):
        status, out = self.call(peer, method, path, data, user, timeout)
        if status != 200:
            raise FedError(out.get("error") if isinstance(out, dict) and out.get("error") else f"HTTP {status}")
        return out

    def send_later(self, peer_id, method, path, data, user):
        """Progression, arrêts, « vu » : envoyés au serveur lié dans l'ordre, sans faire attendre l'appareil."""
        with self.lock:
            q = self.queues.get(peer_id)
            if q is None:
                q = self.queues[peer_id] = queue.Queue(500)
                threading.Thread(target=self._sender, args=(peer_id, q), daemon=True).start()
        try:
            q.put_nowait((method, path, data, {"uid": user["uid"]} if user else None))
        except queue.Full:
            pass

    def _sender(self, peer_id, q):
        while not self.stop_event.is_set():
            method, path, data, user = q.get()
            try:
                self.call(self.peer(peer_id), method, path, data, user=user, timeout=20)
            except FedError as e:
                log.debug("Envoi au serveur lié %s : %s", peer_id, e)
            finally:
                self.ctx.db.release()

    # ------------------------------------------------------------------ tunnel
    def pool_for(self, pid):
        with self.lock:
            return self.pools.setdefault(pid, Pool())

    def spawn_tunnels(self, pid):
        with self.lock:
            if self.stop_event.is_set() or time.time() - self.tunnel_fail.get(pid, 0) < 15:
                return
            n = IDLE_TUNNELS - self.tunnels.get(pid, 0)
            self.tunnels[pid] = self.tunnels.get(pid, 0) + max(0, n)
            gen = self.tunnel_gen.get(pid, 0)
        for _ in range(max(0, n)):
            threading.Thread(target=self._tunnel, args=(pid, gen), daemon=True, name="fed-tunnel").start()

    def reset_link(self, pid):
        """Lien remplacé ou supprimé : connexions, tunnels et échecs passés oubliés."""
        with self.lock:
            self.tunnel_gen[pid] = self.tunnel_gen.get(pid, 0) + 1
            self.tunnels.pop(pid, None)
            self.tunnel_fail.pop(pid, None)
            self.status.pop(pid, None)
            self.rgroups.pop(pid, None)
            pool = self.pools.pop(pid, None)
            for sid in [s for s, v in self.proxied.items() if v[0] == pid]:
                self.proxied.pop(sid)
        while pool:
            got = pool.take(0)
            if not got:
                break
            try:
                got[0].close()
            except OSError:
                pass
            got[1]()  # libère le fil qui gardait cette connexion

    def _uncount(self, pid, gen):
        with self.lock:
            if self.tunnel_gen.get(pid, 0) == gen:
                self.tunnels[pid] = max(0, self.tunnels.get(pid, 1) - 1)

    def _tunnel(self, pid, gen=0):
        """Connexion ouverte vers un serveur lié, pour qu'il nous envoie ses requêtes (il n'a pas à nous joindre)."""
        counted = True
        sock = None
        try:
            peer = self.peer(pid)
            self.ctx.db.release()
            if not peer["address"]:
                return
            sock = self._tls_connect(peer, 15)
            keepalive(sock)
            sock.sendall((f"GET /fed/reverse HTTP/1.1\r\nHost: nexis\r\nX-Fed-Peer: {self.uid()}\r\n"
                          f"X-Fed-Key: {peer['secret']}\r\n\r\n").encode())
            self.ctx.db.release()
            head = b""
            while b"\r\n\r\n" not in head:
                c = sock.recv(1)
                if not c or len(head) > 4096:
                    raise FedError("Tunnel refusé")
                head += c
            if not head.startswith(b"HTTP/1.1 101"):
                raise FedError("Tunnel refusé : " + head.split(b"\r\n", 1)[0].decode(errors="replace"))
            sock.settimeout(None)
            if not sock.pending():
                ready, _, _ = select.select([sock], [], [], TUNNEL_TTL + 60)
                if not ready:
                    return
            # une requête arrive : on rouvre tout de suite une connexion d'avance, puis on la traite
            self._uncount(pid, gen)
            counted = False
            self.spawn_tunnels(pid)
            self.handler(sock, split_address(peer["address"]), None)
        except (FedError, OSError, ssl.SSLError) as e:
            with self.lock:
                if self.tunnel_gen.get(pid, 0) == gen:
                    self.tunnel_fail[pid] = time.time()
            log.debug("Tunnel vers le serveur lié %s : %s", pid, e)
        except Exception:
            log.exception("Tunnel vers le serveur lié %s", pid)
        finally:
            if counted:
                self._uncount(pid, gen)
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
            self.ctx.db.release()
            if not counted and not self.stop_event.is_set() and self.tunnel_gen.get(pid, 0) == gen:
                self.spawn_tunnels(pid)
