"""Updating Noxa from the administration interface.

- Noxa (without privileges) checks on GitHub whether a newer version exists: check_latest().
- The root service seance-mountd starts the update: the same command as the manual install
  (`curl …/deploy/install.sh | bash`), from the repository and branch written by install.sh in
  /etc/seance/source. No address or command comes from the interface.
"""
import json
import os
import re
import shlex
import subprocess
import time
import urllib.request
from . import env

SOURCE = env("SOURCE_FILE", "/etc/seance/source")
LOG = env("UPDATE_LOG", "/var/log/seance-update.log")
UNIT = "seance-update"
DEFAULT_REPO, DEFAULT_BRANCH = "https://github.com/xenix13/Noxa.git", "main"
REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100}?)(?:\.git)?$")
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
VERSION = re.compile(r'__version__\s*=\s*"([0-9][0-9A-Za-z.+-]{0,30})"')
CHECK_EVERY = 1800


class UpdateError(Exception):
    pass


def read_source():
    """(owner, repository, branch) this installation comes from."""
    vals = {}
    try:
        with open(SOURCE, encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                vals[k] = v
    except OSError:
        pass
    repo, branch = vals.get("SEANCE_REPO") or DEFAULT_REPO, vals.get("SEANCE_BRANCH") or DEFAULT_BRANCH
    m = REPO.match(repo)
    if not m or not BRANCH.match(branch) or ".." in branch:
        raise UpdateError("Source de mise à jour invalide dans /etc/seance/source")
    return m.group(1), m.group(2), branch


CHANNELS = {"main": "Stable", "test": "Test"}


def set_channel(branch):
    """Root side: changes the followed branch (same repository). Only known branches are accepted."""
    if branch not in CHANNELS:
        raise UpdateError("Canal inconnu")
    owner, repo, _ = read_source()
    tmp = SOURCE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"SEANCE_REPO=https://github.com/{owner}/{repo}.git\nSEANCE_BRANCH={branch}\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, SOURCE)
    forget()
    return {"branch": branch}


def _version_key(v):
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.+-]", v)]


def newer(latest, current):
    return _version_key(latest) > _version_key(current)


_cache = {}


def forget():
    _cache.clear()


def check_latest(timeout=8):
    """Latest version published on the followed branch, and its latest changes (kept 30 min)."""
    owner, repo, branch = read_source()
    key = (owner, repo, branch)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CHECK_EVERY:
        return hit[1]
    headers = {"User-Agent": "Noxa"}
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/noxa/__init__.py"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            m = VERSION.search(r.read(4096).decode("utf-8", "replace"))
    except OSError as e:
        raise UpdateError(f"GitHub injoignable ({getattr(e, 'reason', e)})")
    if not m:
        raise UpdateError("Version introuvable sur GitHub")
    changes = []
    try:  # titles of the latest changes; optional (GitHub API limit)
        api = f"https://api.github.com/repos/{owner}/{repo}/commits?sha={branch}&per_page=8"
        with urllib.request.urlopen(urllib.request.Request(api, headers=headers), timeout=timeout) as r:
            for c in json.loads(r.read(400_000)):
                msg = (c.get("commit") or {}).get("message") or ""
                changes.append({"title": msg.splitlines()[0][:200] if msg else "",
                                "date": ((c.get("commit") or {}).get("committer") or {}).get("date")})
    except (OSError, ValueError):
        pass
    out = {"latest": m.group(1), "changes": changes, "source": f"{owner}/{repo} ({branch})", "branch": branch}
    _cache[key] = (time.time(), out)
    return out


# ------------------------------------------------------------------ root side (seance-mountd)
def running():
    # "oneshot" unit: it stays "activating" during the whole installation (never "active")
    p = subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", UNIT],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return p.stdout.strip() in ("active", "activating", "reloading", "deactivating")


def start():
    """Starts the up-to-date installation in a separate systemd unit: it survives the restart of
    seance-mountd and Noxa that it causes."""
    if running():
        raise UpdateError("Une mise à jour est déjà en cours")
    owner, repo, branch = read_source()
    script = (f"set -o pipefail; curl -fsSL {shlex.quote(f'https://raw.githubusercontent.com/{owner}/{repo}/{branch}/deploy/install.sh')}"
              f" | SEANCE_REPO={shlex.quote(f'https://github.com/{owner}/{repo}.git')} SEANCE_BRANCH={shlex.quote(branch)} bash")
    # --no-block: without it, systemd-run waits for the END of the installation (oneshot unit) and the request times out
    cmd = ["systemd-run", f"--unit={UNIT}", "--collect", "--quiet", "--no-block", "--property=Type=oneshot", "/bin/bash", "-c",
           f"{{ date; {script}; echo \"[terminé, code $?]\"; }} > {shlex.quote(LOG)} 2>&1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise UpdateError("systemd est introuvable : mise à jour automatique impossible sur cette installation (Docker : mettre à jour l'image)")
    if p.returncode != 0:
        raise UpdateError("Lancement impossible : " + (p.stderr.decode("utf-8", "replace").strip()[-300:] or "erreur"))
    return {"started": True}


# Steps shown by install.sh / setup-system.sh ("==> …"): enough to show a progress.
# Messages in English since 4.1.3, in French before (a server can start the script of another version).
STEPS = [(("System packages", "Paquets système"), 10, "Mise à jour des paquets système"),
         (("Fetching the code", "Récupération du code"), 25, "Téléchargement de la nouvelle version"),
         (("Copying the application", "Copie de l'application"), 45, "Installation des fichiers"),
         (("Checking the bundled libraries", "Vérification des bibliothèques"), 55, "Vérification des fichiers"),
         (("System user", "Utilisateur système"), 65, "Configuration du système"),
         (("systemd service", "Service systemd"), 80, "Redémarrage de Noxa")]
END = re.compile(r"^\[terminé, code (\d+)\]")


def progress(lines):
    """Where the installation stands according to its log: (percentage, step, finished, succeeded)."""
    percent, label, done, ok = 3, "Démarrage", False, None
    for line in lines:
        m = END.match(line)
        if m:
            done, ok = True, m.group(1) == "0"
            percent, label = (100, "Terminé") if ok else (percent, "Échec")
            continue
        if line.startswith("==> "):
            for keys, pct, text in STEPS:
                if line[4:].startswith(keys) and pct >= percent:
                    percent, label = pct, text
    return percent, label, done, ok


def status():
    try:
        with open(LOG, "rb") as f:
            data = f.read()[-200_000:]  # whole log (for the steps), only its end is returned
            started = os.fstat(f.fileno()).st_ctime
    except OSError:
        data, started = b"", None
    lines = data.decode("utf-8", "replace").splitlines()
    percent, label, done, ok = progress(lines)
    return {"running": running(), "log": lines[-25:], "percent": percent, "step": label, "done": done, "ok": ok,
            "log_time": started}
