"""Per-account limits, set by an administrator (Settings → Users):

- simultaneous streams: beyond, a new playback is refused (the others go on);
- bandwidth outside the local network: from outside, the quality is lowered to fit this bandwidth (useful
  to spare the upload of one's router, or a lent account); at home, nothing changes;
- screen time (children's profiles mostly): minutes per day and bedtime. Playback time is
  counted during playback; at the limit, the video stops with a message, and a new playback is
  refused until the next day (or until the end of the night).
"""
import re
import threading
import time

from . import dlna, transcoder

BEDTIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")
ACTIVE = 90          # a stream whose player gave a sign of life in the last 90 s is running
MAX_GAP = 60         # at most 60 s counted between two playback reports (pause, sleep…)
WARN_MINUTES = 5


def valid_limits(b):
    """Limit fields sent by the administration -> {column: value} (None: no limit)."""
    out = {}
    for k, lo, hi in (("max_streams", 1, 20), ("remote_kbps", 500, 200000), ("daily_minutes", 10, 1440)):
        if k in b:
            v = b[k]
            if v in (None, "", 0):
                out[k] = None
            elif isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                out[k] = v
            else:
                raise ValueError(k)
    if "bedtime" in b:
        v = b["bedtime"]
        if v in (None, ""):
            out["bedtime"] = None
        elif isinstance(v, str) and BEDTIME.match(v) and v[:5] != v[6:]:
            out["bedtime"] = v
        else:
            raise ValueError("bedtime")
    return out


def _minutes(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def in_bedtime(bedtime, now=None):
    """"21:00-07:00": true from 9 pm to 7 am (the range can go past midnight)."""
    if not bedtime or not BEDTIME.match(bedtime):
        return False
    t = time.localtime(now or time.time())
    cur = t.tm_hour * 60 + t.tm_min
    start, end = _minutes(bedtime[:5]), _minutes(bedtime[6:])
    return start <= cur < end if start < end else cur >= start or cur < end


def today(now=None):
    return time.strftime("%Y-%m-%d", time.localtime(now or time.time()))


class Limits:
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.Lock()
        self.last_report = {}  # (account, stream) -> instant of the last playback report

    # ------------------------------------------------------------ screen time
    def used_today(self, uid):
        return self.ctx.db.scalar("SELECT seconds FROM screen_time WHERE user_id=? AND day=?", (uid, today())) or 0

    def screen(self, user):
        """Screen time state of an account -> {limit, used, left, bedtime, blocked, reason} (minutes)."""
        limit = user.get("daily_minutes")
        bedtime = user.get("bedtime")
        used = self.used_today(user["id"]) // 60 if limit else 0
        out = {"limit": limit, "used": used, "left": max(0, limit - used) if limit else None, "bedtime": bedtime,
               "blocked": False, "reason": None}
        if bedtime and in_bedtime(bedtime):
            out.update(blocked=True, reason="bedtime")
        elif limit and used >= limit:
            out.update(blocked=True, reason="daily")
        return out

    def count(self, user, stream, state):
        """Playback report: adds the time elapsed since the previous one -> screen time state, or None
        (account without a time limit)."""
        if not user.get("daily_minutes") and not user.get("bedtime"):
            return None
        now = time.time()
        key = (user["id"], stream or "")
        with self.lock:
            prev = self.last_report.get(key)
            self.last_report[key] = now
            if len(self.last_report) > 2000:
                for k, t in list(self.last_report.items()):
                    if now - t > 3600:
                        del self.last_report[k]
        if prev and state == "playing":
            delta = int(min(MAX_GAP, max(0, now - prev)))
            if delta:
                self.ctx.db.run("INSERT INTO screen_time(user_id, day, seconds) VALUES(?,?,?) ON CONFLICT(user_id, day) "
                                "DO UPDATE SET seconds=seconds+excluded.seconds", (user["id"], today(now), delta))
        return self.screen(user)

    # ------------------------------------------------------------ when a playback starts
    def check_play(self, user, replace=None):
        """Raises LimitError when this account cannot start a playback now."""
        s = self.screen(user)
        if s["blocked"]:
            if s["reason"] == "bedtime":
                raise LimitError(403, "C'est l'heure d'aller dormir : la lecture reprendra à {end}.",
                                 end=user["bedtime"][6:])
            raise LimitError(403, "Temps d'écran du jour écoulé ({n} min) : à demain !", n=user["daily_minutes"])
        n = user.get("max_streams")
        if n:
            db = self.ctx.db
            busy = db.scalar("SELECT COUNT(*) FROM streams WHERE user_id=? AND last_seen>=? AND id<>?",
                             (user["id"], int(time.time()) - ACTIVE, str(replace or "")))
            if busy >= n:
                raise LimitError(429, "Ce compte regarde déjà {n} vidéo(s) en même temps : c'est la limite fixée par "
                                      "l'administrateur. Arrêtez une autre lecture pour lancer celle-ci.", n=n)

    def remote_cap(self, user, req):
        """Maximum bandwidth (kbit/s) for this playback, or None: only outside the local network."""
        cap = user.get("remote_kbps")
        if not cap or dlna.is_local(req.client_ip):
            return None
        return cap


class LimitError(Exception):
    def __init__(self, status, message, **values):
        super().__init__(message)
        self.status = status
        self.message = message
        self.values = values


ORDER = ("original", "1080p", "720p", "480p")


def cap_quality(quality, probe, cap, file_size=0, duration_ms=0):
    """The higher of the two qualities must not exceed what the maximum bandwidth allows."""
    allowed = transcoder.auto_quality(probe, cap, file_size, duration_ms)
    return quality if ORDER.index(quality) >= ORDER.index(allowed) else allowed
