.PHONY: install dev install-nomadnet test test-serial test-hil test-cov lint format format-check docs-check docs-help-refresh docs-reference-refresh dashboard-assets dashboard-assets-check package-wheel package-check clean docker-test docker-test-arm64

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

test-hil:
	@test -n "$(HIL_CONFIG)" || { echo "HIL_CONFIG must name an absolute private lab config" >&2; exit 2; }
	@test -n "$(HIL_REPORT)" || { echo "HIL_REPORT must name a new absolute report path" >&2; exit 2; }
	@test ! -e "$(HIL_REPORT)" || { echo "HIL_REPORT already exists; choose a new path" >&2; exit 2; }
	@RETICULUMPI_HIL_CONFIG="$(HIL_CONFIG)" RETICULUMPI_HIL_REPORT="$(HIL_REPORT)" \
		.venv/bin/pytest -v -n0 --timeout=420 -m integration tests/test_lab_hil_live.py

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
	@set -eu; \
		package_check_dir=$$(mktemp -d); \
		trap 'rm -rf "$$package_check_dir"' 0; \
		.venv/bin/python -m build --no-isolation --outdir "$$package_check_dir"; \
		set -- "$$package_check_dir"/*; \
		test "$$#" -eq 2; \
		set -- "$$package_check_dir"/*.whl; \
		test "$$#" -eq 1; \
		test -f "$$1"; \
		wheel=$$1; \
		set -- "$$package_check_dir"/*.tar.gz; \
		test "$$#" -eq 1; \
		test -f "$$1"; \
		sdist=$$1; \
		.venv/bin/twine check "$$wheel" "$$sdist"; \
		.venv/bin/python scripts/verify_wheel.py "$$wheel" \
			--requirements constraints/production-universal-dashboard-nomadnet.txt

docker-test: package-wheel
	docker build --target test -f docker/Dockerfile -t reticulumpi-test .

docker-test-arm64: package-wheel
	docker build --platform linux/arm64 --target test -f docker/Dockerfile -t reticulumpi-test-arm64 .

clean:
	rm -rf .venv build dist *.egg-info src/*.egg-info
