"""Preparation in advance: internal subtitles and preview thumbnails (seek bar).

Extracting a subtitle track from an MKV forces ffmpeg to read the whole file: on a
network share, that can take one to several minutes. So all the text tracks
of a file are extracted in ONE read, and as early as possible:
- first, at full speed, before starting a playback (the player waits until everything is ready);
- urgently for the next episode, with a capped read rate (-readrate) so as not to
  take bandwidth on the network share away from the current stream;
- in the background (with the thumbnails) only when nothing is playing: a
  background job stops as soon as a playback starts, and resumes later.
"""
import collections
import contextlib
import json
import math
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

from . import media
from .fonts import font_names
from .subtitles import parse_subtitles

log = logging.getLogger("noxa.subs")


class Job:
    def __init__(self, item_id, kind="play"):
        self.item_id = item_id
        self.kind = kind          # play | soon (capped) | idle (with thumbnails) | gentle (background during a playback)
        self.progress = 0.0
        self.until_ms = 0         # time up to which the subtitles are already extracted (progressive playback)
        self.tmp = None           # folder where ffmpeg writes as it goes
        self.done = threading.Event()
        self.error = None
        self.aborted = False


class Aborted(Exception):
    pass


SOON_READRATE = 8     # × real time: ~40 Mb/s for a 5 Mb/s file
GENTLE_READRATE = 2   # background preparation while someone watches: very little bandwidth
PARTIAL_MARGIN_MS = 5000


TRICK_INTERVAL = 10   # one thumbnail every 10 s
TRICK_W = 240
TRICK_COLS = TRICK_ROWS = 10


ASS_CODECS = {"ass", "ssa"}


def text_tracks(probe):
    return [s["index"] for s in (probe or {}).get("subs", []) if s.get("codec") in media.TEXT_SUB_CODECS]


PGS_CODECS = {"hdmv_pgs_subtitle", "pgssub"}


def pgs_tracks(probe):
    return [s["index"] for s in (probe or {}).get("subs", []) if s.get("codec") in PGS_CODECS]


def ass_tracks(probe):
    return [s["index"] for s in (probe or {}).get("subs", []) if s.get("codec") in ASS_CODECS]


def _complete_sup(data):
    """Cuts a .sup being written after the last complete "display set" (END segment)."""
    i, end = 0, 0
    while i + 13 <= len(data) and data[i:i + 2] == b"PG":
        size = int.from_bytes(data[i + 11:i + 13], "big")
        if i + 13 + size > len(data):
            break
        if data[i + 10] == 0x80:
            end = i + 13 + size
        i += 13 + size
    return data[:end]


def _probe(item):
    return (json.loads(item["probe"] or "{}") if isinstance(item["probe"], str) else item["probe"]) or {}


class SubtitleCache:
    def __init__(self, ctx):
        self.ctx = ctx
        self.base = os.path.join(ctx.data_dir, "cache", "subs")
        self.trick_base = os.path.join(ctx.data_dir, "cache", "trickplay")
        os.makedirs(self.base, exist_ok=True)
        os.makedirs(self.trick_base, exist_ok=True)
        self.lock = threading.Lock()
        self.jobs = {}
        self.urgent = collections.OrderedDict()
        self.idle = collections.OrderedDict()
        self.slots = threading.BoundedSemaphore(2)
        self.wake = threading.Event()
        self.stopped = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------ cache
    @staticmethod
    def content_key(item):
        """The cache follows the FILE (size + date), not its record: a file renamed, moved or indexed again
        finds its subtitles and thumbnails already exported, without reading the file again."""
        size = item["file_size"] if "file_size" in item.keys() else None
        return f"f{size}_{item['file_mtime'] or 0}" if size else None

    def _keyed(self, base, item):
        key = self.content_key(item)
        legacy = os.path.join(base, f"{item['id']}_{item['file_mtime'] or 0}")
        if not key:
            return legacy
        path = os.path.join(base, key)
        if not os.path.isdir(path) and os.path.isdir(legacy):
            try:  # cache of an earlier version (stored by record number): taken over as is
                os.replace(legacy, path)
            except OSError:
                return legacy
        return path

    def _dir(self, item):
        return self._keyed(self.base, item)

    def path(self, item, index):
        return os.path.join(self._dir(item), f"{int(index)}.json")

    def sup_path(self, item, index):
        return os.path.join(self._dir(item), f"{int(index)}.sup")

    def ass_path(self, item, index):
        return os.path.join(self._dir(item), f"{int(index)}.ass")

    def fonts_index(self, item):
        try:
            with open(os.path.join(self._dir(item), "fonts.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def font_path(self, item, name):
        return os.path.join(self._dir(item), "fonts", os.path.basename(name))

    def missing(self, item):
        """Tracks to extract: text (JSON), original ASS (faithful rendering) and attached fonts."""
        if item.get("peer_id"):
            return []  # item of a linked server: prepared by that server
        probe = _probe(item)
        ass = ass_tracks(probe)
        need = [i for i in text_tracks(probe) if not os.path.isfile(self.path(item, i))
                or (i in ass and not os.path.isfile(self.ass_path(item, i)) and not self._failed(item, i))]
        # PGS (Blu-ray): original .sup stream, rendered in the browser rather than burnt in
        need += [i for i in pgs_tracks(probe) if not os.path.isfile(self.sup_path(item, i))
                 and not os.path.isfile(self.path(item, i))]
        if ass and self.fonts_index(item) is None:
            need = sorted(set(need) | set(ass))
        return need

    def _failed(self, item, index):
        try:
            with open(self.path(item, index), encoding="utf-8") as f:
                return bool(json.load(f).get("error"))
        except (OSError, ValueError):
            return False

    # -- preview thumbnails
    def trick_dir(self, item):
        return self._keyed(self.trick_base, item)

    def trick_index(self, item):
        try:
            with open(os.path.join(self.trick_dir(item), "index.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def needs_trick(self, item):
        if item.get("peer_id") or self.ctx.db.setting("trickplay", "1") != "1" or media.is_strm(item["key"]):
            return False
        probe = json.loads(item["probe"] or "{}") if isinstance(item["probe"], str) else item["probe"]
        if not (probe or {}).get("video") or (probe.get("duration") or 0) < 60:
            return False
        return self.trick_index(item) is None

    def has_work(self, item):
        return bool(self.missing(item)) or self.needs_trick(item)

    def ready(self, item):
        """Are all the internal text tracks extracted?"""
        return not self.missing(item)

    def read(self, item, index):
        try:
            with open(self.path(item, index), "rb") as f:
                return f.read()
        except OSError:
            return None

    def read_sup(self, item, index):
        try:
            with open(self.sup_path(item, index), "rb") as f:
                return f.read()
        except OSError:
            return None

    def read_ass(self, item, index):
        try:
            with open(self.ass_path(item, index), "rb") as f:
                return f.read()
        except OSError:
            return None

    def status(self, item):
        with self.lock:
            job = self.jobs.get(item["id"])
        if job and not job.done.is_set():
            return {"state": "running", "progress": round(job.progress, 3)}
        if job and job.error:
            return {"state": "error", "error": job.error}
        if self.missing(item):
            queued = item["id"] in self.urgent or item["id"] in self.idle
            return {"state": "queued" if queued else "missing", "progress": 0}
        return {"state": "ready", "progress": 1}

    # ------------------------------------------------------------ demandes
    def ensure_now(self, item):
        """Priority extraction (a playback is starting). Returns the Job (or None when everything is ready)."""
        if not self.missing(item):
            return None
        with self.lock:
            job = self.jobs.get(item["id"])
            if job and not job.done.is_set():
                if job.kind not in ("idle", "gentle"):
                    return job
                job.aborted = True  # a background job: replaced by a priority extraction
            job = Job(item["id"], "play")
            self.jobs[item["id"]] = job
            self.urgent.pop(item["id"], None)
            self.idle.pop(item["id"], None)
        row = dict(item)
        threading.Thread(target=self._run_slot, args=(row, job), daemon=True).start()
        return job

    def enqueue(self, item_id, urgent=False):
        with self.lock:
            job = self.jobs.get(item_id)
            if job and not job.done.is_set():
                if not (urgent and job.kind in ("idle", "gentle")):
                    return
                job.aborted = True  # started again with priority
            if urgent:
                self.idle.pop(item_id, None)
                self.urgent[item_id] = True
                self.urgent.move_to_end(item_id)
            elif item_id not in self.urgent:
                self.idle[item_id] = True
        self.wake.set()

    def wait(self, job, timeout):
        return job.done.wait(timeout)

    # ------------------------------------------------------------ lecture progressive
    def running(self, item):
        with self.lock:
            job = self.jobs.get(item["id"])
        return job if job and not job.done.is_set() else None

    def partial(self, item, index, ext):
        """During the extraction: (data already extracted, up to which time in ms) or None."""
        job = self.running(item)
        if not job or not job.tmp:
            return None
        try:
            with open(os.path.join(job.tmp, f"{int(index)}.{ext}"), "rb") as f:
                data = f.read()
        except OSError:
            return None
        if ext == "srt":
            data = data[:data.rfind(b"\n\n") + 2] if b"\n\n" in data else b""
        elif ext == "ass":
            data = data[:data.rfind(b"\n") + 1]
        elif ext == "sup":
            data = _complete_sup(data)
        return data, job.until_ms

    # ------------------------------------------------------------ travail
    def _streaming(self):
        try:
            return bool(self.ctx.db.scalar("SELECT COUNT(*) FROM streams WHERE last_seen > ?", (int(time.time()) - 90,)))
        except Exception:
            return False

    def _next(self):
        with self.lock:
            if self.urgent:
                return self.urgent.popitem(last=False)[0], "soon"
        streaming = self._streaming()
        with self.lock:
            if self.idle:
                # during a playback, the background queue still moves on, gently and one file at a time
                if streaming and any(j.kind == "gentle" and not j.done.is_set() for j in self.jobs.values()):
                    return None, None
                return self.idle.popitem(last=False)[0], ("gentle" if streaming else "idle")
        return None, None

    def _worker(self):
        while not self.stopped.is_set():
            self.wake.wait(10)
            self.wake.clear()
            while not self.stopped.is_set():
                item_id, kind = self._next()
                if item_id is None:
                    break
                try:
                    row = self.ctx.db.one("SELECT id, key, probe, file_size, file_mtime, duration_ms FROM items WHERE id=?", (item_id,))
                    if not row:
                        continue
                    todo = self.missing(row) if kind in ("soon", "gentle") else self.has_work(row)
                    if not todo:
                        if kind == "soon" and self.needs_trick(row):
                            self.enqueue(item_id)
                        if kind == "gentle" and self.needs_trick(row):
                            with self.lock:  # thumbnails only: wait until nobody is watching
                                self.idle[item_id] = True
                            break
                        continue
                finally:
                    self.ctx.db.release()
                prev = self.jobs.get(item_id)
                if prev and prev.aborted:
                    prev.done.wait(10)  # background job interrupted to be started again with priority
                with self.lock:
                    if item_id in self.jobs and not self.jobs[item_id].done.is_set():
                        continue
                    job = Job(item_id, kind)
                    self.jobs[item_id] = job
                self._run_slot(row, job)

    def _run_slot(self, item, job):
        # a waiting playback never queues behind background jobs
        slot = contextlib.nullcontext() if job.kind == "play" else self.slots
        aborted = False
        with slot:
            try:
                self._extract(item, job)
            except Aborted:
                aborted = True
                log.debug("Préparation de %s interrompue (lecture en cours)", item.get("key"))
            except Exception as e:
                log.warning("Sous-titres de %s : %s", item.get("key"), e)
                job.error = str(e)
            finally:
                job.progress = 1.0 if not job.error else job.progress
                with self.lock:
                    # jobs that ended in an error are kept for a while to show it, not the others
                    if not job.error and self.jobs.get(item["id"]) is job:
                        self.jobs.pop(item["id"], None)
                job.done.set()
        try:
            if aborted and job.kind == "idle" and not job.aborted:
                self.enqueue(item["id"])  # reprendra quand plus personne ne regarde
            elif job.kind != "idle" and not job.error and self.needs_trick(item):
                self.enqueue(item["id"])  # thumbnails later, in the background
        finally:
            self.ctx.db.release()

    def _extract(self, item, job):
        tracks = self.missing(item)
        trick = job.kind == "idle" and self.needs_trick(item)
        if not tracks and not trick:
            return
        if not item["key"] or not os.path.isfile(item["key"]):
            raise media.MediaError("fichier introuvable")
        tmp = tempfile.mkdtemp(dir=self.base, prefix=".tmp-")
        job.tmp = tmp
        try:
            try:
                self._ffmpeg(item, tracks, trick, tmp, job)  # a single read of the file for everything
            except media.MediaError as e:
                log.info("Passe combinée en échec (%s), reprise séparée : %s", e, item["key"])
                if trick:
                    try:
                        self._ffmpeg(item, [], True, tmp, job)
                    except media.MediaError as e2:
                        log.warning("Vignettes de %s : %s", item["key"], e2)
                for i in tracks:
                    try:
                        self._ffmpeg(item, [i], False, tmp, job, fonts=False)
                    except media.MediaError as e2:
                        log.warning("Piste %s de %s : %s", i, item["key"], e2)
            if tracks:
                os.makedirs(self._dir(item), exist_ok=True)
            still = []
            pgs = set(pgs_tracks(_probe(item)))
            for i in tracks:
                if i in pgs:
                    sup = os.path.join(tmp, f"{i}.sup")
                    if os.path.isfile(sup) and os.path.getsize(sup) > 0:
                        os.replace(sup, self.sup_path(item, i))
                    else:
                        still.append(i)
                        with open(self.path(item, i), "w", encoding="utf-8") as f:
                            json.dump({"cues": [], "error": "piste illisible"}, f)
                    continue
                src = os.path.join(tmp, f"{i}.srt")
                dest = self.path(item, i)
                if os.path.isfile(src):
                    with open(src, "rb") as f:
                        data = {"cues": parse_subtitles(f.read())}
                else:
                    # unreadable track: noted so the whole file is not read again at each playback
                    still.append(i)
                    data = {"cues": [], "error": "piste illisible"}
                with open(dest + ".tmp", "wb") as f:
                    f.write(json.dumps(data, ensure_ascii=False).encode())
                os.replace(dest + ".tmp", dest)
                raw_ass = os.path.join(tmp, f"{i}.ass")
                if os.path.isfile(raw_ass) and os.path.getsize(raw_ass) > 0:
                    os.replace(raw_ass, self.ass_path(item, i))
            if self._want_fonts(item):
                self._store_fonts(item, tmp)
            if trick:
                self._store_trick(item, tmp)
            if still:
                raise media.MediaError(f"{len(still)} piste(s) de sous-titres illisible(s)")
        finally:
            job.tmp = None
            shutil.rmtree(tmp, ignore_errors=True)

    def _want_fonts(self, item):
        return bool(ass_tracks(_probe(item))) and self.fonts_index(item) is None

    def _attachments(self, item):
        probe = _probe(item)
        if "attachments" in probe:
            return probe["attachments"]
        try:  # media scanned before fonts were supported
            return media.attachments(item["key"])
        except media.MediaError:
            return []

    def _store_fonts(self, item, tmp):
        dest = os.path.join(self._dir(item), "fonts")
        os.makedirs(dest, exist_ok=True)
        index = []
        for f in sorted(os.listdir(tmp)):
            if not f.startswith("font_"):
                continue
            src = os.path.join(tmp, f)
            try:
                with open(src, "rb") as fh:
                    names = font_names(fh.read(64 * 1024 * 1024))
            except OSError:
                continue
            if not names:
                continue
            os.replace(src, os.path.join(dest, f))
            index.append({"file": f, "names": sorted(names), "size": os.path.getsize(os.path.join(dest, f))})
        with open(os.path.join(self._dir(item), "fonts.json"), "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False)

    def _trick_size(self, item):
        probe = json.loads(item["probe"] or "{}") if isinstance(item["probe"], str) else item["probe"]
        v = (probe or {}).get("video") or {}
        w, h = v.get("width") or 16, v.get("height") or 9
        return TRICK_W, max(2, int(round(TRICK_W * h / w / 2)) * 2)

    def _store_trick(self, item, tmp):
        sheets = sorted(f for f in os.listdir(tmp) if f.startswith("t") and f.endswith(".jpg"))
        if not sheets:
            return
        dest = self.trick_dir(item)
        os.makedirs(dest, exist_ok=True)
        for n, f in enumerate(sheets, 1):
            os.replace(os.path.join(tmp, f), os.path.join(dest, f"{n}.jpg"))
        w, h = self._trick_size(item)
        dur = (item["duration_ms"] or 0) / 1000
        index = {"interval": TRICK_INTERVAL, "w": w, "h": h, "cols": TRICK_COLS, "rows": TRICK_ROWS,
                 "count": int(math.ceil(dur / TRICK_INTERVAL)) if dur else len(sheets) * TRICK_COLS * TRICK_ROWS,
                 "sheets": len(sheets)}
        with open(os.path.join(dest, "index.json"), "w", encoding="utf-8") as f:
            json.dump(index, f)

    def _ffmpeg(self, item, tracks, trick, tmp, job, fonts=True):
        cmd = [media.FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y"]
        if trick:
            cmd += ["-skip_frame", "nokey"]  # only keyframes are decoded: very little computing
        if job.kind in ("soon", "gentle") and self._streaming() and media.readrate_ok():
            # leaves bandwidth to the current stream
            cmd += ["-readrate", str(SOON_READRATE if job.kind == "soon" else GENTLE_READRATE)]
        if fonts and tracks and self._want_fonts(item):
            for a in self._attachments(item)[:200]:
                cmd += [f"-dump_attachment:{int(a['index'])}", os.path.join(tmp, f"font_{int(a['index'])}{a['ext']}")]
        cmd += [*media.LOCAL_ONLY, "-analyzeduration", "200M", "-probesize", "1G", "-i", item["key"]]
        probe = _probe(item)
        if not trick and tracks:
            # clock: an audio track copied to nowhere (no decoding) shows precisely
            # how far the file has been read, even during a long stretch without subtitles
            clock = "0:a:0" if probe.get("audio") else "0:v:0"
            cmd += ["-map", clock, "-c", "copy", "-f", "null", "-"]
        if trick:
            w, h = self._trick_size(item)
            cmd += ["-map", "0:v:0", "-an", "-sn",
                    "-vf", f"fps=1/{TRICK_INTERVAL},scale={w}:{h},tile={TRICK_COLS}x{TRICK_ROWS}",
                    "-q:v", "6", "-f", "image2", os.path.join(tmp, "t%04d.jpg")]
        ass = set(ass_tracks(_probe(item)))
        pgs = set(pgs_tracks(_probe(item)))
        for i in tracks:
            # -flush_packets: each line is written right away (progressive playback)
            if i in pgs:
                cmd += ["-map", f"0:{i}", "-c:s", "copy", "-flush_packets", "1", "-f", "sup",
                        os.path.join(tmp, f"{i}.sup")]
                continue
            cmd += ["-map", f"0:{i}", "-c:s", "srt", "-flush_packets", "1", "-f", "srt", os.path.join(tmp, f"{i}.srt")]
            if i in ass:  # the original ASS script, for a faithful rendering (positions, fonts, karaoke)
                cmd += ["-map", f"0:{i}", "-c:s", "copy", "-flush_packets", "1", "-f", "ass",
                        os.path.join(tmp, f"{i}.ass")]
        cmd += ["-progress", "pipe:1", "-nostats"]
        duration = (item["duration_ms"] or 0) * 1000  # in microseconds
        nice = 0 if job.kind == "play" else 10
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             preexec_fn=lambda: os.nice(nice))
        err = []
        t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
        t.start()
        checked = time.time()
        fonts_stored = False
        for line in p.stdout:
            if job.kind in ("idle", "gentle") and (job.aborted or time.time() - checked > 3):
                checked = time.time()
                if job.aborted or (job.kind == "idle" and self._streaming()):
                    p.kill()
                    p.wait()
                    t.join(5)
                    p.stdout.close()
                    p.stderr.close()
                    raise Aborted()
            if line.startswith(b"out_time_us=") or line.startswith(b"out_time_ms="):
                try:
                    us = int(line.split(b"=", 1)[1])
                except ValueError:
                    continue
                job.until_ms = max(job.until_ms, us // 1000 - PARTIAL_MARGIN_MS)
                if fonts and not fonts_stored and tracks and self._want_fonts(item):
                    # attached fonts are written as soon as the file is opened: available before the extraction ends
                    fonts_stored = True
                    try:
                        self._store_fonts(item, tmp)
                    except OSError as e:
                        log.debug("Polices de %s : %s", item.get("key"), e)
                if duration > 0:
                    job.progress = max(job.progress, min(0.99, us / duration))
        p.wait()
        t.join(5)
        p.stdout.close()
        p.stderr.close()
        if p.returncode != 0:
            msg = (err[0] if err else b"").decode("utf-8", "replace").strip().splitlines()
            raise media.MediaError(msg[-1] if msg else "échec de l'extraction")

    def stop(self):
        self.stopped.set()
        self.wake.set()
