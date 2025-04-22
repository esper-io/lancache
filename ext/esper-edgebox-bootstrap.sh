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
  # Restart (stop + start)
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
# 0) Root or sudo check                    #
#############################################
if [[ $EUID -ne 0 ]]; then SUDO="sudo"; else SUDO=""; fi

#############################################
# 1) Colors & Paths                        #
#############################################
RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[1;36m"; NC="\033[0m"
TOP_DIR="$(pwd)"

#############################################
# 2) Logging                               #
#############################################
LOG_FILE="$TOP_DIR/bootstrap.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo -e "${GREEN}Bootstrap started at $(date)${NC}"

#############################################
# 3) Hostname                              #
#############################################
set_hostname() {
  echo -e "\n${GREEN}Checking hostname…${NC}"
  CURRENT="$(hostnamectl --static 2>/dev/null || hostname)"
  if [[ -n "$CURRENT" ]]; then
    echo -e "${YELLOW}Hostname is $CURRENT${NC}"
    return
  fi
  NEW="esper-edgebox-$(openssl rand -hex 3)"
  echo -e "${YELLOW}Setting hostname: $NEW${NC}"
  $SUDO hostnamectl set-hostname "$NEW"
}
set_hostname

#############################################
# 4) Firewall (8020/8021/5353)              #
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
# 5) Package manager & base packages       #
#############################################
if command -v apt-get &>/dev/null; then
  INSTALL="apt-get install -y"
  UPDATE="apt-get update -y"
elif command -v dnf &>/dev/null; then
  INSTALL="dnf install -y"
  UPDATE="dnf makecache"
elif command -v yum &>/dev/null; then
  INSTALL="yum install -y"
  UPDATE="yum makecache"
else
  echo "Unsupported package manager" >&2; exit 1
fi

echo -e "\n${GREEN}Installing base packages…${NC}"
$SUDO $UPDATE
$SUDO $INSTALL jq curl wget python3-pip

#############################################
# 6) Python libraries                      #
#############################################
echo -e "\n${GREEN}Installing Python libraries…${NC}"
$SUDO pip3 install --no-cache-dir Flask flask-apscheduler requests pyOpenSSL zeroconf crcmod

#############################################
# 7) Fetch & version‑control Python service #
#############################################
PY_SCRIPT_URL="https://raw.githubusercontent.com/your-org/your-repo/main/esper_edgebox.py"
VER_URL      ="https://raw.githubusercontent.com/your-org/your-repo/main/esper_edgebox.version"
UPDATED=0

echo -e "\n${GREEN}Checking Esper EdgeBox service version…${NC}"
REMOTE_VER=$($SUDO curl -fsSL "$VER_URL")
LOCAL_VER=$(grep -E "^__version__" "$TOP_DIR/esper_edgebox.py" 2>/dev/null \
            | head -1 | cut -d\" -f2 || echo "none")

if [[ "$REMOTE_VER" != "$LOCAL_VER" ]]; then
  echo -e "${GREEN}Updating esper_edgebox.py → v${REMOTE_VER}…${NC}"
  $SUDO curl -fsSL "$PY_SCRIPT_URL" -o "$TOP_DIR/esper_edgebox.py"
  $SUDO chmod +x "$TOP_DIR/esper_edgebox.py"
  UPDATED=1
else
  echo -e "${YELLOW}esper_edgebox.py up-to-date (v${LOCAL_VER})${NC}"
fi

#############################################
# 8) Launcher wrapper                      #
#############################################
cat > "$TOP_DIR/run_esper_edgebox.sh" <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "${BASH_SOURCE[0]}")"
exec python3 "$(pwd)/esper_edgebox.py"
EOF
$SUDO chmod +x "$TOP_DIR/run_esper_edgebox.sh"

#############################################
# 9) Systemd unit                          #
#############################################
UNIT="/etc/systemd/system/esper-edgebox.service"
$SUDO tee "$UNIT" > /dev/null <<EOF
[Unit]
Description=Esper EdgeBox Local Cache Server
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/env bash $TOP_DIR/run_esper_edgebox.sh
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

#############################################
# 10) Enable & start/restart service       #
#############################################
echo -e "\n${GREEN}Reloading systemd & enabling service…${NC}"
$SUDO systemctl daemon-reload
$SUDO systemctl enable esper-edgebox

if [[ $UPDATED -eq 1 ]]; then
  echo -e "${GREEN}Restarting esper-edgebox…${NC}"
  $SUDO systemctl restart esper-edgebox
else
  echo -e "${GREEN}Starting esper-edgebox…${NC}"
  $SUDO systemctl start esper-edgebox
fi

echo -e "\n${GREEN}Bootstrap complete!${NC}"
echo -e "${YELLOW}Logs: sudo journalctl -u esper-edgebox -f${NC}"