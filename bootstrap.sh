#!/bin/bash
set -e
set -x

BASE_DIR="/root/ai_netmgr"
LOG_FILE="$BASE_DIR/bootstrap.log"

mkdir -p "$BASE_DIR"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "[hmanagement] Bootstrap started at $(date)"

echo "[hmanagement] Interfaces:"
ip -br addr || true

echo "[hmanagement] Routes:"
ip route || true

echo "[hmanagement] Waiting for default route through eth9..."

i=0
while [ "$i" -lt 60 ]; do
  if ip route | grep -q '^default .* eth9'; then
    echo "[hmanagement] Default route OK"
    break
  fi

  i=$((i + 1))
  sleep 2
done

if ! ip route | grep -q '^default .* eth9'; then
  echo "[hmanagement] ERROR: no default route via eth9"
  ip -br addr || true
  ip route || true
  exit 1
fi

echo "[hmanagement] Waiting for Internet by IP..."

i=0
while [ "$i" -lt 30 ]; do
  if ping -c 1 -W 2 8.8.8.8 >/dev/null 2>/dev/null; then
    echo "[hmanagement] Internet by IP OK"
    break
  fi

  i=$((i + 1))
  sleep 2
done

if ! ping -c 1 -W 2 8.8.8.8 >/dev/null 2>/dev/null; then
  echo "[hmanagement] ERROR: no Internet by IP"
  ip -br addr || true
  ip route || true
  exit 1
fi

echo "[hmanagement] Forcing DNS resolvers"

rm -f /etc/resolv.conf

cat > /etc/resolv.conf <<EOF
nameserver 192.168.122.1
nameserver 8.8.8.8
nameserver 1.1.1.1
EOF

echo "[hmanagement] resolv.conf:"
cat /etc/resolv.conf

echo "[hmanagement] Testing DNS with getent"

i=0
while [ "$i" -lt 30 ]; do
  if getent hosts archive.ubuntu.com >/dev/null 2>/dev/null; then
    echo "[hmanagement] DNS OK"
    getent hosts archive.ubuntu.com || true
    break
  fi

  echo "[hmanagement] DNS not ready yet, attempt $i"
  i=$((i + 1))
  sleep 2
done

if ! getent hosts archive.ubuntu.com >/dev/null 2>/dev/null; then
  echo "[hmanagement] WARNING: DNS test failed, apt-get update will try anyway"
  cat /etc/resolv.conf || true
fi

echo "[hmanagement] Installing packages"


export DEBIAN_FRONTEND=noninteractive

cat <<EOF > /etc/apt/preferences.d/no-openssh-server
Package: openssh-server
Pin: release *
Pin-Priority: -1
EOF

apt-get update -y

apt-get install -y --no-install-recommends  curl jq net-tools iputils-ping openssh-client ca-certificates

# ----------------------------
# Verify ssh/scp only (no ssh server!)
# ----------------------------
command -v ssh >/dev/null || { echo "ssh missing"; exit 1; }
command -v scp >/dev/null || { echo "scp missing"; exit 1; }

# ----------------------------
# Install uv
# ----------------------------
echo "[hmanagement] Installing uv"

if ! command -v uv >/dev/null 2>&1; then
  curl -Ls https://astral.sh/uv/install.sh | env HOME=/root sh
fi

export PATH="/root/.local/bin:$PATH"

command -v uv >/dev/null 2>&1 || {
  echo "[hmanagement] ERROR: uv not installed"
  exit 1
}

# ----------------------------
# Python environment (clean)
# ----------------------------
echo "[hmanagement] Creating Python env (uv + Python 3.11)"

uv venv "$BASE_DIR/.venv" --python 3.11

source /root/ai_netmgr/.venv/bin/activate

uv pip install --python /root/ai_netmgr/.venv/bin/python requests python-dotenv pyyaml openai

# ----------------------------
# Verify
# ----------------------------
echo "[hmanagement] Verifying Python environment"

"$BASE_DIR/.venv/bin/python" -c "
import requests, yaml
import dotenv
from openai import OpenAI
print('AI NETMGR PYTHON ENV OK')
"

/root/ai_netmgr/.venv/bin/python /root/ai_netmgr/orchestratorv2.py --all

echo "[hmanagement] Bootstrap finished successfully at $(date)"