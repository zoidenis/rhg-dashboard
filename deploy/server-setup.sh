#!/usr/bin/env bash
# Run ONCE on 139.162.169.104 as root. Installs Docker, opens the firewall,
# and creates the self-signed certificate nginx serves.
set -euo pipefail

echo "==> Installing Docker"
apt-get update -qq
apt-get install -y -qq ca-certificates curl openssl ufw
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker

echo "==> Firewall: only 22, 80, 443"
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> Self-signed certificate (10 years, IP SAN)"
mkdir -p /opt/rhg/deploy/certs
if [ ! -f /opt/rhg/deploy/certs/server.crt ]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
    -keyout /opt/rhg/deploy/certs/server.key \
    -out    /opt/rhg/deploy/certs/server.crt \
    -subj "/C=AL/O=RHG/CN=139.162.169.104" \
    -addext "subjectAltName=IP:139.162.169.104"
  chmod 600 /opt/rhg/deploy/certs/server.key
  echo "    certificate created"
else
  echo "    certificate already present, left alone"
fi

echo "==> Done. Next: push the code, create the two .env files, then deploy.sh"
