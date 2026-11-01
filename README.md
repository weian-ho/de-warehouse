# de-warehouse: Binance trades into Postgres
 
Ingests Binance spot **aggregate trades** into a local PostgreSQL warehouse, by two routes into one table:
 
- **Backfill**: history from Binance's daily zip files (`data.binance.vision`).
- **Incremental**: the newest trades from the public REST API (`data-api.binance.vision`), resuming from a stored watermark.
Every load can be re-run without creating duplicates. A crash never leaves the table and the watermark disagreeing. Each run can be checked with automated PASS/FAIL data-quality checks.
 
> Scope: Milestone A1 of a data-engineering learning roadmap. Single machine, run by hand, verified on `BTCUSDT`. Not a production service.
 
## How it fits together
 
```text
                         Binance (public data, no API key)
   data.binance.vision                              data-api.binance.vision
   daily aggTrades zips                             GET /api/v3/aggTrades?fromId=N
          |                                                   |
          |  downloaded once into data/binance/               |  client.py   pages by trade id, timeouts,
          |                                                   |              retries 429 / 5xx, stops on 418
          |                                                   v
          |                                         extract.py ----> bronze.api_pages
          |                                         starts at          every response exactly as received,
          |                                         watermark + 1      append-only, one commit per page
          |                                                   |
   bulk.py  backfill                                   load.py
   one transaction per day                             one transaction: rows + watermark
          |                                                   |
          +---------------->  parse_trade()  <----------------+
                              Decimal price/qty, timestamps -> microseconds
                                       |
                                       v
                        raw.binance_agg_trades
                        PRIMARY KEY (symbol, agg_trade_id)
                        INSERT ... ON CONFLICT DO NOTHING
                        lineage columns: source, bronze_page_id
                                       |
                                       v
                        validate.py: freshness / volume / schema -> PASS or FAIL
 
   meta.ingest_state (the watermark: last trade id safely in raw, per source + symbol)
     - read by extract.py to choose where to start
     - moved ONLY by load.py, in the same transaction as the rows it loaded
     - never read or moved by the backfill
```
 
**Two paths, one parser, one insert.** A trade arriving from a zip and the same trade arriving from REST go through the same `parse_trade()` and the same `INSERT ... ON CONFLICT DO NOTHING`. They come out identical and can only be stored once. The paths differ only in how they keep state: REST resumes from the watermark, while a backfill is told its date range.
 
## Setup from zero
 
**You need:** Git, Docker Desktop, [uv](https://docs.astral.sh/uv/), and GNU make (optional, see [Make on Windows](#make-on-windows)). uv normally downloads the Python version pinned in `.python-version` (3.13). On machines that block downloaded executables, install Python 3.13 yourself and `export UV_NO_MANAGED_PYTHON=1`.
 
```bash
git clone <repo-url> de-warehouse && cd de-warehouse
cp .env.example .env        # then edit .env: set POSTGRES_PASSWORD (never commit .env)
uv sync --locked            # exact package versions from uv.lock
make up                     # start Postgres (container dw_postgres), wait until ready, create the tables
make test                   # 19 tests; integration tests need the database from 'make up'
uv run warehouse show-config   # settings as the app sees them; password masked
```
 
Without make, `make up` is equivalent to:
 
```bash
docker compose up -d
docker exec -i dw_postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < sql/ddl/01_bronze_meta.sql
docker exec -i dw_postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < sql/ddl/02_raw_binance.sql
```
 
The DDL files use `IF NOT EXISTS`, so `make up` is safe to run every time.
 
**Where settings live:**
 
| Where | What | Committed? |
|---|---|---|
| `.env` | Postgres user, password, database, port | **No** (`.gitignore`) |
| `config.yaml` | symbols, API base URL, page size, timeouts, retry policy, log level | Yes |
| CLI flags | per-run choices (`--symbol`, `--from-id`, `--max-pages`, ...) | n/a |
 
When a setting appears in more than one place, the order of precedence is: environment variable, then `.env`, then `config.yaml`.
 
## Running it
 
| Task | Command |
|---|---|
| Fetch new trades (REST) | `make ingest`, or `uv run warehouse ingest --symbol BTCUSDT --max-pages 100` |
| Check the latest run | `uv run warehouse validate --symbol BTCUSDT` (exits 1 on any FAIL) |
| Load history | `uv run warehouse backfill --start 2024-12-31 --end 2025-01-01` |
| Re-load landed pages, no API calls | `uv run warehouse load --run-id <run_id>` |
| All commands and flags | `uv run warehouse --help`, `uv run warehouse <command> --help` |
 
Every log line carries `run=<run_id>`. That same id is stored on the bronze pages, so a run's log lines and its data can be matched to each other.
 
**Incremental ingest.** `ingest` = `extract` then `load` under one run id.
 
- **Extract** starts at watermark + 1 (or the newest trades on the first run, or `--from-id`). It pages forward by trade id and commits each page to `bronze.api_pages`.
- **Extract** stops on an empty page, a short page, `--stop-at-id`, or `--max-pages`. It also stops if a page fails to move past `fromId`, so a bad cursor can't loop forever.
- **Load** parses that run's pages into `raw.binance_agg_trades` and advances the watermark in the same transaction.
- **Load** refuses to advance the watermark if the run starts after a gap (watermark + 1 was never loaded), and logs a warning. It never moves the watermark backwards.
**Backfill.** First download the days you want. Files must be named `<SYMBOL>-aggTrades-<YYYY-MM-DD>.zip`:
 
```bash
mkdir -p data/binance && cd data/binance
curl -O https://data.binance.vision/data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-12-31.zip
cd ../..
```
 
How `backfill` behaves:
 
- **Missing files:** it checks that every day in the range has a file *before* touching the database. One missing file means nothing starts, so no gap can be silently skipped.
- **Loading:** it streams each zip in 50,000-row chunks, one transaction per day.
- **Watermark:** it never touches it, because the watermark belongs to the REST tail. If a backfill moved it, the next `ingest` would think it was caught up to the backfilled date.
### Re-running safely
 
- **Any load can be repeated.** The primary key plus `ON CONFLICT DO NOTHING` makes a repeat insert 0 rows. The logs show it as `N offered, 0 new`.
- **After a failure, run the same command again.** Nothing needs cleaning up first.
- **Bronze can hold the same page twice** (a re-fetch after a crash). That's expected; raw still holds each trade once.
- **Where REST and a backfill overlap, the first writer wins.** Check `source` to see which path loaded a row.
## When things go wrong
 
| Event | What the code does | What you do |
|---|---|---|
| **429** rate limited | Waits exactly `Retry-After` seconds (60 if the header is missing), then retries. Each retry uses one of `max_attempts` (5). | Nothing for a one-off. If it repeats, lower `--max-pages` or run less often. |
| **418** IP banned | Raises `Banned` straight away, **no retry** (retrying while banned makes bans longer). | Wait until `Retry-After` has passed before running again. Find out what caused the request burst. |
| **5xx**, timeout, connection error | Retries with capped exponential backoff plus full jitter (random wait between 0 and `min(30, 1 x 2^(n-1))` seconds), up to `max_attempts`. | Check the logs if retries run out. |
| Other **4xx** | `PermanentError`, no retry. | Fix the request or config (symbol, URL). |
| Extract stops early on any of the above | Pages already committed stay in bronze. `ingest` still loads whatever pages landed, then exits with code 1. | Run `ingest` again: it resumes from the watermark. |
| Process killed mid-extract (Ctrl+C, power loss) | Committed pages stay in bronze. Nothing was loaded, so the watermark hasn't moved. | Run `ingest` again: it re-fetches those ids (bronze gets duplicate pages, raw doesn't). Or load the landed pages with `load --run-id <id>`. |
| Crash **during load** | One transaction, so the rows and the watermark both roll back. Nothing is half-written. | `uv run warehouse load --run-id <id>`, or just `ingest` again. |
| Backfill fails on day N | Days before N stay committed. Day N rolls back completely. The error names the file and data row. | Fix the file (re-download it), then re-run the same range. Done days show `0 new`. |
| Database down | `database unavailable: ...`, exit 1. Nothing written. | `make up`, then re-run. |
 
Retries handle short HTTP trouble. Transactions stop partial writes. Idempotency makes your manual re-run safe. These are three different mechanisms, and you need all three.
 
## How it was verified
 
| Check | Compared | Result |
|---|---|---|
| **T025: refactor reconciliation** | The week-3 Bash loader vs its Python port, same zip files | Matched per file: row count, `sum(qty)`, first/last trade id |
| **T031: source reconciliation** | REST vs bulk zip for the same window (REST loaded into a separate table with `--no-watermark`) | Counts, trade ids, `sum(qty)` and prices matched. Timestamps differed on 46,237 of 46,267 rows (see open items). |
| **T035: integration tests** | Real Postgres, throwaway database `warehouse_test` | Loading the same pages twice adds nothing. A crash forced between the INSERT and the watermark update commits **0** rows and leaves the watermark unchanged. |
| **T039: backfill** | Trade-id continuity per UTC day | 2024-12-31: 1,218,370 rows; 2025-01-01: 653,485 rows. Both equal `max(id) - min(id) + 1`, i.e. no gaps. Re-run: 0 new. |
| **Every run** | `validate` | Freshness (watermark reached the run's last id), volume (every id in the window present, zero tolerance), schema (every landed row has the 8 fields with the right types) |
 
The T025 and T031 comparisons are different tests. T025 fed the same input to two programs, so it checks *the code*. T031 used two independent sources, so it checks *the data*.
 
Check continuity yourself:
 
```sql
SELECT (to_timestamp(ts_us / 1e6) AT TIME ZONE 'UTC')::date AS utc_day,
       count(*) AS rows, max(agg_trade_id) - min(agg_trade_id) + 1 AS expected_if_gapless, sum(qty)
FROM raw.binance_agg_trades WHERE symbol = 'BTCUSDT'
GROUP BY 1 ORDER BY 1;
```
 
Don't filter on `source` here. Where REST loaded some ids first, a bulk-only filter shows gaps that aren't real.
 
## Tests and code quality
 
```bash
make test     # uv run pytest -v -rs : all 19 tests
make lint     # uv run ruff check . && uv run ruff format --check .  (exactly what CI runs)
uv run pre-commit install    # once per clone: ruff runs on every commit
```
 
- **Unit tests** (`test_transform`, `test_client`, `test_validate`) fake HTTP with `responses` and replace `sleep`, so they run in about a second without network or database. They cover timestamp normalisation, Decimal money, 429/418/5xx classification and retry counts, pagination stop rules, and the validation verdicts.
- **Integration tests** (`test_integration`) need Postgres. If the database can't be reached, they are **skipped, not failed**. That includes a wrong password in `.env`. `-rs` prints the reason, so read the skip lines.
- **pre-commit** runs `ruff check --fix` and `ruff format`. If they change a file, the commit is stopped. Review with `git diff`, `git add` again, and commit again.
- **CI** (`.github/workflows/ci.yml`) runs on every pull request and every push to `main`:
  - `uv sync --locked`, which fails if `uv.lock` doesn't match `pyproject.toml`
  - lint and format check
  - unit tests
  CI does **not** start Postgres, so a green CI run says nothing about the integration tests.
## Code map
 
| File | Job |
|---|---|
| `src/warehouse/cli.py` | The only entry point (`uv run warehouse ...`). Parses arguments, sets up logging once, calls the modules, maps errors to exit codes. |
| `config.py` | Typed settings from `.env` + `config.yaml`; password as `SecretStr` |
| `errors.py` | `TransientError`, `RateLimited`, `Banned`, `PermanentError`. The type decides retry or stop. |
| `client.py` | Three layers: `iter_agg_trades` (pagination) → `get_json` (retry policy) → `_send_once` (one HTTP call, classify the answer) |
| `extract.py` | Chooses the start id, lands pages in bronze, never moves the watermark |
| `load.py` | Bronze → raw plus watermark in one transaction; owns `parse_trade` and `INSERT_SQL` |
| `bulk.py` | Zip loading: the T025 port of the Bash loader, and the T039 backfill |
| `transform.py` | `normalise_ts_to_us` (ms → µs), `parse_bool` (`"False"` is truthy in Python) |
| `models.py` | `AggTrade`: frozen dataclass, rejects non-Decimal price/qty |
| `validate.py` | Freshness, volume, schema checks |
| `log.py` | Log format and `run_id` on every line. Each module only calls `logging.getLogger(__name__)`. |
| `sql/ddl/` | `01_bronze_meta.sql` (bronze, watermark), `02_raw_binance.sql` (raw) |
| `sql/analytical/`, `sql/reconcile_bulk_refactor.sql` | Week-3 lab tables and the T025 reconciliation (not used by the pipeline) |
 
## Make on Windows
 
Git Bash doesn't include `make`. Check with `make --version`. To install it: `winget install ezwinports.make`, then open a new terminal. If company policy blocks the install, every target is only a shortcut: run the commands shown under each target in the `Makefile`.
 
## Known limits and open items
 
- **Timestamp precision (T031).** The hypothesis is that pre-2025 zips store milliseconds while REST returns microseconds. Converting ms to µs changes the unit but can't add the missing digits. Confirm it by comparing `ts_us / 1000` on both sides: if that matches on every row, the hypothesis holds. Until then, compare timestamps across sources at millisecond precision.
- **Throughput.** The backfill uses `executemany` at about 40k rows/s (measured on synthetic data; Docker on Windows is slower). COPY into a staging table would be faster if needed.
- **No scheduler.** Runs are manual. Rate limits are handled reactively (no request budget tracking).
- **Verified on one symbol** (`BTCUSDT`). Other symbols use the same code but haven't been reconciled.
 
Claude finished the response
