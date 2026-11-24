"""Stockage froid : des copies des films et épisodes rangées ailleurs que sur les disques de la bibliothèque.

Un titre archivé garde tout ce qui le décrit dans Noxa (affiches, résumé, historique, sous-titres téléchargés) ;
seul son fichier vidéo existe aussi (ou seulement, plus tard) sur un stockage froid. Quand le fichier de la
bibliothèque n'est plus là, Noxa lit la copie froide : directement si le stockage est un dossier de la machine
(disque USB, NAS monté, partage SMB), sinon par une adresse locale que seul ffmpeg connaît (/cold/<jeton>), qui
relit le stockage morceau par morceau (WebDAV, S3, SFTP, rclone).

Plusieurs stockages peuvent coexister ; un titre peut en avoir une copie sur chacun. Les copies se font en tâche de
fond, une à la fois, débit limité pendant une lecture, et chaque copie est vérifiée (taille, morceaux relus) avant
d'être comptée.

Types :
  - local  : un dossier de cette machine (disque USB, NAS déjà monté par le système, volume Docker) ;
  - smb    : un partage SMB/CIFS monté en écriture par seance-mountd (installation par script ou paquet) ;
  - sftp   : un serveur SSH (Hetzner Storage Box, NAS…) — par curl, clé du serveur vérifiée (empreinte retenue) ;
  - webdav : Nextcloud, Synology, Hetzner Storage Box… ;
  - s3     : AWS et compatibles (Backblaze B2, Wasabi, Scaleway, OVH, MinIO), envoi en plusieurs parties ;
  - rclone : n'importe quel « remote » rclone déjà configuré (pCloud, Mega, Google Drive…), si rclone est installé ;
  - peer   : un serveur de la fédération qui accepte d'accueillir des archives (place réservée par son administrateur),
             par le canal chiffré des serveurs liés.

Cache local : un titre lu depuis un stockage en ligne est ramené dans <données>/cold-cache, avec les épisodes qui
suivent ; la place prise reste dans un budget, les plus anciens repartent d'abord (la copie froide, elle, reste).
"""
import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import i18n, media
from ..remote_storage import S3 as S3Signer

log = logging.getLogger("noxa.cold")

KINDS = ("local", "smb", "sftp", "webdav", "s3", "rclone", "peer")
SECRETS = ("password", "secret_key")
BLOCK = 1 << 20
SIDECAR_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup")
SAMPLES = 6              # morceaux relus pour vérifier une copie (en plus du début et de la fin)
SAMPLE = 256 * 1024
PART = 64 * 1024 * 1024  # S3 : taille d'une partie (agrandie au-delà de ~600 Go : 10 000 parties au plus)
TOKEN_TTL = 12 * 3600
SYSTEM_DIRS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/run", "/sbin", "/sys", "/usr", "/var/lib")


class ColdError(Exception):
    pass


def safe_part(s):
    """Nom utilisable partout (SMB, Windows, S3) : sans caractères interdits, sans « .. »."""
    s = re.sub(r'[<>:"\\|?*\x00-\x1f]', "_", str(s)).strip().rstrip(".")
    return "_" if s in ("", ".", "..") else s[:200]


def safe_name(name):
    parts = [safe_part(p) for p in str(name).replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ColdError("Nom de fichier invalide")
    return "/".join(parts)


# ====================================================================== stockages
class Backend:
    """Interface : put(name, reader, size), size(name), open(name, start, end) -> itérateur d'octets,
    delete(name), check(), free() ; local_path(name) : chemin lisible directement (dossier), sinon None."""
    remote = True

    def __init__(self, conf, cold):
        self.conf = conf
        self.cold = cold

    def local_path(self, name):
        return None

    def free(self):
        return None

    def check(self):
        data = secrets.token_bytes(4096)
        name = f".nexis-test-{secrets.token_hex(4)}"
        self.put(name, _BytesReader(data), len(data))
        try:
            if self.size(name) != len(data):
                raise ColdError("Le fichier d'essai n'a pas la bonne taille sur le stockage")
            got = b"".join(self.open(name, 100, 199))
            if got != data[100:200]:
                raise ColdError("Le fichier d'essai relu ne correspond pas")
        finally:
            try:
                self.delete(name)
            except ColdError:
                pass


class _BytesReader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, n=-1):
        n = len(self.data) - self.pos if n is None or n < 0 else n
        b = self.data[self.pos:self.pos + n]
        self.pos += len(b)
        return b


class Local(Backend):
    remote = False

    def root(self):
        return self.conf["path"]

    def local_path(self, name):
        root = os.path.realpath(self.root())
        p = os.path.realpath(os.path.join(root, safe_name(name)))
        if not p.startswith(root + os.sep):
            raise ColdError("Chemin hors du stockage")
        return p

    def _ready(self):
        if not os.path.isdir(self.root()):
            raise ColdError(i18n.tr("Dossier du stockage introuvable : {path}").format(path=self.root()))

    def put(self, name, reader, size):
        self._ready()
        p = self.local_path(name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".nexis-part"
        try:
            with open(tmp, "wb") as out:
                while True:
                    b = reader.read(BLOCK)
                    if not b:
                        break
                    out.write(b)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, p)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise ColdError(i18n.tr("Écriture impossible sur le stockage : {e}").format(e=e.strerror or e))

    def size(self, name):
        p = self.local_path(name)
        return os.path.getsize(p) if os.path.isfile(p) else None

    def open(self, name, start, end):
        with open(self.local_path(name), "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                b = f.read(min(BLOCK, left))
                if not b:
                    break
                left -= len(b)
                yield b

    def delete(self, name):
        p = self.local_path(name)
        try:
            os.remove(p)
        except FileNotFoundError:
            return
        except OSError as e:
            raise ColdError(i18n.tr("Suppression impossible : {e}").format(e=e.strerror or e))
        # dossiers devenus vides, jusqu'à la racine du stockage
        root = os.path.realpath(self.root())
        d = os.path.dirname(p)
        while d.startswith(root + os.sep):
            try:
                os.rmdir(d)
            except OSError:
                break
            d = os.path.dirname(d)

    def free(self):
        try:
            return shutil.disk_usage(self.root()).free
        except OSError:
            return None


class SMB(Local):
    """Partage monté en écriture par seance-mountd sous /mnt/seance/cold-<id>."""

    def root(self):
        return self.conf["mount"]  # le sous-dossier éventuel est monté directement à cet endroit

    def _ready(self):
        if not os.path.ismount(self.conf["mount"]):
            raise ColdError("Partage SMB du stockage non monté")
        super()._ready()


def _conn(url, timeout=60):
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https":
        c = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ssl.create_default_context(),
                                        blocksize=BLOCK)
    elif u.scheme == "http":
        c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout, blocksize=BLOCK)
    else:
        raise ColdError("Adresse invalide")
    return c, (u.path or "/") + (f"?{u.query}" if u.query else "")


def _request(method, url, headers=None, body=None, timeout=60, stream=False):
    c, path = _conn(url, timeout)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
    except (OSError, http.client.HTTPException) as e:
        c.close()
        raise ColdError(i18n.tr("Stockage injoignable : {e}").format(e=getattr(e, "strerror", None) or e))
    if stream:
        return c, r
    try:
        return r.status, dict(r.getheaders()), r.read(1 << 20)
    finally:
        c.close()


def _iter_response(c, r):
    try:
        while True:
            b = r.read(BLOCK)
            if not b:
                break
            yield b
    finally:
        c.close()


class WebDAV(Backend):
    def _auth(self):
        import base64
        tok = base64.b64encode(f"{self.conf['username']}:{self.conf.get('password', '')}".encode()).decode()
        return {"Authorization": f"Basic {tok}"}

    def url(self, name):
        return self.conf["url"].rstrip("/") + "/" + urllib.parse.quote(safe_name(name), safe="/")

    def _check(self, st, body, what):
        if st == 401:
            raise ColdError("Identifiant ou mot de passe refusé par le serveur WebDAV")
        if st == 507:
            raise ColdError("Plus de place sur le stockage")
        if st >= 400:
            raise ColdError(f"{i18n.tr(what)} : HTTP {st}")

    def _mkdirs(self, name):
        parts = safe_name(name).split("/")[:-1]
        base = self.conf["url"].rstrip("/")
        for i in range(len(parts)):
            u = base + "/" + urllib.parse.quote("/".join(parts[:i + 1])) + "/"
            st, _, body = _request("MKCOL", u, self._auth())
            if st not in (201, 405, 301, 302) and st != 200:
                self._check(st, body, "Création d'un dossier WebDAV")

    def put(self, name, reader, size):
        self._mkdirs(name)
        tmp, final = self.url(name + ".nexis-part"), self.url(name)
        st, _, body = _request("PUT", tmp, {**self._auth(), "Content-Length": str(size),
                                            "Content-Type": "application/octet-stream"}, reader, timeout=300)
        self._check(st, body, "Envoi WebDAV")
        st, _, body = _request("MOVE", tmp, {**self._auth(), "Destination": final, "Overwrite": "T"})
        if st >= 400:
            _request("DELETE", tmp, self._auth())
        self._check(st, body, "Envoi WebDAV")

    def size(self, name):
        st, h, body = _request("PROPFIND", self.url(name), {**self._auth(), "Depth": "0",
                                                            "Content-Type": "application/xml"},
                               b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength/>'
                               b'</d:prop></d:propfind>')
        if st == 404:
            return None
        self._check(st, body, "Lecture WebDAV")
        m = re.search(rb"getcontentlength[^>]*>\s*(\d+)\s*<", body)
        return int(m.group(1)) if m else None

    def open(self, name, start, end):
        c, r = _request("GET", self.url(name), {**self._auth(), "Range": f"bytes={start}-{end}"}, timeout=60, stream=True)
        if r.status not in (200, 206):
            c.close()
            raise ColdError(f"Lecture WebDAV : HTTP {r.status}")
        it = _iter_response(c, r)
        if r.status == 200 and start:  # serveur sans Range : on saute le début
            skip = start
            for b in it:
                if skip >= len(b):
                    skip -= len(b)
                    continue
                it = _chain([b[skip:]], it)
                break
        yield from _limit(it, end - start + 1)

    def delete(self, name):
        st, _, body = _request("DELETE", self.url(name), self._auth())
        if st not in (200, 204, 404):
            self._check(st, body, "Suppression WebDAV")

    def free(self):
        try:
            st, _, body = _request("PROPFIND", self.conf["url"].rstrip("/") + "/", {**self._auth(), "Depth": "0"},
                                   b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                                   b'<d:quota-available-bytes/></d:prop></d:propfind>', timeout=20)
        except ColdError:
            return None
        m = re.search(rb"quota-available-bytes[^>]*>\s*(\d+)\s*<", body or b"")
        return int(m.group(1)) if st < 400 and m else None


def _chain(*its):
    for it in its:
        yield from it


def _limit(it, n):
    for b in it:
        if n <= 0:
            return
        if len(b) > n:
            b = b[:n]
        n -= len(b)
        yield b


class _Slice:
    """Une partie d'un envoi S3 : au plus n octets lus dans la source."""

    def __init__(self, reader, n):
        self.reader, self.left = reader, n

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        n = self.left if n is None or n < 0 else min(n, self.left)
        b = self.reader.read(n)
        self.left -= len(b)
        return b


class S3(Backend):
    def __init__(self, conf, cold):
        super().__init__(conf, cold)
        self.signer = S3Signer(conf, lambda c: None)

    def key(self, name):
        return self.signer._key(safe_name(name))

    def _send(self, method, key, query=None, body=None, extra=None, headers=None, timeout=120, stream=False):
        url, h = self.signer._signed(method, key, query=query, extra=extra)
        h.update(headers or {})
        return _request(method, url, h, body, timeout=timeout, stream=stream)

    def _fail(self, st, body, what):
        m = re.search(rb"<Message>([^<]{1,300})</Message>", body or b"")
        raise ColdError(f"{i18n.tr(what)} : {m.group(1).decode('utf-8', 'replace') if m else ''} ({st})")

    def put(self, name, reader, size):
        key = self.key(name)
        if size <= PART:
            st, _, body = self._send("PUT", key, extra={"content-length": str(size)}, body=reader,
                                     headers={"Content-Type": "application/octet-stream"}, timeout=600)
            if st not in (200, 201):
                self._fail(st, body, "Envoi S3")
            return
        part = max(PART, -(-size // 9000))
        st, _, body = self._send("POST", key, query={"uploads": ""})
        m = re.search(rb"<UploadId>([^<]+)</UploadId>", body or b"")
        if st != 200 or not m:
            self._fail(st, body, "Envoi S3")
        upload = m.group(1).decode()
        etags = []
        try:
            n, left = 1, size
            while left > 0:
                this = min(part, left)
                st, h, body = self._send("PUT", key, query={"partNumber": n, "uploadId": upload},
                                         extra={"content-length": str(this)}, body=_Slice(reader, this), timeout=900)
                if st != 200:
                    self._fail(st, body, "Envoi S3")
                etags.append((n, {k.lower(): v for k, v in h.items()}.get("etag", "")))
                n, left = n + 1, left - this
            xml = "<CompleteMultipartUpload>" + "".join(
                f"<Part><PartNumber>{i}</PartNumber><ETag>{e}</ETag></Part>" for i, e in etags) + "</CompleteMultipartUpload>"
            st, _, body = self._send("POST", key, query={"uploadId": upload}, body=xml.encode(), timeout=300)
            if st != 200 or b"<Error>" in (body or b""):
                self._fail(st, body, "Envoi S3")
        except BaseException:
            try:
                self._send("DELETE", key, query={"uploadId": upload})
            except ColdError:
                pass
            raise

    def size(self, name):
        st, h, body = self._send("HEAD", self.key(name))
        if st == 404:
            return None
        if st != 200:
            self._fail(st, body, "Lecture S3")
        return int({k.lower(): v for k, v in h.items()}.get("content-length") or 0)

    def open(self, name, start, end):
        c, r = self._send("GET", self.key(name), headers={"Range": f"bytes={start}-{end}"}, timeout=60, stream=True)
        if r.status not in (200, 206):
            body = r.read(4000)
            c.close()
            self._fail(r.status, body, "Lecture S3")
        yield from _iter_response(c, r)

    def delete(self, name):
        st, _, body = self._send("DELETE", self.key(name))
        if st not in (200, 204, 404):
            self._fail(st, body, "Suppression S3")


def curl_sftp():
    """curl de cette machine sait-il faire du SFTP ?"""
    exe = shutil.which("curl")
    if not exe:
        return False
    try:
        out = subprocess.run([exe, "-V"], capture_output=True, timeout=10).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(re.search(r"^Protocols:.*\bsftp\b", out, re.M))


def _pump(p, reader, timeout=None):
    """Envoie la source sur l'entrée du programme (curl, rclone) en lisant ses sorties à côté ; -> (sortie, erreurs).
    (communicate() refermerait l'entrée dès le premier morceau.)"""
    out, err = [], []
    readers = [threading.Thread(target=lambda: out.append(p.stdout.read()), daemon=True),
               threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)]
    for t in readers:
        t.start()
    try:
        if reader is not None:
            while True:
                b = reader.read(BLOCK)
                if not b:
                    break
                p.stdin.write(b)
    except BrokenPipeError:
        pass  # le programme a abandonné : son message d'erreur le dira
    except BaseException:
        p.kill()
        raise
    finally:
        if p.stdin:
            try:
                p.stdin.close()
            except OSError:
                pass
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        raise ColdError("Délai dépassé")
    for t in readers:
        t.join(10)
    for f in (p.stdout, p.stderr):
        if f:
            f.close()
    return (out[0] if out else b""), (err[0] if err else b"")


def _curl_quote(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


class SFTP(Backend):
    """Par curl (libssh) : identifiants passés par un descripteur (jamais sur la ligne de commande), clé du serveur
    vérifiée (empreinte MD5 retenue au premier essai, confirmée par l'administrateur)."""

    def _path(self, name=""):
        base = (self.conf.get("path") or "").strip().strip("/")
        return "/".join(p for p in (base, safe_name(name) if name else "") if p)

    def url(self, name="", folder=False):
        c = self.conf
        rel = "/".join(p for p in ((c.get("path") or "").strip().strip("/"), name) if p)
        absolute = (c.get("path") or "").startswith("/")
        path = ("/" if absolute else "/~/") + urllib.parse.quote(rel, safe="/")
        if folder or not name:
            path = path.rstrip("/") + "/"
        return f"sftp://{c['host']}:{int(c.get('port') or 22)}{path}"

    def _run(self, args, stdin=None, check_key=True, timeout=None, stream=False):
        r, w = os.pipe()
        cfg = f"user = {_curl_quote(self.conf['username'] + ':' + self.conf.get('password', ''))}\n".encode()
        os.write(w, cfg)
        os.close(w)
        key = self.conf.get("hostkey") if check_key else "0" * 32
        cmd = [shutil.which("curl") or "curl", "-sS", "-K", f"/dev/fd/{r}", "--connect-timeout", "20",
               "--hostpubmd5", key or "0" * 32, *args]
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(r,))
        finally:
            os.close(r)
        if stream:
            return p
        out, err = _pump(p, stdin, timeout)
        if p.returncode != 0:
            raise ColdError(self._explain(err.decode("utf-8", "replace")))
        return out

    @staticmethod
    def _explain(err):
        if "mismatch md5 fingerprint" in err:
            return i18n.tr("La clé du serveur SFTP a changé : vérifiez le serveur, puis testez de nouveau le stockage")
        if re.search(r"Login denied|Authentication failure|access denied", err, re.I):
            return i18n.tr("Identifiant ou mot de passe refusé par le serveur SFTP")
        if re.search(r"Could not resolve|Failed to connect|Connection refused|timed out", err, re.I):
            return i18n.tr("Serveur SFTP injoignable")
        lines = [l for l in err.strip().splitlines() if l.strip()]
        return i18n.tr("SFTP : {e}").format(e=(lines[0] if lines else "erreur")[:200])

    def fingerprint(self):
        """Empreinte MD5 de la clé du serveur (curl la donne en refusant une empreinte fausse)."""
        p = self._run(["-o", "/dev/null", self.url()], check_key=False, stream=True)
        _, err = p.communicate(timeout=40)
        p.stdout.close()
        p.stderr.close()
        m = re.search(r"Remote ([0-9a-f]{32}) is not equal", err.decode("utf-8", "replace"))
        if not m:
            raise ColdError(self._explain(err.decode("utf-8", "replace")))
        return m.group(1)

    def put(self, name, reader, size):
        self._run(["--ftp-create-dirs", "-T", "-", self.url(safe_name(name))], stdin=reader)

    def size(self, name):
        parent, _, base = safe_name(name).rpartition("/")
        try:
            out = self._run([self.url(parent, folder=True)], timeout=60)
        except ColdError as e:
            if "No such file" in str(e) or "not found" in str(e).lower():
                return None
            raise
        for line in out.decode("utf-8", "replace").splitlines():
            f = line.split(None, 8)
            if len(f) == 9 and f[8] == base and line[:1] == "-":
                return int(f[4])
        return None

    def open(self, name, start, end):
        p = self._run(["-r", f"{start}-{end}", self.url(safe_name(name))], stream=True)
        try:
            while True:
                b = p.stdout.read(BLOCK)
                if not b:
                    break
                yield b
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()

    def delete(self, name):
        target = self._path(name)
        absolute = (self.conf.get("path") or "").startswith("/")
        try:
            self._run(["-Q", f"rm {_curl_quote(('/' if absolute else '') + target)}", "-o", "/dev/null", self.url()], timeout=60)
        except ColdError as e:
            if "No such file" not in str(e):
                raise


def rclone_exe():
    return shutil.which("rclone")


class Rclone(Backend):
    def target(self, name=""):
        remote = self.conf["remote"].rstrip("/")
        return f"{remote}/{safe_name(name)}" if name else remote

    def _cmd(self, *args):
        cmd = [rclone_exe() or "rclone", *args]
        if self.conf.get("config"):
            cmd += ["--config", self.conf["config"]]
        return cmd

    def _run(self, args, stdin=None, timeout=None):
        try:
            p = subprocess.Popen(self._cmd(*args), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise ColdError("rclone n'est pas installé sur ce serveur")
        out, err = _pump(p, stdin, timeout)
        if p.returncode != 0:
            lines = [l for l in err.decode("utf-8", "replace").splitlines() if l.strip()]
            raise ColdError(i18n.tr("rclone : {e}").format(e=(lines[-1] if lines else "erreur")[-200:]))
        return out

    def put(self, name, reader, size):
        self._run(["rcat", "--size", str(size), self.target(name)], stdin=reader)

    def size(self, name):
        try:
            out = self._run(["lsjson", "--stat", self.target(name)], timeout=120)
        except ColdError as e:
            if re.search(r"not found|doesn't exist|no such", str(e), re.I):
                return None
            raise
        try:
            return int(json.loads(out or b"{}").get("Size"))
        except (ValueError, TypeError):
            return None

    def open(self, name, start, end):
        try:
            p = subprocess.Popen(self._cmd("cat", "--offset", str(start), "--count", str(end - start + 1), self.target(name)),
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except OSError:
            raise ColdError("rclone n'est pas installé sur ce serveur")
        try:
            while True:
                b = p.stdout.read(BLOCK)
                if not b:
                    break
                yield b
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()

    def delete(self, name):
        try:
            self._run(["deletefile", self.target(name)], timeout=120)
        except ColdError as e:
            if not re.search(r"not found|doesn't exist|no such", str(e), re.I):
                raise


PEER_CHUNK = 16 * 1024 * 1024


class Peer(Backend):
    """Archives gardées par un serveur lié (canal chiffré des serveurs liés, morceaux de 16 Mo)."""

    def _fed(self):
        from ..federation import FedError
        fed = self.cold.ctx.fed
        try:
            return fed, fed.peer(int(self.conf["peer"])), FedError
        except FedError as e:
            raise ColdError(str(e))

    def _req(self, method, path, body=None, headers=None, timeout=120):
        fed, peer, FedError = self._fed()
        try:
            return fed.request(peer, method, path, body=body, headers=headers, timeout=timeout)
        except FedError as e:
            raise ColdError(str(e))

    def _json(self, method, path, body=None, ok=(200,)):
        resp, close = self._req(method, path, body)
        try:
            raw = resp.read(1 << 20)
        except OSError as e:
            raise ColdError(i18n.tr("Stockage injoignable : {e}").format(e=e))
        finally:
            close()
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        if resp.status == 404 and 404 in ok:
            return None
        if resp.status not in ok:
            raise ColdError(data.get("error") or f"HTTP {resp.status}")
        return data

    @staticmethod
    def _q(name):
        return urllib.parse.quote(safe_name(name), safe="")

    def put(self, name, reader, size):
        off, q = 0, self._q(name)
        while True:
            chunk = bytearray()
            while len(chunk) < PEER_CHUNK:
                b = reader.read(min(BLOCK, PEER_CHUNK - len(chunk)))
                if not b:
                    break
                chunk += b
            if chunk or not off:
                self._json("PUT", f"/fed/cold/blob?name={q}&offset={off}", bytes(chunk))
                off += len(chunk)
            if not chunk or off >= size:
                break
        self._json("POST", f"/fed/cold/commit?name={q}&size={size}")

    def size(self, name):
        r = self._json("GET", f"/fed/cold/stat?name={self._q(name)}", ok=(200, 404))
        return None if r is None else int(r.get("size") or 0)

    def open(self, name, start, end):
        resp, close = self._req("GET", f"/fed/cold/blob?name={self._q(name)}", headers={"Range": f"bytes={start}-{end}"})
        try:
            if resp.status not in (200, 206):
                raise ColdError(f"HTTP {resp.status}")
            while True:
                b = resp.read(BLOCK)
                if not b:
                    break
                yield b
        finally:
            close()

    def delete(self, name):
        self._json("DELETE", f"/fed/cold/blob?name={self._q(name)}", ok=(200, 404))

    def free(self):
        try:
            return (self._json("GET", "/fed/cold/info") or {}).get("free")
        except ColdError:
            return None


BACKENDS = {"local": Local, "smb": SMB, "sftp": SFTP, "webdav": WebDAV, "s3": S3, "rclone": Rclone, "peer": Peer}


# ====================================================================== réglages d'un stockage
def valid_conf(kind, b, old=None):
    """Réglages envoyés par l'administration -> conf (un secret laissé vide garde l'ancienne valeur)."""
    old = dict(old or {})

    def txt(k, pattern, limit=300, required=True):
        v = str(b.get(k) if b.get(k) is not None else old.get(k, "")).strip()
        if not v and not required:
            return ""
        if not v or len(v) > limit or not re.match(pattern, v):
            raise ColdError(i18n.tr("Réglage invalide : {k}").format(k=k))
        return v

    def secret(k):
        v = b.get(k)
        return old.get(k, "") if v in (None, "") else str(v)[:500]

    if kind == "local":
        path = os.path.normpath(txt("path", r"^/.{0,1000}$", 1000))
        if path == "/" or any(path == d or path.startswith(d + "/") for d in SYSTEM_DIRS):
            raise ColdError("Ce dossier ne peut pas servir de stockage")
        return {"path": path}
    if kind == "smb":
        return {"host": txt("host", r"^[A-Za-z0-9.\-\[\]:]{1,253}$", 253), "share": txt("share", r"^[^/\\]{1,80}$", 80),
                "subdir": txt("subdir", r"^[^\\]{0,200}$", 200, False).strip("/"),
                "username": txt("username", r"^[^\n\r]{1,100}$", 100), "domain": txt("domain", r"^[A-Za-z0-9._-]{0,64}$", 64, False),
                "version": txt("version", r"^(auto|1\.0|2\.0|2\.1|3\.0|3\.1\.1)$", 10, False) or "auto",
                "password": secret("password")}
    if kind == "sftp":
        port = int(b.get("port") or old.get("port") or 22)
        if not 1 <= port <= 65535:
            raise ColdError("Port invalide")
        conf = {"host": txt("host", r"^[A-Za-z0-9.\-]{1,253}$", 253), "port": port,
                "username": txt("username", r"^[A-Za-z0-9._@+-]{1,100}$", 100),
                "path": re.sub(r"^~/?", "", txt("path", r"^[^\n\r\\]{0,300}$", 300, False)) or "nexis",
                "password": secret("password"),
                "hostkey": str(b.get("hostkey") or old.get("hostkey") or "").lower()}
        if conf["hostkey"] and not re.match(r"^[0-9a-f]{32}$", conf["hostkey"]):
            raise ColdError("Empreinte de la clé du serveur invalide")
        if old.get("host") and (old.get("host"), old.get("port")) != (conf["host"], conf["port"]) and not b.get("hostkey"):
            conf["hostkey"] = ""  # autre serveur : nouvelle empreinte à confirmer
        return conf
    if kind == "webdav":
        url = txt("url", r"^https?://[^\s]{3,1000}$", 1000)
        return {"url": url.rstrip("/"), "username": txt("username", r"^[^\n\r:]{1,200}$", 200), "password": secret("password")}
    if kind == "s3":
        conf = {"endpoint": str(b.get("endpoint") if b.get("endpoint") is not None else old.get("endpoint", "")).strip().rstrip("/"),
                "region": txt("region", r"^[a-z0-9-]{2,40}$", 40),
                "bucket": txt("bucket", r"^[a-z0-9][a-z0-9.\-]{1,62}$", 63),
                "prefix": str(b.get("prefix") if b.get("prefix") is not None else old.get("prefix", "nexis-archive")).strip("/")[:200],
                "access_key": txt("access_key", r"^[\x21-\x7e]{3,200}$", 200),
                "secret_key": secret("secret_key")}
        if conf["endpoint"] and not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~\-]+)*$", conf["endpoint"]):
            raise ColdError("Adresse S3 invalide (ex. https://s3.eu-west-3.amazonaws.com)")
        if not conf["secret_key"]:
            raise ColdError("Clé secrète S3 requise")
        return conf
    if kind == "rclone":
        conf = {"remote": txt("remote", r"^[A-Za-z0-9_. -]{1,64}:[^\n\r]{0,300}$", 370),
                "config": txt("config", r"^/[^\n\r]{1,500}$", 500, False)}
        return conf
    if kind == "peer":
        try:
            return {"peer": int(b.get("peer") or old.get("peer") or 0)}
        except (TypeError, ValueError):
            raise ColdError("Serveur lié invalide")
    raise ColdError("Type de stockage inconnu")


def public_conf(conf):
    out = {k: v for k, v in conf.items() if k not in SECRETS}
    out["secrets_set"] = sorted(k for k in SECRETS if conf.get(k))
    return out


# ====================================================================== lecture de la source pendant une copie
class _Reader:
    """Source d'une copie : empreinte SHA-256, progression, débit limité pendant une lecture, annulation."""

    def __init__(self, it, job, cold):
        self.it = iter(it)
        self.buf = b""
        self.job = job
        self.cold = cold
        self.sha = hashlib.sha256()
        self.done = 0
        self.t0 = time.monotonic()
        self.sent_since = 0
        self.rate_at = 0
        self.rate = 0

    def read(self, n=-1):
        if self.job.get("cancel"):
            raise ColdError("Archivage annulé")
        n = BLOCK if n is None or n < 0 else n
        while len(self.buf) < n:
            try:
                self.buf += next(self.it)
            except StopIteration:
                break
        b, self.buf = self.buf[:n], self.buf[n:]
        if b:
            self.sha.update(b)
            self.done += len(b)
            self.job["done"] = self.done
            self._throttle(len(b))
        return b

    def _throttle(self, n):
        now = time.monotonic()
        if now - self.rate_at > 3:
            self.rate_at = now
            self.rate = self.cold.rate_limit()
            self.t0, self.sent_since = now, 0
        self.sent_since += n
        if self.rate:
            ahead = self.sent_since / self.rate - (now - self.t0)
            if ahead > 0:
                time.sleep(min(ahead, 2))


MIN_GAIN = 0.9  # compressé, le fichier doit faire moins de 90 % de l'original (sinon l'original est gardé)
QUALITY = {"high": (22, 21), "balanced": (26, 24), "small": (30, 27)}  # (carte graphique : cq, processeur : crf)


def compress_cmd(src, out, probe, hw, quality, height):
    """ffmpeg : vidéo en HEVC, tout le reste recopié (MKV)."""
    from ..transcoder import hw_input
    v = probe["video"]
    vi = v["index"]
    ten = "10" in (v.get("pix_fmt") or "")
    cq, crf = QUALITY.get(quality, QUALITY["balanced"])
    cmd = [media.FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-progress", "pipe:1", "-y"]
    if hw:
        cmd += hw_input(hw)
    cmd += [*media.input_args(src), "-map", "0", "-map", "-0:d", "-c", "copy", "-map_metadata", "0", "-map_chapters", "0",
            "-max_muxing_queue_size", "4096"]
    for sub in probe.get("subs") or []:
        if sub.get("codec") == "mov_text":  # sous-titres d'un MP4 : du texte que le MKV n'accepte qu'en SRT
            cmd += [f"-c:{sub['index']}", "srt"]
    vf = [f"scale=-2:{height}"] if height and (v.get("height") or 0) > height else []
    if hw and hw["engine"] == "vaapi":
        vf += ["format=p010" if ten else "format=nv12", "hwupload"]
    if vf:
        cmd += [f"-filter:{vi}", ",".join(vf)]
    if hw and hw["engine"] == "nvenc":
        cmd += [f"-c:{vi}", "hevc_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
        if ten:
            cmd += [f"-pix_fmt:{vi}", "p010le"]
    elif hw:
        cmd += [f"-c:{vi}", "hevc_vaapi", "-rc_mode", "CQP", "-qp", str(cq)]
    else:
        cmd += [f"-c:{vi}", "libx265", "-preset", "medium", "-crf", str(crf), "-x265-params", "log-level=error",
                f"-pix_fmt:{vi}", "yuv420p10le" if ten else "yuv420p"]
    return cmd + [out]


def _file_blocks(path, start=0, end=None):
    with open(path, "rb") as f:
        f.seek(start)
        left = None if end is None else end - start + 1
        while left is None or left > 0:
            b = f.read(BLOCK if left is None else min(BLOCK, left))
            if not b:
                break
            if left is not None:
                left -= len(b)
            yield b


# ====================================================================== le service
