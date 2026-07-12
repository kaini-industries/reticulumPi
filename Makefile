.PHONY: install dev install-nomadnet test test-serial test-cov lint format format-check docs-check docs-help-refresh docs-reference-refresh dashboard-assets dashboard-assets-check package-wheel package-check clean docker-test docker-test-arm64

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install .

dev:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev,dashboard]"

install-nomadnet:
	.venv/bin/pip install ".[nomadnet]"

test:
	.venv/bin/pytest -v

test-serial:
	.venv/bin/pytest -v -n0

test-cov:
	.venv/bin/pytest -v --cov=src/reticulumpi --cov-branch --cov-report=term-missing

lint:
	.venv/bin/ruff check src/ plugins/ tests/ tools/

format-check:
	.venv/bin/ruff format --check src/ plugins/ tests/ tools/

format:
	.venv/bin/ruff format src/ plugins/ tests/ tools/
	.venv/bin/ruff check --fix src/ plugins/ tests/ tools/

docs-check:
	.venv/bin/python tools/check_docs.py

docs-help-refresh:
	.venv/bin/python tools/check_docs.py --refresh-help

docs-reference-refresh:
	.venv/bin/python tools/check_docs.py --refresh-generated

dashboard-assets:
	npm run build:dashboard

dashboard-assets-check:
	npm run check:dashboard

package-wheel: dashboard-assets-check
	.venv/bin/python -m build --wheel --no-isolation

package-check: dashboard-assets-check
	.venv/bin/python -m build --no-isolation
	.venv/bin/twine check dist/*
	.venv/bin/python scripts/verify_wheel.py dist/*.whl \
		--requirements constraints/production-universal-dashboard-nomadnet.txt

docker-test: package-wheel
	docker build --target test -f docker/Dockerfile -t reticulumpi-test .

docker-test-arm64: package-wheel
	docker build --platform linux/arm64 --target test -f docker/Dockerfile -t reticulumpi-test-arm64 .

clean:
	rm -rf .venv build dist *.egg-info src/*.egg-info
