#!/usr/bin/env bash
# Builds Noxa's Debian/Ubuntu package: deploy/build-deb.sh [output folder]
# Install: apt install ./noxa_<version>_all.deb   (also installs ffmpeg, python3…)
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="$(realpath "${1:-dist}")"
VERSION=$(python3 -c 'import re; print(re.search(r"__version__ = \"(.+?)\"", open("noxa/__init__.py").read()).group(1))')
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
PKG="$WORK/noxa_${VERSION}_all"

mkdir -p "$PKG/DEBIAN" "$PKG/opt/seance" "$OUT"
cp -r noxa seance static deploy "$PKG/opt/seance/"
find "$PKG" -name __pycache__ -type d -prune -exec rm -rf {} +
( cd "$PKG/opt/seance/static/vendor" && sha256sum -c --quiet hls.min.js.sha256 )
( cd "$PKG/opt/seance/static/vendor/qrcode" && sha256sum -c --quiet qrcode.mjs.sha256 )
( cd "$PKG/opt/seance/static/vendor/jassub" && sha256sum -c --quiet jassub.sha256 )
( cd "$PKG/opt/seance/static/vendor/libpgs" && sha256sum -c --quiet libpgs.sha256 )

cat > "$PKG/DEBIAN/control" <<CONTROL
Package: noxa
Provides: nexis
Replaces: nexis
Conflicts: nexis
Version: $VERSION
Architecture: all
Maintainer: Noxa <https://github.com/xenix13/Noxa>
Depends: python3 (>= 3.9), ffmpeg, ca-certificates, cifs-utils, smbclient, systemd, passwd, openssl
Recommends: curl, git
Section: video
Priority: optional
Homepage: https://github.com/xenix13/Noxa
Description: Noxa - serveur multimédia auto-hébergé
 Films, séries et animes lus dans le navigateur (lecture directe, remux ou
 transcodage), profils, regarder ensemble, TV en direct, demandes via Seerr.
CONTROL

cat > "$PKG/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
  # same preparation as the script install: user, folders, services, graphics card
  bash /opt/seance/deploy/setup-system.sh || echo "Noxa : voir journalctl -u seance -n 50" >&2
fi
POSTINST

cat > "$PKG/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -e
if [ "$1" = "remove" ] && command -v systemctl >/dev/null; then
  systemctl disable --now seance.service seance-mountd.service >/dev/null 2>&1 || true
fi
PRERM

cat > "$PKG/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
  rm -f /etc/systemd/system/seance.service /etc/systemd/system/seance-mountd.service
  rm -rf /etc/systemd/system/seance.service.d
  rm -f /usr/local/bin/noxa /usr/local/bin/nexis
  command -v systemctl >/dev/null && systemctl daemon-reload || true
fi
if [ "$1" = "purge" ]; then
  # "purge" also erases the configuration and the data (database, cache); "remove" keeps them
  rm -rf /etc/seance /var/lib/seance
fi
POSTRM

chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm" "$PKG/DEBIAN/postrm"
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$OUT/noxa_${VERSION}_all.deb" >/dev/null
echo "$OUT/noxa_${VERSION}_all.deb"
