#!/usr/bin/env bash
set -euo pipefail

#############################################
# Usage / Help                             #
#############################################
usage() {
  cat <<EOF
Usage: $(basename "$0") [-h|--help]

Bootstraps Esper EdgeBox on a Linux host.

Options:
  -h, --help           Show this help and exit

After bootstrap completes, manage the service with:
  # Enable on boot
  sudo systemctl enable  esper-edgebox
  # Disable on boot
  sudo systemctl disable esper-edgebox
  # Start now
  sudo systemctl start   esper-edgebox
  # Stop now
  sudo systemctl stop    esper-edgebox
  # Restart
  sudo systemctl restart esper-edgebox
  # Check status
  sudo systemctl status  esper-edgebox
  # Follow logs
  sudo journalctl -u esper-edgebox -f
EOF
}

#############################################
# Parse arguments                          #
#############################################
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0;;
    *)         echo "Unknown option: $1" >&2; usage; exit 1;;
  esac
done

#############################################
# Root / sudo check                        #
#############################################
if (( EUID != 0 )); then
  SUDO="sudo"
else
  SUDO=""
fi

#############################################
# Colors & Paths                           #
#############################################
GREEN="\033[32m"; YELLOW="\033[1;36m"; NC="\033[0m"
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
TOP_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

#############################################
# Logging                                  #
#############################################
LOG_FILE="$TOP_DIR/bootstrap.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo -e "${GREEN}Bootstrap started at $(date)${NC}"

#############################################
# Hostname                                 #
#############################################
echo -e "\n${GREEN}Checking hostname…${NC}"
CURRENT="$($SUDO hostnamectl --static 2>/dev/null || hostname)"
if [[ -n "$CURRENT" ]]; then
  echo -e "${YELLOW}Hostname is $CURRENT${NC}"
else
  NEW="esper-edgebox-$(openssl rand -hex 3)"
  echo -e "${YELLOW}Setting hostname: $NEW${NC}"
  $SUDO hostnamectl set-hostname "$NEW"
fi

#############################################
# Firewall (8020/8021/5353)                 #
#############################################
if command -v firewall-cmd &>/dev/null; then
  echo -e "\n${GREEN}Configuring firewall…${NC}"
  $SUDO firewall-cmd --permanent --add-port=8020/tcp
  $SUDO firewall-cmd --permanent --add-port=8021/tcp
  $SUDO firewall-cmd --permanent --add-port=5353/udp
  $SUDO firewall-cmd --reload
else
  echo -e "${YELLOW}firewalld not found; ensure ports 8020,8021,5353 are open.${NC}"
fi

#############################################
# Ensure curl & CA certificates are present #
#############################################
echo -e "\n${GREEN}Installing base packages…${NC}"
if command -v apt-get &>/dev/null; then
  $SUDO apt-get update -y
  $SUDO apt-get install -y curl ca-certificates
elif command -v dnf &>/dev/null; then
  $SUDO dnf makecache
  $SUDO dnf install -y curl ca-certificates
elif command -v yum &>/dev/null; then
  $SUDO yum makecache
  $SUDO yum install -y curl ca-certificates
else
  echo "Unsupported package manager; please install curl & ca-certificates manually" >&2
  exit 1
fi

#############################################
# Fetch Go binary from Artifact Hub         #
#############################################
# BINARY_BASE_URL="https://artifacthub.esper.cloud/esperedgebox/binaries"
BINARY_BASE_URL="https://raw.githubusercontent.com/esper-io/lancache/master/ext/"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
MACHINE="$(uname -m)"
case "$MACHINE" in
  x86_64) ARCH="amd64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *) echo "Unsupported arch: $MACHINE" >&2; exit 1 ;;
esac

BINARY_NAME="esper-edgebox-${OS}-${ARCH}"
echo -e "\n${GREEN}Downloading $BINARY_NAME…${NC}"
$SUDO curl -fsSL "$BINARY_BASE_URL/$BINARY_NAME" -o "$TOP_DIR/esper-edgebox"
$SUDO chmod +x "$TOP_DIR/esper-edgebox"

#############################################
# Launcher wrapper                         #
#############################################
cat > "$TOP_DIR/run_esper_edgebox.sh" <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "${BASH_SOURCE[0]:-$0}")"
exec ./esper-edgebox -data "$(pwd)"
EOF
$SUDO chmod +x "$TOP_DIR/run_esper_edgebox.sh"

#############################################
# Systemd unit                             #
#############################################
UNIT="/etc/systemd/system/esper-edgebox.service"
$SUDO tee "$UNIT" > /dev/null <<EOF
[Unit]
Description=Esper EdgeBox Local Cache Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$TOP_DIR
ExecStart=/usr/bin/env bash $TOP_DIR/run_esper_edgebox.sh
Restart=always
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

#############################################
# Enable & start                           #
#############################################
echo -e "\n${GREEN}Reloading systemd & enabling service…${NC}"
$SUDO systemctl daemon-reload
$SUDO systemctl enable esper-edgebox
$SUDO systemctl restart esper-edgebox

echo -e "\n${GREEN}Bootstrap complete!${NC}"
echo -e "${YELLOW}Logs: sudo journalctl -u esper-edgebox -f${NC}"
