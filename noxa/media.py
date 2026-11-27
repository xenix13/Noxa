"""Calls to ffprobe / ffmpeg: file analysis, pictures, subtitles."""
import json
import logging
import os
import re
import shutil
import subprocess
import threading

log = logging.getLogger("noxa.media")

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "pgssub"}
_image_slots = threading.BoundedSemaphore(3)  # limits the number of picture ffmpegs at the same time
# Library files: ffmpeg must only read local files. A booby-trapped file (an HLS playlist or
# a "concat" list disguised as .mkv) could otherwise make it open network addresses or other files.
LOCAL_ONLY = ["-protocol_whitelist", "file,pipe"]
REFUSED_FORMATS = {"hls", "applehttp", "concat", "ffconcat", "tee"}
# .strm files: an http(s) address of the video (IPTV VOD, another server…). ffmpeg only opens the network there
# (never a local file, even if a remote playlist points to one) and reconnects when the connection drops.
STRM_EXT = ".strm"
REMOTE_ONLY = ["-protocol_whitelist", "http,https,tcp,tls,crypto", "-reconnect", "1", "-reconnect_streamed", "1",
               "-reconnect_delay_max", "10", "-rw_timeout", "20000000"]


def is_strm(path):
    return bool(path) and path.lower().endswith(STRM_EXT)


def strm_url(path):
    """Address held by a .strm file (first useful line), http(s) only."""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            lines = f.read(8192).splitlines()
    except OSError as e:
        raise MediaError(f"fichier .strm illisible ({e.strerror})")
    url = next((l.strip() for l in lines if l.strip() and not l.strip().startswith("#")), "")
    if not url.lower().startswith(("http://", "https://")) or len(url) > 4000 or any(c.isspace() or ord(c) < 32 for c in url):
        raise MediaError("le fichier .strm doit contenir une adresse http(s)")
    return url


COLD = "cold:"  # cold copy read through Noxa's local address (see cold/): "cold:http://127.0.0.1:…/cold/<token>"


def is_remote(path):
    """Source read over the network (.strm, remote cold copy): no direct play of the file."""
    return bool(path) and (path.startswith(COLD) or is_strm(path))


def input_args(path):
    """ffmpeg / ffprobe input arguments for a library file (local, .strm or cold copy)."""
    if path.startswith(COLD):
        return [*REMOTE_ONLY, "-i", path[len(COLD):]]
    if is_strm(path):
        return [*REMOTE_ONLY, "-i", strm_url(path)]
    return [*LOCAL_ONLY, "-i", path]


class MediaError(Exception):
    pass


_options = {}


def has_option(name):
    """Does ffmpeg know this option? (-readrate only exists since ffmpeg 5)"""
    if name not in _options:
        try:
            out = subprocess.run([FFMPEG, "-hide_banner", "-h", "full"], capture_output=True, timeout=20).stdout
            _options[name] = f"-{name} ".encode() in out
        except (OSError, subprocess.SubprocessError):
            _options[name] = False
    return _options[name]


_filters = {}


def has_filter(name):
    """Does ffmpeg have this filter? (zscale, to convert HDR, depends on how it was built)"""
    if name not in _filters:
        try:
            out = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, timeout=20).stdout
            _filters[name] = re.search(rb"^\s*\S+\s+" + re.escape(name.encode()) + rb"\s", out, re.M) is not None
        except (OSError, subprocess.SubprocessError):
            _filters[name] = False
    return _filters[name]


_version = []


def ffmpeg_major():
    """Major ffmpeg version (6, 7, 8…), 0 when unknown (development version "N-…")."""
    if not _version:
        try:
            out = subprocess.run([FFMPEG, "-hide_banner", "-version"], capture_output=True, timeout=20).stdout
            m = re.match(rb"ffmpeg version n?(\d+)\.", out)
            _version.append(int(m.group(1)) if m else 0)
        except (OSError, subprocess.SubprocessError):
            _version.append(0)
    return _version[0]


def readrate_ok():
    """-readrate usable to spare the network share. Since ffmpeg 7, rate-limited reading freezes half-way
    on some files (seen with ffmpeg 8.0: preparation stuck in the middle of the file): it is left out,
    the low priority (nice, ionice) is enough to leave room for playback."""
    major = ffmpeg_major()
    return has_option("readrate") and 0 < major < 7


def available():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def run(cmd, timeout=120, input_bytes=None):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, input=input_bytes)
    except subprocess.TimeoutExpired:
        raise MediaError("délai dépassé")
    except FileNotFoundError:
        raise MediaError("ffmpeg est introuvable (apt install ffmpeg)")
    if p.returncode != 0:
        raise MediaError((p.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["échec"])
    return p.stdout


def _lang(tags):
    from .names import LANG_WORDS
    code = (tags.get("language") or "").lower()
    if code in ("und", "unk", "mis", "zxx"):
        return ""
    return LANG_WORDS.get(code, code)


FONT_EXT = (".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2")
FONT_MIME = ("font/", "application/x-truetype-font", "application/x-font", "application/vnd.ms-opentype",
             "application/font-sfnt", "application/x-opentype")


def _fps(rate):
    try:
        num, den = (rate or "0/1").split("/")
        return round(int(num) / int(den), 3) if int(den) else 0
    except (ValueError, ZeroDivisionError):
        return 0


def attachment_info(s, tags=None):
    """Fonts attached to the MKV (used by the ASS subtitles of anime)."""
    tags = tags if tags is not None else {k.lower(): v for k, v in (s.get("tags") or {}).items()}
    name = (tags.get("filename") or "").strip()
    mime = (tags.get("mimetype") or "").lower()
    ext = os.path.splitext(name)[1].lower()
    if ext in FONT_EXT or mime.startswith(FONT_MIME) or s.get("codec_name") in ("ttf", "otf"):
        return {"index": s["index"], "filename": name[:200], "ext": ext if ext in FONT_EXT else ".ttf"}
    return None


def attachments(path):
    out = run([FFPROBE, "-v", "error", *LOCAL_ONLY, "-print_format", "json", "-show_streams", "-select_streams", "t", path],
              timeout=60)
    return [a for a in (attachment_info(s) for s in json.loads(out or b"{}").get("streams", [])) if a]


def probe(path):
    remote = is_strm(path)
    args = input_args(path)
    out = run([FFPROBE, "-v", "error", *args[:-2], "-print_format", "json", "-show_format", "-show_streams",
               "-show_chapters", args[-1]], timeout=90)
    data = json.loads(out or b"{}")
    fmt = data.get("format", {})
    if not remote and set((fmt.get("format_name") or "").split(",")) & REFUSED_FORMATS:
        raise MediaError("liste de lecture déguisée en vidéo : fichier ignoré")
    info = {
        "container": fmt.get("format_name", ""),
        "duration": float(fmt.get("duration") or 0),
        "bitrate": int(fmt.get("bit_rate") or 0),
        "video": None, "audio": [], "subs": [], "external": [], "chapters": [], "attachments": [],
    }
    for c in data.get("chapters", []):
        try:
            start, end = float(c.get("start_time") or 0), float(c.get("end_time") or 0)
        except (TypeError, ValueError):
            continue
        title = ((c.get("tags") or {}).get("title") or "").strip()
        if end > start:
            info["chapters"].append({"start": round(start, 3), "end": round(end, 3), "title": title[:120]})
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        if kind == "video" and info["video"] is None and not disp.get("attached_pic"):
            info["video"] = {
                "index": s["index"], "codec": s.get("codec_name"), "profile": s.get("profile") or "",
                "pix_fmt": s.get("pix_fmt") or "", "width": s.get("width") or 0, "height": s.get("height") or 0,
                "bitrate": int(s.get("bit_rate") or 0), "fps": _fps(s.get("avg_frame_rate") or s.get("r_frame_rate")),
                # device profile: level, HDR, reference frames
                "level": s.get("level") if isinstance(s.get("level"), int) and s.get("level") > 0 else None,
                "transfer": s.get("color_transfer") or "", "refs": s.get("refs") or None,
                "interlaced": (s.get("field_order") or "progressive") not in ("progressive", "unknown"),
            }
        elif kind == "audio":
            info["audio"].append({
                "index": s["index"], "codec": s.get("codec_name"), "channels": s.get("channels") or 2,
                "layout": s.get("channel_layout") or "", "rate": int(s.get("sample_rate") or 0),
                "lang": _lang(tags), "title": tags.get("title") or "", "default": bool(disp.get("default")),
            })
        elif kind == "attachment" and not remote:
            att = attachment_info(s, tags)
            if att:
                info["attachments"].append(att)
        elif kind == "subtitle" and not remote:
            # shortcut: internal subtitles of a .strm are ignored (extracting them would read the whole remote stream again,
            # and an IPTV provider often cuts the 2nd connection); .srt files placed next to it are still offered
            info["subs"].append({
                "index": s["index"], "codec": s.get("codec_name") or "", "lang": _lang(tags),
                "title": tags.get("title") or "", "forced": bool(disp.get("forced")),
                "default": bool(disp.get("default")),
            })
    if not info["duration"] and info["video"] is None and not info["audio"]:
        raise MediaError("fichier illisible")
    return info


def resize_image(src, dest, w, h, crop=True, orient=None):
    """Resizes (and crops) a picture with ffmpeg -> JPEG. orient: filters that turn it the right way up
    (EXIF orientation of a photo, applied here rather than guessed by ffmpeg depending on its version)."""
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}" if crop
          else f"scale='min({w},iw)':'min({h},ih)':force_original_aspect_ratio=decrease")
    if orient:
        vf = f"{orient},{vf}"
    tmp = f"{dest}.{threading.get_ident()}.tmp.jpg"
    with _image_slots:
        run([FFMPEG, "-y", "-v", "error", *(["-noautorotate"] if orient is not None else []), "-i", src, "-vf", vf,
             "-frames:v", "1", "-q:v", "4", tmp], timeout=60)
    _keep_image(tmp, dest)


def grab_frame(video, dest, at_seconds, w, h):
    tmp = f"{dest}.{threading.get_ident()}.tmp.jpg"
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    with _image_slots:
        run([FFMPEG, "-y", "-v", "error", *LOCAL_ONLY, "-ss", f"{at_seconds:.2f}", "-i", video, "-vf", vf,
             "-frames:v", "1", "-q:v", "4", tmp], timeout=90)
    _keep_image(tmp, dest)


def _keep_image(tmp, dest):
    """ffmpeg can succeed without writing anything (time after the last frame, empty video track)."""
    if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise MediaError("aucune image à cet instant de la vidéo")
    os.replace(tmp, dest)


def extract_subtitle(source, stream_index=None):
    """Converts a text subtitle track (internal) or file (external) to SRT."""
    cmd = [FFMPEG, "-v", "error", *LOCAL_ONLY, "-i", source]
    if stream_index is not None:
        cmd += ["-map", f"0:{stream_index}"]
    cmd += ["-c:s", "srt", "-f", "srt", "-"]
    return run(cmd, timeout=600)
