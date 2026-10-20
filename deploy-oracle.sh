#!/usr/bin/env bash
# Install Docker and start MaxTools hosted-downloader on an Ubuntu VM.
# Run as the VM's normal user (sudo is used where needed):
#   chmod +x deploy-oracle.sh && ./deploy-oracle.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/maxrtools-hosted

if ! command -v docker >/dev/null 2>&1; then
  echo '==> Installing Docker'
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sudo sh /tmp/get-docker.sh
fi
if ! sudo docker compose version >/dev/null 2>&1; then
  echo 'Docker Compose v2 is missing; install it and rerun.' >&2
  exit 1
fi

echo '==> Copying files to /opt/maxrtools-hosted'
sudo mkdir -p "$APP_DIR"
sudo cp -a "$SCRIPT_DIR/." "$APP_DIR/"

PUBLIC_IP="${PUBLIC_IP:-$(curl -fsS --max-time 10 https://api.ipify.org || true)}"
if [ -z "$PUBLIC_IP" ]; then
  echo 'Could not determine the public IP. Set PUBLIC_IP=x.x.x.x and rerun.' >&2
  exit 1
fi
DOMAIN="${DOMAIN:-api.${PUBLIC_IP}.sslip.io}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://${DOMAIN}}"

if [ ! -f "$APP_DIR/.env" ]; then
  sudo cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi
sudo sed -i "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" "$APP_DIR/.env"
sudo sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=${PUBLIC_BASE_URL}|" "$APP_DIR/.env"
sudo sed -i "s|^TRUST_PROXY=.*|TRUST_PROXY=1|" "$APP_DIR/.env"
if [ -n "${YTDLP_PROXY:-}" ]; then
  sudo sed -i "s|^YTDLP_PROXY=.*|YTDLP_PROXY=${YTDLP_PROXY}|" "$APP_DIR/.env"
fi
sudo chown root:root "$APP_DIR/.env"
sudo chmod 600 "$APP_DIR/.env"

# Oracle's Ubuntu images ship with iptables rules that can drop 80/443 even
# after the VCN security list is opened. Insert accept rules once.
if command -v iptables >/dev/null 2>&1; then
  for port in 80 443; do
    if ! sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
      sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$port" -j ACCEPT
    fi
  done
  if command -v netfilter-persistent >/dev/null 2>&1; then
    sudo netfilter-persistent save >/dev/null 2>&1 || true
  fi
fi

echo '==> Starting the downloader'
cd "$APP_DIR"
sudo docker compose up -d --build

echo '==> Waiting for HTTPS'
ok=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 10 "$PUBLIC_BASE_URL/" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 2
done
if [ "$ok" != 1 ]; then
  echo "The API did not answer yet. Check: cd $APP_DIR && sudo docker compose logs --tail=80" >&2
  exit 1
fi

printf '\nAPI URL: %s\n' "$PUBLIC_BASE_URL"
printf 'Health:  '; curl -fsS "$PUBLIC_BASE_URL/"; printf '\n\n'
echo 'Oracle reminder: the VCN security list must allow TCP 80 and 443 from 0.0.0.0/0.'
echo 'If the target video is still blocked, set YTDLP_PROXY in /opt/maxrtools-hosted/.env and run:'
echo '  cd /opt/maxrtools-hosted && sudo docker compose up -d'
