#!/usr/bin/env bash
# Benchmark VM for Proxmox VE: run on the Proxmox host, as root.
#
#   bash proxmox-vm.sh          create a Debian 12 VM and install everything (Docker, the 3 servers, test library)
#   bash proxmox-vm.sh run      run the benchmark in it (about 1 hour) and copy the results back to this host
#   bash proxmox-vm.sh quick    same, short run to check that everything works (about 10 minutes)
#   bash proxmox-vm.sh delete   remove the VM and its files
#
# Settings (environment variables): VMID, STORAGE (VM disk), BRIDGE (vmbr0), CORES (4), MEMORY (8192 MB),
# DISK (24G), NOXA_BENCH_BRANCH (main). Nothing else on the host is changed, except that the "local" storage is
# allowed to hold cloud-init "snippets" if no storage does yet.
set -euo pipefail

BRANCH="${NOXA_BENCH_BRANCH:-main}"
HOME_DIR=/root/noxa-bench
KEY="$HOME_DIR/ssh-key"
NAME=noxa-bench
IMAGE_URL=https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2
RUN_URL="https://raw.githubusercontent.com/xenix13/Noxa/${BRANCH}/tools/benchmark/run.sh"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5)

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run it as root on the Proxmox host"
command -v qm >/dev/null || die "qm not found: run this on the Proxmox host itself, not in a VM"
mkdir -p "$HOME_DIR"

vmid() { [ -f "$HOME_DIR/vmid" ] && cat "$HOME_DIR/vmid" || true; }

vm_ip() {  # first IPv4 address reported by the QEMU guest agent
  qm guest cmd "$1" network-get-interfaces 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit()
for i in data:
    if i.get("name") == "lo":
        continue
    for a in i.get("ip-addresses", []):
        if a.get("ip-address-type") == "ipv4":
            print(a["ip-address"]); sys.exit()'
}

wait_ip() {
  local id=$1 ip="" t=0
  while [ -z "$ip" ]; do
    [ $t -lt 600 ] || die "the VM did not report an IP address after 10 minutes (is there a DHCP server on $BRIDGE?)"
    sleep 5; t=$((t + 5))
    ip=$(vm_ip "$id" || true)
  done
  echo "$ip"
}

# ------------------------------------------------------------------------------------------------- create
create() {
  [ -z "$(vmid)" ] || die "a benchmark VM already exists (VMID $(vmid)). 'bash proxmox-vm.sh delete' removes it."
  VMID="${VMID:-$(pvesh get /cluster/nextid)}"
  BRIDGE="${BRIDGE:-vmbr0}"
  if [ -z "${STORAGE:-}" ]; then
    stores=$(pvesm status --content images --enabled 1 2>/dev/null | awk 'NR>1 {print $1}')
    for s in local-lvm local-zfs; do echo "$stores" | grep -qx "$s" && STORAGE=$s && break; done
    STORAGE="${STORAGE:-$(echo "$stores" | head -1)}"
  fi
  [ -n "${STORAGE:-}" ] || die "no storage for VM disks found: set STORAGE=<name>"
  SNIP=$(pvesm status --content snippets --enabled 1 2>/dev/null | awk 'NR>1 {print $1; exit}')
  if [ -z "$SNIP" ]; then
    content=$(pvesh get /storage/local --output-format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["content"])')
    say "Allowing cloud-init snippets on the 'local' storage (content: $content,snippets)"
    pvesm set local --content "$content,snippets"
    SNIP=local
  fi
  snip_dir=$(pvesh get "/storage/$SNIP" --output-format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])')/snippets
  mkdir -p "$snip_dir"

  say "VM $VMID: ${CORES:-4} cores, ${MEMORY:-8192} MB, ${DISK:-24G} on $STORAGE, network $BRIDGE"
  [ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C noxa-bench -f "$KEY"

  img="$HOME_DIR/debian-12-genericcloud-amd64.qcow2"
  if [ ! -f "$img" ]; then
    say "Downloading Debian 12 (cloud image, ~350 MB)"
    curl -fL --progress-bar "$IMAGE_URL" -o "$img.part" && mv "$img.part" "$img"
  fi

  cat > "$snip_dir/noxa-bench-user.yaml" <<EOF
#cloud-config
hostname: $NAME
users: [default]
disable_root: false
ssh_pwauth: false
ssh_authorized_keys:
  - $(cat "$KEY.pub")
package_update: true
packages: [qemu-guest-agent, curl, ca-certificates, python3]
runcmd:
  - systemctl enable --now qemu-guest-agent
  - curl -fsSL $RUN_URL -o /usr/local/bin/noxa-bench
  - chmod +x /usr/local/bin/noxa-bench
  - NOXA_BENCH_BRANCH=$BRANCH /usr/local/bin/noxa-bench --prepare > /var/log/noxa-bench-prepare.log 2>&1 && touch /var/lib/noxa-bench-ready || touch /var/lib/noxa-bench-failed
EOF

  say "Creating the VM"
  qm create "$VMID" --name "$NAME" --ostype l26 --cpu host --cores "${CORES:-4}" --memory "${MEMORY:-8192}" \
    --net0 "virtio,bridge=$BRIDGE" --scsihw virtio-scsi-single --agent enabled=1 --serial0 socket --vga serial0
  echo "$VMID" > "$HOME_DIR/vmid"
  qm set "$VMID" --scsi0 "$STORAGE:0,import-from=$img" >/dev/null
  qm resize "$VMID" scsi0 "${DISK:-24G}" >/dev/null
  qm set "$VMID" --ide2 "$STORAGE:cloudinit" --boot order=scsi0 --ipconfig0 ip=dhcp \
    --cicustom "user=$SNIP:snippets/noxa-bench-user.yaml" >/dev/null
  qm start "$VMID"

  say "Waiting for the VM to start (1-3 minutes)"
  ip=$(wait_ip "$VMID")
  echo "VM address: $ip"

  say "Installing Docker, the 3 servers and the test library in the VM (10-20 minutes)"
  local t=0 shown=0
  while :; do
    if "${SSH[@]}" "root@$ip" test -f /var/lib/noxa-bench-ready 2>/dev/null; then break; fi
    if "${SSH[@]}" "root@$ip" test -f /var/lib/noxa-bench-failed 2>/dev/null; then
      "${SSH[@]}" "root@$ip" tail -30 /var/log/noxa-bench-prepare.log || true
      die "the preparation failed (log above). Send me /var/log/noxa-bench-prepare.log from the VM."
    fi
    [ $t -lt 3600 ] || die "the preparation is taking more than an hour: check /var/log/noxa-bench-prepare.log in the VM"
    line=$("${SSH[@]}" "root@$ip" 'tail -1 /var/log/noxa-bench-prepare.log 2>/dev/null' 2>/dev/null || true)
    [ -n "$line" ] && [ "$line" != "${shown:-}" ] && echo "  $line" && shown=$line
    sleep 15; t=$((t + 15))
  done
  say "Ready. Now run the benchmark with:  bash proxmox-vm.sh run   (or 'quick' for a 10-minute check)"
}

# ------------------------------------------------------------------------------------------------- run
run() {
  local id ip mode=("$@")
  id=$(vmid)
  [ -n "$id" ] || die "no benchmark VM yet: run 'bash proxmox-vm.sh' first"
  [ "$(qm status "$id" | awk '{print $2}')" = running ] || qm start "$id"
  ip=$(wait_ip "$id")
  "${SSH[@]}" "root@$ip" test -f /var/lib/noxa-bench-ready || die "the VM is not ready yet: run 'bash proxmox-vm.sh' again later"
  say "Running the benchmark in the VM ($ip). The run keeps going even if this window is closed."
  # refresh the scripts, then run as a service so that a dropped connection doesn't stop it
  "${SSH[@]}" "root@$ip" "curl -fsSL $RUN_URL -o /usr/local/bin/noxa-bench && chmod +x /usr/local/bin/noxa-bench &&
    : > /opt/noxa-bench/last-run.log; systemctl reset-failed noxa-bench-run 2>/dev/null;
    systemd-run --unit noxa-bench-run --collect --setenv=NOXA_BENCH_BRANCH=$BRANCH /usr/local/bin/noxa-bench ${mode[*]:-}" >/dev/null
  sleep 3
  "${SSH[@]}" "root@$ip" 'tail -n +1 -f /opt/noxa-bench/last-run.log --pid=$(systemctl show -p MainPID --value noxa-bench-run)' || true
  latest=$("${SSH[@]}" "root@$ip" 'ls -td /opt/noxa-bench/results/*/ 2>/dev/null | head -1')
  [ -n "$latest" ] || die "no results found in the VM: see /opt/noxa-bench/last-run.log there"
  dest="$HOME_DIR/results/$(basename "$latest")"
  mkdir -p "$dest"
  scp -q -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "root@$ip:$latest*" "$dest/"
  say "Results copied to $dest"
  echo "Send me $dest/summary.md (and results.json)."
}

# ------------------------------------------------------------------------------------------------- delete
delete() {
  local id
  id=$(vmid)
  if [ -n "$id" ]; then
    qm stop "$id" 2>/dev/null || true
    qm destroy "$id" --purge 2>/dev/null || true
    rm -f "$HOME_DIR/vmid"
    echo "VM $id removed."
  fi
  for s in $(pvesm status --content snippets 2>/dev/null | awk 'NR>1 {print $1}'); do
    p=$(pvesh get "/storage/$s" --output-format json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("path",""))' || true)
    [ -n "$p" ] && rm -f "$p/snippets/noxa-bench-user.yaml"
  done
  echo "The results stay in $HOME_DIR/results; rm -rf $HOME_DIR removes everything."
}

case "${1:-create}" in
  create) create ;;
  run) run ;;
  quick) run --quick ;;
  delete) delete ;;
  *) die "unknown command '$1' (create, run, quick or delete)" ;;
esac
