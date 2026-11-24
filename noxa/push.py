"""Push notifications (Web Push): new episode of a followed show, requested title arrived, test.

- Server keys (VAPID, P-256 curve) created with openssl at first use, in <data>/push.
- Messages are sent WITHOUT content: nothing to encrypt, only the signed header (JWT ES256) proves they come
  from this server. The device's service worker, woken up, then asks Noxa what to show.
- Only the browsers' push services are contacted (Google, Mozilla, Apple, Microsoft): an arbitrary
  subscription address cannot be used to make the server call a machine of the network.
- Android app: its web view does not know Web Push. The device gets a token (subscription
  "device:<token fingerprint>") and fetches its messages itself (/api/push/device, about every 15 min):
  neither Google nor a developer account.
"""
import hashlib
import secrets
import base64
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("noxa.push")

PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "push.services.mozilla.com",
              "web.push.apple.com", "notify.windows.com")
CONTACT = "https://github.com/xenix13/Noxa"
KEEP_MESSAGES = 20
DEVICE = "device:"  # subscription of an Android app (messages fetched by the device)
DEVICE_IDLE = 60 * 86400  # device that has asked nothing for 60 days: unsubscribed


def device_key(token):
    return DEVICE + hashlib.sha256(str(token).encode()).hexdigest()


def in_quiet_hours(spec, now=None):
    """spec "22:30-07:00" (server time): true during this range, which can go past midnight."""
    if not spec or len(spec) != 11:
        return False
    try:
        a = int(spec[:2]) * 60 + int(spec[3:5])
        b = int(spec[6:8]) * 60 + int(spec[9:11])
    except ValueError:
        return False
    t = time.localtime(now)
    m = t.tm_hour * 60 + t.tm_min
    return a <= m < b if a < b else (m >= a or m < b)


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def valid_endpoint(url):
    u = urllib.parse.urlsplit(str(url or ""))
    host = (u.hostname or "").lower()
    return (u.scheme == "https" and len(url) <= 1000 and not u.username and not u.port and
            any(host == h or host.endswith("." + h) for h in PUSH_HOSTS))


def _der_to_raw(sig):
    """ECDSA DER signature (openssl) -> r || s on 32 bytes each (JWT ES256 format)."""
    if sig[0] != 0x30:
        raise ValueError("signature")
    i = 2 if sig[1] < 0x80 else 2 + (sig[1] & 0x7F)
    out = b""
    for _ in range(2):
        if sig[i] != 0x02:
            raise ValueError("signature")
        n = sig[i + 1]
        v = sig[i + 2:i + 2 + n].lstrip(b"\0")
        out += v.rjust(32, b"\0")
        i += 2 + n
    return out


class Push:
    def __init__(self, ctx):
        self.ctx = ctx
        self.dir = os.path.join(ctx.data_dir, "push")
        self.key = os.path.join(self.dir, "vapid.pem")
        self._pub = None
        self.lock = threading.Lock()
        self.messages = {}  # account -> latest messages (what the device shows once woken up)

    # ------------------------------------------------------------ keys
    def ensure_key(self):
        with self.lock:
            if os.path.isfile(self.key):
                return True
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            tmp = self.key + ".tmp"
            old = os.umask(0o077)
            try:
                subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", tmp],
                               check=True, capture_output=True, timeout=30)
                os.replace(tmp, self.key)
            except (OSError, subprocess.SubprocessError) as e:
                log.error("Clés des notifications push : %s", e)
                return False
            finally:
                os.umask(old)
            return True

    def public_key(self):
        """Public key (uncompressed P-256 point, base64url) the browser ties to the subscription."""
        if self._pub is None and self.ensure_key():
            try:
                der = subprocess.run(["openssl", "ec", "-in", self.key, "-pubout", "-outform", "DER"],
                                     check=True, capture_output=True, timeout=30).stdout
                self._pub = b64url(der[-65:])
            except (OSError, subprocess.SubprocessError) as e:
                log.error("Clé publique des notifications push : %s", e)
        return self._pub

    def _jwt(self, endpoint):
        u = urllib.parse.urlsplit(endpoint)
        head = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        body = b64url(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": CONTACT},
                                 separators=(",", ":")).encode())
        unsigned = f"{head}.{body}".encode()
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.key], input=unsigned,
                             check=True, capture_output=True, timeout=30).stdout
        return f"{head}.{body}.{b64url(_der_to_raw(sig))}"

    # ------------------------------------------------------------ envoi
    def _send_one(self, sub):
        db = self.ctx.db
        try:
            req = urllib.request.Request(sub["endpoint"], data=b"", method="POST", headers={
                "TTL": "86400", "Urgency": "normal", "Content-Length": "0",
                "Authorization": f"vapid t={self._jwt(sub['endpoint'])}, k={self.public_key()}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read(1024)
            db.run("UPDATE push_subs SET failures=0, last_ok=? WHERE id=?", (int(time.time()), sub["id"]))
            return True
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):  # subscription revoked (notifications refused, app uninstalled)
                db.run("DELETE FROM push_subs WHERE id=?", (sub["id"],))
            else:
                db.run("UPDATE push_subs SET failures=failures+1 WHERE id=?", (sub["id"],))
                log.info("Notification push refusée (%s) : %s", e.code, e.read(300))
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            db.run("UPDATE push_subs SET failures=failures+1 WHERE id=?", (sub["id"],))
            log.info("Notification push impossible : %s", e)
        finally:
            db.run("DELETE FROM push_subs WHERE failures>=20")
        return False

    def prefs(self, uid):
        x = getattr(self.ctx, "x", None)
        return x.get_prefs(uid) if x and hasattr(x, "get_prefs") else {}

    def notify(self, uid, title, body, url="#/", tag="nexis", kind=None):
        """Message for all the subscriptions of this account (in the background).

        kind: "episodes" -> the account can refuse them (notify_<kind>); during its quiet hours,
        these messages do not make the device ring. Alerts and tests always get through."""
        prefs = self.prefs(uid) if kind else {}
        if kind and prefs.get(f"notify_{kind}") is False:
            return 0
        quiet = bool(kind and in_quiet_hours(prefs.get("quiet_hours")))
        msg = {"title": title[:120], "body": body[:240], "url": url, "tag": tag, "at": time.time(), "ring": not quiet}
        with self.lock:
            lst = self.messages.setdefault(uid, [])
            lst.append(msg)
            del lst[:-KEEP_MESSAGES]
        if quiet:
            return 0
        subs = self.ctx.db.all("SELECT * FROM push_subs WHERE user_id=?", (uid,))
        devices = [x for x in subs if x["endpoint"].startswith(DEVICE)]  # they will come and fetch the message
        web = [x for x in subs if not x["endpoint"].startswith(DEVICE)]
        if web and self.public_key():
            threading.Thread(target=self._send_all, args=(web,), daemon=True, name="push").start()
            return len(subs)
        return len(devices)

    def _send_all(self, subs):
        try:
            for s in subs:
                self._send_one(s)
        finally:
            self.ctx.db.release()

    def pending(self, uid, since, ringing=False):
        with self.lock:
            return [{k: v for k, v in m.items() if k != "ring"} | {"at": int(m["at"])}
                    for m in self.messages.get(uid, []) if m["at"] >= since and (m["ring"] or not ringing)]

    # ------------------------------------------------------------ new episodes (after a scan)
    def after_scan(self, x):
        db = self.ctx.db
        db.run("DELETE FROM push_subs WHERE endpoint LIKE ? AND COALESCE(last_ok, created_at) < ?",
               (DEVICE + "%", int(time.time()) - DEVICE_IDLE))
        uids = [r["user_id"] for r in db.all("SELECT DISTINCT user_id FROM push_subs")]
        for uid in uids:
            user = db.one("SELECT * FROM users WHERE id=? AND disabled=0", (uid,))
            if not user:
                continue
            last = user.get("push_seen") or 0
            fresh = [(sh, [e for e in eps if (e["added_at"] or 0) > last]) for sh, eps in x.new_episodes(user)]
            fresh = [(sh, eps) for sh, eps in fresh if eps]
            if not fresh:
                continue
            db.run("UPDATE users SET push_seen=? WHERE id=?", (max(e["added_at"] or 0 for _, eps in fresh for e in eps), uid))
            from .i18n import tr
            if len(fresh) == 1:
                sh, eps = fresh[0]
                e = sorted(eps, key=lambda e: ((e.get("parent_idx") or 0), e["idx"] or 0))[0]
                title = tr("Nouvel épisode") if len(eps) == 1 else tr("{n} nouveaux épisodes").format(n=len(eps))
                body = f"{sh['title']} · S{e.get('parent_idx') or 0}E{e['idx'] or 0} · {e['title']}" if len(eps) == 1 else sh["title"]
                self.notify(uid, title, body, f"#/item/{sh['id']}", tag=f"show-{sh['id']}", kind="episodes")
            else:
                n = sum(len(eps) for _, eps in fresh)
                self.notify(uid, tr("{n} nouveaux épisodes").format(n=n), ", ".join(sh["title"] for sh, _ in fresh[:4]),
                            "#/", tag="episodes", kind="episodes")


def register(app, ctx, x):
    from .web import HTTPError
    db = ctx.db
    push = ctx.push

    @app.route("GET", "/api/push/key")
    def push_key(req):
        return {"key": push.public_key(), "subscribed": bool(db.scalar(
            "SELECT 1 FROM push_subs WHERE user_id=? AND endpoint=?", (req.user["id"], req.arg("endpoint") or "")))}

    @app.route("POST", "/api/push/subscribe")
    def push_subscribe(req):
        b = req.json()
        endpoint = str(b.get("endpoint") or "")
        if not valid_endpoint(endpoint):
            raise HTTPError(400, "Service de notifications non reconnu")
        uid, now = req.user["id"], int(time.time())
        if db.scalar("SELECT COUNT(*) FROM push_subs WHERE user_id=?", (uid,)) >= 20:
            db.run("DELETE FROM push_subs WHERE id=(SELECT id FROM push_subs WHERE user_id=? ORDER BY created_at LIMIT 1)", (uid,))
        db.run("INSERT INTO push_subs(user_id,endpoint,created_at) VALUES(?,?,?) ON CONFLICT(endpoint) DO UPDATE SET "
               "user_id=excluded.user_id, failures=0", (uid, endpoint, now))
        # from now on: no burst for the episodes added before the subscription
        db.run("UPDATE users SET push_seen=? WHERE id=? AND push_seen<?", (now, uid, now - 31 * 86400))
        return {"ok": True}

    @app.route("POST", "/api/push/unsubscribe")
    def push_unsubscribe(req):
        db.run("DELETE FROM push_subs WHERE user_id=? AND endpoint=?", (req.user["id"], str(req.json().get("endpoint") or "")))
        return {"ok": True}

    @app.route("POST", "/api/push/test")
    def push_test(req):
        from .i18n import tr
        n = push.notify(req.user["id"], tr("Notifications activées"), tr("Noxa vous préviendra des nouveaux épisodes."), "#/", "test")
        if not n:
            raise HTTPError(409, "Aucun appareil inscrit aux notifications")
        return {"sent": n}

    # ------------------------------------------------------------ application Android
    @app.route("POST", "/api/push/device")
    def push_device_add(req):
        """Subscribes this device's Android app: the token returned only serves to read its notifications."""
        uid, now = req.user["id"], int(time.time())
        token = secrets.token_urlsafe(32)
        if db.scalar("SELECT COUNT(*) FROM push_subs WHERE user_id=?", (uid,)) >= 20:
            db.run("DELETE FROM push_subs WHERE id=(SELECT id FROM push_subs WHERE user_id=? ORDER BY created_at LIMIT 1)", (uid,))
        # last_ok: the device's read cursor, to the fraction of a second (nothing from before the subscription)
        db.run("INSERT INTO push_subs(user_id,endpoint,created_at,last_ok) VALUES(?,?,?,?)", (uid, device_key(token), now, time.time()))
        db.run("UPDATE users SET push_seen=? WHERE id=? AND push_seen<?", (now, uid, now - 31 * 86400))
        return {"token": token}

    @app.route("POST", "/api/push/device/remove")
    def push_device_remove(req):
        db.run("DELETE FROM push_subs WHERE user_id=? AND endpoint=?", (req.user["id"], device_key(req.json().get("token") or "")))
        return {"ok": True}

    @app.route("GET", "/api/push/device", auth=None)
    def push_device_poll(req):
        """Messages for the Android app to show since its last visit (X-Noxa-Device header, X-Nexis-Device before 4.5)."""
        token = (req.headers.get("X-Noxa-Device") or req.headers.get("X-Nexis-Device") or "").strip()  # Nexis: apps < 4.5
        sub = db.one("SELECT s.* FROM push_subs s JOIN users u ON u.id=s.user_id WHERE s.endpoint=? AND u.disabled=0",
                     (device_key(token),)) if 20 <= len(token) <= 100 else None
        if not sub:
            raise HTTPError(410, "Appareil désinscrit des notifications")
        now = time.time()
        since = sub["last_ok"] or sub["created_at"]
        db.run("UPDATE push_subs SET last_ok=? WHERE id=?", (now, sub["id"]))
        return {"messages": push.pending(sub["user_id"], since, ringing=True)}

    @app.route("GET", "/api/push/pending")
    def push_pending(req):
        """What the device woken up by a notification shows (the messages of the last 10 minutes)."""
        return {"messages": push.pending(req.user["id"], int(time.time()) - 600)}
