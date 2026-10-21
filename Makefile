.PHONY: install lint test test-sql local-up local-run local-down local-clean

install:
	python -m pip install -r requirements-dev.txt

lint:
	ruff check .
	ruff format --check .

test:
	pytest

# The gold SQL tests need the Trino and Iceberg catalog containers from compose.yaml.
test-sql: local-up
	REQUIRE_TRINO=1 pytest tests/test_gold_sql.py

local-up:
	mkdir -p /tmp/thaq-warehouse
	docker compose up -d --wait

local-run:
	PYTHONPATH=src/ingest:src/glue:scripts python scripts/run_local.py $(ARGS)

local-down:
	docker compose down

local-clean: local-down
	rm -rf .local /tmp/thaq-warehouse
