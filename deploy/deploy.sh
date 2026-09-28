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
echo
docker compose ps
echo
echo "Open: https://139.162.169.104/   (accept the self-signed warning once)"
echo "First run prints the admin password - read it with:"
echo "  docker compose logs finance | head -20"
echo "  docker compose logs purchasing | head -20"
