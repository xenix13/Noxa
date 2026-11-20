#!/usr/bin/env bash
# Installs or updates Noxa on Ubuntu / Debian (LXC container or VM).
#
# One command (install AND update), as root:
#   curl -fsSL https://raw.githubusercontent.com/xenix13/Noxa/main/deploy/install.sh | bash
# Or from a clone of the repository:
#   sudo ./deploy/install.sh
set -euo pipefail

REPO_URL="${SEANCE_REPO:-https://github.com/xenix13/Noxa.git}"
BRANCH="${SEANCE_BRANCH:-main}"
APP_DIR=/opt/seance
DATA_DIR=/var/lib/seance
CONF_DIR=/etc/seance
SRC_CACHE=/usr/local/src/seance

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this script as root (sudo)." >&2
  exit 1
fi

echo "==> System packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 ca-certificates curl git ffmpeg cifs-utils smbclient openssl >/dev/null
echo "   $(ffmpeg -version | head -1 | cut -d' ' -f1-3)"

PYV=$(python3 -c 'import sys; print("%d%02d" % sys.version_info[:2])')
if [ "$PYV" -lt 309 ]; then
  echo "Python 3.9 or newer is required (found: $(python3 --version))." >&2
  exit 1
fi

# Run from a clone: use that folder. Run through "curl | bash": clone or update the repository.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
# (never the installed folder itself: /opt/seance/deploy/install.sh would erase itself while copying)
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/install.sh" ] && [ -f "$SCRIPT_DIR/../seance/api.py" ] \
   && [ "$(cd "$SCRIPT_DIR/.." && pwd)" != "$APP_DIR" ]; then
  SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  echo "==> Fetching the code ($REPO_URL, branch $BRANCH)"
  if [ -d "$SRC_CACHE/.git" ]; then
    git -C "$SRC_CACHE" fetch -q --depth 1 origin "$BRANCH"
    git -C "$SRC_CACHE" reset -q --hard FETCH_HEAD
  else
    rm -rf "$SRC_CACHE"
    git clone -q --depth 1 --branch "$BRANCH" "$REPO_URL" "$SRC_CACHE"
  fi
  SRC_DIR="$SRC_CACHE"
fi
echo "   version: $(git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || echo local)"

echo "==> Copying the application to $APP_DIR"
mkdir -p "$APP_DIR" "$DATA_DIR" "$CONF_DIR" /mnt/seance
rm -rf "$APP_DIR/seance" "$APP_DIR/static.new" "$APP_DIR/deploy"
cp -r "$SRC_DIR/seance" "$APP_DIR/seance"
cp -r "$SRC_DIR/static" "$APP_DIR/static.new"
cp -r "$SRC_DIR/deploy" "$APP_DIR/deploy"
rm -rf "$APP_DIR/static"
mv "$APP_DIR/static.new" "$APP_DIR/static"
find "$APP_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +

echo "==> Checking the bundled libraries (hls.js, qrcode, JASSUB, libpgs)"
( cd "$APP_DIR/static/vendor" && sha256sum -c --quiet hls.min.js.sha256 ) || { echo "hls.js has been altered: installation stopped." >&2; exit 1; }
( cd "$APP_DIR/static/vendor/qrcode" && sha256sum -c --quiet qrcode.mjs.sha256 ) || { echo "qrcode has been altered: installation stopped." >&2; exit 1; }
( cd "$APP_DIR/static/vendor/jassub" && sha256sum -c --quiet jassub.sha256 ) || { echo "JASSUB has been altered: installation stopped." >&2; exit 1; }
( cd "$APP_DIR/static/vendor/libpgs" && sha256sum -c --quiet libpgs.sha256 ) || { echo "libpgs has been altered: installation stopped." >&2; exit 1; }

rm -rf "$APP_DIR/jellyfin-web"  # folder of an old version (removed)

# where this install comes from: updates started from the interface start from there again
printf 'SEANCE_REPO=%s\nSEANCE_BRANCH=%s\n' "$REPO_URL" "$BRANCH" > "$CONF_DIR/source"
chmod 644 "$CONF_DIR/source"

export APP_DIR DATA_DIR CONF_DIR
bash "$APP_DIR/deploy/setup-system.sh"
