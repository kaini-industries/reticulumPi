.PHONY: install dev test lint format clean

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install .

dev:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"

test:
	.venv/bin/pytest -v

test-cov:
	.venv/bin/pytest -v --cov=reticulumpi --cov-report=term-missing

lint:
	.venv/bin/ruff check src/ plugins/ tests/

format:
	.venv/bin/ruff format src/ plugins/ tests/
	.venv/bin/ruff check --fix src/ plugins/ tests/

clean:
	rm -rf .venv build dist *.egg-info src/*.egg-info
