# Proxmox VE Helper-Scripts

Draft of the two files the [Proxmox VE Helper-Scripts](https://github.com/community-scripts/ProxmoxVE) project needs to add Noxa to its catalogue. They follow its current layout and install the `.deb` attached to the latest GitHub release.

- `ct/noxa.sh`: creates the LXC container (Ubuntu 24.04, unprivileged, 2 cores, 2 GB, 8 GB disk, GPU passthrough offered) and updates Noxa from the latest release.
- `install/noxa-install.sh`: runs inside the container; installs the `.deb`, sets up hardware acceleration for the `seance` user.

New scripts are submitted to [DevScripts](https://github.com/community-scripts/DevScripts) first (not to ProxmoxVE), after testing them on a real Proxmox host. See their [CONTRIBUTING.md](https://github.com/community-scripts/ProxmoxVE/blob/main/CONTRIBUTING.md).

Until then, [`../proxmox-lxc.sh`](../proxmox-lxc.sh) remains the supported way to create a Noxa container on Proxmox.
