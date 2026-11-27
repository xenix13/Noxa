#!/usr/bin/env bash
# Prepares the system for Noxa: user, folders, configuration, systemd services, graphics card.
# Shared by install.sh (install from GitHub) and the .deb package (postinst). As root.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/seance}"
DATA_DIR="${DATA_DIR:-/var/lib/seance}"
CONF_DIR="${CONF_DIR:-/etc/seance}"
UNITS="$APP_DIR/deploy"

echo "==> System user 'seance'"
if ! id seance >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin seance
fi

mkdir -p "$DATA_DIR" "$CONF_DIR" /mnt/seance
chown -R root:root "$APP_DIR"
chmod -R a+rX "$APP_DIR"
chown -R seance:seance "$DATA_DIR"
chmod 700 "$DATA_DIR"

if [ ! -f "$CONF_DIR/seance.env" ]; then
  cat > "$CONF_DIR/seance.env" <<'ENVFILE'
# Port Noxa listens on
SEANCE_PORT=8420
SEANCE_HOST=0.0.0.0
# 1 if Noxa is served over HTTPS behind a reverse proxy (Caddy, nginx, Nginx Proxy Manager…)
SEANCE_COOKIE_SECURE=0
# 1 to read the visitors' real address from X-Forwarded-For
# (only if Noxa can be reached through that proxy only)
SEANCE_TRUST_PROXY=0
# Detailed logs
SEANCE_DEBUG=0
# Linked servers port (TLS link between your Noxa servers, opened with the first invitation)
SEANCE_FED_PORT=8421
ENVFILE
fi

echo "==> Admin command \"noxa\""
cat > /usr/local/bin/noxa <<CMD
#!/bin/sh
# Noxa rescue commands, as root:
#   noxa list-users                 accounts and the state of their two-factor authentication
#   noxa reset-2fa <username>       turns off two-factor authentication for an account
if [ "\$(id -u)" -ne 0 ]; then echo "Run as root: sudo noxa …" >&2; exit 1; fi
cd "$APP_DIR" && exec runuser -u seance -- /usr/bin/python3 -m noxa --data "$DATA_DIR" "\$@"
CMD
chmod 755 /usr/local/bin/noxa
ln -sf noxa /usr/local/bin/nexis  # former name of the command (Nexis)

echo "==> systemd service"
cp "$UNITS/seance.service" /etc/systemd/system/seance.service
cp "$UNITS/seance-mountd.service" /etc/systemd/system/seance-mountd.service
# Graphics card (hardware transcoding): the service must see /dev/nvidia* or /dev/dri, hidden otherwise
if [ -e /dev/nvidia0 ] || ls /dev/dri/renderD* >/dev/null 2>&1; then
  echo "==> Graphics card found: the service can use it (hardware transcoding, to turn on in Settings)"
  GPU_GROUPS=""
  for g in video render; do getent group "$g" >/dev/null && GPU_GROUPS="$GPU_GROUPS $g"; done
  mkdir -p /etc/systemd/system/seance.service.d
  {
    echo "[Service]"
    echo "PrivateDevices=false"
    [ -n "$GPU_GROUPS" ] && echo "SupplementaryGroups=$GPU_GROUPS"
  } > /etc/systemd/system/seance.service.d/gpu.conf
else
  rm -f /etc/systemd/system/seance.service.d/gpu.conf
fi
systemctl daemon-reload
systemctl enable seance-mountd seance >/dev/null 2>&1
systemctl restart seance-mountd || true
systemctl restart seance || true
sleep 2

# Unprivileged LXC container without "nesting": the systemd sandbox fails (226/NAMESPACE).
if ! systemctl is-active --quiet seance && \
   [ "$(systemctl show -p ExecMainStatus --value seance)" = "226" ]; then
  echo "==> systemd sandbox not available in this container: the service runs with fewer protections"
  mkdir -p /etc/systemd/system/seance.service.d
  cat > /etc/systemd/system/seance.service.d/lxc.conf <<'DROPIN'
[Service]
PrivateTmp=false
PrivateDevices=false
ProtectSystem=false
ProtectHome=false
ProtectKernelTunables=false
ProtectKernelModules=false
ProtectControlGroups=false
ProtectHostname=false
ProtectClock=false
ProtectKernelLogs=false
RestrictNamespaces=false
ReadWritePaths=
DROPIN
  systemctl daemon-reload
  systemctl restart seance || true
  sleep 2
fi

if ! systemctl is-active --quiet seance; then
  echo "The service did not start. See: journalctl -u seance -n 50" >&2
  exit 1
fi

PORT=$(grep -E '^SEANCE_PORT=' "$CONF_DIR/seance.env" | cut -d= -f2 || true)
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if ! systemctl is-active --quiet seance-mountd; then
  echo "Warning: the mount service seance-mountd did not start (journalctl -u seance-mountd -n 30)."
fi
if [ -r /proc/self/uid_map ] && [ "$(awk 'NR==1{print $2}' /proc/self/uid_map)" != "0" ]; then
  echo
  echo "Note: this LXC container is UNPRIVILEGED. Linux forbids SMB mounts in it:"
  echo "the Network shares page can list the NAS shares but not mount them."
  echo "Fix: a privileged container with \"features: mount=cifs\", or mount the shares on the host (README)."
fi
echo
echo "Noxa is running at http://${IP:-<ip>}:${PORT:-8420}"
for d in /mnt/*/ /mnt/seance/*/; do
  [ -d "$d" ] || continue
  if ! runuser -u seance -- ls "$d" >/dev/null 2>&1; then
    echo "Warning: the 'seance' user cannot read $d (mount permissions)."
  fi
done
if [ -f "$DATA_DIR/setup-code.txt" ]; then
  echo "Setup code (asked on the first page): $(cat "$DATA_DIR/setup-code.txt")"
fi
