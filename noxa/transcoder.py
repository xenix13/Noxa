"""Playback: direct / remux / transcode decision, and "dynamic" HLS streams.

Principles:
- a complete VOD playlist is announced from the start (6 s segments; in remux, aligned on the keyframes
  read from the MKV index): the player knows the whole timeline and seeks without restarting the stream;
- segments are made on demand in fMP4 (no TS -> MP4 conversion in the browser) with the original
  timestamps (-copyts); when the player asks for a segment far from what ffmpeg is preparing,
  ffmpeg is started again there (-ss + -start_number);
- in remux, the target is 0.25 s after the keyframe (otherwise ffmpeg falls back to the previous one);
- tolerant input options (high analyzeduration / probesize, +genpts), normalised stereo mixes.

Without a GPU, the whole point is to transcode the video only when there is no other choice:
- direct    : the browser plays the file as is                                        -> 0 % CPU
- copy      : video copied (H.264, and HEVC/AV1 when the browser can play them)       -> very little CPU
- transcode : codec the browser cannot play, burnt-in picture subtitles, lower quality -> libx264
"""
import glob
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time

from . import media
from .keyframes import equal_segments, segments_from_keyframes

log = logging.getLogger("noxa.transcode")

QUALITY = {"original": None, "1080p": (1080, 8000), "720p": (720, 4000), "480p": (480, 1500)}
QUALITIES = ("auto", *QUALITY)  # "auto": chosen from the device's measured bandwidth (auto_quality)
AUDIO_KBPS = 192
HEADROOM = 1.3  # bandwidth margin: peaks in the file, changes in the connection
SEGMENT = 6            # secondes
COPY_SEEK_PAD = 0.25   # remux: aim just after the keyframe
SEG_RE = re.compile(r"^(\d{1,6})\.m4s$")
INIT = "init.mp4"
AHEAD_PAUSE = 30       # segments ahead (~3 min) before pausing ffmpeg
AHEAD_RESUME = 15
KEEP_BEHIND = 20       # segments kept behind the position (~2 min)
RESTART_GAP = 4        # segments (24 s): beyond this, ffmpeg is started again where the player asks
INPUT_OPTS = ["-analyzeduration", "200M", "-probesize", "1G"]

# Browser with no declared profile: what they can all play
DEFAULT_CAPS = {"video": {"h264"}, "audio": {"aac", "mp3"}, "containers": {"mp4", "mov"}}

# Stereo downmixes: (algorithm, layout) -> filter
DOWNMIX_FILTERS = {
    ("dave750", "5.1"): "pan=stereo|c0=0.5*c2+0.707*c0+0.707*c4+0.5*c3|c1=0.5*c2+0.707*c1+0.707*c5+0.5*c3",
    ("dave750", "7.1"): "pan=5.1(side)|c0=c0|c1=c1|c2=c2|c3=c3|c4=0.707*c4+0.707*c6|c5=0.707*c5+0.707*c7,"
                        "pan=stereo|c0=0.5*c2+0.707*c0+0.707*c4+0.5*c3|c1=0.5*c2+0.707*c1+0.707*c5+0.5*c3",
    ("nightmode", "5.1"): "pan=stereo|c0=c2+0.30*c0+0.30*c4|c1=c2+0.30*c1+0.30*c5",
    ("nightmode", "7.1"): "pan=5.1(side)|c0=c0|c1=c1|c2=c2|c3=c3|c4=0.707*c4+0.707*c6|c5=0.707*c5+0.707*c7,"
                          "pan=stereo|c0=c2+0.30*c0+0.30*c4|c1=c2+0.30*c1+0.30*c5",
    ("rfc7845", "3.0"): "pan=stereo|c0=0.414214*c2+0.585786*c0|c1=0.414214*c2+0.585786*c1",
    ("rfc7845", "quad"): "pan=stereo|c0=0.422650*c0+0.366025*c2+0.211325*c3|c1=0.422650*c1+0.366025*c3+0.211325*c2",
    ("rfc7845", "5.0"): "pan=stereo|c0=0.460186*c2+0.650802*c0+0.563611*c3+0.325401*c4|"
                        "c1=0.460186*c2+0.650802*c1+0.563611*c4+0.325401*c3",
    ("rfc7845", "5.1"): "pan=stereo|c0=0.374107*c2+0.529067*c0+0.458186*c4+0.264534*c5+0.374107*c3|"
                        "c1=0.374107*c2+0.529067*c1+0.458186*c5+0.264534*c4+0.374107*c3",
    ("rfc7845", "6.1"): "pan=stereo|c0=0.321953*c2+0.455310*c0+0.394310*c5+0.227655*c6+0.278819*c4+0.321953*c3|"
                        "c1=0.321953*c2+0.455310*c1+0.394310*c6+0.227655*c5+0.278819*c4+0.321953*c3",
    ("rfc7845", "7.1"): "pan=stereo|c0=0.274804*c2+0.388631*c0+0.336565*c6+0.194316*c7+0.336565*c4+0.194316*c5"
                        "+0.274804*c3|c1=0.274804*c2+0.388631*c1+0.336565*c7+0.194316*c6+0.336565*c5+0.194316*c4"
                        "+0.274804*c3",
    ("ac4", "3.0"): "pan=stereo|c0=c0+0.707*c2|c1=c1+0.707*c2",
    ("ac4", "5.0"): "pan=stereo|c0=c0+0.707*c2+0.707*c3|c1=c1+0.707*c2+0.707*c4",
    ("ac4", "5.1"): "pan=stereo|c0=c0+0.707*c2+0.707*c4|c1=c1+0.707*c2+0.707*c5",
    ("ac4", "7.0"): "pan=5.0(side)|c0=c0|c1=c1|c2=c2|c3=0.707*c3+0.707*c5|c4=0.707*c4+0.707*c6,"
                    "pan=stereo|c0=c0+0.707*c2+0.707*c3|c1=c1+0.707*c2+0.707*c4",
    ("ac4", "7.1"): "pan=5.1(side)|c0=c0|c1=c1|c2=c2|c3=c3|c4=0.707*c4+0.707*c6|c5=0.707*c5+0.707*c7,"
                    "pan=stereo|c0=c0+0.707*c2+0.707*c4|c1=c1+0.707*c2+0.707*c5",
}
DOWNMIX_ALGOS = ("none", "dave750", "nightmode", "rfc7845", "ac4")
# HDR (PQ: HDR10, HDR10+, Dolby Vision with an HDR10 base; HLG) converted to SDR when the video is transcoded:
# without it, the H.264 output has washed-out, grey colours. "off": nothing is converted.
HDR_TRANSFERS = ("smpte2084", "arib-std-b67")
TONEMAPS = ("hable", "mobius", "reinhard", "off")


def tonemap_filter(p):
    """HDR -> SDR (BT.709) filter chain, or "" (SDR source, "off" setting, or ffmpeg without zscale)."""
    algo = p.get("tonemap")
    if not p.get("hdr") or algo in (None, "off") or not media.has_filter("zscale") or not media.has_filter("tonemap"):
        return ""
    return (f"zscale=tin={p['hdr']}:min=bt2020nc:pin=bt2020:t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            f"tonemap=tonemap={algo}:desat=0,zscale=t=bt709:m=bt709:r=tv,")
# our touch: a limiter AFTER the mix and the gain, so that no setting can clip
LIMITER = "alimiter=limit=0.8:attack=5:release=80:level=disabled"
# compat: former name used by the tests
DOWNMIX = "aformat=channel_layouts=stereo,volume=2," + LIMITER


class Busy(Exception):
    pass


class StreamError(Exception):
    pass


def infer_layout(a):
    lay = (a.get("layout") or "").split("(")[0]
    if lay:
        return lay
    return {1: "mono", 2: "stereo", 3: "2.1", 4: "4.0", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}.get(a.get("channels") or 2, "")


def audio_filter(a, algo="none", boost=2.0):
    """-af chain that downmixes a multichannel track to stereo (empty when already stereo)."""
    if not a or (a.get("channels") or 2) <= 2:
        return ""
    parts = []
    pan = DOWNMIX_FILTERS.get((algo, infer_layout(a)))
    parts.append(pan or "aformat=channel_layouts=stereo")
    if abs(boost - 1.0) > 0.01:
        parts.append(f"volume={boost:g}")
    parts.append(LIMITER)
    return ",".join(parts)


def _bit_depth(v):
    prof = (v.get("profile") or "").lower()
    fmt = (v.get("pix_fmt") or "")
    return 10 if ("10" in prof or "10le" in fmt or "10be" in fmt or "12" in fmt) else 8


def video_ok(v, caps=None):
    """Can the video track be copied as is for this device (browser or native player)?"""
    caps = caps or DEFAULT_CAPS
    if not v:
        return False
    codec, depth = v.get("codec"), _bit_depth(v)
    fmt = v.get("pix_fmt") or "yuv420p"
    if "422" in fmt or "444" in fmt or "4:2:2" in (v.get("profile") or "") or "4:4:4" in (v.get("profile") or ""):
        return False
    if codec == "h264":
        return depth == 8 and "h264" in caps["video"]
    if codec in ("mpeg2video", "vc1"):  # decoded by most TVs and boxes, never by a browser
        return codec in caps["video"]
    if codec == "hevc":
        return ("hevc10" if depth == 10 else "hevc") in caps["video"] and depth <= 10
    if codec == "av1":
        return ("av1_10" if depth == 10 else "av1") in caps["video"]
    if codec == "vp9":
        return "vp9" in caps["video"] and depth == 8
    return False


def source_kbps(probe, file_size=0, duration_ms=0):
    """Average bitrate of the file (kbit/s), from the analysis, otherwise from its size and duration."""
    rate = int((probe.get("bitrate") or 0) / 1000)
    if not rate and file_size and duration_ms:
        rate = int(file_size * 8 / (duration_ms / 1000) / 1000)
    return rate


def auto_quality(probe, kbps, file_size=0, duration_ms=0):
    """Quality for a measured bandwidth (kbit/s): the original when it fits with a margin, otherwise the best
    converted quality that fits, 480p at the lowest. Without a measurement: the original."""
    if not kbps:
        return "original"
    src = source_kbps(probe, file_size, duration_ms)
    src_h = (probe.get("video") or {}).get("height") or 0
    if src and kbps >= src * HEADROOM or not src and kbps >= 25000:
        return "original"
    for q in ("1080p", "720p", "480p"):
        height, rate = QUALITY[q]
        if src_h and height > src_h and (not src or rate * HEADROOM >= src):
            continue  # above the file's resolution and bitrate: nothing to gain
        if kbps >= (rate + AUDIO_KBPS) * HEADROOM:
            return q
    return "480p"


def plan(probe, audio_index=None, burn_index=None, quality="original", caps=None, algo="none", boost=2.0,
         tonemap="hable"):
    caps = caps or DEFAULT_CAPS
    v = probe.get("video")
    audios = probe.get("audio") or []
    a = next((x for x in audios if x["index"] == audio_index), audios[0] if audios else None)
    target = QUALITY.get(quality)
    src_h = (v or {}).get("height") or 0
    rate_kbps = int((probe.get("bitrate") or 0) / 1000)
    need_scale = bool(target and src_h > target[0])
    over_rate = bool(target and rate_kbps and rate_kbps > target[1] * 1.3)
    vcopy = video_ok(v, caps) and burn_index is None and not need_scale and not over_rate
    native = bool(caps.get("native"))
    channels = (a or {}).get("channels") or 2
    # multichannel: downmixed to stereo by us (the browser's default mix clips), except for a native
    # player that passes it to the TV or the amplifier. In HLS (fMP4), only multichannel AC3 / E-AC3 / AAC / FLAC / Opus
    # are copied; DTS and TrueHD only work in direct play.
    multi_ok = bool(caps.get("multichannel")) and (a or {}).get("codec") in ("ac3", "eac3", "aac", "flac", "opus")
    acopy = bool(a and a["codec"] in caps["audio"] and (channels <= 2 or multi_ok))
    audio_direct = a is None or (a["codec"] in caps["audio"] and (channels <= 2 or caps.get("multichannel")))
    container = (probe.get("container") or "").split(",")
    # browser: MP4 only in direct play (MKV goes through remux); native player: any container it can read
    direct_container = bool(set(container) & set(caps.get("containers") or ()) & ({"mp4", "mov", "matroska", "webm", "mpegts"} if native else {"mp4", "mov"}))
    first_audio = audios[0]["index"] if audios else None
    # the native player picks its audio track in the file itself; the browser only plays the first one
    if vcopy and direct_container and (audio_direct if native else (a is None or (acopy and a["index"] == first_audio))):
        mode = "direct"
    elif vcopy:
        mode = "copy"
    else:
        mode = "transcode"
    if target:
        height, kbps = min(src_h or target[0], target[0]), target[1]
    else:
        height = min(src_h or 1080, 1080)  # without a GPU, no transcoded 4K
        kbps = 10000 if height >= 1080 else 6000 if height >= 720 else 3000
    reasons = []
    if mode == "transcode":
        if v and not video_ok(v, caps):
            depth = _bit_depth(v)
            reasons.append(f"vidéo {str(v.get('codec')).upper()}{' 10 bits' if depth == 10 else ''} "
                           "illisible par cet appareil")
        if burn_index is not None:
            reasons.append("sous-titres incrustés")
        if need_scale or over_rate:
            reasons.append(f"qualité {quality}")
    hdr = (v or {}).get("transfer") if (v or {}).get("transfer") in HDR_TRANSFERS else None
    if mode == "transcode" and hdr and tonemap != "off" and media.has_filter("zscale"):
        reasons.append("HDR converti en SDR")
    if mode != "direct" and a and not acopy:
        if (a.get("channels") or 2) > 2:
            reasons.append(f"audio {str(a['codec']).upper()} {a['channels']} canaux mixé en stéréo")
        else:
            reasons.append(f"audio {str(a['codec']).upper()} converti")
    fps = (v or {}).get("fps") or 0
    seglen = float(SEGMENT)
    if mode == "transcode" and fps and abs(fps - math.floor(fps + 0.001)) > 0.001:
        # 23.976 fps: the forced GOP lasts 6.006 s, the playlist must say so
        seglen = math.ceil(SEGMENT * 1000 * math.ceil(fps) / fps) / 1000
    return {"mode": mode, "video_index": (v or {}).get("index"), "video_codec": (v or {}).get("codec"),
            "audio_index": a["index"] if a else None, "acopy": acopy, "audio_codec": (a or {}).get("codec"),
            "audio_channels": (a or {}).get("channels") or 2, "audio_filter": audio_filter(a, algo, boost) if a else "",
            "burn_index": burn_index, "height": height, "kbps": kbps, "src_kbps": rate_kbps, "src_height": src_h,
            "src_width": (v or {}).get("width") or 0, "fps": fps, "seglen": seglen, "reasons": reasons,
            "hdr": hdr, "tonemap": tonemap if tonemap in TONEMAPS else "hable"}


# Hardware transcoding, chosen by the administrator. Decoding by the card (automatic software fallback if
# it does not know the codec), filters on the processor (scale, burnt-in subtitles), encoding by the
# card. shortcut: frames brought back to memory between decoding and encoding; an all-GPU pipeline
# (scale_cuda / scale_vaapi) to do if the processor stays the bottleneck.
HW_ENGINES = ("none", "nvenc", "vaapi")
# Faster x264 analysis for real-time conversion (the same trade-off Jellyfin makes): simplest sub-pixel
# refinement, hexagon motion search, short rate-control lookahead.
X264_FAST = "subme=0:me_range=16:rc_lookahead=10:me=hex:open_gop=0"


def hw_input(hw):
    if hw["engine"] == "nvenc":
        return ["-hwaccel", "cuda"]
    return ["-init_hw_device", f"vaapi=va:{hw['device']}", "-hwaccel", "vaapi", "-hwaccel_device", "va",
            "-filter_hw_device", "va"]


def video_encoder(hw, k, gop, seglen):
    keys = ["-force_key_frames:0", f"expr:gte(t,n_forced*{seglen:g})", "-g:v:0", str(gop)]
    rate = ["-maxrate", f"{k}k", "-bufsize", f"{k * 2}k", "-profile:v", "high", "-level:v", "4.1"]
    if not hw:
        # X264_FAST on top of "veryfast": about 20 % faster on 2 cores, same quality (SSIM measured on the benchmark)
        return ["-codec:v:0", "libx264", "-preset", "veryfast", "-crf", "23", "-x264opts:0", X264_FAST, *rate, *keys,
                "-sc_threshold:v:0", "0", "-keyint_min:v:0", str(gop)]
    if hw["engine"] == "nvenc":
        return ["-codec:v:0", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0", *rate, *keys,
                "-forced-idr", "1", "-no-scenecut", "1"]
    return ["-codec:v:0", "h264_vaapi", "-rc_mode", "VBR", "-b:v", f"{k * 3 // 4}k", *rate, *keys]


def hw_devices():
    """Cards seen by the container: NVIDIA (driver loaded) and VAAPI devices (Intel, AMD)."""
    return {"nvidia": os.path.exists("/dev/nvidia0") and os.path.exists("/dev/nvidiactl"),
            "dri": sorted(glob.glob("/dev/dri/renderD*"))}


def hw_test(hw, tmp_dir):
    """Encodes 2 s of test picture with the card, like a real playback. Raises MediaError otherwise."""
    out = os.path.join(tmp_dir, f"hwtest-{os.getpid()}.mp4")
    fmt = "format=nv12,hwupload" if hw["engine"] == "vaapi" else "format=yuv420p"
    try:
        media.run([media.FFMPEG, "-hide_banner", "-v", "error", "-y", *hw_input(hw), "-f", "lavfi",
                   "-i", "testsrc2=size=1280x720:rate=25", "-t", "2", "-vf", fmt,
                   *video_encoder(hw, 4000, 50, 2), out], timeout=60)
        if not os.path.getsize(out):
            raise media.MediaError("aucune image produite")
    except OSError as e:
        raise media.MediaError(str(e))
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def media_args(p, hw=None, seglen=SEGMENT):
    """Video and audio of a plan (copy or conversion, scale, burnt-in subtitles, graphics card):
    shared by the playback stream (HLS) and downloads (MP4)."""
    vi = p["video_index"]
    if p["mode"] == "copy":
        out = ["-map", f"0:{vi}", "-codec:v:0", "copy"]
        if p.get("video_codec") == "hevc":
            out += ["-tag:v:0", "hvc1"]
    else:
        scale = f"scale=-2:{p['height']}:flags=bicubic," if p["src_height"] > p["height"] else ""
        fmt = "format=nv12,hwupload" if hw and hw["engine"] == "vaapi" else "format=yuv420p"
        tm = tonemap_filter(p)  # after downscaling: fewer pixels to convert
        if p["burn_index"] is not None:
            # HDR: converted before the subtitles are laid on top (otherwise they would be converted with the video)
            base = f"[0:{vi}]{tm.rstrip(',')}[b];[b]" if tm else f"[0:{vi}]"
            out = ["-filter_complex",
                   f"[0:{p['burn_index']}]format=yuva420p[s];"
                   f"{base}[s]overlay=eof_action=pass:repeatlast=0[o];[o]{scale}{fmt}[v]",
                   "-map", "[v]"]
        else:
            out = ["-map", f"0:{vi}", "-vf", f"{scale}{tm}{fmt}"]
        gop = math.ceil(seglen * (p.get("fps") or 24))
        out += video_encoder(hw, p["kbps"], gop, seglen)
    if p["audio_index"] is not None:
        out += ["-map", f"0:{p['audio_index']}"]
        if p["acopy"]:
            out += ["-codec:a:0", "copy"]
            if p.get("audio_codec") in ("flac", "opus"):
                out += ["-strict", "-2"]
        else:
            out += ["-codec:a:0", "aac", "-ac", "2", "-ab", "192000", "-ar", "48000"]
            if p.get("audio_filter"):
                out += ["-af", p["audio_filter"]]
    else:
        out += ["-an"]
    return out


def build_command(path, p, start, out_dir, start_number=0, threads=0, hw=None):
    """ffmpeg command of an fMP4 HLS stream starting at `start` (seconds) at segment `start_number`.

    threads: decoding and encoding threads (0 = as many as cores). Several transcodes at the same
    time share the cores: each takes fewer, which also greatly reduces its memory."""
    cmd = [media.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "warning", "-y", *INPUT_OPTS]
    if threads and p["mode"] == "transcode":
        cmd += ["-threads", str(threads)]  # decoding
    hw = hw if p["mode"] == "transcode" else None
    if hw:
        cmd += hw_input(hw)
    if p["mode"] == "copy":
        cmd += ["-fflags", "+genpts"]
    if p["burn_index"] is not None and p.get("src_width") and p.get("src_height"):
        # picture subtitles (PGS…): canvas size known in advance ("unspecified size" otherwise)
        cmd += ["-canvas_size", f"{p['src_width']}x{p['src_height']}"]
    if start > 0:
        # In copy mode, ffmpeg steps back ~0.13 s before seeking and landed on the PREVIOUS keyframe
        # (0.5 s margin for the same reason): the target is just after the wanted keyframe.
        ss = start + COPY_SEEK_PAD if p["mode"] == "copy" else start
        cmd += ["-ss", f"{ss:.3f}"]
    cmd += [*media.input_args(path), "-map_metadata", "-1", "-map_chapters", "-1",
            "-threads", str(threads if p["mode"] == "transcode" else 0)]
    seglen = p.get("seglen") or SEGMENT
    cmd += [*media_args(p, hw, seglen), "-start_at_zero"]
    seg_opt = "-hls_segment_options" if media.has_option("hls_segment_options") else "-hls_ts_options"
    cmd += ["-sn", "-dn", "-copyts", "-avoid_negative_ts", "disabled", "-max_muxing_queue_size", "2048",
            "-f", "hls", "-max_delay", "5000000", "-hls_time", f"{seglen:g}", "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", INIT, "-start_number", str(start_number),
            "-hls_segment_filename", os.path.join(out_dir, "%d.m4s"), "-hls_playlist_type", "vod",
            "-hls_list_size", "0", seg_opt, "movflags=+frag_discont+skip_sidx", "-hls_flags", "temp_file",
            os.path.join(out_dir, "ffmpeg.m3u8")]
    return cmd


def rss_mb(proc):
    """Memory really used by an ffmpeg (Linux), in MB."""
    if not proc or proc.poll() is not None:
        return None
    try:
        with open(f"/proc/{proc.pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


def system_memory():
    """Memory and swap of the machine (Linux), in MB."""
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) // 1024
    except (OSError, ValueError):
        return {}
    return {"mem_total": info.get("MemTotal"), "mem_available": info.get("MemAvailable"),
            "swap_total": info.get("SwapTotal"), "swap_used": (info.get("SwapTotal") or 0) - (info.get("SwapFree") or 0)}


class Session:
    def __init__(self, sid, path, p, segments, out_dir, hw=None):
        self.sid = sid
        self.hw = hw                # hardware transcoding ({engine, device}) or None
        self.path = path
        self.plan = p
        self.segments = segments    # [(start, duration)] in seconds
        self.dir = out_dir
        self.proc = None
        self.run_start = 0          # first segment of the running ffmpeg
        self.paused = False
        self.active = 0.0           # seconds of real ffmpeg work (pauses left out)
        self.speed = None           # production speed (× real time)
        self.last_req = 0
        self.last_access = time.time()
        self.log_file = None
        self.start_index = 0
        self.lock = threading.Lock()

    def produced(self):
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        return sorted(int(m.group(1)) for m in map(SEG_RE.match, names) if m)


class Transcoder:
    def __init__(self, work_dir, limits, hw=lambda: None):
        self.work_dir = work_dir
        self.limits = limits  # callable -> (max_streams, max_transcodes)
        self.hw = hw          # callable -> chosen hardware transcoding ({engine, device}) or None
        self.sessions = {}
        self.lock = threading.Lock()
        shutil.rmtree(work_dir, ignore_errors=True)
        os.makedirs(work_dir, exist_ok=True)
        self._stop = threading.Event()
        threading.Thread(target=self._throttle_loop, daemon=True).start()

    def threads_per_transcode(self):
        """Cores shared between the transcodes allowed at the same time (at least 2 each)."""
        cpus = os.cpu_count() or 2
        _, max_transcodes = self.limits()
        return max(2, cpus // max(1, max_transcodes)) if max_transcodes > 1 else 0

    def counts(self):
        with self.lock:
            live = [s for s in self.sessions.values() if s.proc]
            return len(live), sum(1 for s in live if s.plan["mode"] == "transcode")

    def start(self, sid, path, p, offset_ms, duration_ms=0, keyframes=None):
        """Prepares a stream and starts ffmpeg at the segment of `offset_ms`. -> start position (ms)."""
        if p["mode"] == "direct":
            return offset_ms
        max_streams, max_transcodes = self.limits()
        n, nt = self.counts()
        if n >= max_streams:
            raise Busy(f"Trop de lectures simultanées ({n}/{max_streams}).")
        if p["mode"] == "transcode" and nt >= max_transcodes:
            raise Busy(f"Trop de transcodages simultanés ({nt}/{max_transcodes}) : le processeur est déjà occupé.")
        duration = (duration_ms or 0) / 1000
        if p["mode"] == "copy" and keyframes:
            kf_dur, kf = keyframes
            segments = segments_from_keyframes(kf, duration or kf_dur, p.get("seglen") or SEGMENT)
        else:
            segments = equal_segments(duration or 3600 * 4, p.get("seglen") or SEGMENT)
        out_dir = os.path.join(self.work_dir, sid)
        os.makedirs(out_dir, exist_ok=True)
        s = Session(sid, path, p, segments, out_dir, self.hw() if p["mode"] == "transcode" else None)
        with self.lock:
            self.sessions[sid] = s
        first = self.index_at(s, offset_ms / 1000)
        s.start_index = first
        with s.lock:
            self._spawn(s, first)
        return offset_ms

    @staticmethod
    def index_at(s, seconds):
        lo, hi, found = 0, len(s.segments) - 1, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if s.segments[mid][0] <= seconds + 0.001:
                found = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return found

    def _kill(self, s):
        if s.proc and s.proc.poll() is None:
            try:
                os.kill(s.proc.pid, signal.SIGCONT)
                s.proc.terminate()
                s.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    s.proc.kill()
                    s.proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        s.paused = False

    def _spawn(self, s, index):
        """(Re)starts ffmpeg to produce from segment `index` (session lock held)."""
        self._kill(s)
        for i in s.produced():
            if i >= index:  # segments of another pass: two cuttings are not mixed
                try:
                    os.remove(os.path.join(s.dir, f"{i}.m4s"))
                except OSError:
                    pass
        start = s.segments[index][0] if s.segments else 0
        cmd = build_command(s.path, s.plan, start, s.dir, index, self.threads_per_transcode(), s.hw)
        log.debug("ffmpeg %s", " ".join(cmd))
        if s.log_file:
            s.log_file.close()
        s.log_file = open(os.path.join(s.dir, "ffmpeg.log"), "ab")
        s.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=s.log_file,
                                  preexec_fn=lambda: os.nice(5))
        s.run_start = index
        s.last_req = index
        s.active = 0.0
        s.speed = None

    def get(self, sid):
        with self.lock:
            return self.sessions.get(sid)

    def _log_tail(self, s):
        try:
            with open(os.path.join(s.dir, "ffmpeg.log"), "rb") as f:
                lines = f.read().decode("utf-8", "replace").strip().splitlines()
            return lines[-1] if lines else "ffmpeg s'est arrêté"
        except OSError:
            return "ffmpeg s'est arrêté"

    def playlist(self, sid):
        """Complete VOD playlist."""
        s = self.get(sid)
        if not s:
            raise StreamError("Flux expiré")
        s.last_access = time.time()
        target = math.ceil(max((d for _, d in s.segments), default=SEGMENT))
        lines = ["#EXTM3U", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{target}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-INDEPENDENT-SEGMENTS", f'#EXT-X-MAP:URI="{INIT}"']
        for n, (_, d) in enumerate(s.segments):
            lines += [f"#EXTINF:{d:.6f},", f"{n}.m4s"]
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def _wait_file(self, s, path, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.isfile(path):
                return path
            proc = s.proc
            if proc and proc.poll() is not None and proc is s.proc:
                time.sleep(0.2)  # last rename of the temporary file
                if os.path.isfile(path):
                    return path
                if proc is s.proc and s.hw:
                    # the graphics card refused (driver, codec, memory): back to the processor
                    log.warning("Transcodage matériel (%s) en échec, repli sur le processeur : %s",
                                s.hw["engine"], self._log_tail(s))
                    with s.lock:
                        if proc is s.proc:
                            s.hw = None
                            self._spawn(s, s.run_start)
                    continue
                if proc is s.proc:
                    raise StreamError("Échec de la conversion : " + self._log_tail(s))
            if s.sid not in self.sessions:
                raise StreamError("Flux arrêté")
            time.sleep(0.1)
        raise StreamError("La conversion est trop lente")

    def init(self, sid, timeout=90):
        s = self.get(sid)
        if not s:
            raise StreamError("Flux expiré")
        s.last_access = time.time()
        with s.lock:
            if not s.proc:
                self._spawn(s, s.start_index)
            first = s.run_start
        # the header is written in one go but without a temporary file: wait for the first segment
        # of the pass (the header is then complete)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(i >= first for i in s.produced()) or (s.proc and s.proc.poll() is not None):
                break
            time.sleep(0.1)
        return self._wait_file(s, os.path.join(s.dir, INIT), max(1, deadline - time.time()))

    def segment(self, sid, name, timeout=90):
        m = SEG_RE.match(name)
        s = self.get(sid)
        if not m or not s:
            raise StreamError("Segment introuvable")
        idx = int(m.group(1))
        if idx >= len(s.segments):
            raise StreamError("Segment introuvable")
        s.last_access = time.time()
        path = os.path.join(s.dir, name)
        with s.lock:
            s.last_req = idx
            if not os.path.isfile(path):
                produced = [i for i in s.produced() if i >= s.run_start]
                current = produced[-1] if produced else s.run_start - 1
                running = s.proc and s.proc.poll() is None
                if not running or idx < s.run_start or idx > current + RESTART_GAP:
                    # the player jumped: ffmpeg is started again where it asks
                    self._spawn(s, idx)
            if s.paused:
                self._resume(s)
        return self._wait_file(s, path, timeout)

    def touch(self, sid):
        s = self.get(sid)
        if s:
            s.last_access = time.time()

    def _resume(self, s):
        try:
            os.kill(s.proc.pid, signal.SIGCONT)
        except OSError:
            pass
        s.paused = False

    def stop(self, sid):
        with self.lock:
            s = self.sessions.pop(sid, None)
        if not s:
            return
        with s.lock:
            self._kill(s)
        if s.log_file:
            s.log_file.close()
        shutil.rmtree(s.dir, ignore_errors=True)

    def stop_all(self):
        self._stop.set()
        for sid in list(self.sessions):
            self.stop(sid)

    def stats(self, sid):
        s = self.get(sid)
        if not s:
            return {}
        try:
            with open(os.path.join(s.dir, "ffmpeg.log"), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 1500))
                warnings = f.read().decode("utf-8", "replace").strip().splitlines()[-4:]
        except OSError:
            warnings = []
        return {"speed": s.speed, "paused": s.paused, "warnings": warnings, "rss_mb": rss_mb(s.proc),
                "hw": s.hw["engine"] if s.hw else None}

    def processes(self):
        """Running ffmpegs: count and total memory (MB)."""
        with self.lock:
            procs = [s.proc for s in self.sessions.values() if s.proc and s.proc.poll() is None]
        return {"ffmpeg": len(procs), "ffmpeg_mb": sum(rss_mb(p) or 0 for p in procs)}

    def idle(self, max_idle):
        now = time.time()
        with self.lock:
            return [sid for sid, s in self.sessions.items() if now - s.last_access > max_idle]

    def _throttle_loop(self):
        while not self._stop.wait(2):
            with self.lock:
                sessions = list(self.sessions.values())
            for s in sessions:
                if not s.proc or s.proc.poll() is not None:
                    continue
                produced = s.produced()
                mine = [i for i in produced if i >= s.run_start]
                if not s.paused:
                    s.active += 2
                if mine and s.active >= 10 and not s.paused:
                    done = sum(d for _, d in s.segments[s.run_start:mine[-1] + 1])
                    s.speed = round(done / s.active, 2)
                ahead = (mine[-1] if mine else s.run_start) - s.last_req
                if not s.paused and ahead > AHEAD_PAUSE:
                    try:
                        os.kill(s.proc.pid, signal.SIGSTOP)
                        s.paused = True
                    except OSError:
                        pass
                elif s.paused and ahead < AHEAD_RESUME:
                    self._resume(s)
                for i in produced:
                    if i >= s.last_req - KEEP_BEHIND:
                        break
                    try:
                        os.remove(os.path.join(s.dir, f"{i}.m4s"))
                    except OSError:
                        pass
