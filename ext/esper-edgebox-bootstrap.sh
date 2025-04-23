#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# Usage / Help
# -----------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") [-h|--help]

Bootstraps Esper EdgeBox on a Linux host.

Options:
  -h, --help        Show this help and exit

After bootstrap completes, manage the service with:
  sudo systemctl enable  esper-edgebox
  sudo systemctl disable esper-edgebox
  sudo systemctl start   esper-edgebox
  sudo systemctl stop    esper-edgebox
  sudo systemctl restart esper-edgebox
  sudo systemctl status  esper-edgebox
  sudo journalctl -u esper-edgebox -f
EOF
}

# -----------------------------------------------------------------------------
# Parse arguments
# -----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0;;
    *)         echo "Unknown option: $1" >&2; usage; exit 1;;
  esac
done

# -----------------------------------------------------------------------------
# Ensure sudo if not root
# -----------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  SUDO="sudo"
else
  SUDO=""
fi

# -----------------------------------------------------------------------------
# Colors & Paths
# -----------------------------------------------------------------------------
GREEN="\033[32m"; YELLOW="\033[1;36m"; NC="\033[0m"
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
TOP_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
LOG_FILE="$TOP_DIR/bootstrap.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo -e "${GREEN}Bootstrap started at $(date)${NC}"

# -----------------------------------------------------------------------------
# 1) Hostname
# -----------------------------------------------------------------------------
set_hostname() {
  echo -e "\n${GREEN}Checking hostname…${NC}"
  CURRENT="$($SUDO hostnamectl --static 2>/dev/null || hostname)"
  if [[ -n "$CURRENT" ]]; then
    echo -e "${YELLOW}Hostname is $CURRENT${NC}"
    return
  fi
  NEW="esper-edgebox-$(openssl rand -hex 3)"
  echo -e "${YELLOW}Setting hostname: $NEW${NC}"
  $SUDO hostnamectl set-hostname "$NEW"
}
set_hostname

# -----------------------------------------------------------------------------
# 2) Firewall (8020/8021/5353)
# -----------------------------------------------------------------------------
if command -v firewall-cmd &>/dev/null; then
  echo -e "\n${GREEN}Configuring firewall…${NC}"
  $SUDO firewall-cmd --permanent --add-port=8020/tcp
  $SUDO firewall-cmd --permanent --add-port=8021/tcp
  $SUDO firewall-cmd --permanent --add-port=5353/udp
  $SUDO firewall-cmd --permanent --add-service=mdns
  $SUDO firewall-cmd --reload
else
  echo -e "${YELLOW}firewalld not found; ensure ports 8020,8021,5353 are open.${NC}"
fi

# -----------------------------------------------------------------------------
# 3) Determine OS/ARCH and binary URL
# -----------------------------------------------------------------------------
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"  # should be "linux"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)   ARCH_TAG="amd64" ;;
  aarch64)  ARCH_TAG="arm64" ;;
  armv7l)   ARCH_TAG="armv7" ;;
  *)        echo "Unsupported arch: $ARCH" >&2; exit 1 ;;
esac

if [[ "$OS" != "linux" ]]; then
  echo "This bootstrap only supports Linux hosts." >&2
  exit 1
fi

# BINARY_URL="https://artifacthub.esper.cloud/esperedgebox/binaries/esper-edgebox-${OS}-${ARCH_TAG}"
BINARY_BASE_URL="https://raw.githubusercontent.com/esper-io/lancache/master/ext/esper-edgebox-${OS}-${ARCH_TAG}"
echo -e "\n${GREEN}Downloading Esper EdgeBox binary for ${OS}-${ARCH_TAG}…${NC}"

TMP_BIN="$(mktemp)"
if ! curl -fSL "$BINARY_URL" -o "$TMP_BIN"; then
  echo "❌ Failed to download $BINARY_URL" >&2
  rm -f "$TMP_BIN"
  exit 1
fi
chmod +x "$TMP_BIN"
$SUDO mv "$TMP_BIN" "$TOP_DIR/esper-edgebox"

# -----------------------------------------------------------------------------
# 4) Systemd unit
# -----------------------------------------------------------------------------
UNIT_PATH="/etc/systemd/system/esper-edgebox.service"
echo -e "\n${GREEN}Installing systemd unit…${NC}"
$SUDO tee "$UNIT_PATH" > /dev/null <<EOF
[Unit]
Description=Esper EdgeBox Local Cache Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$TOP_DIR
ExecStart=$TOP_DIR/esper-edgebox -data $TOP_DIR
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# -----------------------------------------------------------------------------
# 5) Enable & restart
# -----------------------------------------------------------------------------
echo -e "\n${GREEN}Reloading systemd & enabling service…${NC}"
$SUDO systemctl daemon-reload
$SUDO systemctl enable esper-edgebox
echo -e "${GREEN}Restarting esper-edgebox…${NC}"
$SUDO systemctl restart esper-edgebox

echo -e "\n${GREEN}Bootstrap complete!${NC}"
echo -e "${YELLOW}Logs: sudo journalctl -u esper-edgebox -f${NC}"
