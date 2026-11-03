#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/../.." && pwd)"
monikey_source="${MONIKEY_SOURCE_DIR:-$repo_root/../monikey}"
image="postgres:17.6-alpine"
container="monikey-schema-inspection-test-$$"
database="monikey_schema_test"
tmp_dir="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
inventory="$tmp_dir/schema-inventory-${RANDOM}.json"
passfile="$tmp_dir/schema-test-pgpass-${RANDOM}"

[[ -f "$monikey_source/database/001_init.sql" ]] || {
  echo "Set MONIKEY_SOURCE_DIR to a MoniKey checkout containing canonical migrations." >&2
  exit 2
}

cleanup() {
  docker rm --force "$container" >/dev/null 2>&1 || true
  rm -f "$inventory" "$passfile"
}
trap cleanup EXIT

docker run --detach --name "$container" \
  --env POSTGRES_DB="$database" \
  --env POSTGRES_PASSWORD=fixture-only-admin-password "$image" >/dev/null

ready=false
for _ in {1..40}; do
  if docker exec "$container" psql -U postgres -d "$database" -Atqc 'SELECT 1' >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
[[ "$ready" == true ]] || { docker logs "$container" >&2; exit 1; }

apply_file() {
  docker exec --interactive "$container" \
    psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" < "$1" >/dev/null
}

# Construct the prior schema using the canonical fresh schema, then replay the
# canonical 002–006 migrations in their intended order. All fixture data is synthetic.
apply_file "$monikey_source/database/001_init.sql"
docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" \
  -c 'DROP TABLE account_transfers' >/dev/null
apply_file "$monikey_source/database/002_account_transfers.sql"
docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" \
  -c 'ALTER TABLE accounts ADD CONSTRAINT accounts_opening_balance_check CHECK (opening_balance >= 0), ADD CONSTRAINT accounts_opening_debt_check CHECK (opening_debt >= 0)' >/dev/null
apply_file "$monikey_source/database/003_current_account_amounts.sql"
apply_file "$monikey_source/database/004_account_card_product.sql"
apply_file "$monikey_source/database/005_kakeibo.sql"
apply_file "$monikey_source/database/006_kakeibo_plan_presence.sql"

# The test login has no table privileges; ordinary public catalog visibility is
# sufficient. This is a disposable DB, never production.
docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" \
  -c "REVOKE TEMPORARY ON DATABASE $database FROM PUBLIC" >/dev/null
docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" \
  -c "CREATE ROLE monikey_schema_inspector LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'fixture-only-password'; GRANT CONNECT ON DATABASE $database TO monikey_schema_inspector;" >/dev/null
docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d "$database" -Atqc \
  "SELECT bool_and(NOT has_table_privilege('monikey_schema_inspector', format('public.%I', t), 'SELECT')) FROM unnest(ARRAY['accounts','categories','transactions','credit_card_payments','account_transfers','kakeibo_month_plans','kakeibo_weekly_reviews']) AS t" \
  | grep -Fx t >/dev/null

printf '127.0.0.1:5432:%s:monikey_schema_inspector:fixture-only-password\n' "$database" > "$passfile"
chmod 600 "$passfile"
docker cp "$passfile" "$container:/tmp/schema-test-pgpass"
docker exec "$container" chmod 600 /tmp/schema-test-pgpass
docker exec --interactive --env PGPASSFILE=/tmp/schema-test-pgpass \
  "$container" psql -X -q -v ON_ERROR_STOP=1 -A -t -h 127.0.0.1 \
  -U monikey_schema_inspector -d "$database" \
  < "$script_dir/inspect.sql" > "$inventory"

python3 - "$inventory" "$script_dir/classify.py" <<'PY'
import importlib.util, json, sys
inventory = json.load(open(sys.argv[1], encoding="utf-8"))
spec = importlib.util.spec_from_file_location("classify", sys.argv[2])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
results = module.classify(inventory)
for number in ("002", "003", "004", "005", "006"):
    actual = results[number]["status"]
    if actual != "APPLIED":
        raise SystemExit(f"Canonical migration {number} classified {actual}: {results[number]['issues']}")
safe, issues = module.role_is_read_only(inventory)
if not safe:
    raise SystemExit(f"Catalog login has unexpected privileges: {issues}")
print("PostgreSQL catalog-only integration: passed; inspector has no table SELECT privileges.")
PY

python3 -m unittest discover -s "$script_dir/tests" -p 'test_*.py'
