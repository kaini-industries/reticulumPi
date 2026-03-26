.PHONY: install dev test lint clean

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

lint:
	.venv/bin/ruff check src/ plugins/ tests/

clean:
	rm -rf .venv build dist *.egg-info src/*.egg-info
