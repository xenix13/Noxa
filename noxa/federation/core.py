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
        self.pools = {}         # linked server id -> Pool (tunnel opened by it)
        self.tunnels = {}       # id -> tunnel connections we keep open to it (waiting)
        self.tunnel_fail = {}   # id -> time of the last failure to open one
        self.tunnel_gen = {}    # id -> generation (new pairing: the old connections no longer count)
        self.status = {}        # id -> state shown (online, link, latency)
        self.proxied = {}       # relayed stream -> (linked server id, user id, local item id)
        self.dl_jobs = {}       # download prepared by a linked server -> (server id, user id, local id)
        self.queues = {}        # id -> queue of background sends (progress, stops), in order
        self.sync_locks = {}
        self.orphans = {}       # items received before their parent: local id -> (parent, show) on the linked server
        self.avatar_todo = {}   # id -> accounts whose avatar is to be fetched
        self.rgroups = {}       # id -> (time, "watch together" groups it hosts)
        self.rgroup_peer = {}   # group of a linked server -> id of that server
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self._fp = None
        self._has_peers = None
        self._columns = None
