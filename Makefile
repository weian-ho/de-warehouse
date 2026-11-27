# Shortcuts for the commands in README.md. Each target runs commands you could type yourself.
# Needs GNU make (Windows: see README, "Make on Windows"). Recipe lines MUST start with a TAB.
 
PG_CONTAINER ?= dw_postgres
SYMBOL       ?= BTCUSDT
MAX_PAGES    ?= 100
DDL          := sql/ddl/01_bronze_meta.sql sql/ddl/02_raw_binance.sql sql/ddl/03_snapshot_state.sql
GOLD_DDL     := sql/gold/00_extensions.sql sql/gold/01_dimensions.sql sql/gold/02_facts.sql sql/gold/03_seed.sql
# psql inside the container, stopping at the first error; NOTICEs ("already exists, skipping") hidden
PSQL         := docker exec -i -e PGOPTIONS="-c client_min_messages=warning" $(PG_CONTAINER) \
                sh -c 'psql -q -X -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" "$$@"' --
 
.DEFAULT_GOAL := help
.PHONY: help up stop test lint ingest goal gold-reset
 
help:
	@echo "make up      start Postgres, wait until it accepts connections, apply the DDL (safe to repeat)"
	@echo "make stop    stop Postgres (data is kept)"
	@echo "make test    all tests; integration tests are SKIPPED if Postgres is down (-rs shows why)"
	@echo "make lint    the same lint + format checks CI runs (never rewrites files)"
	@echo "make ingest  REST ingest from the watermark: SYMBOL=$(SYMBOL) MAX_PAGES=$(MAX_PAGES)"
	@echo "make gold    create the gold star schema if missing + seed unknown members, venues, dates (safe to repeat)"
	@echo "make gold-reset CONFIRM=yes   DROP the gold schema and rebuild it empty (destroys gold data)"
 
up:
	@test -f .env || { echo "no .env: run  cp .env.example .env  and set POSTGRES_PASSWORD"; exit 1; }
	docker compose up -d
	@echo "waiting for Postgres in $(PG_CONTAINER) ..."
	@n=0; until docker exec $(PG_CONTAINER) pg_isready -q -h 127.0.0.1; do \
		n=$$((n+1)); \
		if [ $$n -ge 30 ]; then echo "not ready after 30s - check: docker logs $(PG_CONTAINER)"; exit 1; fi; \
		sleep 1; \
	done
	@for f in $(DDL); do \
		echo "applying $$f"; \
		docker exec -i $(PG_CONTAINER) sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' < $$f || exit 1; \
	done
	@echo "Postgres is up and the A1 tables exist."
 
stop:
	docker compose stop
 
test:
	uv run pytest -v -rs
 
lint:
	uv run ruff check .
	uv run ruff format --check .
 
ingest:
	uv run warehouse ingest --symbol $(SYMBOL) --max-pages $(MAX_PAGES)


# All four gold files run as ONE transaction (-1): a failure in any file leaves gold unchanged.
gold: up
	@echo "applying $(GOLD_DDL) in one transaction"
	@cat $(GOLD_DDL) | $(PSQL) -1 -f -
	@$(PSQL) -t -A -F ' ' -c "SELECT 'gold ready:', (SELECT count(*) FROM gold.dim_date), 'dates,', \
		(SELECT count(*) FROM gold.dim_venue), 'venues,', (SELECT count(*) FROM gold.dim_instrument), 'instrument versions'"

# Destructive on purpose, so it needs CONFIRM=yes. Gold is rebuildable: facts from staging,
# dim_instrument history by replaying the bronze exchangeInfo snapshots in order (T055).
gold-reset: up
	@if [ "$(CONFIRM)" != "yes" ]; then echo "refusing: this drops schema gold and all its data. Run: make gold-reset CONFIRM=yes"; exit 1; fi
	$(PSQL) -c "DROP SCHEMA IF EXISTS gold CASCADE; DELETE FROM meta.snapshot_state WHERE source = 'binance_exchangeinfo'"
	@"$(MAKE)" --no-print-directory gold