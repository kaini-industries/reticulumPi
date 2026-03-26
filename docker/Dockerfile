# ---- Base stage: install the project ----
FROM python:3.11-slim-bookworm AS base

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

# Install NomadNet (optional, used by nomadnet_server plugin)
RUN pip install --no-cache-dir nomadnet

# ---- Test stage: run the test suite against the installed package ----
FROM base AS test

COPY tests/ tests/
COPY config/ config/

RUN pip install --no-cache-dir pytest pytest-cov pytest-mock

# Verify non-editable install works: plugins discoverable, tests pass
RUN python -c "from reticulumpi import __version__; print(f'reticulumpi v{__version__} installed')" && \
    pytest tests/ -v --tb=short

# ---- Runtime stage: production image ----
FROM base AS runtime

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN useradd --system --create-home reticulumpi
USER reticulumpi

VOLUME ["/config", "/data", "/home/reticulumpi/.nomadnet"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import RNS; import reticulumpi"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--config", "/config/config.yaml"]
