"""Fédération : plusieurs serveurs Noxa d'un même propriétaire, liés entre eux.

- Jumelage : un serveur crée une invitation (son adresse, l'empreinte de son certificat, un jeton à usage unique
  valable 15 minutes). L'autre la colle, vérifie l'empreinte du serveur qu'il contacte et reçoit un secret partagé.
  Ensuite tout passe par un port dédié (8421) en TLS : chaque serveur épingle le certificat de l'autre (pas
  d'autorité de certification, pas de nom de domaine) et présente le secret à chaque requête.
- Tunnel : un serveur qui peut joindre l'autre lui garde quelques connexions ouvertes, par lesquelles l'autre lui
  envoie ses requêtes. Un seul des deux doit donc être joignable : rien à ouvrir sur la box de l'autre.
- Comptes : répliqués dans les deux sens ; la modification la plus récente l'emporte, une suppression est définitive.
- Catalogue : les bibliothèques de chaque serveur sont recopiées chez l'autre (fiches ; affiches relayées).
- Lecture : le serveur auquel l'appareil est connecté relaie le flux du serveur qui a le fichier, et lui renvoie la
  progression ; la progression enregistrée chez l'un revient chez l'autre.
"""
import base64
import json
import logging
import re
import secrets
import select
import socket
import threading
import time
import urllib.parse

from ..auth import find_user
from ..catalog import SID
from ..discover import SeerrError
from ..metadata import TMDB, MetaError
from ..web import HTTPError

log = logging.getLogger("noxa.fed")

DEFAULT_PORT = 8421
INVITE_PREFIX = "NEXIS1-"
INVITE_TTL = 15 * 60
SYNC_EVERY = 15          # s entre deux synchronisations avec chaque serveur lié
IDLE_TUNNELS = 4         # connexions de tunnel gardées ouvertes vers chaque serveur lié joignable
TUNNEL_TTL = 240         # s : une connexion de tunnel inutilisée est renouvelée
CATALOG_PAGE = 400
PROGRESS_PAGE = 1000
HEX32 = re.compile(r"^[0-9a-f]{32}$")
BEDTIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ADDRESS = re.compile(r"^(\[[0-9a-fA-F:.]{2,45}\]|[A-Za-z0-9.-]{1,253})(:\d{1,5})?$")
USER_FIELDS = ("username", "display_name", "password_hash", "is_admin", "all_libraries", "disabled", "avatar_color",
               "pin_hash", "managed", "home", "max_age", "allow_unrated", "avatar_ver", "kids", "totp_enabled",
               "totp_secret", "totp_recovery", "language", "max_streams", "remote_kbps", "daily_minutes", "bedtime")
LIMIT_FIELDS = ("max_streams", "remote_kbps", "daily_minutes", "bedtime")  # depuis la 3.34 : absents chez un serveur plus ancien
ITEM_LOCAL = {"id", "library_id", "parent_id", "show_id", "key", "fseq", "peer_id", "remote_id", "src_lib", "src_parent",
              "src_show", "alias_of"}
ITEM_SUB = re.compile(r"^/api/items/(\d{1,12})/(prepare|subtitles/.+|fonts/.+|trickplay(?:/.+)?|chapters/.+)$")
STREAM = re.compile(rf"^/api/stream/{SID}/(.+)$")
WATCHED = re.compile(r"^/api/items/(\d{1,12})/watched$")
ADMIN_ITEM = re.compile(r"^/api/admin/(?:identify|refresh|markers)/(\d{1,12})$")
ADMIN_STREAM = re.compile(rf"^/api/admin/streams/{SID}$")
DOWNLOAD_JOB = re.compile(r"^/api/downloads/([A-Za-z0-9_-]{8,20})(/file)?$")
TMDB_PATH = re.compile(r"^/(movie|tv|search|discover|trending|watch|genre|find|configuration|collection|person)"
                       r"(/[A-Za-z0-9_.-]{1,40}){0,5}$")
SEERR_ALLOWED = (("GET", re.compile(r"^/media\?[A-Za-z0-9=&_.-]{0,200}$")),
                 ("GET", re.compile(r"^/user\?[A-Za-z0-9=&_.-]{0,200}$")),
                 ("POST", re.compile(r"^/request$")))
# ce qu'un serveur lié peut demander au nom d'un compte : la lecture des éléments de ce serveur, rien d'autre
PEER_USER_PATHS = re.compile(r"^/api/(play|progress|stop|stream/.+|downloads(/options|/[A-Za-z0-9_-]{8,20}(/file)?)?|"
                             r"items/\d{1,12}/(prepare|subtitles/.+|fonts/.+|"
                             r"trickplay(/.+)?|chapters/.+|watched))$")
# administration à distance : un administrateur connecté à un serveur lié règle celui-ci (même compte, répliqué)
PEER_ADMIN_PATHS = re.compile(r"^/api/admin/.+$")
LOCAL_ADMIN_ONLY = re.compile(r"^/api/admin/servers$")  # la liste des serveurs administrables : toujours d'ici
STREAMED_UPLOAD = re.compile(r"^/api/admin/backups/upload$")


class _Limited:
    """Corps d'une requête lu au fur et à mesure (au plus `left` octets), pour le relayer sans le copier en mémoire."""

    def __init__(self, f, left):
        self.f, self.left = f, left

    def read(self, n=65536):
        if self.left <= 0:
            return b""
        chunk = self.f.read(min(n, self.left, 1024 * 1024))
        self.left -= len(chunk)
        return chunk


class FedError(Exception):
    pass


def split_address(address):
    u = urllib.parse.urlsplit("//" + address)
    return u.hostname, u.port or DEFAULT_PORT


def valid_address(v):
    v = str(v or "").strip()
    if v.startswith(("http://", "https://")):
        v = v.split("://", 1)[1]
    v = v.rstrip("/")
    if not v:
        return None
    if not ADDRESS.match(v) or (":" in v and not v.endswith("]") and not 0 < int(v.rsplit(":", 1)[1]) < 65536):
        raise HTTPError(400, "Adresse invalide : nom ou adresse IP, suivi du port si besoin (ex. 192.168.1.20:8421)")
    return v


def encode_invite(data):
    raw = json.dumps(data, separators=(",", ":")).encode()
    return INVITE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_invite(text):
    text = "".join(str(text or "").split())
    if not text.startswith(INVITE_PREFIX):
        raise HTTPError(400, "Invitation invalide : copiez-la en entier depuis l'autre serveur")
    body = text[len(INVITE_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        ok = (isinstance(data, dict) and HEX32.match(str(data.get("id"))) and HEX64.match(str(data.get("f")))
              and isinstance(data.get("t"), str) and 20 <= len(data["t"]) <= 100 and data.get("a"))
    except (ValueError, TypeError):
        ok = False
    if not ok:
        raise HTTPError(400, "Invitation invalide : copiez-la en entier depuis l'autre serveur")
    data["a"] = valid_address(data["a"])
    return data


def free_username(db, base, taken=()):
    """Identifiant libre dérivé de base (« nina-2 », « nina-3 »…), ici et parmi `taken`."""
    taken = {t.casefold() for t in taken}
    for n in range(2, 1000):
        cand = f"{base[:31 - len(str(n))]}-{n}"
        if cand.casefold() not in taken and not find_user(db, cand):
            return cand
    return f"u-{secrets.token_hex(6)}"


def keepalive(sock):
    """Connexion coupée sans prévenir (changement d'adresse IP, box redémarrée) : détectée en une minute environ,
    au lieu de rester ouverte « dans le vide » jusqu'à son renouvellement."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, v in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), v)
    except OSError:
        pass


def alive(sock):
    """Connexion de tunnel inutilisée encore valable : rien à lire (sinon c'est sa fermeture par l'autre côté)."""
    try:
        if sock.pending():
            return False
        ready, _, _ = select.select([sock], [], [], 0)
        return not ready
    except (OSError, ValueError):
        return False


class Pool:
    """Connexions de tunnel ouvertes par un serveur lié, en attente d'une requête de notre part."""

    def __init__(self):
        self.cond = threading.Condition()
        self.idle = []

    def offer(self, sock):
        """Fil de la connexion entrante : attend qu'une requête s'en serve (puis qu'elle soit finie), ou l'expiration."""
        keepalive(sock)
        entry = {"sock": sock, "born": time.monotonic(), "done": threading.Event(), "taken": False}
        with self.cond:
            self.idle.append(entry)
            self.cond.notify_all()
        if entry["done"].wait(TUNNEL_TTL):
            return
        with self.cond:
            if not entry["taken"]:
                if entry in self.idle:
                    self.idle.remove(entry)
                return
        entry["done"].wait()

    def take(self, timeout):
        end = time.monotonic() + timeout
        with self.cond:
            while True:
                now = time.monotonic()
                while self.idle:
                    e = self.idle.pop()
                    if now - e["born"] < TUNNEL_TTL - 15 and alive(e["sock"]):
                        e["taken"] = True
                        return e["sock"], e["done"].set
                    e["done"].set()  # trop ancienne ou coupée : l'autre serveur va la renouveler
                if now >= end:
                    return None
                self.cond.wait(end - now)

    def count(self):
        with self.cond:
            return len(self.idle)


class PeerTMDB(TMDB):
    """TMDB interrogé avec la clé d'un serveur lié (ce serveur n'en a pas) : même usage qu'une clé locale."""

    def __init__(self, fed, peer, language, country):
        super().__init__("", language, country)
        self.fed, self.peer = fed, peer

    @property
    def enabled(self):
        return True

    def get(self, path, lang=None, **params):
        params = {k: v for k, v in params.items() if v not in (None, "")}
        key = json.dumps([path, lang or self.language, sorted(params.items())], default=str)
        if key in self._cache:
            return self._cache[key]
        try:
            status, data = self.fed.call(self.peer, "POST", "/fed/tmdb",
                                         {"path": path, "lang": lang or self.language, "params": params}, timeout=60)
        except FedError as e:
            raise MetaError(f"TMDB injoignable ({e})")
        if status != 200:
            raise MetaError(data.get("error") or f"TMDB a répondu {status}")
        self._cache[key] = data
        if len(self._cache) > 500:
            self._cache.clear()
        return data


class PeerSeerr:
    """Seerr d'un serveur lié : les demandes de ce serveur passent par lui."""
    enabled = True

    def __init__(self, fed, peer):
        self.fed, self.peer = fed, peer
        self.name = peer["name"]

    def call(self, method, path, body=None, user=None, timeout=15):
        try:
            status, data = self.fed.call(self.peer, "POST", "/fed/seerr",
                                         {"method": method, "path": path, "body": body, "user": user}, timeout=timeout + 15)
        except FedError as e:
            raise SeerrError(f"Seerr injoignable ({e})") from None
        if status != 200:
            raise SeerrError(data.get("error") or f"Seerr a répondu {status}")
        return data.get("data") or {}
