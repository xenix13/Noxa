"""Builds the documentation demo library from free films: Blender Foundation open movies (Creative Commons)
and public domain classics, downloaded from Wikimedia Commons.

python3 tools/screenshots/free_library.py <downloads_folder> <posters_folder> <library_folder> [<photos_folder>]

- <downloads_folder>: the 480p WebM versions from Wikimedia Commons, named "<file>.webm" as in free_titles.json
- <posters_folder>: one poster per title, named "<title>.jpg" (or .png)
- <library_folder>: receives Movies/ and Shows/, laid out like a real library, with Kodi-style .nfo files
- <photos_folder> (optional): the CC0 photos listed in free_titles.json, named as on Commons; they are copied
  into Photos/<album>/ and dated (file date) when they were taken, since Commons thumbnails drop the EXIF data

The Blender films are re-encoded to 1080p H.264 (they were made in HD; the 480p copies only save download time).
Classics stay as they are. Backdrops are taken from a frame of each film.
"""
import json
import os
import shutil
import time
import subprocess
import sys
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))


def ff(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip() or 0)


def video(src, dst, upscale):
    if os.path.exists(dst):
        return
    tmp = dst + ".part.mkv"
    if upscale:
        ff("-i", src, "-map", "0:v:0", "-map", "0:a?", "-vf", "scale=-2:1080:flags=lanczos,format=yuv420p",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "160k",
           "-metadata:s:a", "title=", tmp)  # the source title ("AC3 5.1 @ 640 Kbps") no longer matches
    else:
        ff("-i", src, "-map", "0", "-c", "copy", tmp)
    os.replace(tmp, dst)


def backdrop(src, dst, at):
    if not os.path.exists(dst):
        ff("-ss", f"{at:.1f}", "-i", src, "-frames:v", "1", "-vf", "scale=1920:-2:flags=lanczos", "-q:v", "3", dst)


def landscape(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", path], capture_output=True, text=True, check=True).stdout
    w, h = (int(x) for x in out.strip().split(",")[:2])
    return w > h


def poster(posters, title, dst):
    for ext in (".jpg", ".jpeg", ".png"):
        p = os.path.join(posters, title + ext)
        if os.path.exists(p):
            # Landscape artwork: centre it on a blurred copy of itself, so the 2:3 poster keeps the whole title
            vf = ("scale=600:-2:flags=lanczos" if not landscape(p) else
                  "split[a][b];[a]scale=600:900:force_original_aspect_ratio=increase,crop=600:900,boxblur=20,eq=brightness=-0.25[bg];"
                  "[b]scale=600:-2:flags=lanczos[fg];[bg][fg]overlay=0:(H-h)/2")
            ff("-i", p, "-filter_complex" if landscape(p) else "-vf", vf, "-q:v", "3", dst)
            return True
    return False


def nfo(path, root, fields):
    lines = ["<?xml version='1.0' encoding='utf-8'?>", f"<{root}>"]
    for k, v in fields:
        if isinstance(v, dict):
            lines.append(f"  <{k}>" + "".join(f"<{a}>{escape(str(b))}</{a}>" for a, b in v.items()) + f"</{k}>")
        elif v not in (None, ""):
            lines.append(f"  <{k}>{escape(str(v))}</{k}>")
    lines.append(f"</{root}>")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def photos(src, root, items):
    for p in items:
        f = os.path.join(src, p["file"])
        if not os.path.exists(f):
            print("missing:", f)
            continue
        d = os.path.join(root, "Photos", p["album"])
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, p["file"])
        shutil.copyfile(f, dst)
        t = time.mktime(time.strptime(p["date"], "%Y-%m-%d %H:%M:%S"))
        os.utime(dst, (t, t))
    print("photos:", len(items), flush=True)


def build(raw, posters, root, photo_src=None):
    data = json.load(open(os.path.join(HERE, "free_titles.json")))
    for m in data["movies"]:
        src = os.path.join(raw, m["file"] + ".webm")
        if not os.path.exists(src):
            print("missing:", src)
            continue
        name = f"{m['title']} ({m['year']})"
        d = os.path.join(root, "Movies", name)
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, name + ".mkv")
        video(src, dst, m.get("upscale"))
        backdrop(src, os.path.join(d, "fanart.jpg"), duration(src) * 0.3)
        if not poster(posters, m["title"], os.path.join(d, "poster.jpg")):
            print("no poster:", m["title"])
        fields = [("title", m["title"]), ("year", m["year"]), ("plot", m["plot"]), ("mpaa", m.get("mpaa")),
                  ("studio", m.get("studio"))]
        fields += [("genre", g) for g in m["genres"]] + [("director", p) for p in m.get("directors", [])]
        nfo(os.path.join(d, "movie.nfo"), "movie", fields)
        print("movie:", name, flush=True)
    for s in data["shows"]:
        d = os.path.join(root, "Shows", f"{s['title']} ({s['year']})")
        sd = os.path.join(d, "Season 01")
        os.makedirs(sd, exist_ok=True)
        first = None
        for n, e in enumerate(s["episodes"], 1):
            src = os.path.join(raw, e["file"] + ".webm")
            if not os.path.exists(src):
                print("missing:", src)
                continue
            first = first or src
            base = os.path.join(sd, f"{s['title']} - S01E{n:02d} - {e['title']}")
            video(src, base + ".mkv", s.get("upscale"))
            nfo(base + ".nfo", "episodedetails", [("title", e["title"]), ("season", 1), ("episode", n),
                                                  ("plot", e["plot"]), ("aired", e["aired"])])
        if first:
            backdrop(first, os.path.join(d, "fanart.jpg"), duration(first) * 0.4)
        if not poster(posters, s["title"], os.path.join(d, "poster.jpg")):
            print("no poster:", s["title"])
        fields = [("title", s["title"]), ("year", s["year"]), ("plot", s["plot"]), ("mpaa", s.get("mpaa")),
                  ("studio", s.get("studio")), ("premiered", s["episodes"][0]["aired"])]
        fields += [("genre", g) for g in s["genres"]]
        nfo(os.path.join(d, "tvshow.nfo"), "tvshow", fields)
        print("show:", s["title"], flush=True)
    if photo_src:
        photos(photo_src, root, data.get("photos", []))


if __name__ == "__main__":
    build(*sys.argv[1:5])
