#!/usr/bin/env bash
_cs_boot="${COMMUNITY_SCRIPTS_CORE_DIR:-$(dirname "${BASH_SOURCE[0]}")/../../core}/core/build.func"
source "$_cs_boot" 2>/dev/null || source <(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/core/main}/core/build.func")
# Copyright (c) 2021-2026 community-scripts ORG
# Author: xenix13
# License: MIT | https://github.com/community-scripts/ProxmoxVE/raw/main/LICENSE
# Source: https://github.com/xenix13/Noxa

APP="Noxa"
var_tags="${var_tags:-media}"
var_cpu="${var_cpu:-2}"
var_ram="${var_ram:-2048}"
var_disk="${var_disk:-8}"
var_os="${var_os:-ubuntu}"
var_version="${var_version:-24.04}"
# Privileged with mount=cifs: Noxa mounts the NAS shares itself (Settings > Libraries > Network shares),
# and Linux forbids SMB mounts in unprivileged containers. Set var_unprivileged=1 to mount them on the host instead.
var_unprivileged="${var_unprivileged:-0}"
var_mount_fs="${var_mount_fs:-cifs}"
var_gpu="${var_gpu:-yes}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources
  if [[ ! -d /opt/seance ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  if check_for_gh_release "noxa" "xenix13/Noxa"; then
    msg_info "Updating ${APP}"
    fetch_and_deploy_gh_release "noxa" "xenix13/Noxa" "binary" "latest" "" "noxa_*_all.deb"
    # the units are copied by the package's setup script, not shipped as package files: restart them here
    systemctl restart seance-mountd seance
    msg_ok "Updated ${APP}"
    msg_ok "Updated successfully!"
  fi
  exit
}

start
build_container
description

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW}Access it using the following URL:${CL}"
echo -e "${GATEWAY}${BGN}http://${IP}:8420${CL}"
echo -e "${INFO}${YW}The setup code asked on the first page is in /var/lib/seance/setup-code.txt${CL}"
