# ---- Base stage: install the project ----
FROM python:3.11-slim-bookworm AS base

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
COPY plugins/ plugins/

RUN pip install --no-cache-dir .

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

RUN useradd --system --create-home reticulumpi
USER reticulumpi

VOLUME ["/config", "/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import RNS; import reticulumpi"

ENTRYPOINT ["reticulumpi"]
CMD ["--config", "/config/config.yaml"]
