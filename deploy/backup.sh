#!/usr/bin/env bash
# Backs up all application data volumes, including identity accounts and the
# Purchasing mail settings. Archives contain secrets, so keep them private.
set -euo pipefail
umask 077
DEST="${1:-/opt/rhg/backups}"
mkdir -p "$DEST"
chmod 700 "$DEST"
STAMP=$(date +%F-%H%M)
for v in deploy_finance-data deploy_purchasing-data deploy_identity-data; do
  docker run --rm -v "$v":/data:ro -v "$DEST":/out alpine \
    sh -c 'umask 077; tar czf "$1" -C /data .' sh "/out/${v}-${STAMP}.tar.gz"
  echo "  $DEST/${v}-${STAMP}.tar.gz"
done
find "$DEST" -name '*.tar.gz' -mtime +30 -delete
