.PHONY: install db-up db-down install-production migrate demo production pipeline test lint

install:
	python3.10 -m venv .venv && .venv/bin/pip install -e '.[dev,ml]'

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

migrate:
	.venv/bin/python scripts/migrate.py

install-production:
	.venv/bin/python scripts/install_production.py

demo:
	APP_MODE=demo .venv/bin/uvicorn credit_risk.api.main:app --host 0.0.0.0 --port 8002 --reload

production:
	APP_MODE=database .venv/bin/uvicorn credit_risk.api.production_main:app --host 0.0.0.0 --port 8002

pipeline:
	APP_MODE=database .venv/bin/credit-risk-full --refresh-ownership

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check src tests scripts
