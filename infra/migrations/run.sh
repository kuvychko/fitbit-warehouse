#!/bin/sh
# Migration runner: waits for the DB, then applies every migrations/*.sql in
# lexical order. All migrations are idempotent, so re-running is always safe.
set -eu

PG_HOST="${PG_HOST:-db}"
PG_PORT="${PG_PORT:-5432}"
PG_DB="${PG_DB:-warehouse}"
PG_MIGRATE_USER="${PG_MIGRATE_USER:-postgres}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-/migrations}"

: "${HEALTH_OWNER_PW:?HEALTH_OWNER_PW must be set}"
: "${HEALTH_RW_PW:?HEALTH_RW_PW must be set}"
: "${HEALTH_RO_PW:?HEALTH_RO_PW must be set}"

echo "Waiting for ${PG_HOST}:${PG_PORT}/${PG_DB} ..."
tries=0
until pg_isready -h "$PG_HOST" -p "$PG_PORT" -U "$PG_MIGRATE_USER" -d "$PG_DB" -q; do
  tries=$((tries + 1))
  if [ "$tries" -ge 30 ]; then
    echo "ERROR: database not reachable after ${tries} attempts" >&2
    exit 1
  fi
  sleep 2
done

for f in "$MIGRATIONS_DIR"/[0-9]*.sql; do
  [ -e "$f" ] || { echo "ERROR: no migration files in $MIGRATIONS_DIR" >&2; exit 1; }
  echo "Applying $(basename "$f") ..."
  psql -v ON_ERROR_STOP=1 -q \
    -h "$PG_HOST" -p "$PG_PORT" -U "$PG_MIGRATE_USER" -d "$PG_DB" \
    -v owner_pw="$HEALTH_OWNER_PW" \
    -v rw_pw="$HEALTH_RW_PW" \
    -v ro_pw="$HEALTH_RO_PW" \
    -f "$f"
done

echo "Migrations applied."
