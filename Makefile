.PHONY: help install format lint typecheck test check dev migrate

help:
	@echo "Available targets:"
	@echo "  install    Install the package and dev dependencies"
	@echo "  format     Auto-fix formatting with ruff"
	@echo "  lint       Run ruff linter"
	@echo "  typecheck  Run mypy type checker"
	@echo "  test       Run pytest"
	@echo "  check      Run all four quality gates (format-check, lint, typecheck, test)"
	@echo "  dev        Start the app locally with Uvicorn in reload mode"
	@echo "  migrate    Run Alembic migrations to head"

install:
	pip install -e ".[dev]"

format:
	ruff format .

lint:
	ruff check .

typecheck:
	mypy app tests

test:
	pytest

check:
	ruff format --check .
	ruff check .
	mypy app tests
	pytest

dev:
	uvicorn app.main:app --reload

migrate:
	alembic upgrade head
