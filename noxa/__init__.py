"""Noxa: self-hosted media server."""
import os

__version__ = "4.2.1"


def env(name, default=None):
    """Setting from the environment: NOXA_<name>, or SEANCE_<name> (Noxa's former internal name, still read so
    that existing installs and Docker files keep working)."""
    v = os.environ.get("NOXA_" + name)
    return v if v is not None else os.environ.get("SEANCE_" + name, default)
