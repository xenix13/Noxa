#!/usr/bin/env bash
# Noxa / Jellyfin / Plex benchmark in one command, on a fresh Debian or Ubuntu VM, as root:
#
#   curl -fsSL https://raw.githubusercontent.com/xenix13/Noxa/main/tools/benchmark/run.sh | bash
#
# Short check run (about 10 minutes):  ... | bash -s -- --quick
# It installs Docker and Python if they are missing, downloads bench.py next to it, and runs it.
# Everything goes in /opt/noxa-bench (NOXA_BENCH_DIR to change it). The summary is printed at the end.
set -euo pipefail

BRANCH="${NOXA_BENCH_BRANCH:-main}"
DIR="${NOXA_BENCH_DIR:-/opt/noxa-bench}"
RAW="https://raw.githubusercontent.com/xenix13/Noxa/${BRANCH}/tools/benchmark"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run it as root (sudo -i first, or: ... | sudo bash)"

if command -v systemd-detect-virt >/dev/null && systemd-detect-virt --container --quiet; then
  echo "Warning: this looks like a container (LXC). Docker inside LXC often fails; a VM is recommended."
fi

say "Checking the machine"
mkdir -p "$DIR"
free_gb=$(df -Pk "$DIR" | awk 'NR==2 {print int($4 / 1048576)}')
[ "$free_gb" -ge 8 ] || die "not enough disk space in $DIR: ${free_gb} GB free, 8 GB needed"
cores=$(nproc)
[ "$cores" -ge 3 ] || echo "Warning: only $cores cores. 4 or more are recommended (2 for the server, the rest for the test itself)."
for port in 8420 8096 32400; do
  if (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q ":$port "; then
    die "port $port is already in use: stop the media server running on this machine (or use a fresh VM)"
  fi
done

if ! command -v docker >/dev/null; then
  say "Installing Docker"
  command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl; }
  curl -fsSL https://get.docker.com | sh
fi
systemctl start docker 2>/dev/null || true
docker info >/dev/null 2>&1 || die "Docker is installed but not running"

if ! command -v python3 >/dev/null; then
  say "Installing Python"
  apt-get update -qq && apt-get install -y -qq python3
fi

if [ -n "${NOXA_BENCH_SRC:-}" ]; then
  cp "$NOXA_BENCH_SRC/bench.py" "$DIR/bench.py"  # local checkout (to test changes before they are pushed)
else
  say "Downloading the benchmark script"
  curl -fsSL "$RAW/bench.py" -o "$DIR/bench.py"
fi

say "Running (about 1 hour; the screen shows each step)"
cd "$DIR"
set +e
python3 bench.py --work "$DIR/work" "$@" 2>&1 | tee "$DIR/last-run.log"
status=${PIPESTATUS[0]}
set -e

latest=$(ls -td "$DIR"/results/*/ 2>/dev/null | head -1)
say "Done"
echo "Results: ${latest:-$DIR/results}"
echo "Send back the file ${latest}summary.md (and results.json). The full log is in $DIR/last-run.log."
echo "To free the space afterwards: rm -rf $DIR && docker image prune -a"
exit "$status"
