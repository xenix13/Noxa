"""Downloads to watch offline (like Netflix).

Noxa prepares an MP4 the device can play, at the chosen quality, with the wanted audio track (and the
picture subtitles burnt in when asked for). The app then fetches it in chunks and keeps it
in the browser's storage. An MP4 already playable as is is sent directly, without preparation.

Prepared files are temporary: erased as soon as the device has finished fetching them, otherwise after
48 h. shortcut: the queue lives in memory; after a restart, the app asks for the
preparation again (it resumes by itself).
"""
import logging
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections import deque

from . import media
from .i18n import tr
from .transcoder import hw_input, media_args

log = logging.getLogger("noxa.downloads")

KEEP = 48 * 3600
MAX_PER_USER = 40
SPARE_DISK = 2 * 1024 ** 3   # 2 GB are always kept free on the server's disk
CPUS = os.cpu_count() or 2
COPY_READRATE = 20          # discreet priority: reading from the share capped at 20x real time
IONICE = shutil.which("ionice")
# Priority of the preparations (Settings → Offline viewing). A conversion costs ~30 s of processor per
# minute of 1080p video, a copy ~2 s.
#  - discreet: as little processor and disk as possible, paused as soon as someone watches (former setting);
#  - normal: full speed when nobody watches; during a playback, low priority, and a conversion
#    only stops when another conversion (a viewer's) needs the processor;
#  - fast: the whole processor, never paused.
PRIORITIES = {
    "low": {"threads": max(1, min(4, CPUS // 4)), "nice": 19, "ionice": ["-c", "3"], "readrate": True, "workers": 1},
    "normal": {"threads": max(2, min(8, CPUS // 2)), "nice": 10, "ionice": ["-c", "2", "-n", "7"], "readrate": False,
               "workers": 2 if CPUS >= 8 else 1},
    "high": {"threads": max(2, min(16, CPUS)), "nice": 0, "ionice": [], "readrate": False, "workers": 2},
}


class DownloadError(Exception):
    pass


class Job:
    def __init__(self, uid, item_id, plan, path, duration, progressive=False):
        self.id = secrets.token_urlsafe(9)
        self.uid = uid
        self.item_id = item_id
        self.plan = plan
        self.source = path
        self.duration = duration or 0     # secondes
        self.mode = "original" if plan["mode"] == "direct" else plan["mode"]
        self.state = "ready" if self.mode == "original" else "queued"
        self.progress = 1.0 if self.mode == "original" else 0.0
        self.path = path if self.mode == "original" else None
        self.size = os.path.getsize(path) if self.mode == "original" else 0
        self.error = None
        self.created = self.touched = time.time()
        self.proc = None
        self.waiting = False      # paused: someone is watching, they go first
        # transfer during the preparation: fragmented MP4, written in order; the device fetches what is already
        # written (ready bytes). gen changes at each new try (graphics card falling back to the processor)
        self.progressive = bool(progressive) and self.mode != "original"
        self.ready = self.size
        self.gen = 0
        self.part = None

    def view(self):
        return {"id": self.id, "item_id": str(self.item_id), "state": self.state, "mode": self.mode,
                "progress": round(self.progress, 3), "size": self.size, "error": tr(self.error), "waiting": self.waiting,
                "progressive": self.progressive, "ready": self.ready, "gen": self.gen}


class Downloads:
    def __init__(self, ctx):
        self.ctx = ctx
        self.base = os.path.join(ctx.data_dir, "downloads")
        shutil.rmtree(self.base, ignore_errors=True)  # preparations from before the restart: asked for again
        os.makedirs(self.base, exist_ok=True)
        self.jobs = {}
        self.queue = deque()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.workers = []

    # ------------------------------------------------------------ demandes
    def request(self, uid, row, plan, duration, progressive=False):
        with self.lock:
            mine = [j for j in self.jobs.values() if j.uid == uid and j.state in ("queued", "preparing")]
            if len(mine) >= MAX_PER_USER:
                raise DownloadError(f"Déjà {MAX_PER_USER} téléchargements en préparation : réessayez une fois ceux-ci prêts")
            job = Job(uid, row["id"], plan, row["key"], duration, progressive)
            self.jobs[job.id] = job
            if job.state == "queued":
                self.queue.append(job.id)
                # simultaneous preparations: depending on the chosen priority and the number of cores
                self.workers = [w for w in self.workers if w.is_alive()]
                while len(self.workers) < self.settings()["workers"]:
                    w = threading.Thread(target=self._loop, daemon=True, name="downloads")
                    self.workers.append(w)
                    w.start()
                self.wake.set()
            return job

    def priority(self):
        try:
            v = self.ctx.db.setting("downloads_priority", "normal")
        finally:
            self.ctx.db.release()
        return v if v in PRIORITIES else "normal"

    def settings(self):
        return PRIORITIES[self.priority()]

    def get(self, jid, uid):
        with self.lock:
            job = self.jobs.get(jid)
        if not job or job.uid != uid:
            return None
        job.touched = time.time()
        return job

    def delete(self, jid, uid):
        with self.lock:
            job = self.jobs.get(jid)
            if not job or job.uid != uid:
                return False
            self.jobs.pop(jid, None)
        self._drop(job)
        return True

    def _drop(self, job):
        job.state = "cancelled"
        if job.proc and job.proc.poll() is None:
            job.proc.kill()
        if job.mode != "original":
            shutil.rmtree(os.path.join(self.base, job.id), ignore_errors=True)

    def reap(self):
        now = time.time()
        with self.lock:
            old = [j for j in self.jobs.values() if now - j.touched > KEEP]
            for j in old:
                self.jobs.pop(j.id, None)
        for j in old:
            self._drop(j)

    # ------------------------------------------------------------ preparation
    def _loop(self):
        while True:
            self.wake.wait(30)
            while True:
                with self.lock:
                    jid = self.queue.popleft() if self.queue else None
                    job = self.jobs.get(jid) if jid else None
                if not jid:
                    self.wake.clear()
                    break
                if job and job.state == "queued":
                    self._prepare(job)

    def _estimate(self, job):
        p = job.plan
        if p["mode"] == "copy":
            return os.path.getsize(job.source) if not media.is_remote(job.source) else 0
        return int((p["kbps"] + 192) * 1000 / 8 * job.duration)

    def _prepare(self, job):
        out_dir = os.path.join(self.base, job.id)
        os.makedirs(out_dir, exist_ok=True)
        if shutil.disk_usage(self.base).free < self._estimate(job) + SPARE_DISK:
            job.state, job.error = "error", "Espace disque insuffisant sur le serveur pour préparer ce téléchargement"
            return
        hw = self.ctx.hw() if job.plan["mode"] == "transcode" else None
        for attempt_hw in ([hw, None] if hw else [None]):  # graphics card, otherwise falling back to the processor
            if job.state == "cancelled":
                return
            job.state, job.progress, job.ready = "preparing", 0.0, 0
            job.gen += 1  # the device starts again from zero if it had started fetching the previous try
            if self._run(job, out_dir, attempt_hw):
                return
        if job.state != "cancelled":
            job.state = "error"

    def _run(self, job, out_dir, hw):
        part, final = os.path.join(out_dir, "video.part.mp4"), os.path.join(out_dir, "video.mp4")
        copy = job.plan["mode"] == "copy"
        prio = self.priority()
        conf = PRIORITIES[prio]
        threads = str(conf["threads"])
        cmd = [*([IONICE, *conf["ionice"]] if IONICE and conf["ionice"] else []),  # share of the disk left to the others
               media.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *(hw_input(hw) if hw else []),
               "-threads", threads,
               *(["-readrate", str(COPY_READRATE)] if copy and conf["readrate"] and media.readrate_ok() else []),
               *media.input_args(job.source), "-map_metadata", "-1", "-map_chapters", "-1",
               "-threads", threads, "-filter_threads", "1",
               *media_args(job.plan, hw), "-sn", "-dn", "-max_muxing_queue_size", "2048", "-nostats"]
        progress_r = progress_w = None
        if job.progressive:
            # fragmented MP4 written in one go to the standard output: nothing is rewritten afterwards, the device can
            # fetch each part as soon as it is written; the progress goes through another pipe
            progress_r, progress_w = os.pipe()
            cmd += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-progress", f"pipe:{progress_w}",
                    "-f", "mp4", "pipe:1"]
        else:
            cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-f", "mp4", part]
        log.info("Préparation du téléchargement %s (%s%s, priorité %s)", job.id, job.mode, f", {hw['engine']}" if hw else "", prio)
        log_path = os.path.join(out_dir, "ffmpeg.log")
        try:
            with open(log_path, "wb") as log_file:
                nice = conf["nice"]
                job.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log_file,
                                            preexec_fn=(lambda: os.nice(nice)) if nice else None,
                                            pass_fds=(progress_w,) if job.progressive else ())
        except OSError as e:
            job.error = f"ffmpeg introuvable ({e})"
            if progress_r is not None:
                os.close(progress_r)
            return False
        finally:
            if progress_w is not None:
                os.close(progress_w)  # ffmpeg garde sa copie
        threading.Thread(target=self._yield_to_viewers, args=(job, job.proc, prio, conf["nice"]), daemon=True).start()
        def read_progress(stream):
            for line in stream:
                if line.startswith(b"out_time_us=") and job.duration:
                    try:
                        job.progress = min(0.99, int(line[12:]) / 1e6 / job.duration)
                    except ValueError:
                        pass
        if job.progressive:
            job.part = part
            def follow():
                with os.fdopen(progress_r, "rb") as stream:
                    read_progress(stream)
            reader = threading.Thread(target=follow, daemon=True)
            reader.start()
            with job.proc.stdout, open(part, "wb") as out:
                while True:
                    block = job.proc.stdout.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
                    out.flush()
                    job.ready += len(block)  # written and flushed: readable by the device
            reader.join(5)
        else:
            with job.proc.stdout:
                read_progress(job.proc.stdout)
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                err = f.read().strip().splitlines()
        except OSError:
            err = []
        job.waiting = False
        if job.proc.wait() != 0 or not os.path.isfile(part):
            job.error = "Préparation impossible : " + (err[-1] if err else "ffmpeg s'est arrêté")
            log.warning("Téléchargement %s : %s", job.id, job.error)
            return False
        os.replace(part, final)
        job.path, job.size, job.progress, job.state, job.error = final, os.path.getsize(final), 1.0, "ready", None
        job.ready = job.size
        return True

    def _yield_to_viewers(self, job, proc, prio, nice):
        """Makes room for viewers: depending on the priority, paused or low priority during a playback."""
        lowered = False
        while proc.poll() is None:
            if prio == "high":
                return
            viewing = self.ctx.viewing()
            if prio == "low":
                pause = viewing  # discreet: paused as soon as someone watches
            else:
                # normal: a conversion gives way to a viewer's; a copy goes on, slowed down
                pause = job.mode == "transcode" and viewing and self.ctx.transcoder.counts()[1] > 0
                if viewing != lowered:
                    try:
                        os.setpriority(os.PRIO_PROCESS, proc.pid, 19 if viewing else nice)
                        lowered = viewing
                    except (OSError, PermissionError):
                        pass  # raising the priority again needs rights: it stays low until the end
            if pause != job.waiting:
                try:
                    os.kill(proc.pid, signal.SIGSTOP if pause else signal.SIGCONT)
                    job.waiting = pause
                except OSError:
                    return
            time.sleep(3)
