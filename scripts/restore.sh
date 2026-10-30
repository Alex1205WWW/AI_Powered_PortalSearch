#!/bin/sh
# Restore a nightly dump.
#
#   scripts/restore.sh imoveis-YYYYMMDD-HHMMSS.dump            -> over the LIVE database (disaster recovery)
#   scripts/restore.sh imoveis-YYYYMMDD-HHMMSS.dump --rehearse -> into a scratch database, live data untouched
#
# The rehearsal is how you check a dump is actually restorable without betting production on it: it creates
# `restore_test`, restores into it, prints the row counts beside the live ones, and drops it again.
#
# This script deliberately does NOT source .env. That was the previous implementation and it was broken:
# `HTTP_USER_AGENT=ImoveisBot/0.1 (+mailto:...)` is not valid shell, so `. ./.env` fails, and with `set -e`
# the restore aborted before it started - the one moment you cannot afford a surprise. The credentials are
# already in the container's environment via `env_file`, so they are read there instead (found by the Day 7
# restore rehearsal, 2026-10-31).
set -e
cd "$(dirname "$0")/.."

DUMP="$1"
MODE="$2"
[ -n "$DUMP" ] || { echo "usage: $0 <dump file name inside the backups volume> [--rehearse]"; exit 1; }

docker compose exec -T worker test -f "/backups/$DUMP" || {
  echo "dump not found: /backups/$DUMP"
  echo "available:"
  docker compose exec -T worker ls -1 /backups/ || true
  exit 1
}

if [ "$MODE" = "--rehearse" ]; then
  echo "rehearsing $DUMP into a scratch database (live data untouched)"
  docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d postgres \
      -c "DROP DATABASE IF EXISTS restore_test;" -c "CREATE DATABASE restore_test;"' >/dev/null
  docker compose exec -T worker sh -c \
      "PGPASSWORD=\$POSTGRES_PASSWORD pg_restore --no-owner -h db -U \$POSTGRES_USER -d restore_test '/backups/$DUMP'"
  echo "row counts (live vs restored):"
  echo "  (DIFF is expected where the live database moved on after the dump was taken -"
  echo "   a crawl adds listings every few minutes. What matters is that every table restored.)"
  for table in listings properties agencies sources property_matches geo criteria buyers notifications; do
    docker compose exec -T db sh -c "psql -tA -U \$POSTGRES_USER -d \$POSTGRES_DB -c 'select count(*) from $table' \
      && psql -tA -U \$POSTGRES_USER -d restore_test -c 'select count(*) from $table'" \
      | tr '\n' ' ' | awk -v t="$table" '{printf "  %-18s live=%-8s restored=%-8s %s\n", t, $1, $2, ($1==$2 ? "ok" : "DIFF")}'
  done
  docker compose exec -T db sh -c 'psql -tA -U "$POSTGRES_USER" -d restore_test -c "select version_num from alembic_version"' \
    | awk '{printf "  alembic revision   %s\n", $1}'
  docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE restore_test;"' >/dev/null
  echo "scratch database dropped. The dump is restorable."
  exit 0
fi

printf 'This REPLACES the live database with %s. Type "sim" to continue: ' "$DUMP"
read -r answer
[ "$answer" = "sim" ] || { echo "cancelled"; exit 1; }

docker compose stop web worker bot
docker compose start worker
docker compose exec -T worker sh -c \
    "PGPASSWORD=\$POSTGRES_PASSWORD pg_restore --clean --if-exists --no-owner -h db -U \$POSTGRES_USER -d \$POSTGRES_DB '/backups/$DUMP'"
docker compose start web bot
echo "restored $DUMP"
