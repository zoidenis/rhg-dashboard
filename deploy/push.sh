#!/usr/bin/env bash
# Run from your Mac, in the rhg-finance-local folder. Syncs code to the server.
# .env is deliberately NOT synced: it holds the real BC credentials and is
# created once directly on the server.
set -euo pipefail

SERVER="${SERVER:-root@139.162.169.104}"
REMOTE=/opt/rhg
HERE="$(cd "$(dirname "$0")/.." && pwd)"

EXCL=(--exclude='.venv' --exclude='__pycache__' --exclude='*.pyc'
      --exclude='.env' --exclude='.DS_Store' --exclude='data')

ssh "$SERVER" "mkdir -p $REMOTE/deploy/nginx/html"

for app in rhg-finance-local rhg-purchasing-local rhg-identity; do
  echo "==> $app"
  rsync -az --delete "${EXCL[@]}" "$HERE/$app/" "$SERVER:$REMOTE/$app/"
done

echo "==> deploy files"
rsync -az "$HERE/deploy/docker-compose.yml" "$HERE/deploy/deploy.sh" \
          "$HERE/deploy/server-setup.sh" "$HERE/deploy/backup.sh" "$HERE/deploy/README-DEPLOY.md" \
          "$SERVER:$REMOTE/deploy/"
rsync -az "$HERE/deploy/nginx/default.conf" "$SERVER:$REMOTE/deploy/nginx/"
# NPM-ja e humb http_top.conf kur volumi rikrijohet, dhe pa te nje kerkese me IP
# te zhveshur refuzohet me "unrecognized name". Prandaj sinkronizohet gjithmone.
ssh "$SERVER" "mkdir -p $REMOTE/deploy/npm"
rsync -az "$HERE/deploy/npm/http_top.conf" "$SERVER:$REMOTE/deploy/npm/"
rsync -az --delete "$HERE/deploy/nginx/html/" "$SERVER:$REMOTE/deploy/nginx/html/"
ssh "$SERVER" "chmod +x $REMOTE/deploy/*.sh"
echo "==> Pushed to $SERVER:$REMOTE"
