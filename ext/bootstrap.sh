#!/usr/bin/env bash
set -euo pipefail

#############################################
# Usage / Help                             #
#############################################
usage() {
  cat <<EOF
Usage: $(basename "$0") [-h|--help]

  -h, --help           Show this help and exit

After bootstrap completes, you can manage the cache server with:
  sudo systemctl start   esper-cache
  sudo systemctl stop    esper-cache
  sudo systemctl restart esper-cache
  sudo systemctl status  esper-cache
  sudo systemctl disable esper-cache
EOF
}

#############################################
# Parse arguments                          #
#############################################
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

#############################################
# 0) Root or sudo check                    #
#############################################
if [ "$EUID" -ne 0 ]; then
    SUDO="sudo"
else
    SUDO=""
fi

#############################################
# 1) Define Colors and Base Directories     #
#############################################
RED="\033[31m"
GREEN="\033[32m"
YELLOW="\033[1;36m"
NC="\033[0m"
TOP_DIR=$(pwd)

#############################################
# 2) Logging                                #
#############################################
LOG_FILE="$TOP_DIR/bootstrap.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "Bootstrap started at $(date)"

#############################################
# 3) Hostname setup                         #
#############################################
set_hostname() {
    echo -e "\n${GREEN}Checking hostname…${NC}"
    CURRENT=$(hostnamectl --static 2>/dev/null || hostname)
    if [ -n "$CURRENT" ]; then
        echo -e "${YELLOW}Hostname is $CURRENT${NC}"
        return
    fi
    DEFAULT="esper-cache-$(openssl rand -hex 3)"
    NEW=${DEFAULT,,}
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
    echo -e "${YELLOW}firewalld not found; please ensure ports 8020,8021,5353 are open manually.${NC}"
fi

#############################################
# 5) Package manager & base packages        #
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
    echo "Unsupported package manager" >&2
    exit 1
fi

echo -e "\n${GREEN}Installing base packages…${NC}"
$SUDO $UPDATE
$SUDO $INSTALL jq curl wget python3-pip

#############################################
# 6) Python deps                            #
#############################################
echo -e "\n${GREEN}Installing Python libs…${NC}"
pip3 install --no-cache-dir Flask flask-apscheduler requests pyOpenSSL zeroconf crcmod

#############################################
# 7) Fetch & version‑control python service #
#############################################
PY_S3_URL="https://raw.githubusercontent.com/esper-io/lancache/master/ext/cache_server.py"
VER_S3_URL="https://raw.githubusercontent.com/esper-io/lancache/master/ext/cache_server.version"
UPDATED=0

echo -e "\n${GREEN}Checking Python service version…${NC}"
REMOTE_VER=$(curl -fsSL "$VER_S3_URL")
LOCAL_VER=$(grep -E "^__version__" "$TOP_DIR/cache_server.py" 2>/dev/null \
             | head -1 | cut -d\" -f2 || echo "none")

if [ "$REMOTE_VER" != "$LOCAL_VER" ]; then
  echo -e "${GREEN}Updating cache_server.py to v$REMOTE_VER…${NC}"
  curl -fsSL "$PY_S3_URL" -o "$TOP_DIR/cache_server.py"
  chmod +x "$TOP_DIR/cache_server.py"
  UPDATED=1
else
  echo -e "${YELLOW}cache_server.py up-to-date (v$LOCAL_VER)${NC}"
fi

#############################################
# 8) Launcher wrapper                      #
#############################################
cat > "$TOP_DIR/run_cache_server.sh" << 'EOF'
#!/usr/bin/env bash
cd "$(dirname "${BASH_SOURCE[0]}")"
exec python3 "$(pwd)/cache_server.py"
EOF
chmod +x "$TOP_DIR/run_cache_server.sh"

#############################################
# 9) Systemd unit                         #
#############################################
UNIT="/etc/systemd/system/esper-cache.service"
$SUDO tee "$UNIT" > /dev/null << EOF
[Unit]
Description=Esper Local Cache Server
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/env bash $TOP_DIR/run_cache_server.sh
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
$SUDO systemctl daemon-reload
$SUDO systemctl enable esper-cache
if [ "$UPDATED" -eq 1 ]; then
  echo -e "${GREEN}Restarting service…${NC}"
  $SUDO systemctl restart esper-cache
else
  echo -e "${GREEN}Starting service…${NC}"
  $SUDO systemctl start esper-cache
fi

echo -e "\n${GREEN}Bootstrap complete! Logs: sudo journalctl -u esper-cache -f${NC}"
