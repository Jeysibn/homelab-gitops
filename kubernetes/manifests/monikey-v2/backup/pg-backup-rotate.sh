#!/bin/sh
set -eu
umask 077

: "${PGHOST:?PGHOST is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGUSER:?PGUSER is required}"
: "${BACKUP_ROOT:?BACKUP_ROOT must be an absolute path on the dedicated backup volume}"

case "$BACKUP_ROOT" in
  /*) ;;
  *) echo "backup_error=BACKUP_ROOT_must_be_absolute" >&2; exit 2 ;;
esac
case "$BACKUP_ROOT" in
  /|/var/lib/postgresql|/var/lib/postgresql/*)
    echo "backup_error=BACKUP_ROOT_must_not_be_root_or_PGDATA" >&2
    exit 2
    ;;
esac

DAILY_RETENTION=${BACKUP_DAILY_RETENTION:-7}
WEEKLY_RETENTION=${BACKUP_WEEKLY_RETENTION:-4}
for retention in "$DAILY_RETENTION" "$WEEKLY_RETENTION"; do
  case "$retention" in
    ''|0*|*[!0-9]*) echo "backup_error=retention_must_be_a_positive_integer_without_leading_zeroes" >&2; exit 2 ;;
  esac
  if [ "$retention" -lt 1 ]; then
    echo "backup_error=retention_must_be_at_least_one" >&2
    exit 2
  fi
done

mkdir -p "$BACKUP_ROOT/daily" "$BACKUP_ROOT/weekly"
BACKUP_ROOT=$(CDPATH= cd -- "$BACKUP_ROOT" && pwd -P)
chmod 700 "$BACKUP_ROOT" "$BACKUP_ROOT/daily" "$BACKUP_ROOT/weekly"
exec 9>>"$BACKUP_ROOT/.backup.lock"
if ! flock -n 9; then
  echo "backup_status=failed stage=already_running" >&2
  exit 75
fi

STATE_FILE="$BACKUP_ROOT/.state"
METRICS_FILE="$BACKUP_ROOT/metrics.prom"
LAST_SUCCESS=0
LAST_SIZE=0
if [ -f "$STATE_FILE" ]; then
  LAST_SUCCESS=$(sed -n 's/^last_success_timestamp_seconds=//p' "$STATE_FILE")
  LAST_SIZE=$(sed -n 's/^last_archive_size_bytes=//p' "$STATE_FILE")
  LAST_SUCCESS=${LAST_SUCCESS:-0}
  LAST_SIZE=${LAST_SIZE:-0}
fi

TEMP_PATH=
write_metrics() {
  metrics_tmp="$BACKUP_ROOT/.metrics.prom.$$"
  volume_stats=$(df -Pk "$BACKUP_ROOT" | awk 'NR == 2 { print $2, $4 }')
  volume_size_kib=${volume_stats%% *}
  volume_available_kib=${volume_stats#* }
  case "$volume_size_kib:$volume_available_kib" in
    *[!0-9:]*|:*) volume_size_bytes=0; volume_available_bytes=0 ;;
    *) volume_size_bytes=$((volume_size_kib * 1024)); volume_available_bytes=$((volume_available_kib * 1024)) ;;
  esac
  {
    printf 'monikey_backup_last_run_success %s\n' "$1"
    printf 'monikey_backup_last_success_timestamp_seconds %s\n' "$LAST_SUCCESS"
    printf 'monikey_backup_last_archive_size_bytes %s\n' "$LAST_SIZE"
    printf 'monikey_backup_volume_available_bytes %s\n' "$volume_available_bytes"
    printf 'monikey_backup_volume_size_bytes %s\n' "$volume_size_bytes"
  } > "$metrics_tmp"
  chmod 600 "$metrics_tmp"
  mv -f "$metrics_tmp" "$METRICS_FILE"
}

cleanup() {
  exit_code=$?
  trap - EXIT HUP INT TERM
  if [ -n "$TEMP_PATH" ] && [ -e "$TEMP_PATH" ]; then
    rm -rf -- "$TEMP_PATH"
  fi
  if [ "$exit_code" -ne 0 ]; then
    write_metrics 0 || true
  fi
  exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

write_metrics 0
run_started=$(date -u +%Y%m%dT%H%M%SZ)
backup_id=$run_started
suffix=0
while [ -e "$BACKUP_ROOT/daily/$backup_id" ]; do
  suffix=$((suffix + 1))
  backup_id="$run_started-$suffix"
done

TEMP_PATH=$(mktemp -d "$BACKUP_ROOT/daily/.pending-$backup_id.XXXXXX")
archive_tmp="$TEMP_PATH/archive.dump"
if ! pg_dump --format=custom --no-owner --no-privileges --file="$archive_tmp" --dbname="$PGDATABASE" 2>"$TEMP_PATH/pg_dump.stderr"; then
  echo "backup_status=failed stage=pg_dump timestamp_utc=$run_started" >&2
  exit 1
fi
if [ ! -s "$archive_tmp" ]; then
  echo "backup_status=failed stage=empty_archive timestamp_utc=$run_started" >&2
  exit 1
fi
if ! pg_restore --list "$archive_tmp" > /dev/null 2>"$TEMP_PATH/pg_restore.stderr"; then
  echo "backup_status=failed stage=archive_validation timestamp_utc=$run_started" >&2
  exit 1
fi

archive_bytes=$(wc -c < "$archive_tmp" | tr -d '[:space:]')
archive_sha256=$(sha256sum "$archive_tmp" | awk '{ print $1 }')
printf '%s  archive.dump\n' "$archive_sha256" > "$TEMP_PATH/SHA256SUM"
{
  printf 'status=success\n'
  printf 'timestamp_utc=%s\n' "$run_started"
  printf 'archive_size_bytes=%s\n' "$archive_bytes"
  printf 'sha256=%s\n' "$archive_sha256"
} > "$TEMP_PATH/metadata"
chmod 600 "$TEMP_PATH/archive.dump" "$TEMP_PATH/SHA256SUM" "$TEMP_PATH/metadata"
mv "$TEMP_PATH" "$BACKUP_ROOT/daily/$backup_id"
TEMP_PATH=

weekly_id=$(date -u +%G-W%V)
if [ ! -e "$BACKUP_ROOT/weekly/$weekly_id" ]; then
  weekly_tmp=$(mktemp -d "$BACKUP_ROOT/weekly/.pending-$weekly_id.XXXXXX")
  if ! ln "$BACKUP_ROOT/daily/$backup_id/archive.dump" "$weekly_tmp/archive.dump"; then
    TEMP_PATH=$weekly_tmp
    echo "backup_status=failed stage=weekly_snapshot timestamp_utc=$run_started" >&2
    exit 1
  fi
  cp "$BACKUP_ROOT/daily/$backup_id/SHA256SUM" "$weekly_tmp/SHA256SUM"
  cp "$BACKUP_ROOT/daily/$backup_id/metadata" "$weekly_tmp/metadata"
  chmod 700 "$weekly_tmp"
  chmod 600 "$weekly_tmp/archive.dump" "$weekly_tmp/SHA256SUM" "$weekly_tmp/metadata"
  mv "$weekly_tmp" "$BACKUP_ROOT/weekly/$weekly_id"
fi

prune_snapshots() {
  directory=$1
  keep=$2
  count=0
  for snapshot in $(
    for path in "$directory"/*; do
      [ -d "$path" ] || continue
      name=$(basename "$path")
      case "$name" in .pending-*) continue ;; esac
      printf '%s\n' "$name"
    done | sort -r
  ); do
    count=$((count + 1))
    if [ "$count" -gt "$keep" ]; then
      rm -rf -- "$directory/$snapshot"
    fi
  done
}

LAST_SUCCESS=$(date -u +%s)
LAST_SIZE=$archive_bytes
state_tmp="$BACKUP_ROOT/.state.$$"
{
  printf 'last_success_timestamp_seconds=%s\n' "$LAST_SUCCESS"
  printf 'last_archive_size_bytes=%s\n' "$LAST_SIZE"
} > "$state_tmp"
chmod 600 "$state_tmp"
mv -f "$state_tmp" "$STATE_FILE"

prune_snapshots "$BACKUP_ROOT/daily" "$DAILY_RETENTION"
prune_snapshots "$BACKUP_ROOT/weekly" "$WEEKLY_RETENTION"
write_metrics 1
printf 'backup_status=success timestamp_utc=%s size_bytes=%s sha256=%s\n' \
  "$run_started" "$archive_bytes" "$archive_sha256"
