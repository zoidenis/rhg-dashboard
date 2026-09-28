#!/usr/bin/env bash
# Run on the server, in /opt/rhg/deploy. Builds and (re)starts everything.
set -euo pipefail
cd "$(dirname "$0")"

for f in ../rhg-finance-local/.env ../rhg-purchasing-local/.env; do
  [ -f "$f" ] || { echo "MISSING: $f - create it first (see README-DEPLOY.md)"; exit 1; }
  chmod 600 "$f"
done

[ -f certs/server.crt ] || { echo "MISSING: certs/server.crt - run server-setup.sh"; exit 1; }

# The towers read SERVICE_TOKEN through compose, which takes it from this file.
# It must be the same value the identity service uses.
if [ ! -f .env ]; then
  grep '^SERVICE_TOKEN=' ../rhg-identity/.env > .env \
    || { echo "MISSING: SERVICE_TOKEN in ../rhg-identity/.env"; exit 1; }
  chmod 600 .env
  echo "Wrote deploy/.env with the shared service token."
fi

docker compose build
docker compose up -d

# NPM mban certifikaten dhe catch-all-in ne volumin e vet. Nese volumi rikrijohet
# ato humbin dhe TLS-i bie me "unrecognized name", ndaj rivendosen sa here nevojitet.
if ! docker compose exec -T npm test -f /data/nginx/custom/http_top.conf 2>/dev/null \
   || ! docker compose exec -T npm test -f /data/custom_ssl/npm-1/fullchain.pem 2>/dev/null; then
  echo "==> Rivendos konfigurimin TLS te NPM-se"
  docker compose exec -T npm mkdir -p /data/custom_ssl/npm-1 /data/nginx/custom
  docker compose cp certs/server.crt npm:/data/custom_ssl/npm-1/fullchain.pem
  docker compose cp certs/server.key npm:/data/custom_ssl/npm-1/privkey.pem
  docker compose cp npm/http_top.conf npm:/data/nginx/custom/http_top.conf
  docker compose exec -T npm chmod 600 /data/custom_ssl/npm-1/privkey.pem
  docker compose exec -T npm nginx -t && docker compose exec -T npm nginx -s reload
fi
echo
docker compose ps
echo
echo "Open: https://139.162.169.104/   (accept the self-signed warning once)"
echo "First run prints the admin password - read it with:"
echo "  docker compose logs finance | head -20"
echo "  docker compose logs purchasing | head -20"
