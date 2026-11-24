"""The Federation object: one per server, made of the topic mixins of this package."""
import os
import threading

from .. import env
from .common import DEFAULT_PORT
from .links import LinksMixin
from .pairing import PairingMixin
from .relay import RelayMixin
from .shared import SharedMixin
from .sync import SyncMixin


class Federation(LinksMixin, PairingMixin, SyncMixin, RelayMixin, SharedMixin):
    def __init__(self, ctx, port=None):
        self.ctx = ctx
        self.dir = os.path.join(ctx.data_dir, "federation")
        self.cert = os.path.join(self.dir, "cert.pem")
        self.key = os.path.join(self.dir, "key.pem")
        self.port = int(port if port is not None else env("FED_PORT", DEFAULT_PORT))
        self.host = env("FED_HOST") or env("HOST") or "0.0.0.0"
        self.handler = None
        self.server = None
        self.x = None
        self.lock = threading.Lock()
        self.pools = {}         # id du serveur lié -> Pool (tunnel ouvert par lui)
        self.tunnels = {}       # id -> connexions de tunnel que nous lui gardons ouvertes (en attente)
        self.tunnel_fail = {}   # id -> instant du dernier échec d'ouverture
        self.tunnel_gen = {}    # id -> génération (nouveau jumelage : les anciennes connexions ne comptent plus)
        self.status = {}        # id -> état affiché (en ligne, liaison, latence)
        self.proxied = {}       # flux relayé -> (id du serveur lié, id de l'utilisateur, id local de l'élément)
        self.dl_jobs = {}       # téléchargement préparé par un serveur lié -> (id du serveur, id de l'utilisateur, id local)
        self.queues = {}        # id -> file des envois en arrière-plan (progression, arrêts), dans l'ordre
        self.sync_locks = {}
        self.orphans = {}       # éléments reçus avant leur parent : id local -> (parent, série) chez le serveur lié
        self.avatar_todo = {}   # id -> comptes dont l'avatar est à récupérer
        self.rgroups = {}       # id -> (instant, groupes « regarder ensemble » qu'il anime)
        self.rgroup_peer = {}   # groupe d'un serveur lié -> id de ce serveur
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self._fp = None
        self._has_peers = None
        self._columns = None
