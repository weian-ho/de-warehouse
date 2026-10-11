#!/usr/bin/env bash
# practice/binance_sql/setup/load_binance.sh
#
# Week 3: download a small slice of Binance spot public market data and load it into
# the local Postgres container. Idempotent: every run rebuilds the tables from scratch.
#
# Run from the repo root:   bash sql/analytical/load_binance.sh
# Needs: the docker stack running, plus curl, unzip, sha256sum on the host
#        (WSL/Ubuntu: sudo apt install -y unzip curl)
# Add  data/  to .gitignore - the zips are not source code.

set -euo pipefail

source .env
CONTAINER=${CONTAINER:-dw_postgres}
SETUP_SQL=${SETUP_SQL:-practice/binance_sql/setup/00_setup.sql}

SYMBOLS=(BTCUSDT ETHUSDT ETHBTC)
# Deliberately spans 2025-01-01, when Binance switched spot timestamps from ms to us.
# If loading is slow on your machine, cut to (2024-12-31 2025-01-01): still spans it.
DAYS=(2024-12-30 2024-12-31 2025-01-01 2025-01-02)

BASE=https://data.binance.vision/data/spot/daily
DATA_DIR=data/binance
mkdir -p "$DATA_DIR"

psql_exec() { docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"; }

fetch() {   # $1 = url  ->  prints the local file path on stdout
  local url=$1
  local f="$DATA_DIR/$(basename "$url")"
  if [[ ! -f $f ]]; then
    echo "  downloading $(basename "$f")" >&2
    curl -fsSL "$url" -o "$f"
    curl -fsSL "$url.CHECKSUM" -o "$f.CHECKSUM"
    (cd "$DATA_DIR" && sha256sum -c --quiet "$(basename "$f").CHECKSUM") >&2
  fi
  echo "$f"
}

load() {    # $1 = aggTrades|klines   $2 = symbol   $3 = zip path   $4 = batch id
  local kind=$1 sym=$2 zip=$3 batch=$4 stage target csv
  if [[ $kind == aggTrades ]]; then stage=raw.agg_trades_stage; target=raw.agg_trades
  else                              stage=raw.klines_1m_stage;  target=raw.klines_1m
  fi
  csv=$(unzip -Z1 "$zip" | head -1)

  psql_exec -c "TRUNCATE $stage"
  # Some Binance files ship a header row and some don't: drop line 1 only if non-numeric.
  unzip -p "$zip" "$csv" \
    | awk 'NR==1 && $0 !~ /^[0-9]/ {next} {print}' \
    | psql_exec -c "\copy $stage FROM pstdin WITH (FORMAT csv)"
  # Remaining trailing column (loaded_at) takes its default.
  psql_exec -c "INSERT INTO $target SELECT '$sym', s.*, '$csv', $batch FROM $stage s"
  echo "  loaded $csv  (batch $batch)"
}

echo "Creating tables..."
psql_exec < "$SETUP_SQL"

echo "Loading..."
for sym in "${SYMBOLS[@]}"; do
  for d in "${DAYS[@]}"; do
    load klines    "$sym" "$(fetch "$BASE/klines/$sym/1m/$sym-1m-$d.zip")"         1
    load aggTrades "$sym" "$(fetch "$BASE/aggTrades/$sym/$sym-aggTrades-$d.zip")"  1
  done
done

# Simulate an operator re-running one day's load. This is the source of the duplicates
# in exercises Q1-Q3 - the most common real-world way duplicates are born.
load aggTrades BTCUSDT "$DATA_DIR/BTCUSDT-aggTrades-2024-12-31.zip" 2

psql_exec -c "ANALYZE raw.agg_trades; ANALYZE raw.klines_1m;"

echo
echo "Row counts:"
psql_exec -c "SELECT 'agg_trades' AS tbl, symbol, count(*) AS n_rows FROM raw.agg_trades GROUP BY symbol
              UNION ALL
              SELECT 'klines_1m', symbol, count(*) FROM raw.klines_1m GROUP BY symbol
              ORDER BY 1, 2;"
echo "Done. Exercise 0: before writing any query, look at min/max ts_raw per source_file."
