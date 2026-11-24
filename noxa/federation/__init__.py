"""Linked servers: several Noxa servers of one owner, linked together (see common.py for how it works).

The Federation class is split by topic: links.py (identity, connections, tunnel), pairing.py, sync.py (accounts,
preferences, catalogue), relay.py (playback relayed to the server that has the file), shared.py (shared services,
load, watch together, admin status); routes.py holds the HTTP routes.
"""
from .common import (DEFAULT_PORT, INVITE_PREFIX, LOCAL_ADMIN_ONLY, PEER_ADMIN_PATHS, PEER_USER_PATHS, FedError,
                     decode_invite, encode_invite, split_address, valid_address)
from .core import Federation
from .routes import register

__all__ = ["DEFAULT_PORT", "INVITE_PREFIX", "LOCAL_ADMIN_ONLY", "PEER_ADMIN_PATHS", "PEER_USER_PATHS", "FedError",
           "Federation", "decode_invite", "encode_invite", "register", "split_address", "valid_address"]
