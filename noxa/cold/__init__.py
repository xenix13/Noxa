"""Cold storage: copies of movies and episodes kept somewhere else than the library disks (see storage.py).

storage.py holds the storage backends (folder, SMB, WebDAV, S3, SFTP, rclone, linked server); the Cold class is split
by topic: copies.py (stores, copies, local cache), compression.py (archives hosted for linked servers, HEVC
compression), jobs.py (archiving queue, one copy, admin status); routes.py holds the HTTP routes.
"""
from .core import Cold
from .routes import register
from .storage import (ColdError, Local, Peer, Rclone, S3, SFTP, SMB, WebDAV, _BytesReader, compress_cmd, curl_sftp,
                      public_conf, rclone_exe, safe_name, safe_part, valid_conf)

__all__ = ["Cold", "ColdError", "Local", "Peer", "Rclone", "S3", "SFTP", "SMB", "WebDAV", "_BytesReader",
           "compress_cmd", "curl_sftp", "public_conf", "rclone_exe", "register", "safe_name", "safe_part", "valid_conf"]
