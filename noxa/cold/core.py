"""The Cold object: one per server, made of the topic mixins of this package."""
import threading

from .compression import CompressionMixin
from .copies import CopiesMixin
from .jobs import JobsMixin


class Cold(CopiesMixin, CompressionMixin, JobsMixin):
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.Lock()
        self.tokens = {}       # jeton -> {store, name, size, expires}
        self.running = None    # tâche en cours (dict partagé avec _Reader : done, cancel)
        self.worker = None
        self.wake = threading.Event()
        self.fetch_queue = []  # titres à ramener en local
        self.fetching = None
        self.fetcher = None
