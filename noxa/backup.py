"""Server backup and restore.

A backup is a .tar.gz archive that holds:
- seance.db: the database (accounts, hashed passwords, two-factor authentication, history, ratings, playlists,
  settings, libraries, metadata), copied through SQLite's backup API (consistent even in the middle of
  writes);
- federation/: identity and certificates of the linked servers (without it, they would have to be linked again);
- push/: notification key (without it, each device would have to subscribe again);
- avatars/: profile pictures;
- subtitles/: subtitles downloaded from OpenSubtitles (counted against a daily quota);
- manifest.json: Noxa and database version, date.
Videos and photos are not in it: they stay in their folders. The caches (resized pictures,
extracted subtitles, previews) rebuild themselves.

Restoring happens in two steps, because the database is open while Noxa runs: the archive is checked
then unpacked into data/restore-pending, and it is at the next start, before the database is opened, that the
files are put in place. The previous ones are kept in data/before-restore-<date>.
"""
import json
import logging
import hashlib
import hmac
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time

from . import __version__ as VERSION

log = logging.getLogger("noxa.backup")

FORMAT = 1
DIRS = ("federation", "push", "avatars", "subtitles", "wallpapers")
# noxa-<server>_YYYYMMDD-HHMMSS[-auto|-envoi].tar.gz (the origin server in the name: copies from linked servers sit
# next to ours). Also accepted: the nexis- prefix of backups made before 4.5, and old names without a server.
NAME = re.compile(r"^(?:noxa|nexis)-(?:(?P<srv>[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?)_)?(?P<date>\d{8}-\d{6})"
                  r"(?:-(?P<label>[a-z0-9]{1,12}))?\.tar\.gz$")
ENC_SUFFIX = ".enc"
MAGIC = b"NEXISENC1\n"
KDF_ROUNDS = 300000
PENDING = "restore-pending"
KEEP_BEFORE = 2  # "before restore" copies kept
MAX_UPLOAD = 8 * 1024 ** 3


class BackupError(Exception):
    pass


def directory(ctx):
    return ctx.db.setting("backup_dir") or os.path.join(ctx.data_dir, "backups")


def check_directory(path):
    """Destination folder chosen by the administrator: absolute, existing, writable."""
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path or len(path) > 400:
        raise BackupError("Chemin absolu attendu (ex. /mnt/nas/sauvegardes)")
    path = os.path.normpath(path)
    if not os.path.isdir(path):
        raise BackupError("Dossier introuvable")
    if not os.access(path, os.W_OK):
        raise BackupError("Noxa ne peut pas écrire dans ce dossier")
    return path


def slug(name):
    """Server name for a file name: "Salon de Léa" -> "salon-de-lea"."""
    from .names import fold
    s = re.sub(r"[^a-z0-9]+", "-", fold(name or "noxa")).strip("-")[:32].strip("-")
    return s or "noxa"


def server_identity(ctx):
    fed = getattr(ctx, "fed", None)
    try:
        return (fed.uid() if fed else ""), (fed.name() if fed else "Noxa")
    except Exception:
        return "", "Noxa"


def _schema(db_path):
    c = sqlite3.connect(db_path)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def create(ctx, label="", keep=None, distribute=True):
    """New backup -> {name, size, created, ...}. keep: number of automatic archives kept."""
    dest = directory(ctx)
    os.makedirs(dest, mode=0o700, exist_ok=True)
    now = time.time()
    uid, server = server_identity(ctx)
    name = (f"noxa-{slug(server)}_" + time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + (f"-{label}" if label else "")
            + ".tar.gz")
    final = os.path.join(dest, name)
    tmp_dir = tempfile.mkdtemp(prefix="backup-", dir=ctx.data_dir)
    part = final + ".partial"
    try:
        db_copy = os.path.join(tmp_dir, "seance.db")
        src = sqlite3.connect(ctx.db.path, timeout=30)
        dst = sqlite3.connect(db_copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        manifest = {"format": FORMAT, "version": VERSION, "schema": _schema(db_copy), "created": int(now),
                    "server": server, "server_uid": uid, "data_dir": os.path.abspath(ctx.data_dir)}
        man = os.path.join(tmp_dir, "manifest.json")
        with open(man, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # passwords, keys: readable by Noxa only
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz", compresslevel=6) as tar:
            tar.add(man, "manifest.json")  # first: the list only reads the start of the archive
            tar.add(db_copy, "seance.db")
            for d in DIRS:
                p = os.path.join(ctx.data_dir, d)
                if os.path.isdir(p):
                    tar.add(p, d, filter=lambda t: t if (t.isfile() or t.isdir()) else None)
        os.replace(part, final)
    except (OSError, sqlite3.Error, tarfile.TarError) as e:
        try:
            os.remove(part)
        except OSError:
            pass
        raise BackupError(f"Sauvegarde impossible : {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    if keep:
        prune(ctx, keep)
    log.info("Sauvegarde %s (%.1f Mo)", final, os.path.getsize(final) / 1e6)
    if distribute:
        distribute_later(ctx, final)  # copies to the linked servers and the external storages, in the background
    return describe(final, uid)


def describe(path, local_uid=None):
    st = os.stat(path)
    base = os.path.basename(path)
    m = NAME.match(base)
    out = {"name": base, "size": st.st_size, "created": int(st.st_mtime), "version": None, "schema": None,
           "auto": bool(m and m.group("label") == "auto"), "uploaded": bool(m and m.group("label") == "envoi"),
           "server": None, "server_uid": None, "slug": m.group("srv") if m else None}
    try:
        with tarfile.open(path, "r:gz") as tar:
            mm = tar.next()
            if mm and mm.name == "manifest.json" and mm.isfile() and mm.size < 65536:
                man = json.loads(tar.extractfile(mm).read())
                out.update(version=man.get("version"), schema=man.get("schema"), created=man.get("created") or out["created"],
                           server=man.get("server") or None, server_uid=man.get("server_uid") or None)
    except (OSError, tarfile.TarError, ValueError, EOFError):
        out["broken"] = True
    # backup of this server, or copy from a linked server (old archives without identity: those from here)
    out["mine"] = not out["server_uid"] or local_uid is None or out["server_uid"] == local_uid
    return out


def listing(ctx):
    d = directory(ctx)
    if not os.path.isdir(d):
        return []
    uid = server_identity(ctx)[0]
    files = [os.path.join(d, n) for n in os.listdir(d) if NAME.match(n)]
    return sorted((describe(p, uid) for p in files), key=lambda b: b["created"], reverse=True)


def path_of(ctx, name):
    if not isinstance(name, str) or not NAME.match(name):
        raise BackupError("Sauvegarde introuvable")
    p = os.path.join(directory(ctx), name)
    if not os.path.isfile(p):
        raise BackupError("Sauvegarde introuvable")
    return p


def origin(b):
    return b.get("server_uid") or b.get("slug") or "local"


def prune(ctx, keep):
    """Keeps only the `keep` most recent automatic backups of each server (those from here and the
    copies of each linked server); those made by hand stay."""
    seen = {}
    for b in listing(ctx):
        if not b["auto"]:
            continue
        o = origin(b)
        seen[o] = seen.get(o, 0) + 1
        if seen[o] > keep:
            try:
                os.remove(os.path.join(directory(ctx), b["name"]))
            except OSError:
                pass


# ------------------------------------------------------------------ encryption (copies sent off the server)
# AES-256 in CTR mode (openssl) then HMAC-SHA256 over the whole (encrypt then authenticate); keys derived from the
# passphrase with PBKDF2-SHA256. Format: MAGIC, salt (16), IV (16), encrypted data, HMAC (32).
def _keys(passphrase, salt):
    k = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, KDF_ROUNDS, 64)
    return k[:32], k[32:]


def encrypt_file(src, dst, passphrase):
    salt, iv = os.urandom(16), os.urandom(16)
    ek, mk = _keys(passphrase, salt)
    mac = hmac.new(mk, MAGIC + salt + iv, hashlib.sha256)
    with open(src, "rb") as fin, open(os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fout:
        fout.write(MAGIC + salt + iv)
        p = subprocess.Popen(["openssl", "enc", "-aes-256-ctr", "-K", ek.hex(), "-iv", iv.hex()],
                             stdin=fin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for chunk in iter(lambda: p.stdout.read(1024 * 1024), b""):
            mac.update(chunk)
            fout.write(chunk)
        p.stdout.close()
        if p.wait() != 0:
            raise BackupError("Chiffrement impossible (openssl)")
        fout.write(mac.digest())


def is_encrypted(path):
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def decrypt_file(src, dst, passphrase):
    size = os.path.getsize(src)
    head = len(MAGIC) + 32
    if size < head + 32:
        raise BackupError("Archive chiffrée incomplète")
    with open(src, "rb") as f:
        f.seek(len(MAGIC))
        salt, iv = f.read(16), f.read(16)
        ek, mk = _keys(passphrase, salt)
        mac = hmac.new(mk, MAGIC + salt + iv, hashlib.sha256)
        left = size - head - 32
        while left:
            chunk = f.read(min(left, 1024 * 1024))
            mac.update(chunk)
            left -= len(chunk)
        if not hmac.compare_digest(mac.digest(), f.read(32)):
            raise BackupError("Phrase secrète incorrecte, ou archive abîmée")
    with open(src, "rb") as fin, open(os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fout:
        fin.seek(head)
        p = subprocess.Popen(["openssl", "enc", "-d", "-aes-256-ctr", "-K", ek.hex(), "-iv", iv.hex()],
                             stdin=subprocess.PIPE, stdout=fout, stderr=subprocess.DEVNULL)
        left = size - head - 32
        while left:
            chunk = fin.read(min(left, 1024 * 1024))
            p.stdin.write(chunk)
            left -= len(chunk)
        p.stdin.close()
        if p.wait() != 0:
            raise BackupError("Déchiffrement impossible (openssl)")


def plain_copy(ctx, path, passphrase=None):
    """Archive to restore: decrypted alongside when needed (given passphrase, otherwise the saved one) -> path."""
    if not is_encrypted(path):
        return path, False
    pw = passphrase or ctx.db.setting("backup_passphrase")
    if not pw:
        raise BackupError("Archive chiffrée : indiquez la phrase secrète")
    out = path + ".plain"
    try:
        decrypt_file(path, out, pw)
    except BaseException:
        try:
            os.remove(out)
        except OSError:
            pass
        raise
    return out, True


def _allowed(name):
    if name in ("manifest.json", "seance.db"):
        return True
    parts = name.split("/")
    return parts[0] in DIRS and all(p not in ("", ".", "..") for p in parts)


def validate(path):
    """Readable archive, made by Noxa, of a database this version can open -> manifest."""
    from .db import MIGRATIONS
    try:
        with tarfile.open(path, "r:gz") as tar:
            members = tar.getmembers()
            names = {m.name for m in members}
            if "manifest.json" not in names or "seance.db" not in names:
                raise BackupError("Ce fichier n'est pas une sauvegarde de Noxa")
            for m in members:
                if m.name.startswith("/") or not _allowed(m.name) or not (m.isfile() or m.isdir()):
                    raise BackupError("Archive refusée : contenu inattendu")
            man = json.loads(tar.extractfile("manifest.json").read())
    except (OSError, tarfile.TarError, ValueError, EOFError, KeyError):
        raise BackupError("Archive illisible ou incomplète")
    if not isinstance(man, dict) or man.get("format") != FORMAT:
        raise BackupError("Format de sauvegarde inconnu")
    if not isinstance(man.get("schema"), int) or man["schema"] > len(MIGRATIONS):
        raise BackupError("Sauvegarde faite par une version plus récente de Noxa : mettez Noxa à jour d'abord")
    return man


def stage(data_dir, path):
    """Checks the archive and unpacks it for the next start."""
    if not os.path.isfile(path):
        raise BackupError("Fichier introuvable")
    man = validate(path)
    pending = os.path.join(data_dir, PENDING)
    shutil.rmtree(pending, ignore_errors=True)
    tmp = pending + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, mode=0o700)
    try:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar.getmembers():
                if m.isdir():
                    os.makedirs(os.path.join(tmp, m.name), mode=0o700, exist_ok=True)
                    continue
                target = os.path.join(tmp, m.name)
                os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
                with tar.extractfile(m) as src, open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
        c = sqlite3.connect(os.path.join(tmp, "seance.db"))
        try:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("La base de cette sauvegarde est abîmée")
        finally:
            c.close()
    except (OSError, tarfile.TarError, sqlite3.Error) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise BackupError(f"Restauration impossible : {e}")
    except BackupError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    os.replace(tmp, pending)
    return man


def apply_pending(data_dir):
    """At start, before opening the database: puts a prepared restore in place. -> manifest or None."""
    pending = os.path.join(data_dir, PENDING)
    if not os.path.isfile(os.path.join(pending, "seance.db")):
        return None
    try:
        with open(os.path.join(pending, "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, ValueError):
        man = {}
    before = os.path.join(data_dir, time.strftime("before-restore-%Y%m%d-%H%M%S"))
    os.makedirs(before, mode=0o700)
    for name in ("seance.db", "seance.db-wal", "seance.db-shm", *DIRS):
        p = os.path.join(data_dir, name)
        if os.path.exists(p):
            shutil.move(p, os.path.join(before, name))
    for name in ("seance.db", *DIRS):
        p = os.path.join(pending, name)
        if os.path.exists(p):
            shutil.move(p, os.path.join(data_dir, name))
    shutil.rmtree(pending, ignore_errors=True)
    old_dir, new_dir = man.get("data_dir"), os.path.abspath(data_dir)
    if isinstance(old_dir, str) and old_dir and old_dir != new_dir:
        # another data folder (new server): paths of the downloaded subtitles updated
        c = sqlite3.connect(os.path.join(data_dir, "seance.db"))
        try:
            c.execute("UPDATE items SET probe=replace(probe, ?, ?) WHERE probe LIKE ?",
                      (old_dir + "/subtitles/", new_dir + "/subtitles/", "%" + old_dir + "/subtitles/%"))
            c.commit()
        except sqlite3.Error as e:
            log.warning("Chemins des sous-titres : %s", e)
        finally:
            c.close()
    olds = sorted(n for n in os.listdir(data_dir) if n.startswith("before-restore-"))
    for n in olds[:-KEEP_BEFORE]:
        shutil.rmtree(os.path.join(data_dir, n), ignore_errors=True)
    log.warning("Sauvegarde du %s restaurée (version %s) ; l'état précédent est dans %s",
                time.strftime("%d/%m/%Y %H:%M", time.localtime(man.get("created") or 0)), man.get("version"), before)
    return man


def read_body(req, dest_path, limit=MAX_UPLOAD):
    """Request body written in chunks to a file (without going through the limit of ordinary requests)."""
    try:
        length = int(req.headers.get("Content-Length") or 0)
    except ValueError:
        raise BackupError("Taille invalide")
    if not 0 < length <= limit:
        raise BackupError("Fichier vide ou trop volumineux")
    left = length
    req._body = b""  # body read here (the server must not read it again after the answer)
    try:
        with open(os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
            while left:
                chunk = req.handler.rfile.read(min(left, 1024 * 1024))
                if not chunk:
                    raise BackupError("Envoi interrompu")
                f.write(chunk)
                left -= len(chunk)
    except BaseException:
        if left:
            req.handler.close_connection = True  # rest of the body not read: the connection cannot be reused
        raise


def adopt(ctx, path, label="envoi", own=False):
    """Archive received (browser, external storage, server path): decrypted when needed, checked, and stored
    in the backups folder under a name that tells which server it comes from -> description.
    own: the file is a temporary file of Noxa (moved rather than copied)."""
    dest = directory(ctx)
    os.makedirs(dest, mode=0o700, exist_ok=True)
    plain, decrypted = plain_copy(ctx, path, getattr(ctx, "_passphrase", None))
    try:
        man = validate(plain)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(man.get("created") or time.time()))
        final = os.path.join(dest, f"noxa-{slug(man.get('server') or 'noxa')}_{stamp}-{label}.tar.gz")
        part = final + ".partial"
        if decrypted or own:
            os.replace(plain, part)
        else:
            shutil.copyfile(plain, part)
        os.chmod(part, 0o600)
        os.replace(part, final)
        return describe(final, server_identity(ctx)[0])
    finally:
        if decrypted and os.path.exists(plain):
            os.remove(plain)


def receive(ctx, req, passphrase=None):
    """Archive sent from the browser (to restore on a new server) -> description."""
    dest = directory(ctx)
    os.makedirs(dest, mode=0o700, exist_ok=True)
    part = os.path.join(dest, f".upload-{os.getpid()}-{time.time_ns()}")
    try:
        read_body(req, part)
        ctx._passphrase = passphrase
        return adopt(ctx, part, own=True)
    finally:
        ctx._passphrase = None
        if os.path.exists(part):
            os.remove(part)


# ------------------------------------------------------------------ copies: linked servers and external storages
def remotes(ctx):
    try:
        return json.loads(ctx.db.setting("backup_remotes") or "[]")
    except ValueError:
        return []


def save_remotes(ctx, items):
    ctx.db.set_setting("backup_remotes", json.dumps(items))


def storage_for(ctx, rid):
    from . import remote_storage
    items = remotes(ctx)
    r = next((r for r in items if r["id"] == rid), None)
    if not r:
        raise BackupError("Stockage introuvable")

    def save(conf):
        cur = remotes(ctx)
        for x in cur:
            if x["id"] == rid:
                x["conf"] = conf
        save_remotes(ctx, cur)
    return r, remote_storage.make(r["kind"], r["conf"], save)


def status(ctx, key, ok, error=None, extra=None):
    try:
        cur = json.loads(ctx.db.setting("backup_copies") or "{}")
    except ValueError:
        cur = {}
    cur[key] = {"at": int(time.time()), "ok": ok, "error": error, **(extra or {})}
    ctx.db.set_setting("backup_copies", json.dumps(cur))


def copies_status(ctx):
    try:
        return json.loads(ctx.db.setting("backup_copies") or "{}")
    except ValueError:
        return {}


def name_for_peer(peer, name):
    """Backup file name as a linked server understands it: servers older than 4.5 only accept the nexis- prefix
    (and so do servers whose version is not known yet)."""
    v = tuple(int(x) for x in re.findall(r"\d+", str(peer.get("version") or ""))[:2])
    if name.startswith("noxa-") and (len(v) < 2 or v < (4, 5)):
        return "nexis-" + name[len("noxa-"):]
    return name


def push_to_peer(ctx, peer, path):
    """Copy of a backup of this server to a linked server (which keeps each server's apart)."""
    from .federation import FedError
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        try:
            resp, close = ctx.fed.request(peer, "POST", "/fed/backup", f, timeout=900,
                                          headers={"Content-Length": str(size),
                                                   "X-Backup-Name": name_for_peer(peer, os.path.basename(path))})
        except FedError as e:
            raise BackupError(str(e))
        try:
            body = resp.read(65536)
        finally:
            close()
    if resp.status != 200:
        try:
            msg = json.loads(body or b"{}").get("error")
        except ValueError:
            msg = None
        raise BackupError(msg or f"HTTP {resp.status}")


def push_to_storage(ctx, rid, path):
    """Upload to an external storage: encrypted when a passphrase is saved; old automatic
    copies of this server removed beyond the number kept."""
    from .remote_storage import StorageError
    r, st = storage_for(ctx, rid)
    pw = ctx.db.setting("backup_passphrase")
    name = os.path.basename(path)
    src = path
    try:
        if pw:
            src = path + ".enc-tmp"
            encrypt_file(path, src, pw)
            name += ENC_SUFFIX
        st.upload(src, name)
        keep = int(r.get("keep") or 7)
        me = slug(server_identity(ctx)[1])
        mine = (f"noxa-{me}_", f"nexis-{me}_")  # nexis-: made before 4.5
        autos = sorted((f for f in st.files() if f["name"].startswith(mine) and "-auto." in f["name"]),
                       key=lambda f: f["name"].split("_", 1)[-1], reverse=True)  # newest first, whatever the prefix
        for f in autos[keep:]:
            st.delete(f)
    except StorageError as e:
        raise BackupError(str(e))
    finally:
        if src != path and os.path.exists(src):
            os.remove(src)


def distribute(ctx, path):
    """After a backup: copies to the linked servers (when enabled) and the enabled external storages.
    A failure is noted (and flagged by an alert) without stopping the other copies."""
    db = ctx.db
    results = []
    if db.setting("backup_to_peers", "1") == "1" and getattr(ctx, "fed", None):
        for p in ctx.fed.peers():
            if not p["online"]:
                status(ctx, f"peer:{p['id']}", False, "serveur injoignable", {"name": p["name"]})
                results.append((p["name"], False))
                continue
            try:
                push_to_peer(ctx, p, path)
                status(ctx, f"peer:{p['id']}", True, None, {"name": p["name"]})
                results.append((p["name"], True))
            except BackupError as e:
                status(ctx, f"peer:{p['id']}", False, str(e), {"name": p["name"]})
                results.append((p["name"], False))
            finally:
                db.release()
    for r in remotes(ctx):
        if not r.get("enabled", True):
            continue
        try:
            push_to_storage(ctx, r["id"], path)
            status(ctx, f"remote:{r['id']}", True, None, {"name": r["name"]})
            results.append((r["name"], True))
        except BackupError as e:
            log.warning("Copie de la sauvegarde vers %s : %s", r["name"], e)
            status(ctx, f"remote:{r['id']}", False, str(e), {"name": r["name"]})
            results.append((r["name"], False))
        finally:
            db.release()
    failed = [n for n, ok in results if not ok]
    health = getattr(ctx, "health", None)
    if health:
        if failed:
            health.raise_alert("backup:copies", "task", "Copie des sauvegardes en échec", ", ".join(failed), "#/admin/backups")
        else:
            health.resolve("backup:copies")
    return results


def distribute_later(ctx, path):
    import threading

    def run():
        try:
            distribute(ctx, path)
        except Exception:
            log.exception("Copie de la sauvegarde")
        finally:
            ctx.db.release()
    threading.Thread(target=run, daemon=True, name="backup-copies").start()


# ------------------------------------------------------------------ routes (recopies, stockages, restauration)
def register(app, ctx, x):
    import secrets
    import threading
    from . import remote_storage
    from .web import HTTPError
    db = ctx.db

    @app.route("POST", "/fed/backup", auth="peer")
    def fed_backup(req):
        """Copy of a backup sent by a linked server: kept apart, under that server's name."""
        if db.setting("backup_from_peers", "1") != "1":
            raise HTTPError(403, "Ce serveur ne garde pas les sauvegardes des serveurs liés")
        name = req.headers.get("X-Backup-Name") or ""
        m = NAME.match(name)
        if not m or not m.group("srv"):
            raise HTTPError(400, "Nom de sauvegarde invalide")
        dest = directory(ctx)
        os.makedirs(dest, mode=0o700, exist_ok=True)
        try:
            need = int(req.headers.get("Content-Length") or 0)
            if shutil.disk_usage(dest).free < need * 2 + 512 * 1024 * 1024:
                raise HTTPError(507, "Espace insuffisant pour garder la sauvegarde")
        except (OSError, ValueError):
            pass
        part = os.path.join(dest, f".peer-{secrets.token_hex(6)}")
        db.release()
        try:
            read_body(req, part)
            man = validate(part)
            if man.get("server_uid") != req.fed_peer["uid"]:
                raise HTTPError(400, "Sauvegarde d'un autre serveur")
            final = os.path.join(dest, name)
            os.replace(part, final)
        except BackupError as e:
            raise HTTPError(400, str(e))
        finally:
            if os.path.exists(part):
                os.remove(part)
        prune(ctx, int(ctx.tasks.conf("backup")["options"].get("keep", 7)))
        log.info("Copie de la sauvegarde de %s reçue : %s", req.fed_peer["name"], name)
        return {"ok": True}

    # ---------------------------------------------------------------- stockages externes
    def remote_view(r):
        st = copies_status(ctx).get(f"remote:{r['id']}")
        return {"id": r["id"], "kind": r["kind"], "name": r["name"], "enabled": r.get("enabled", True), "keep": r.get("keep", 7),
                "conf": remote_storage.public_conf(r["conf"]),
                "linked": bool(r["conf"].get("refresh_token")) if r["kind"] in ("gdrive", "onedrive") else True,
                "last": st}

    @app.route("GET", "/api/admin/backups/remotes", auth="admin")
    def list_remotes(req):
        return {"remotes": [remote_view(r) for r in remotes(ctx)],
                "peers": [dict(v, key=k) for k, v in copies_status(ctx).items() if k.startswith("peer:")]}

    @app.route("POST", "/api/admin/backups/remotes", auth="admin")
    def add_remote(req):
        b = req.json()
        kind = b.get("kind")
        if kind not in remote_storage.KINDS:
            raise HTTPError(400, "Type de stockage inconnu")
        try:
            conf = remote_storage.valid_conf(kind, b.get("conf") or {})
        except remote_storage.StorageError as e:
            raise HTTPError(400, str(e))
        items = remotes(ctx)
        if len(items) >= 8:
            raise HTTPError(400, "8 stockages au plus")
        r = {"id": secrets.token_hex(4), "kind": kind, "conf": conf, "enabled": True,
             "name": str(b.get("name") or {"s3": "S3", "gdrive": "Google Drive", "onedrive": "OneDrive"}[kind]).strip()[:60],
             "keep": max(1, min(90, int(b.get("keep") or 7)))}
        if kind == "s3":
            db.release()
            try:
                remote_storage.make(kind, conf, lambda c: None).check()  # keys and bucket checked right away
            except remote_storage.StorageError as e:
                raise HTTPError(400, str(e))
        items.append(r)
        save_remotes(ctx, items)
        return remote_view(r)

    def get_remote(req):
        r = next((r for r in remotes(ctx) if r["id"] == req.params["rid"]), None)
        if not r:
            raise HTTPError(404, "Stockage introuvable")
        return r

    RID = r"(?P<rid>[0-9a-f]{8})"

    @app.route("PUT", f"/api/admin/backups/remotes/{RID}", auth="admin")
    def edit_remote(req):
        r = get_remote(req)
        b = req.json()
        items = remotes(ctx)
        for it in items:
            if it["id"] != r["id"]:
                continue
            if "enabled" in b:
                it["enabled"] = bool(b["enabled"])
            if "keep" in b:
                it["keep"] = max(1, min(90, int(b["keep"] or 7)))
            if b.get("name"):
                it["name"] = str(b["name"]).strip()[:60]
            if "conf" in b:
                try:
                    it["conf"] = remote_storage.valid_conf(it["kind"], b["conf"], it["conf"])
                except remote_storage.StorageError as e:
                    raise HTTPError(400, str(e))
            r = it
        save_remotes(ctx, items)
        return remote_view(r)

    @app.route("DELETE", f"/api/admin/backups/remotes/{RID}", auth="admin")
    def delete_remote(req):
        get_remote(req)
        save_remotes(ctx, [r for r in remotes(ctx) if r["id"] != req.params["rid"]])
        return {"ok": True}

    @app.route("POST", f"/api/admin/backups/remotes/{RID}/link", auth="admin")
    def link_remote(req):
        r = get_remote(req)
        if r["kind"] not in ("gdrive", "onedrive"):
            raise HTTPError(400, "Ce stockage ne se relie pas par un code")
        db.release()
        try:
            return storage_for(ctx, r["id"])[1].device_start()
        except remote_storage.StorageError as e:
            raise HTTPError(502, str(e))

    @app.route("POST", f"/api/admin/backups/remotes/{RID}/poll", auth="admin")
    def poll_remote(req):
        r = get_remote(req)
        db.release()
        try:
            return {"status": storage_for(ctx, r["id"])[1].device_poll()}
        except remote_storage.StorageError as e:
            raise HTTPError(502, str(e))

    @app.route("GET", f"/api/admin/backups/remotes/{RID}/files", auth="admin")
    def remote_files(req):
        r = get_remote(req)
        db.release()
        try:
            files = storage_for(ctx, r["id"])[1].files()
        except remote_storage.StorageError as e:
            raise HTTPError(502, str(e))
        out = []
        for f in files:
            base = f["name"][:-len(ENC_SUFFIX)] if f["name"].endswith(ENC_SUFFIX) else f["name"]
            m = NAME.match(base)
            if m:
                out.append({**f, "encrypted": f["name"].endswith(ENC_SUFFIX), "server": m.group("srv"),
                            "auto": m.group("label") == "auto"})
        return {"files": sorted(out, key=lambda f: (f["name"].split("_")[-1]), reverse=True)}

    @app.route("POST", f"/api/admin/backups/remotes/{RID}/push", auth="admin")
    def remote_push(req):
        r = get_remote(req)
        mine = [b for b in listing(ctx) if b["mine"] and not b.get("broken")]
        if not mine:
            raise HTTPError(400, "Aucune sauvegarde de ce serveur à envoyer")
        db.release()
        try:
            push_to_storage(ctx, r["id"], os.path.join(directory(ctx), mine[0]["name"]))
            status(ctx, f"remote:{r['id']}", True, None, {"name": r["name"]})
        except BackupError as e:
            status(ctx, f"remote:{r['id']}", False, str(e), {"name": r["name"]})
            raise HTTPError(502, str(e))
        return remote_view(get_remote(req))

    def restart_after(man):
        log.warning("Restauration de la sauvegarde du %s demandée", time.strftime("%d/%m/%Y %H:%M", time.localtime(man.get("created") or 0)))
        restarting = bool(getattr(ctx, "restart", None))
        if restarting:
            threading.Timer(1.5, ctx.restart).start()
        return {"ok": True, "restarting": restarting, "version": man.get("version"), "created": man.get("created"),
                "server": man.get("server")}

    def stage_described(b):
        try:
            return stage(ctx.data_dir, os.path.join(directory(ctx), b["name"]))
        except BackupError as e:
            raise HTTPError(400, str(e))

    @app.route("POST", f"/api/admin/backups/remotes/{RID}/restore", auth="admin")
    def remote_restore(req):
        """Restore a backup kept on an external storage: downloaded, decrypted, then put in place."""
        r = get_remote(req)
        b = req.json()
        fid, pw = b.get("id"), b.get("passphrase") or None
        db.release()
        try:
            st = storage_for(ctx, r["id"])[1]
            f = next((f for f in st.files() if f["id"] == fid), None)
            if not f:
                raise HTTPError(404, "Fichier introuvable sur le stockage")
            tmp = os.path.join(directory(ctx), f".dl-{secrets.token_hex(6)}")
            os.makedirs(directory(ctx), mode=0o700, exist_ok=True)
            try:
                st.download(f, tmp)
                ctx._passphrase = pw
                desc = adopt(ctx, tmp, "restauree", own=True)
            finally:
                ctx._passphrase = None
                if os.path.exists(tmp):
                    os.remove(tmp)
        except (remote_storage.StorageError, BackupError) as e:
            raise HTTPError(400, str(e))
        return restart_after(stage_described(desc))

    # ---------------------------------------------------------------- one-step restore
    @app.route("POST", "/api/admin/backups/restore-file", auth="admin")
    def restore_file(req):
        """Restore directly from a file picked on the device (.tar.gz archive, or encrypted .enc)."""
        db.release()
        try:
            desc = receive(ctx, req, req.headers.get("X-Backup-Passphrase") or None)
        except BackupError as e:
            raise HTTPError(400, str(e))
        return restart_after(stage_described(desc))

    @app.route("POST", "/api/admin/backups/restore-path", auth="admin")
    def restore_path(req):
        """Restore from a file present on the server (external disk, mounted NAS…)."""
        b = req.json()
        path = str(b.get("path") or "")
        if not path.startswith("/") or "\0" in path or not os.path.isfile(path):
            raise HTTPError(400, "Fichier introuvable sur le serveur")
        db.release()
        try:
            ctx._passphrase = b.get("passphrase") or None
            desc = adopt(ctx, path, "restauree")
        except BackupError as e:
            raise HTTPError(400, str(e))
        finally:
            ctx._passphrase = None
        return restart_after(stage_described(desc))
