#!/bin/sh
# Restore a dump from the backups volume into the running db container.
# Usage: scripts/restore.sh imoveis-YYYYMMDD-HHMMSS.dump
set -e
cd "$(dirname "$0")/.."
[ -n "$1" ] || { echo "usage: $0 <dump file name inside the backups volume>"; exit 1; }
. ./.env
docker compose stop web worker bot
docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" worker \
  pg_restore --clean --if-exists --no-owner -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" "/backups/$1"
docker compose start web worker bot
echo "restored $1"
