#!/usr/bin/env bash
# Backs up both data volumes. All application state (users, actions, settings,
# audit, closing) lives there - there is no database.
set -euo pipefail
DEST="${1:-/opt/rhg/backups}"
mkdir -p "$DEST"
STAMP=$(date +%F-%H%M)
for v in deploy_finance-data deploy_purchasing-data; do
  docker run --rm -v "$v":/data:ro -v "$DEST":/out alpine \
    tar czf "/out/${v}-${STAMP}.tar.gz" -C /data .
  echo "  $DEST/${v}-${STAMP}.tar.gz"
done
find "$DEST" -name '*.tar.gz' -mtime +30 -delete
