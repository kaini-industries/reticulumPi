.PHONY: install dev install-nomadnet test lint format clean docker-test docker-test-arm64

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install .

dev:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"

install-nomadnet:
	.venv/bin/pip install ".[nomadnet]"

test:
	.venv/bin/pytest -v

test-cov:
	.venv/bin/pytest -v --cov=reticulumpi --cov-report=term-missing

lint:
	.venv/bin/ruff check src/ plugins/ tests/

format:
	.venv/bin/ruff format src/ plugins/ tests/
	.venv/bin/ruff check --fix src/ plugins/ tests/

docker-test:
	docker build --target test -f docker/Dockerfile -t reticulumpi-test .

docker-test-arm64:
	docker build --platform linux/arm64 --target test -f docker/Dockerfile -t reticulumpi-test-arm64 .

clean:
	rm -rf .venv build dist *.egg-info src/*.egg-info
