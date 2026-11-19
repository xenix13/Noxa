#!/usr/bin/env bash

# Copyright (c) 2021-2026 community-scripts ORG
# Author: xenix13
# License: MIT | https://github.com/community-scripts/ProxmoxVE/raw/main/LICENSE
# Source: https://github.com/xenix13/Noxa

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing Noxa"
# the .deb pulls python3 and ffmpeg, creates the "seance" user and starts the service
fetch_and_deploy_gh_release "noxa" "xenix13/Noxa" "binary" "latest" "" "noxa_*_all.deb"
msg_ok "Installed Noxa"

setup_hwaccel "seance"

msg_info "Starting Noxa"
systemctl restart seance
msg_ok "Started Noxa"

motd_ssh
customize
cleanup_lxc
