#!/usr/bin/env python3
"""Noxa / Jellyfin / Plex benchmark: same machine, same test library, same limits, one server at a time.

Run through run.sh (it installs what is missing). Everything here is Python standard library plus the docker CLI.

For each server, in turn:
  1. start it in a container (--cpus / --memory limits, read-only test library, host network);
  2. set it up through its own API (admin account, one movie and one show library, online metadata off);
  3. scan: time until every title is listed, with the CPU / memory peak;
  4. wait until its background work is over (CPU under 3 % for 30 s, at most 15 minutes);
  5. idle: CPU and memory for a few minutes;
  6. response time of two equivalent requests (library page of 60 movies, one movie's details);
  7. direct play: 3 clients reading 3 different 8 Mbit/s files at playback speed;
  8. one software conversion, HEVC 10-bit 1080p -> H.264 720p: CPU, memory and conversion speed;
  9. stop it and remove its container.

CPU and memory come from the Docker stats API once a second (memory without page cache, like `docker stats`).
Results: results/<date>/summary.md (to share), results.json, and one CSV of raw samples per server.
"""
import argparse
import datetime
import http.client
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGES = {
    "noxa": "ghcr.io/xenix13/noxa:4.1.4",
    "jellyfin": "jellyfin/jellyfin:10.10.7",
    "plex": "plexinc/pms-docker:1.43.5.11079-b3b13e506",
}
PORTS = {"noxa": 8420, "jellyfin": 8096, "plex": 32400}
FFMPEG_IMAGE = "jellyfin/jellyfin:10.10.7"  # its ffmpeg builds the test library: nothing to install on the host
FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
USER, PASSWORD = "bench", "bench-pass-2026"
MOVIES, SHOWS, SEASONS, EPISODES = 30, 3, 2, 8
SOURCE_SECONDS = 90
EPISODE_SECONDS = 30

NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Failed(Exception):
    pass


# ---------------------------------------------------------------------------------------------- HTTP helpers
def call(method, url, body=None, headers=None, timeout=60, raw=False):
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with NO_PROXY.open(req, timeout=timeout) as r:
            content = r.read()
            if raw:
                return r.status, content, dict(r.headers)
            text = content.decode("utf-8", "replace")
            try:
                return r.status, json.loads(text) if text.strip() else None
            except ValueError:
                return r.status, text
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        if raw:
            return e.code, text.encode(), dict(e.headers)
        return e.code, text


def wait_http(url, timeout=300, ok=(200,)):
    end = time.time() + timeout
    while time.time() < end:
        try:
            st = call("GET", url, timeout=5)[0]
            if st in ok:
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(2)
    raise Failed(f"{url} did not answer within {timeout} s")


# ---------------------------------------------------------------------------------------------- Docker stats
class DockerSocket(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=30)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect("/var/run/docker.sock")


class Sampler(threading.Thread):
    """Reads the container's stats stream: one sample a second (CPU in % of one core, memory without cache)."""

    def __init__(self, container):
        super().__init__(daemon=True)
        self.container, self.samples, self.stop_flag = container, [], False

    def run(self):
        try:
            c = DockerSocket()
            c.request("GET", f"/containers/{self.container}/stats?stream=true")
            r = c.getresponse()
            while not self.stop_flag:
                line = r.readline()
                if not line:
                    break
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                cpu, pre = s.get("cpu_stats") or {}, s.get("precpu_stats") or {}
                cd = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (pre.get("cpu_usage") or {}).get("total_usage", 0)
                sd = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
                n = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
                pct = cd / sd * n * 100 if sd > 0 and cd >= 0 else 0.0
                m = s.get("memory_stats") or {}
                st = m.get("stats") or {}
                mem = m.get("usage", 0) - st.get("inactive_file", st.get("total_inactive_file", 0))
                self.samples.append((time.time(), pct, max(0, mem)))
        except Exception as e:  # the container went away: sampling simply stops
            log(f"  (stats stopped: {e})")

    def window(self, t0, t1):
        return [(c, m) for t, c, m in self.samples if t0 <= t <= t1]

    def summary(self, t0, t1):
        w = self.window(t0, t1)
        if not w:
            return {}
        cpu = [c for c, _ in w]
        mem = [m for _, m in w]
        return {"cpu_avg": round(statistics.mean(cpu), 1), "cpu_max": round(max(cpu), 1),
                "mem_avg_mb": round(statistics.mean(mem) / 2**20), "mem_max_mb": round(max(mem) / 2**20),
                "seconds": round(t1 - t0)}


def docker(*args, check=True, capture=True):
    r = subprocess.run(["docker", *args], capture_output=capture, text=True)
    if check and r.returncode:
        raise Failed(f"docker {' '.join(args[:3])}: {(r.stderr or '').strip()[-400:]}")
    return (r.stdout or "").strip()


# ---------------------------------------------------------------------------------------------- test library
def build_library(media, quick):
    """Three sources (ffmpeg test pattern), copied as many times as needed: 30 movies, 3 shows of 2x8 episodes.
    Real copies, not hard links: Plex counts hard links to one file as a single title."""
    done = os.path.join(media, ".complete")
    if os.path.exists(done):
        log("Test library already built")
        return
    log("Building the test library (a few minutes; only once)")
    shutil.rmtree(media, ignore_errors=True)
    src = os.path.join(media, "sources")
    os.makedirs(src, exist_ok=True)
    secs = 20 if quick else SOURCE_SECONDS
    v = ["-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=24", "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000"]
    jobs = {
        # 1080p H.264 8 Mbit/s + AAC stereo: direct play for every server
        "h264.mkv": v + ["-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-b:v", "8M",
                         "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2"],
        # 1080p HEVC 10-bit + AC3 5.1: what a browser cannot play, so it gets converted
        "hevc10.mkv": v + ["-map", "0:v", "-map", "1:a", "-c:v", "libx265", "-preset", "ultrafast", "-b:v", "6M",
                           "-pix_fmt", "yuv420p10le", "-x265-params", "log-level=error", "-c:a", "ac3", "-ac", "6"],
        # 720p H.264, two audio tracks and a subtitle track
        "multi.mkv": ["-f", "lavfi", "-i", "testsrc2=s=1280x720:r=24", "-f", "lavfi", "-i", "sine=f=330",
                      "-f", "lavfi", "-i", "sine=f=660", "-i", "/media/sources/sub.srt",
                      "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s", "-c:v", "libx264", "-preset", "veryfast",
                      "-b:v", "3M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "srt",
                      "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=fre", "-metadata:s:s:0", "language=eng"],
    }
    with open(os.path.join(src, "sub.srt"), "w") as f:
        for i in range(0, secs, 4):
            f.write(f"{i // 4 + 1}\n00:{i // 60:02d}:{i % 60:02d},000 --> 00:{i // 60:02d}:{i % 60:02d},900\nLine {i // 4 + 1}\n\n")
    for name, args in jobs.items():
        log(f"  encoding {name}")
        docker("run", "--rm", "--entrypoint", FFMPEG, "-v", f"{media}:/media", FFMPEG_IMAGE,
               "-loglevel", "error", "-y", *args, "-t", str(secs), f"/media/sources/{name}")
    log("  episode source")
    docker("run", "--rm", "--entrypoint", FFMPEG, "-v", f"{media}:/media", FFMPEG_IMAGE, "-loglevel", "error", "-y",
           "-i", "/media/sources/multi.mkv", "-map", "0", "-c", "copy", "-t", str(min(secs, EPISODE_SECONDS)),
           "/media/sources/episode.mkv")
    cycle = ["h264.mkv", "hevc10.mkv", "multi.mkv"]
    for n in range(1, MOVIES + 1):
        title = f"Bench Movie {n:02d} ({1990 + n})"
        d = os.path.join(media, "movies", title)
        os.makedirs(d, exist_ok=True)
        shutil.copyfile(os.path.join(src, cycle[(n - 1) % 3]), os.path.join(d, title + ".mkv"))
    for s in range(1, SHOWS + 1):
        for season in range(1, SEASONS + 1):
            d = os.path.join(media, "shows", f"Bench Show {s} (201{s})", f"Season {season:02d}")
            os.makedirs(d, exist_ok=True)
            for e in range(1, EPISODES + 1):
                shutil.copyfile(os.path.join(src, "episode.mkv"), os.path.join(d, f"Bench Show {s} - S{season:02d}E{e:02d}.mkv"))
    os.chmod(media, 0o755)
    for root, dirs, files in os.walk(media):
        for x in dirs:
            os.chmod(os.path.join(root, x), 0o755)
        for x in files:
            os.chmod(os.path.join(root, x), 0o644)
    open(done, "w").close()


def age_library(media):
    """Dates every file of the library yesterday, like an existing library. Noxa skips files changed in the last
    45 s (still being copied by Radarr / Sonarr): right after the library was built, its scan looked slow."""
    day_ago = time.time() - 86400
    for root, _, files in os.walk(media):
        for x in files:
            os.utime(os.path.join(root, x), (day_ago, day_ago))


MOVIE_FILE = {"h264": "Bench Movie 01 (1991)", "hevc": "Bench Movie 02 (1992)"}
DIRECT_TITLES = ["Bench Movie 01 (1991)", "Bench Movie 04 (1994)", "Bench Movie 07 (1997)"]  # the 8 Mbit/s H.264 copies


# ---------------------------------------------------------------------------------------------- servers
class Server:
    name = ""

    def __init__(self, work, media, cpus, memory):
        self.work, self.media, self.cpus, self.memory = work, media, cpus, memory
        self.data = os.path.join(work, "data", self.name)
        self.base = f"http://127.0.0.1:{PORTS[self.name]}"
        self.container = f"bench-{self.name}"

    def run_args(self):
        return []

    def start(self):
        docker("rm", "-f", self.container, check=False)
        shutil.rmtree(self.data, ignore_errors=True)
        os.makedirs(self.data)
        docker("run", "-d", "--name", self.container, "--network", "host", f"--cpus={self.cpus}",
               f"--memory={self.memory}", "-v", f"{self.media}/movies:/media/movies:ro",
               "-v", f"{self.media}/shows:/media/shows:ro", *self.run_args(), IMAGES[self.name])

    def stop(self):
        docker("rm", "-f", self.container, check=False)


class Noxa(Server):
    name = "noxa"

    def run_args(self):
        os.chown(self.data, 1000, 1000)
        return ["-v", f"{self.data}:/data"]

    def h(self):
        return {"Cookie": self.cookie, "X-Seance": "1"}  # X-Seance: Noxa's CSRF guard for anything but GET

    def setup(self):
        wait_http(self.base + "/api/version", ok=(200, 401, 404))
        code_file = os.path.join(self.data, "setup-code.txt")
        for _ in range(60):
            if os.path.exists(code_file):
                break
            time.sleep(1)
        code = open(code_file).read().strip()
        req = urllib.request.Request(self.base + "/api/setup", method="POST",
                                     data=json.dumps({"username": USER, "password": PASSWORD, "language": "en"}).encode(),
                                     headers={"Content-Type": "application/json", "X-Setup-Code": code, "X-Seance": "1"})
        with NO_PROXY.open(req, timeout=30) as r:
            self.cookie = "; ".join(c.split(";")[0] for c in r.headers.get_all("Set-Cookie") or [])
        # same as the others: no preview thumbnails (Plex makes them at night, Jellyfin not by default)
        st, _ = call("PUT", self.base + "/api/admin/settings", {"trickplay": False}, self.h())
        if st != 200:
            raise Failed(f"settings: {st}")

    def add_libraries(self):
        for name, kind, path in (("Movies", "movie", "/media/movies"), ("Shows", "show", "/media/shows")):
            st, r = call("POST", self.base + "/api/admin/libraries", {"name": name, "kind": kind, "paths": [path]}, self.h())
            if st != 200:
                raise Failed(f"library {name}: {st} {r}")
        call("POST", self.base + "/api/admin/scan", {}, self.h())

    def scanned(self):
        st, r = call("GET", self.base + "/api/admin/scan", headers=self.h())
        if st != 200 or r.get("running"):
            return False
        st, r = call("GET", self.base + "/api/admin/libraries", headers=self.h())
        counts = {l["name"]: l.get("counts", {}) for l in r["libraries"]}
        done = counts.get("Movies", {}).get("movie") == MOVIES and \
            counts.get("Shows", {}).get("episode") == SHOWS * SEASONS * EPISODES
        if not done:
            # up to 4.1.x a library added during another scan waited for the next scan: ask for it, like a user would
            call("POST", self.base + "/api/admin/scan", {}, self.h())
        return done

    def _ids(self):
        st, r = call("GET", self.base + "/api/admin/libraries", headers=self.h())
        lib = next(l["id"] for l in r["libraries"] if l["name"] == "Movies")
        st, r = call("GET", self.base + f"/api/libraries/{lib}/items?sort=title&size=100", headers=self.h())
        return lib, {x["title"]: x["id"] for x in r["items"]}

    def requests(self):
        lib, ids = self._ids()
        any_id = ids[next(iter(ids))]
        return [("library page (60 movies)", self.base + f"/api/libraries/{lib}/items?sort=title&size=60", self.h()),
                ("movie details", self.base + f"/api/items/{any_id}", self.h())]

    def _play(self, title, caps, quality="original"):
        _, ids = self._ids()
        tid = next(v for k, v in ids.items() if k.startswith(title.split(" (")[0]))
        st, p = call("POST", self.base + "/api/play", {"id": tid, "caps": caps, "quality": quality}, self.h())
        if st != 200:
            raise Failed(f"play: {st} {p}")
        return p

    def direct_urls(self):
        caps = {"video": ["h264", "hevc"], "audio": ["aac", "ac3"], "containers": ["matroska", "mp4"], "native": True}
        out = []
        for t in DIRECT_TITLES:
            p = self._play(t, caps)
            if p["mode"] != "direct":
                raise Failed(f"expected direct play, got {p['mode']}")
            out.append((self.base + p["url"], self.h()))
        return out

    def transcode_url(self):
        p = self._play(MOVIE_FILE["hevc"], {"video": ["h264"], "audio": ["aac"], "containers": ["mp4"]}, "720p")
        if p["mode"] != "transcode":
            raise Failed(f"expected a conversion, got {p['mode']}")
        return self.base + p["url"], self.h()


class Jellyfin(Server):
    name = "jellyfin"
    AUTH = 'MediaBrowser Client="bench", Device="bench", DeviceId="noxa-bench", Version="1.0"'

    def run_args(self):
        os.makedirs(os.path.join(self.data, "cache"), exist_ok=True)
        return ["-v", f"{self.data}:/config", "-v", f"{self.data}/cache:/cache"]

    def h(self):
        return {"Authorization": f'{self.AUTH}, Token="{self.token}"'}

    def setup(self):
        wait_http(self.base + "/System/Info/Public")
        for _ in range(60):  # the wizard answers a little after the server
            if call("GET", self.base + "/Startup/User")[0] == 200:
                break
            time.sleep(2)
        for path, body in (("/Startup/Configuration", {"UICulture": "en-US", "MetadataCountryCode": "US",
                                                       "PreferredMetadataLanguage": "en"}),
                           ("/Startup/User", {"Name": USER, "Password": PASSWORD}),
                           ("/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False}),
                           ("/Startup/Complete", None)):
            st, r = call("POST", self.base + path, body if body is not None else b"")
            if st not in (200, 204):
                raise Failed(f"{path}: {st} {r}")
        st, r = call("POST", self.base + "/Users/AuthenticateByName", {"Username": USER, "Pw": PASSWORD},
                     {"Authorization": self.AUTH})
        if st != 200:
            raise Failed(f"login: {st} {r}")
        self.token, self.uid = r["AccessToken"], r["User"]["Id"]

    def add_libraries(self):
        for name, ctype, path in (("Movies", "movies", "/media/movies"), ("Shows", "tvshows", "/media/shows")):
            opts = {"LibraryOptions": {"PathInfos": [{"Path": path}], "EnableInternetProviders": False,
                                       "MetadataSavers": [], "DisabledLocalMetadataReaders": [],
                                       "EnableChapterImageExtraction": False, "ExtractChapterImagesDuringLibraryScan": False,
                                       "EnableTrickplayImageExtraction": False, "ExtractTrickplayImagesDuringLibraryScan": False,
                                       "EnableRealtimeMonitor": False, "SaveLocalMetadata": False,
                                       "TypeOptions": [{"Type": t, "MetadataFetchers": [], "ImageFetchers": []}
                                                       for t in ("Movie", "Series", "Season", "Episode")]}}
            q = urllib.parse.urlencode({"name": name, "collectionType": ctype, "refreshLibrary": "true"})
            st, r = call("POST", self.base + "/Library/VirtualFolders?" + q, opts, self.h())
            if st not in (200, 204):
                raise Failed(f"library {name}: {st} {r}")

    def scanned(self):
        st, r = call("GET", self.base + "/ScheduledTasks?isHidden=false", headers=self.h())
        if st != 200 or any(t.get("Key") == "RefreshLibrary" and t.get("State") != "Idle" for t in r):
            return False
        st, c = call("GET", self.base + "/Items/Counts", headers=self.h())
        return st == 200 and c.get("MovieCount") == MOVIES and c.get("EpisodeCount") == SHOWS * SEASONS * EPISODES

    def _movies(self):
        st, views = call("GET", self.base + f"/UserViews?userId={self.uid}", headers=self.h())
        lib = next(v["Id"] for v in views["Items"] if v["Name"] == "Movies")
        q = (f"/Users/{self.uid}/Items?ParentId={lib}&IncludeItemTypes=Movie&Recursive=true&SortBy=SortName"
             "&SortOrder=Ascending&Fields=PrimaryImageAspectRatio&ImageTypeLimit=1&EnableImageTypes=Primary,Backdrop,Thumb")
        st, r = call("GET", self.base + q + "&Limit=100", headers=self.h())
        return lib, q, {x["Name"]: x["Id"] for x in r["Items"]}

    def requests(self):
        lib, q, ids = self._movies()
        any_id = ids[next(iter(ids))]
        return [("library page (60 movies)", self.base + q + "&StartIndex=0&Limit=60", self.h()),
                ("movie details", self.base + f"/Users/{self.uid}/Items/{any_id}", self.h())]

    def _id(self, title):
        _, _, ids = self._movies()
        return next(v for k, v in ids.items() if k.startswith(title.split(" (")[0]))

    def direct_urls(self):
        return [(self.base + f"/Videos/{self._id(t)}/stream?static=true&MediaSourceId={self._id(t)}&api_key={self.token}", {})
                for t in DIRECT_TITLES]

    def transcode_url(self):
        i = self._id(MOVIE_FILE["hevc"])
        q = urllib.parse.urlencode({"MediaSourceId": i, "VideoCodec": "h264", "AudioCodec": "aac", "MaxHeight": 720,
                                    "VideoBitrate": 4000000, "AudioBitrate": 192000, "MaxStreamingBitrate": 4200000,
                                    "TranscodingMaxAudioChannels": 2, "SegmentContainer": "ts", "MinSegments": 1,
                                    "BreakOnNonKeyFrames": "true", "PlaySessionId": "bench", "DeviceId": "noxa-bench",
                                    "api_key": self.token})
        return self.base + f"/Videos/{i}/master.m3u8?{q}", {}


class Plex(Server):
    """Unclaimed server, reached from the same machine: its API needs no account or token."""
    name = "plex"
    H = {"Accept": "application/json", "X-Plex-Client-Identifier": "noxa-bench", "X-Plex-Product": "noxa-bench",
         "X-Plex-Platform": "Chrome", "X-Plex-Device": "Linux", "X-Plex-Version": "1.0"}

    def run_args(self):
        os.makedirs(os.path.join(self.data, "transcode"), exist_ok=True)
        return ["-v", f"{self.data}:/config", "-v", f"{self.data}/transcode:/transcode", "-e", "PLEX_UID=0", "-e", "PLEX_GID=0",
                "-e", "TZ=UTC"]

    def setup(self):
        wait_http(self.base + "/identity")
        for _ in range(150):  # until its plug-ins are ready ("startState" disappears from /identity)
            st, r = call("GET", self.base + "/identity", headers=self.H)
            if st == 200 and isinstance(r, dict) and not r.get("MediaContainer", {}).get("startState"):
                break
            time.sleep(2)
        else:
            raise Failed("Plex still starting after 5 minutes")
        # preview thumbnails: never (by default they are made during the night maintenance, i.e. not during the test)
        call("PUT", self.base + "/:/prefs?GenerateBIFBehavior=never&GenerateChapterThumbBehavior=never"
                                "&LoudnessAnalysisBehavior=never&GenerateIntroMarkerBehavior=never"
                                "&GenerateCreditsMarkerBehavior=never&GenerateAdMarkerBehavior=never", b"", self.H)

    def add_libraries(self):
        for name, typ, scanner, path in (("Movies", "movie", "Plex Movie", "/media/movies"),
                                         ("Shows", "show", "Plex TV Series", "/media/shows")):
            q = urllib.parse.urlencode({"name": name, "type": typ, "agent": "tv.plex.agents.none", "scanner": scanner,
                                        "language": "xn", "location": path})
            st, r = call("POST", self.base + "/library/sections?" + q, b"", self.H)
            if st not in (200, 201):
                raise Failed(f"library {name}: {st} {r}")

    def _sections(self):
        st, r = call("GET", self.base + "/library/sections", headers=self.H)
        return {d["title"]: d for d in r["MediaContainer"].get("Directory", [])}

    def scanned(self):
        secs = self._sections()
        if len(secs) < 2 or any(d.get("refreshing") for d in secs.values()):
            return False
        st, m = call("GET", self.base + f"/library/sections/{secs['Movies']['key']}/all", headers=self.H)
        st, e = call("GET", self.base + f"/library/sections/{secs['Shows']['key']}/all?type=4", headers=self.H)
        return m["MediaContainer"].get("size") == MOVIES and e["MediaContainer"].get("size") == SHOWS * SEASONS * EPISODES

    def _movies(self):
        key = self._sections()["Movies"]["key"]
        st, r = call("GET", self.base + f"/library/sections/{key}/all", headers=self.H)
        return key, {x["title"]: x for x in r["MediaContainer"]["Metadata"]}

    def requests(self):
        key, items = self._movies()
        rk = items[next(iter(items))]["ratingKey"]
        return [("library page (60 movies)", self.base + f"/library/sections/{key}/all?sort=titleSort"
                                             "&X-Plex-Container-Start=0&X-Plex-Container-Size=60", self.H),
                ("movie details", self.base + f"/library/metadata/{rk}", self.H)]

    def _item(self, title):
        _, items = self._movies()
        return next(v for k, v in items.items() if k.startswith(title.split(" (")[0]))

    def direct_urls(self):
        return [(self.base + self._item(t)["Media"][0]["Part"][0]["key"], {"X-Plex-Client-Identifier": "noxa-bench"})
                for t in DIRECT_TITLES]

    def transcode_url(self):
        rk = self._item(MOVIE_FILE["hevc"])["ratingKey"]
        q = urllib.parse.urlencode({"path": f"/library/metadata/{rk}", "mediaIndex": 0, "partIndex": 0, "protocol": "hls",
                                    "directPlay": 0, "directStream": 0, "directStreamAudio": 0, "fastSeek": 1,
                                    "videoResolution": "1280x720", "maxVideoBitrate": 4000, "videoQuality": 100,
                                    "session": "noxa-bench", "X-Plex-Session-Identifier": "noxa-bench",
                                    "X-Plex-Client-Profile-Extra": "add-transcode-target(type=videoProfile&context=streaming"
                                                                   "&protocol=hls&container=mpegts&videoCodec=h264&audioCodec=aac)",
                                    **{k: v for k, v in self.H.items() if k.startswith("X-Plex")}})
        return self.base + f"/video/:/transcode/universal/start.m3u8?{q}", {k: v for k, v in self.H.items() if k != "Accept"}


SERVERS = {"noxa": Noxa, "jellyfin": Jellyfin, "plex": Plex}


# ---------------------------------------------------------------------------------------------- workloads
def latency(reqs, n):
    out = {}
    for name, url, headers in reqs:
        call("GET", url, headers=headers)  # first call warms up the caches
        times = []
        for _ in range(n):
            t = time.perf_counter()
            st = call("GET", url, headers=headers)[0]
            times.append((time.perf_counter() - t) * 1000)
            if st != 200:
                raise Failed(f"{name}: HTTP {st}")
        times.sort()
        out[name] = {"median_ms": round(statistics.median(times), 1), "p95_ms": round(times[int(len(times) * 0.95) - 1], 1)}
    return out


def stream_reader(url, headers, rate, seconds, result):
    """Reads a file like a player would: at `rate` bytes a second, after a first 4 MB burst."""
    got, t0 = 0, time.time()
    try:
        req = urllib.request.Request(url, headers=headers)
        with NO_PROXY.open(req, timeout=30) as r:
            if r.status not in (200, 206):
                raise Failed(f"HTTP {r.status}")
            while time.time() - t0 < seconds:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
                ahead = (got - 4 * 2**20) / rate - (time.time() - t0)
                if ahead > 0:
                    time.sleep(ahead)
        result.append(got)
    except Exception as e:
        result.append(e)


def hls_fetch(url, headers, seconds):
    """Downloads the HLS segments as fast as the server makes them: media seconds produced per second of wall time."""
    t0 = time.time()
    st, text = call("GET", url, headers=headers, timeout=120)
    if st != 200 or "#EXTM3U" not in str(text):
        raise Failed(f"playlist: HTTP {st} {str(text)[:200]}")
    base = url
    if "#EXT-X-STREAM-INF" in text:
        line = next(l for l in text.splitlines() if l and not l.startswith("#"))
        base = urllib.parse.urljoin(url, line)
    seen, media_s, segs = set(), 0.0, 0
    while time.time() - t0 < seconds:
        st, pl = call("GET", base, headers=headers, timeout=120)
        if st != 200:
            raise Failed(f"media playlist: HTTP {st}")
        dur, new = None, False
        for line in pl.splitlines():
            if line.startswith("#EXTINF:"):
                dur = float(line[8:].split(",")[0])
            elif line and not line.startswith("#") and line not in seen:
                if time.time() - t0 >= seconds:
                    break
                st, _, _ = call("GET", urllib.parse.urljoin(base, line), headers=headers, timeout=180, raw=True)
                if st != 200:
                    raise Failed(f"segment: HTTP {st}")
                seen.add(line)
                media_s += dur or 0
                segs += 1
                new = True
        if "#EXT-X-ENDLIST" in pl and not new:
            break
        if not new:
            time.sleep(0.5)
    wall = time.time() - t0
    return {"segments": segs, "media_seconds": round(media_s, 1), "speed_x": round(media_s / wall, 2) if wall else 0}


# ---------------------------------------------------------------------------------------------- one server
def bench_server(cls, args, outdir):
    s = cls(args.work, args.media, args.cpus, args.memory)
    res = {"image": IMAGES[s.name]}
    log(f"=== {s.name} ({IMAGES[s.name]})")
    try:
        res["image_mb"] = round(int(docker("image", "inspect", IMAGES[s.name], "--format", "{{.Size}}")) / 2**20)
        s.start()
        sampler = Sampler(s.container)
        sampler.start()
        t_start = time.time()
        s.setup()
        res["startup_s"] = round(time.time() - t_start, 1)
        log(f"  ready and set up in {res['startup_s']} s")

        log("  scanning the library")
        t0 = time.time()
        s.add_libraries()
        while not s.scanned():
            if time.time() - t0 > 1800:
                raise Failed("scan not finished after 30 minutes")
            time.sleep(1)
        t1 = time.time()
        res["scan"] = {"duration_s": round(t1 - t0, 1), **sampler.summary(t0, t1)}
        log(f"  scan: {res['scan']['duration_s']} s")

        limit = args.settle
        log(f"  waiting for background work to finish (CPU under 3 % for 30 s, at most {limit // 60} min)")
        t2 = time.time()
        while time.time() - t2 < limit:
            time.sleep(5)
            w = sampler.window(time.time() - 30, time.time())
            if len(w) >= 20 and max(c for c, _ in w) < 3:
                break
        res["background_work_s"] = round(max(0, time.time() - t2 - 30), 1) if time.time() - t2 < limit \
            else f"> {limit}"

        log(f"  idle for {args.idle} s")
        t3 = time.time()
        time.sleep(args.idle)
        res["idle"] = sampler.summary(t3, time.time())

        log("  response times")
        res["latency"] = latency(s.requests(), args.requests)

        log(f"  direct play: 3 clients for {args.play} s")
        urls = s.direct_urls()
        results, threads = [], []
        t4 = time.time()
        for url, h in urls:
            th = threading.Thread(target=stream_reader, args=(url, h, 8e6 / 8 * 1.1, args.play, results))
            th.start()
            threads.append(th)
        for th in threads:
            th.join()
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            raise Failed(f"direct play: {errors[0]}")
        res["direct_play_3"] = {**sampler.summary(t4, time.time()), "mb_read": round(sum(results) / 2**20)}

        log(f"  conversion HEVC 10-bit 1080p -> H.264 720p for {args.play} s")
        for attempt in range(3):  # Plex downloads its codecs the first time it converts: give it a moment
            try:
                url, h = s.transcode_url()
                t5 = time.time()
                hls = hls_fetch(url, h, args.play)
                break
            except Failed as e:
                if attempt == 2:
                    raise
                log(f"  conversion not ready ({e}), new try in 20 s")
                time.sleep(20)
        res["transcode"] = {**sampler.summary(t5, time.time()), **hls}
        log(f"  conversion: x{hls['speed_x']} real time")
        res["ok"] = True
    except Exception as e:
        res["ok"] = False
        res["error"] = f"{type(e).__name__}: {e}"
        log(f"  FAILED: {res['error']}")
        logs = docker("logs", "--tail", "60", s.container, check=False, capture=True)
        with open(os.path.join(outdir, f"{s.name}-container.log"), "w") as f:
            f.write(logs)
    finally:
        try:
            sampler.stop_flag = True
            with open(os.path.join(outdir, f"samples-{s.name}.csv"), "w") as f:
                f.write("time,cpu_percent_of_one_core,memory_mb\n")
                for t, c, m in sampler.samples:
                    f.write(f"{t:.1f},{c:.1f},{m / 2**20:.1f}\n")
        except NameError:
            pass
        s.stop()
    return res


# ---------------------------------------------------------------------------------------------- report
def machine():
    cpu = ""
    try:
        cpu = next(l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name"))
    except (OSError, StopIteration):
        pass
    mem = 0
    try:
        mem = int(next(l.split()[1] for l in open("/proc/meminfo") if l.startswith("MemTotal"))) // 1024
    except (OSError, StopIteration):
        pass
    return {"cpu": cpu, "cores": os.cpu_count(), "ram_mb": mem, "kernel": platform.release(),
            "docker": docker("version", "--format", "{{.Server.Version}}", check=False)}


def summary_md(all_res, info, args):
    names = [n for n in all_res]
    L = [f"# Media server benchmark ({datetime.date.today()})", "",
         f"Machine: {info['cpu']}, {info['cores']} cores, {info['ram_mb']} MB RAM, kernel {info['kernel']}, "
         f"Docker {info['docker']}.  ",
         f"Each server alone, limited to {args.cpus} CPU and {args.memory} of memory, same test library "
         f"({MOVIES} movies, {SHOWS * SEASONS * EPISODES} episodes; generated test pattern, online metadata off).  ",
         "CPU is in % of one core (200 % = two cores busy). Memory excludes the page cache, like `docker stats`.", ""]

    def cell(n, path, unit=""):
        r = all_res[n]
        for k in path:
            if not isinstance(r, dict) or k not in r:
                return "–"
            r = r[k]
        return f"{r}{unit}"

    rows = [("Version", ["image"], ""), ("Docker image size", ["image_mb"], " MB"),
            ("Start and setup", ["startup_s"], " s"),
            ("Scan: duration", ["scan", "duration_s"], " s"), ("Scan: CPU peak", ["scan", "cpu_max"], " %"),
            ("Scan: memory peak", ["scan", "mem_max_mb"], " MB"),
            ("Background work after the scan", ["background_work_s"], " s"),
            ("Idle: CPU", ["idle", "cpu_avg"], " %"), ("Idle: memory", ["idle", "mem_avg_mb"], " MB"),
            ("Library page: median", ["latency", "library page (60 movies)", "median_ms"], " ms"),
            ("Library page: 95th percentile", ["latency", "library page (60 movies)", "p95_ms"], " ms"),
            ("Movie details: median", ["latency", "movie details", "median_ms"], " ms"),
            ("Direct play x3: CPU", ["direct_play_3", "cpu_avg"], " %"),
            ("Direct play x3: memory", ["direct_play_3", "mem_max_mb"], " MB"),
            ("Conversion: speed", ["transcode", "speed_x"], "x real time"),
            ("Conversion: CPU", ["transcode", "cpu_avg"], " %"),
            ("Conversion: memory", ["transcode", "mem_max_mb"], " MB")]
    L.append("| | " + " | ".join(n.capitalize() for n in names) + " |")
    L.append("|---|" + "---|" * len(names))
    for label, path, unit in rows:
        L.append(f"| {label} | " + " | ".join(cell(n, path, unit) for n in names) + " |")
    failed = [n for n in names if not all_res[n].get("ok")]
    if failed:
        L += ["", "Not completed:"] + [f"- {n}: {all_res[n].get('error')}" for n in failed]
    L += ["", "Raw samples: samples-<server>.csv. Method and scripts: tools/benchmark in the Noxa repository."]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--servers", default="noxa,jellyfin,plex")
    ap.add_argument("--work", default=os.path.join(HERE, "work"), help="test library and server data (needs about 4 GB)")
    ap.add_argument("--cpus", default="2")
    ap.add_argument("--memory", default="4g")
    ap.add_argument("--idle", type=int, default=300, help="seconds of idle measurement")
    ap.add_argument("--play", type=int, default=90, help="seconds of direct play and of conversion")
    ap.add_argument("--requests", type=int, default=50)
    ap.add_argument("--settle", type=int, default=900, help="longest wait for background work after the scan (s)")
    ap.add_argument("--quick", action="store_true", help="short run to check that everything works")
    ap.add_argument("--prepare", action="store_true", help="only download the images and build the test library")
    args = ap.parse_args()
    if args.quick:
        args.idle, args.play, args.requests, args.settle = 20, 20, 10, 120
    args.work = os.path.abspath(args.work)
    args.media = os.path.join(args.work, "media" + ("-quick" if args.quick else ""))
    if os.geteuid() != 0:
        sys.exit("Run as root (it starts containers and prepares their folders).")
    outdir = os.path.join(HERE, "results", datetime.datetime.now().strftime("%Y-%m-%d_%H%M"))
    os.makedirs(outdir, exist_ok=True)
    info = machine()
    log(f"Machine: {info['cpu']}, {info['cores']} cores, {info['ram_mb']} MB")
    names = [n.strip() for n in args.servers.split(",") if n.strip() in SERVERS]
    for n in names + ["ffmpeg"]:
        img = IMAGES.get(n, FFMPEG_IMAGE)
        if not docker("images", "-q", img, check=False):
            log(f"Downloading {img}")
            docker("pull", "-q", img)
    build_library(args.media, args.quick)
    age_library(args.media)
    if args.prepare:
        log("Ready: images downloaded and test library built")
        shutil.rmtree(outdir, ignore_errors=True)
        return 0
    all_res = {}
    for n in names:
        all_res[n] = bench_server(SERVERS[n], args, outdir)
        with open(os.path.join(outdir, "results.json"), "w") as f:
            json.dump({"machine": info, "settings": vars(args), "results": all_res}, f, indent=2)
    md = summary_md(all_res, info, args)
    with open(os.path.join(outdir, "summary.md"), "w") as f:
        f.write(md)
    print("\n" + md)
    log(f"Results in {outdir}")
    return 0 if all(r.get("ok") for r in all_res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
