.DEFAULT_GOAL := all
sources = src

.PHONY: .uv  # Check that uv is installed
.uv:
	@uv --version || echo 'Please install uv: https://docs.astral.sh/uv/getting-started/installation/'

.PHONY: install  # Install the package, dependencies, and pre-commit for local development
install: .uv
	uv sync --frozen --group lint
	uv run pre-commit install --install-hooks

.PHONY: format  # Format the code
format:
	uv run ruff format
	uv run ruff check --fix --fix-only

.PHONY: lint  # Lint the code
lint:
	uv run ruff format --check
	uv run ruff check

.PHONY: mypy
mypy:
	uv run python -m mypy $(sources)

.PHONY: typecheck
typecheck:
	uv run pyright

.PHONY: test
test:
	uv run pytest

.PHONY: test-random  # Run the suite in a randomised order (guards against order coupling)
test-random:
	uv run pytest -p randomly

.PHONY: test-all-python  # Run tests on Python 3.11 to 3.13
test-all-python:
	uv run --python 3.11 coverage run -p -m pytest --junitxml=junit.xml -o junit_family=legacy
	UV_PROJECT_ENVIRONMENT=.venv312 uv run --python 3.12 coverage run -p -m pytest
	UV_PROJECT_ENVIRONMENT=.venv313 uv run --python 3.13 coverage run -p -m pytest
	@uv run coverage xml -o coverage.xml
	@uv run coverage report

.PHONY: html  # Generate HTML coverage report
html: test-all-python
	uv run coverage html -d htmlcov

.PHONY: sync-version  # Fix all version references to match pyproject.toml
sync-version:
	uv run python scripts/sync_version.py --fix

.PHONY: check-version  # Check that all version references are in sync
check-version:
	uv run python scripts/sync_version.py

.PHONY: all
all: format mypy lint typecheck test-all-python