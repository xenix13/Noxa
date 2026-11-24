"""Federation: several Noxa servers of one owner, linked together.

- Pairing: a server creates an invitation (its address, its certificate's fingerprint, a one-time token
  valid 15 minutes). The other one pastes it, checks the fingerprint of the server it contacts and gets a shared secret.
  Then everything goes through a dedicated port (8421) over TLS: each server pins the other's certificate (no
  certificate authority, no domain name) and presents the secret with each request.
- Tunnel: a server that can reach the other keeps a few connections open to it, through which the other sends it
  its requests. So only one of the two has to be reachable: nothing to open on the other one's router.
- Accounts: replicated both ways; the most recent change wins, a deletion is final.
- Catalogue: each server's libraries are copied to the other (records; posters relayed).
- Playback: the server the device is connected to relays the stream of the server that has the file, and sends it
  the progress back; the progress saved on one comes back on the other.
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
SYNC_EVERY = 15          # s between two synchronisations with each linked server
IDLE_TUNNELS = 4         # tunnel connections kept open to each reachable linked server
TUNNEL_TTL = 240         # s: an unused tunnel connection is renewed
CATALOG_PAGE = 400
PROGRESS_PAGE = 1000
HEX32 = re.compile(r"^[0-9a-f]{32}$")
BEDTIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ADDRESS = re.compile(r"^(\[[0-9a-fA-F:.]{2,45}\]|[A-Za-z0-9.-]{1,253})(:\d{1,5})?$")
USER_FIELDS = ("username", "display_name", "password_hash", "is_admin", "all_libraries", "disabled", "avatar_color",
               "pin_hash", "managed", "home", "max_age", "allow_unrated", "avatar_ver", "kids", "totp_enabled",
               "totp_secret", "totp_recovery", "language", "max_streams", "remote_kbps", "daily_minutes", "bedtime")
LIMIT_FIELDS = ("max_streams", "remote_kbps", "daily_minutes", "bedtime")  # since 3.34: missing on an older server
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
# what a linked server may ask for on behalf of an account: playing this server's items, nothing else
PEER_USER_PATHS = re.compile(r"^/api/(play|progress|stop|stream/.+|downloads(/options|/[A-Za-z0-9_-]{8,20}(/file)?)?|"
                             r"items/\d{1,12}/(prepare|subtitles/.+|fonts/.+|"
                             r"trickplay(/.+)?|chapters/.+|watched))$")
# remote administration: an administrator signed in to a linked server sets this one up (same account, replicated)
PEER_ADMIN_PATHS = re.compile(r"^/api/admin/.+$")
LOCAL_ADMIN_ONLY = re.compile(r"^/api/admin/servers$")  # the list of servers that can be administered: always from here
STREAMED_UPLOAD = re.compile(r"^/api/admin/backups/upload$")


class _Limited:
    """Request body read as it comes (at most `left` bytes), to relay it without copying it in memory."""

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
    """Free username derived from base ("nina-2", "nina-3"…), here and among `taken`."""
    taken = {t.casefold() for t in taken}
    for n in range(2, 1000):
        cand = f"{base[:31 - len(str(n))]}-{n}"
        if cand.casefold() not in taken and not find_user(db, cand):
            return cand
    return f"u-{secrets.token_hex(6)}"


def keepalive(sock):
    """Connection cut without warning (IP address change, router restarted): detected in about a minute,
    instead of staying open "into the void" until it is renewed."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, v in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), v)
    except OSError:
        pass


def alive(sock):
    """Unused tunnel connection still valid: nothing to read (otherwise the other side closed it)."""
    try:
        if sock.pending():
            return False
        ready, _, _ = select.select([sock], [], [], 0)
        return not ready
    except (OSError, ValueError):
        return False


class Pool:
    """Tunnel connections opened by a linked server, waiting for a request from us."""

    def __init__(self):
        self.cond = threading.Condition()
        self.idle = []

    def offer(self, sock):
        """Thread of the incoming connection: waits for a request to use it (and to finish), or for the expiry."""
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
                    e["done"].set()  # too old or cut: the other server will renew it
                if now >= end:
                    return None
                self.cond.wait(end - now)

    def count(self):
        with self.cond:
            return len(self.idle)


class PeerTMDB(TMDB):
    """TMDB queried with a linked server's key (this server has none): same use as a local key."""

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
    """Seerr of a linked server: this server's requests go through it."""
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
