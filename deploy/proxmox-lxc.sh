#!/usr/bin/env bash
# Creates an Ubuntu 24.04 LXC container ready for Noxa and installs it. Run on the Proxmox VE HOST, as root:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/xenix13/Noxa/main/deploy/proxmox-lxc.sh)"
# Settings (environment variables): CTID, CT_HOSTNAME, STORAGE, TEMPLATE_STORAGE, BRIDGE,
# CORES, MEMORY (MB), SWAP (MB), DISK (GB), PRIVILEGED (1 = SMB shares can be mounted from Noxa), SEANCE_BRANCH.
set -euo pipefail

command -v pct >/dev/null || { echo "Run this on a Proxmox VE host (pct command not found)." >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "Run this script as root." >&2; exit 1; }

CTID="${CTID:-$(pvesh get /cluster/nextid)}"
CT_HOSTNAME="${CT_HOSTNAME:-noxa}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
CORES="${CORES:-4}"
MEMORY="${MEMORY:-4096}"
SWAP="${SWAP:-2048}"          # a 4K transcode takes 0.5 to 0.7 GB: some margin avoids stutter
DISK="${DISK:-16}"
PRIVILEGED="${PRIVILEGED:-1}"
BRANCH="${SEANCE_BRANCH:-main}"

echo "==> Ubuntu 24.04 template"
pveam update >/dev/null
TEMPLATE=$(pveam available --section system | awk '/ubuntu-24\.04-standard/ {print $2}' | sort -V | tail -1)
[ -n "$TEMPLATE" ] || { echo "ubuntu-24.04-standard template not found (pveam available)." >&2; exit 1; }
pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE" || pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"

if [ "$PRIVILEGED" = "1" ]; then
  UNPRIV=0; FEATURES="nesting=1,mount=cifs"   # privileged: Noxa can mount SMB shares itself
else
  UNPRIV=1; FEATURES="nesting=1"              # unprivileged: shares are mounted on the host (README)
fi

echo "==> Container $CTID ($CT_HOSTNAME): $CORES cores, $MEMORY MB of memory, $SWAP MB of swap, $DISK GB"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$CT_HOSTNAME" --ostype ubuntu --cores "$CORES" --memory "$MEMORY" --swap "$SWAP" \
  --rootfs "$STORAGE:$DISK" --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
  --unprivileged "$UNPRIV" --features "$FEATURES" --onboot 1 --start 1

echo "==> Waiting for the network"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts raw.githubusercontent.com >/dev/null 2>&1 && break
  sleep 2
done

echo "==> Installing Noxa in the container"
pct exec "$CTID" -- bash -c "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates >/dev/null \
  && curl -fsSL https://raw.githubusercontent.com/xenix13/Noxa/$BRANCH/deploy/install.sh | SEANCE_BRANCH=$BRANCH bash"

echo
echo "Container $CTID is ready. For a graphics card (hardware transcoding), follow the \"Hardware transcoding\""
echo "section of the installation guide, then run the Noxa update again from its interface."
