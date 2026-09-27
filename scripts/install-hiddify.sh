#!/bin/bash
# Ubuntu 22.04. Hiddify Manager only v12.3.3, never release channel.
set -euo pipefail

HIDDIFY_VERSION="12.3.3"
HIDDIFY_TAG="v12.3.3"
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
export APT_LISTCHANGES_FRONTEND=none

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root: sudo bash $0" >&2
  exit 1
fi

# shellcheck disable=SC1091
. /etc/os-release
if [[ "${VERSION_CODENAME:-}" != "jammy" ]]; then
  echo "need Ubuntu 22.04 jammy, got: ${VERSION_ID:-unknown} ${VERSION_CODENAME:-unknown}" >&2
  exit 1
fi

installed_version=""
if [[ -f /opt/hiddify-manager/VERSION ]]; then
  installed_version="$(tr -d ' \t\r\nv' < /opt/hiddify-manager/VERSION)"
fi

if [[ -d /opt/hiddify-manager && -n "$installed_version" && "$installed_version" != "$HIDDIFY_VERSION" ]]; then
  echo "other version installed: $installed_version; want $HIDDIFY_VERSION; stop" >&2
  exit 2
fi

apt_quiet=( -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold )

install_sysctl() {
  cat >/etc/sysctl.d/99-disable-ipv6.conf <<'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
  sysctl --system >/dev/null
}

ensure_swap() {
  if swapon --show --noheadings | grep -q .; then
    echo "swap already active"
    return 0
  fi
  local avail_kb size_label count
  avail_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
  if [[ "$avail_kb" -gt 1500000 ]]; then
    size_label=1G
    count=1024
  else
    size_label=300M
    count=300
  fi
  if [[ ! -e /swapfile ]]; then
    if ! fallocate -l "$size_label" /swapfile; then
      dd if=/dev/zero of=/swapfile bs=1M count="$count" status=none
    fi
  fi
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -qE '^/swapfile[[:space:]]' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "swap $size_label"
}

install_haproxy() {
  apt-get install "${apt_quiet[@]}" software-properties-common ca-certificates curl
  add-apt-repository -y ppa:vbernat/haproxy-3.0
  apt-get update
  local ver
  ver="$(apt-cache madison haproxy | awk -F'|' '/3\.0\./ {gsub(/ /,"",$2); print $2; exit}')"
  if [[ -z "$ver" ]]; then
    echo "no haproxy 3.0 in PPA" >&2
    exit 1
  fi
  apt-get install "${apt_quiet[@]}" "haproxy=${ver}"
  echo "haproxy $ver"
}

lock_panel() {
  apt-mark hold haproxy || true
  local cli=""
  if command -v hiddify-panel-cli >/dev/null 2>&1; then
    cli="hiddify-panel-cli"
  elif [[ -x /opt/hiddify-manager/.venv313/bin/hiddifypanel ]]; then
    cli="/opt/hiddify-manager/.venv313/bin/hiddifypanel"
  elif [[ -x /opt/hiddify-manager/.venv/bin/hiddifypanel ]]; then
    cli="/opt/hiddify-manager/.venv/bin/hiddifypanel"
  fi
  if [[ -z "$cli" ]]; then
    echo "WARN: hiddifypanel CLI not found, auto_update not locked yet" >&2
    return 0
  fi
  (
    cd /opt/hiddify-manager/hiddify-panel
    "$cli" set-setting -k auto_update -v false
    "$cli" set-setting -k package_mode -v "$HIDDIFY_TAG"
  )
  echo "panel locked: auto_update=false package_mode=$HIDDIFY_TAG"
}

if [[ "$installed_version" == "$HIDDIFY_VERSION" ]]; then
  echo "Hiddify $HIDDIFY_VERSION already installed"
  install_sysctl
  lock_panel
  exit 0
fi

# apt upgrade already done on this host; skip if recent success marker
if [[ ! -f /var/log/hiddify-apt-upgraded.ok ]]; then
  apt-get update
  apt-get upgrade "${apt_quiet[@]}"
  date -u > /var/log/hiddify-apt-upgraded.ok
fi
install_sysctl
ensure_swap
install_haproxy

bash <(curl -fsSL "https://raw.githubusercontent.com/hiddify/Hiddify-Manager/refs/tags/${HIDDIFY_TAG}/common/download.sh") "$HIDDIFY_TAG" --no-gui

got="$(tr -d ' \t\r\nv' < /opt/hiddify-manager/VERSION || true)"
if [[ "$got" != "$HIDDIFY_VERSION" ]]; then
  echo "want $HIDDIFY_VERSION, VERSION file: ${got:-empty}" >&2
  exit 3
fi
lock_panel
echo "Hiddify $HIDDIFY_VERSION installed"